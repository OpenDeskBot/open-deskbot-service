from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import uuid
import weakref
from typing import Any, Optional

from deskbot_server.constants import FACE_EXPR_SCENES_FILE
from deskbot_server.face_expr_scenes_store import (
    design_frames_to_pb_chain,
    find_design_scene_by_name,
    load_face_expr_scenes_file,
)
from deskbot_server.pb.shapes import (
    PB_ACTION_DEFAULT,
    PB_LEVEL_IDLE,
    apply_pb_dispatch_fields,
)
from deskbot_server.ws.pb_idle_registry import note_pb_idle_for_device
from deskbot_server.settings import _is_pb_downlink_payload
from deskbot_server.util import _json_msg
from deskbot_server.ws.ws_send import (
    _pb_ws_chain_serial_lock,
    _PerWsFireAndForget,
    _safe_send,
    _stop_pb_device_downlink_worker,
    enqueue_pb_device_downlink,
    enqueue_pb_device_downlink_unlocked,
)

logger = logging.getLogger("deskbot-server")


class AsrChatHub:
    """按 device_id 索引当前所有 /asr_chat 长连接，允许其它通道主动下发消息。

    可选用途：在 ``send_face_info_to_asr_chat`` 开启时，``/camera`` / ``/face_pos`` 可将
    ``face_info`` 转发到同 device 的 ``/asr_chat``（与 ``device_pb_only`` 互斥）。

    ``device_pb_only`` 为 true 时：经 :meth:`send` 仅接受 ``pb_*`` 载荷，且与同连接 TTS 共用
    :func:`enqueue_pb_device_downlink` 队列顺序写出；其它载荷直接丢弃计数为 0。
    """

    def __init__(self, device_pb_only: bool = False) -> None:
        self._by_device: dict = {}
        self._lock = asyncio.Lock()
        # 给 ESP32 反压（比如它在播 TTS 时 RX 满）时不会卡住调用方
        self._fanout = _PerWsFireAndForget()
        # 每条 /asr_chat WebSocket -> device_id（供下行空闲打盹计时；WeakKey 随 ws 释放）
        self._asr_ws_dev = weakref.WeakKeyDictionary()
        self.pb_idle_snore: Optional[Any] = None
        self.pb_idle_silence: Optional[Any] = None
        self._device_pb_only = bool(device_pb_only)

    def ws_asr_device_id(self, ws) -> Optional[str]:
        return self._asr_ws_dev.get(ws)

    async def attach(self, device_id: str, ws) -> None:
        if not device_id:
            return
        async with self._lock:
            self._by_device.setdefault(device_id, set()).add(ws)
            self._asr_ws_dev[ws] = device_id
        setattr(ws, "_asr_chat_pb_serial_queue", self._device_pb_only)
        note_pb_idle_for_device(device_id)

    async def detach(self, device_id: str, ws) -> None:
        if not device_id:
            return
        removed_last = False
        async with self._lock:
            self._asr_ws_dev.pop(ws, None)
            conns = self._by_device.get(device_id)
            if conns is None:
                return
            conns.discard(ws)
            if not conns:
                self._by_device.pop(device_id, None)
                removed_last = True
        await _stop_pb_device_downlink_worker(ws)
        self._fanout.discard(ws)
        if removed_last:
            for sched in (self.pb_idle_snore, self.pb_idle_silence):
                if sched is not None:
                    sched.cancel_for_device(device_id)

    async def first_ws(self, device_id: str):
        """返回该 device 任意一条已连接的 ``/asr_chat`` WebSocket（供 HTTP 下行复用）。"""
        if not device_id:
            return None
        async with self._lock:
            conns = self._by_device.get(device_id, ())
            return next(iter(conns), None) if conns else None

    async def send(self, device_id: str, payload: dict) -> int:
        if not device_id:
            return 0
        if self._device_pb_only and not _is_pb_downlink_payload(payload):
            return 0
        async with self._lock:
            targets = list(self._by_device.get(device_id, ()))
        if not targets:
            return 0
        msg = json.dumps(payload, ensure_ascii=False)
        sent = 0
        for ws in targets:
            if getattr(ws, "_asr_chat_pb_serial_queue", False):
                await enqueue_pb_device_downlink(ws, msg, None)
                sent += 1
            elif self._fanout.submit(ws, msg):
                sent += 1
        return sent

    async def send_pb_chain_ordered(self, device_id: str, frames: list[dict]) -> int:
        """按顺序逐帧下发 pb JSON（经 :func:`_json_msg`）。

        ``device_pb_only`` 连接上整链持 :func:`_pb_ws_chain_serial_lock` 后经
        :func:`enqueue_pb_device_downlink_unlocked` 入队，避免协程间插队导致仅首帧到达；
        否则仍 ``await`` :func:`_safe_send`。
        """
        if not device_id or not frames:
            return 0
        async with self._lock:
            targets = list(self._by_device.get(device_id, ()))
        if not targets:
            return 0
        n = 0
        for ws in targets:
            if getattr(ws, "_asr_chat_pb_serial_queue", False):
                async with _pb_ws_chain_serial_lock(ws):
                    for payload in frames:
                        if not isinstance(payload, dict):
                            continue
                        wire = _json_msg(payload)
                        await enqueue_pb_device_downlink_unlocked(ws, wire, None)
                        n += 1
            else:
                for payload in frames:
                    if not isinstance(payload, dict):
                        continue
                    wire = _json_msg(payload)
                    await _safe_send(ws, wire)
                    n += 1
        return n

    async def send_pb_single_then_chain_ordered(
        self,
        device_id: str,
        single_payload: dict,
        tail_frames: Optional[list[dict]],
    ) -> int:
        """在 ``device_pb_only`` 下持**同一把**链锁：先发 ``pb_single``，再顺序发 ``tail_frames``。

        用于注视/跟随舵机与 ``happy_smile`` 等场景同批入队，避免与其它下行插队。
        ``tail_frames`` 可为空，则等价于单发 ``pb_single``。
        """
        if not device_id or not isinstance(single_payload, dict):
            return 0
        if self._device_pb_only and not _is_pb_downlink_payload(single_payload):
            return 0
        tail = [f for f in (tail_frames or []) if isinstance(f, dict)]
        async with self._lock:
            targets = list(self._by_device.get(device_id, ()))
        if not targets:
            return 0
        n = 0
        for ws in targets:
            if getattr(ws, "_asr_chat_pb_serial_queue", False):
                async with _pb_ws_chain_serial_lock(ws):
                    wire0 = _json_msg(single_payload)
                    await enqueue_pb_device_downlink_unlocked(ws, wire0, None)
                    n += 1
                    for payload in tail:
                        wire = _json_msg(payload)
                        await enqueue_pb_device_downlink_unlocked(ws, wire, None)
                        n += 1
            else:
                wire0 = _json_msg(single_payload)
                await _safe_send(ws, wire0)
                n += 1
                for payload in tail:
                    wire = _json_msg(payload)
                    await _safe_send(ws, wire)
                    n += 1
        return n


