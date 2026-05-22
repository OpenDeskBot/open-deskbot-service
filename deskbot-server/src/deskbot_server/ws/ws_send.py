from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Optional

from websockets.exceptions import ConnectionClosed

from deskbot_server.constants import SAFE_SEND_TIMEOUT
from deskbot_server.util import _peer_str
from deskbot_server.ws.pb_idle_registry import note_pb_idle_after_successful_asr_send

logger = logging.getLogger("deskbot-server")

_WS_OUTBOUND_LOCK_ATTR = "_bot_outbound_send_lock"
_PB_DEVICE_QUEUE_ATTR = "_bot_pb_device_downlink_queue"
_PB_DEVICE_WORKER_ATTR = "_bot_pb_device_downlink_worker"
_PB_WS_CHAIN_SERIAL_LOCK_ATTR = "_bot_pb_ws_chain_serial_lock"


def _get_ws_send_lock(ws) -> asyncio.Lock:
    lock = getattr(ws, _WS_OUTBOUND_LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(ws, _WS_OUTBOUND_LOCK_ATTR, lock)
    return lock


async def _safe_send_once(
    websocket, message, *, timeout: float = SAFE_SEND_TIMEOUT
) -> bool:
    """对 ``websocket`` 执行单次 ``send``（**不**加锁；由调用方保证互斥或独占锁）。

    返回是否成功写出（``True``）；连接已关/超时/其它异常返回 ``False``。
    """
    try:
        await asyncio.wait_for(websocket.send(message), timeout=timeout)
        return True
    except ConnectionClosed:
        return False
    except asyncio.TimeoutError:
        try:
            await websocket.close(code=1011, reason="send timeout")
        except Exception:
            pass
        try:
            peer = _peer_str(websocket)
        except Exception:
            peer = "?"
        logger.warning(
            "[ws] _safe_send 超时 (>%.1fs)，主动关闭 ws peer=%s msg_kind=%s",
            timeout,
            peer,
            "bytes" if isinstance(message, (bytes, bytearray)) else "text",
        )
        return False
    except Exception:
        return False


class _PerWsFireAndForget:
    """每个 ws 同时最多保留 1 个未完成的发送任务；上一发送未完成则**丢弃当前消息**。

    用于把"广播给若干订阅者"从同步 ``await ws.send`` 改成非阻塞调度：
    - 任一订阅者写得慢/挂死，绝不会反压回到调用方协程
    - 慢订阅者代价是降帧（直到上次 send 完成或超时关闭），但**生产端永远不卡**
    - 配合 :func:`_safe_send` 内置 ``timeout`` 保底——单个 inflight 任务最坏
      ``WS_SEND_TIMEOUT_SEC`` 秒后必然结束（超时则主动 close 该 ws，
      下一次 publish 直接 done）。
    """

    def __init__(self) -> None:
        self._inflight: dict = {}

    def submit(self, ws, message) -> bool:
        """非阻塞地往 ``ws`` 发一条消息。返回是否真正提交（False = 被丢弃）。"""
        prev = self._inflight.get(ws)
        if prev is not None and not prev.done():
            return False
        self._inflight[ws] = asyncio.create_task(_safe_send(ws, message))
        return True

    def discard(self, ws) -> None:
        """清理某 ws 的 inflight task（订阅者断开时调用）。"""
        task = self._inflight.pop(ws, None)
        if task is not None and not task.done():
            task.cancel()


async def _safe_send(websocket, message, *, timeout: float = SAFE_SEND_TIMEOUT):
    """往 WS 发一条消息；与同连接上其它发送共享互斥锁，保证帧顺序。

    - 客户端已断开：吞掉 ConnectionClosed，避免 ERROR 日志刷屏。
    - **写超时**：超过 ``timeout`` 秒（默认 10s，``WS_SEND_TIMEOUT_SEC``）视为对端反压/挂死，主动
      ``close()`` 这条连接并返回，**绝不让一个慢/死的客户端把生产端的
      协程整个卡住**（典型场景：ESP32 在播 TTS 时 RX 满，服务端 await
      ws.send(face_info) 卡 → 整条 /camera 链路冻结 → 越来越多僵尸连接）。
    - 其它异常（比如 RuntimeError）也被吞掉，默认行为不抛。
    """
    ok = False
    async with _get_ws_send_lock(websocket):
        ok = await _safe_send_once(websocket, message, timeout=timeout)
    if ok:
        note_pb_idle_after_successful_asr_send(websocket)


async def _safe_send_pb_json_then_pcm(
    websocket,
    text_msg: str,
    pcm: bytes,
    *,
    timeout: float = SAFE_SEND_TIMEOUT,
) -> None:
    """发送一条 pb 文本帧后**立即**发送紧随的 PCM binary（若有），中间不允许插入其它帧。"""
    async with _get_ws_send_lock(websocket):
        ok_t = await _safe_send_once(websocket, text_msg, timeout=timeout)
        if ok_t:
            note_pb_idle_after_successful_asr_send(websocket)
        if pcm:
            ok_p = await _safe_send_once(websocket, pcm, timeout=timeout)
            if ok_p:
                note_pb_idle_after_successful_asr_send(websocket)


_PB_DEVICE_QUEUE_ATTR = "_bot_pb_device_downlink_queue"
_PB_DEVICE_WORKER_ATTR = "_bot_pb_device_downlink_worker"
_PB_WS_CHAIN_SERIAL_LOCK_ATTR = "_bot_pb_ws_chain_serial_lock"


def _pb_ws_chain_serial_lock(ws) -> asyncio.Lock:
    """``device_pb_only`` 连接上：保证整段 pb 链（TTS 一轮、``send_pb_chain_ordered``）在入队时不被插队。"""
    lock = getattr(ws, _PB_WS_CHAIN_SERIAL_LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(ws, _PB_WS_CHAIN_SERIAL_LOCK_ATTR, lock)
    return lock


@asynccontextmanager
async def _maybe_pb_serial_chain_guard(ws):
    """仅 ``_asr_chat_pb_serial_queue`` 为真时持链锁；否则空上下文。"""
    if getattr(ws, "_asr_chat_pb_serial_queue", False):
        async with _pb_ws_chain_serial_lock(ws):
            yield
    else:
        yield


@dataclass
class _PbDeviceJob:
    wire: str
    pcm: Optional[bytes] = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


async def _pb_device_downlink_worker(ws) -> None:
    """单连接一条队列：顺序执行 pb 文本帧与可选紧随 PCM；内部仍走 ``_safe_send*`` 与 WS 互斥锁。"""
    q: asyncio.Queue = getattr(ws, _PB_DEVICE_QUEUE_ATTR)
    while True:
        job = await q.get()
        try:
            if job is None:
                break
            if job.pcm:
                await _safe_send_pb_json_then_pcm(ws, job.wire, job.pcm)
            else:
                await _safe_send(ws, job.wire)
        except Exception:
            pass
        finally:
            if job is not None:
                job.done.set()
            try:
                q.task_done()
            except ValueError:
                pass


def _ensure_pb_device_downlink_worker(ws) -> None:
    if getattr(ws, _PB_DEVICE_WORKER_ATTR, None) is not None:
        return
    q: asyncio.Queue = asyncio.Queue()
    setattr(ws, _PB_DEVICE_QUEUE_ATTR, q)
    setattr(ws, _PB_DEVICE_WORKER_ATTR, asyncio.create_task(_pb_device_downlink_worker(ws)))


async def enqueue_pb_device_downlink_unlocked(
    ws, wire: str, pcm: Optional[bytes] = None
) -> None:
    """将 pb 下行排入队列（不设链锁；链式发送方须已持 :func:`_pb_ws_chain_serial_lock`）。"""
    _ensure_pb_device_downlink_worker(ws)
    q: asyncio.Queue = getattr(ws, _PB_DEVICE_QUEUE_ATTR)
    job = _PbDeviceJob(wire=wire, pcm=pcm)
    await q.put(job)
    await job.done.wait()


async def enqueue_pb_device_downlink(ws, wire: str, pcm: Optional[bytes] = None) -> None:
    """单条 pb 入队；``device_pb_only`` 时持链锁，避免与其它生产者单片交叉。"""
    if getattr(ws, "_asr_chat_pb_serial_queue", False):
        async with _pb_ws_chain_serial_lock(ws):
            await enqueue_pb_device_downlink_unlocked(ws, wire, pcm)
    else:
        await enqueue_pb_device_downlink_unlocked(ws, wire, pcm)


async def _stop_pb_device_downlink_worker(ws) -> None:
    task = getattr(ws, _PB_DEVICE_WORKER_ATTR, None)
    q = getattr(ws, _PB_DEVICE_QUEUE_ATTR, None)
    if task is None or q is None:
        return
    try:
        await q.put(None)
    except Exception:
        pass
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        if not task.done():
            task.cancel()
            try:
                await task
            except Exception:
                pass
    try:
        delattr(ws, _PB_DEVICE_WORKER_ATTR)
        delattr(ws, _PB_DEVICE_QUEUE_ATTR)
    except Exception:
        pass
    try:
        delattr(ws, _PB_WS_CHAIN_SERIAL_LOCK_ATTR)
    except Exception:
        pass


async def _send_pb_wire_to_asr_device(
    websocket, wire: str, pcm: Optional[bytes]
) -> None:
    """TTS 等：在仅 pb 设备连接上经队列发送，否则直接 ``_safe_send``。"""
    if getattr(websocket, "_asr_chat_pb_serial_queue", False):
        await enqueue_pb_device_downlink_unlocked(
            websocket, wire, pcm if pcm else None
        )
    else:
        if pcm:
            await _safe_send_pb_json_then_pcm(websocket, wire, pcm)
        else:
            await _safe_send(websocket, wire)