class PbIdleSnoreAfterDownlink:
    """记录「距上次成功下行」的空闲时长：每次有数据写到该设备的 ``/asr_chat`` WebSocket 则重新计时；
    连续空闲 ``idle_sec`` 秒后向该设备顺序下发指定场景。多帧链在 ``device_pb_only`` 下须原子入队；
    ``level=0``（idle）+ ``action=default``：队列中更高优先级序列不超过 1 条时追加，否则丢弃（见 esp32_playback_protocol §2.1）。

    与 ``/camera`` 同步：**``is_frontal``（正脸）** 为真时刷新空闲打盹计时且**不下发**打盹场景。
    （调试页「注视感知」另含虹膜区间，仅用于舵机；打盹抑制只看正脸，避免虹膜略偏仍下发 sleep。）
    """

    _GAZE_STALE_SEC = 0.7
    _GAZE_NOTE_MIN_INTERVAL = 0.25

    def __init__(self, hub: AsrChatHub, *, idle_sec: float, scene_name: str) -> None:
        self._hub = hub
        self._idle_sec = float(idle_sec)
        self._scene_lc = (scene_name or "").strip().lower()
        self._tasks: dict = {}
        self._gaze_frontal: dict[str, bool] = {}
        self._gaze_last_mono: dict[str, float] = {}
        self._gaze_last_note_mono: dict[str, float] = {}
        # 下发 sleep_snore 等链时，各片会触发 note_activity；若此时 _reschedule 取消正在 await 的
        # _sleep_then_fire，协程会在首帧后即被取消，后续 chunk/end 发不出去。
        self._suppress_note_devices: set[str] = set()

    def _gaze_blocks_idle_snore(self, device_id: str) -> bool:
        """最近一帧 /camera 仍为正脸（``is_frontal``）且流未断（无新帧超过 _GAZE_STALE_SEC 视为已离开）。"""
        if not device_id or not self._gaze_frontal.get(device_id):
            return False
        last = self._gaze_last_mono.get(device_id)
        if last is None:
            return False
        return (time.monotonic() - last) < self._GAZE_STALE_SEC

    def on_camera_gaze_tick(self, device_id: str, frontal: bool) -> None:
        """由 ``/camera`` 每帧调用：``frontal`` 为 ``is_frontal``；正脸时刷新打盹计时，离开正脸时再计一轮。"""
        if not device_id or self._idle_sec <= 0:
            return
        now = time.monotonic()
        self._gaze_last_mono[device_id] = now
        prev = self._gaze_frontal.get(device_id)
        self._gaze_frontal[device_id] = frontal
        if frontal:
            last_note = self._gaze_last_note_mono.get(device_id, 0.0)
            if now - last_note >= self._GAZE_NOTE_MIN_INTERVAL:
                self._gaze_last_note_mono[device_id] = now
                self.note_activity(device_id)
        elif prev is True:
            self._gaze_last_note_mono.pop(device_id, None)
            self.note_activity(device_id)

    def note_activity(self, device_id: str) -> None:
        if not device_id or self._idle_sec <= 0:
            return
        if device_id in self._suppress_note_devices:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_soon(self._reschedule, device_id)

    def cancel_for_device(self, device_id: str) -> None:
        if not device_id:
            return
        old = self._tasks.pop(device_id, None)
        if old is not None and not old.done():
            old.cancel()
        self._gaze_frontal.pop(device_id, None)
        self._gaze_last_mono.pop(device_id, None)
        self._gaze_last_note_mono.pop(device_id, None)

    def _reschedule(self, device_id: str) -> None:
        old = self._tasks.pop(device_id, None)
        try:
            cur = asyncio.current_task()
        except RuntimeError:
            cur = None
        if old is not None and not old.done() and old is not cur:
            old.cancel()
        self._tasks[device_id] = asyncio.create_task(self._sleep_then_fire(device_id))

    async def _sleep_then_fire(self, device_id: str) -> None:
        this = asyncio.current_task()
        try:
            await asyncio.sleep(self._idle_sec)
            await self._deliver_scene(device_id)
        except asyncio.CancelledError:
            raise
        finally:
            if self._tasks.get(device_id) is this:
                self._tasks.pop(device_id, None)

    async def _deliver_scene(self, device_id: str) -> None:
        if not self._scene_lc:
            return
        if self._gaze_blocks_idle_snore(device_id):
            logger.info(
                "[pb_idle_snore] 跳过：/camera 正脸 is_frontal，重新计时 device_id=%s scene=%s",
                device_id,
                self._scene_lc,
            )
            self.note_activity(device_id)
            return
        rows = load_face_expr_scenes_file(seed_if_missing=False) or []
        ent = find_design_scene_by_name(rows, self._scene_lc)
        if ent is None:
            logger.warning(
                "[pb_idle_snore] 场景 %r 不在 %s 中，无法下发 device_id=%s",
                self._scene_lc,
                os.path.basename(FACE_EXPR_SCENES_FILE),
                device_id,
            )
            return
        req_id = uuid.uuid4().hex[:16]
        frames = design_frames_to_pb_chain(ent.get("frames") or [], runtime_req=req_id)
        if not frames:
            return
        apply_pb_dispatch_fields(frames, action=PB_ACTION_DEFAULT, level=PB_LEVEL_IDLE)
        self._suppress_note_devices.add(device_id)
        try:
            n = await self._hub.send_pb_chain_ordered(device_id, frames)
            logger.info(
                "[pb_idle_snore] scene=%s level=%d action=%s device_id=%s req=%s frames=%d ws_sends=%d",
                self._scene_lc,
                PB_LEVEL_IDLE,
                PB_ACTION_DEFAULT,
                device_id,
                req_id,
                len(frames),
                n,
            )
        except Exception:
            logger.exception(
                "[pb_idle_snore] 下发失败 scene=%s device_id=%s",
                self._scene_lc,
                device_id,
            )
        finally:
            self._suppress_note_devices.discard(device_id)
        # 整链发完后重新起算空闲窗口（与「任一下行刷新计时」一致）
        self.note_activity(device_id)


class PbIdleSilenceServoAfterDownlink:
    """距上次成功 pb 下行空闲 ``idle_sec`` 秒后，下发低头沉默舵机（绝对 x=90 y=135）。"""

    _SILENCE_SERVO_X = 90
    _SILENCE_SERVO_Y = 135
    _SILENCE_SERVO_MS = 500

    def __init__(self, hub: AsrChatHub, *, idle_sec: float) -> None:
        self._hub = hub
        self._idle_sec = float(idle_sec)
        self._tasks: dict = {}
        # 下发低头沉默时会触发 note_activity；抑制以免取消正在 await 的计时协程
        self._suppress_note_devices: set[str] = set()

    def note_activity(self, device_id: str) -> None:
        if not device_id or self._idle_sec <= 0:
            return
        if device_id in self._suppress_note_devices:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_soon(self._reschedule, device_id)

    def cancel_for_device(self, device_id: str) -> None:
        if not device_id:
            return
        old = self._tasks.pop(device_id, None)
        if old is not None and not old.done():
            old.cancel()

    def _reschedule(self, device_id: str) -> None:
        old = self._tasks.pop(device_id, None)
        try:
            cur = asyncio.current_task()
        except RuntimeError:
            cur = None
        if old is not None and not old.done() and old is not cur:
            old.cancel()
        self._tasks[device_id] = asyncio.create_task(self._sleep_then_fire(device_id))

    async def _sleep_then_fire(self, device_id: str) -> None:
        this = asyncio.current_task()
        try:
            await asyncio.sleep(self._idle_sec)
            await self._deliver_silence_servo(device_id)
        except asyncio.CancelledError:
            raise
        finally:
            if self._tasks.get(device_id) is this:
                self._tasks.pop(device_id, None)

    async def _deliver_silence_servo(self, device_id: str) -> None:
        req_id = uuid.uuid4().hex[:16]
        ms = self._SILENCE_SERVO_MS
        payload = {
            "type": "pb_single",
            "req": req_id,
            "idx": 0,
            "chunk_ms": ms,
            "pb_ver": 2,
            "action": PB_ACTION_DEFAULT,
            "level": PB_LEVEL_IDLE,
            "servo": {
                "xm": 0,
                "ym": 0,
                "x": self._SILENCE_SERVO_X,
                "y": self._SILENCE_SERVO_Y,
                "ms": ms,
            },
        }
        self._suppress_note_devices.add(device_id)
        try:
            n = await self._hub.send(device_id, payload)
            logger.info(
                "[pb_idle_silence] 低头沉默 device_id=%s req=%s x=%d y=%d xm=0 ym=0 delivered=%d",
                device_id,
                req_id,
                self._SILENCE_SERVO_X,
                self._SILENCE_SERVO_Y,
                n,
            )
        except Exception:
            logger.exception(
                "[pb_idle_silence] 下发失败 device_id=%s",
                device_id,
            )
        finally:
            self._suppress_note_devices.discard(device_id)
        self.note_activity(device_id)
