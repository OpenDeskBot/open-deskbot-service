from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Optional

from deskbot_server.auto_reply import get_asr_voice_auto_reply_enabled, set_asr_voice_auto_reply_enabled
from deskbot_server.constants import PB_SCENES_FILE
from deskbot_server.pb.scenes import (
    _load_pb_scenes_document,
    _pb_scene_entry_by_name,
    _pb_scene_keys_sorted,
    _prepare_pb_scene_chain_frames,
)
from deskbot_server.util import _extract_device_id, _parse_query
from deskbot_server.ws.registry import DeviceRegistry

if TYPE_CHECKING:
    from deskbot_server.application.chat_service import ChatService
    from deskbot_server.ws.asr_chat_hub import AsrChatHub
    from deskbot_server.ws.device_pipeline import DevicePipelineBroker

logger = logging.getLogger("deskbot-server")

def _build_http_request_handler(
    device_pipeline_broker: DevicePipelineBroker,
    registry: DeviceRegistry,
    *,
    asr_chat_hub: "AsrChatHub",
    chat: "ChatService",
):
    """在同端口之上提供极简 HTTP 接口（不新增依赖）。

    - GET /api/devices：返回当前注册的设备列表（源自 DeviceRegistry）
    - GET /api/pipeline_recent?device_id=xxx&limit=100：返回滚动窗口内的事件
    - GET /api/device_servo?device_id=…&dyaw=…&dpitch=…&ms=…&xm=…&ym=…&action=…：向该设备
      已连接的 ``/asr_chat`` 下发仅含 ``servo`` 的 **单片** ``pb_single``。``xm``/``ym`` 缺省
      ``0``（绝对，x/y 钳位 0–180）；``xm=ym=1`` 时 dyaw/dpitch 为相对增量（±90）。可选 ``action``：``replace`` / ``append`` / ``opportunistic``，缺省
      ``opportunistic``（与调试舵机「队列空才入队」一致）。可选 ``with_scene`` / ``append_scene``：
      与 ``pb_single`` 在**同一条** ``device_pb_only`` 链锁内**原子**顺序下发该场景
      的 ``pb_start``→…→``pb_end``（与 ``/api/device_pb_scene`` 同源 JSON，无 PCM），
      用于注视/跟随舵机与 ``happy_smile`` 同批入队。下位机约定：仅一帧时用
      ``pb_single``；多帧须以 ``pb_start`` 开头、``pb_end`` 收尾，勿单发 ``pb_end`` 冒充单片。
    - GET /api/device_pb_scenes：列出 ``data/pb_scenes_idle_sleep_guard.json`` 中 ``scenes`` 下
      含非空 ``frames`` 的场景 id（按名排序）。
    - GET /api/device_pb_scene?device_id=…&scene=<id>：按上述文件 ``scenes.<id>`` 向该设备
      ``/asr_chat`` **顺序**下发 ``pb_start`` → ``pb_chunk``* → ``pb_end``（含 ``anim``+``servo``，无音频）。
      ``scene`` 与文件中的 key **不区分大小写**。
    - GET /api/asr_auto_reply：返回 ``{"ok", "enabled"}`` 是否对 ``/asr_chat`` 执行 LLM+TTS；
      带 ``?enabled=1|0|true|false`` 时同时写入开关（调试页「启用自动应答」）。
    - GET/POST /api/device_tts：``device_id`` + ``text``，跳过 LLM 直接音素 TTS 并下发 pb；
      可选 ``scene``：与 TTS 在同一条 pb 链锁内顺序追加该场景帧（先语音后场景）。
    - GET /health：存活探针
    - 其它 /api/* 返回 404；非 WS 升级请求才会进入该分支。
    """
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    def _cors_headers() -> Headers:
        headers = Headers()
        headers["Access-Control-Allow-Origin"] = "*"
        headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        headers["Access-Control-Allow-Headers"] = "Content-Type"
        headers["Cache-Control"] = "no-store"
        return headers

    def _json_resp(status: int, payload: object) -> Response:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = _cors_headers()
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Length"] = str(len(body))
        reason = {200: "OK", 404: "Not Found", 400: "Bad Request"}.get(
            status, "OK"
        )
        return Response(status, reason, headers, body)

    def _no_content() -> Response:
        headers = _cors_headers()
        headers["Content-Length"] = "0"
        return Response(204, "No Content", headers, b"")

    def _peer_from_conn(connection) -> str:
        try:
            peer = getattr(connection, "remote_address", None)
            if peer and isinstance(peer, tuple) and len(peer) >= 2:
                return f"{peer[0]}:{peer[1]}"
        except Exception:
            pass
        return "?"

    async def handler(connection, request):
        upgrade = (request.headers.get("Upgrade") or "").strip().lower()
        if upgrade == "websocket":
            return None

        raw_path = request.path or ""
        path_only, _, query = raw_path.partition("?")
        qargs = _parse_query(query)
        peer = _peer_from_conn(connection)
        method = (getattr(request, "method", None) or "GET").upper()
        if method == "OPTIONS" and path_only.startswith("/api/"):
            return _no_content()

        if path_only == "/health":
            logger.info("[HTTP] GET /health peer=%s -> 200", peer)
            return _json_resp(200, {"ok": True})

        if path_only == "/api/devices":
            snap = registry.snapshot()
            device_ids = [d.get("device_id") for d in snap]
            logger.info(
                "[HTTP] GET /api/devices peer=%s -> %d 台设备 device_ids=%s",
                peer,
                len(snap),
                device_ids,
            )
            return _json_resp(
                200,
                {
                    "devices": snap,
                    "t": time.time(),
                },
            )

        if path_only == "/api/asr_auto_reply":
            raw_e = qargs.get("enabled")
            if raw_e is None:
                logger.info(
                    "[HTTP] GET /api/asr_auto_reply -> enabled=%s peer=%s",
                    get_asr_voice_auto_reply_enabled(),
                    peer,
                )
            if raw_e is not None:
                se = str(raw_e).strip().lower()
                if se in ("1", "true", "yes", "on"):
                    set_asr_voice_auto_reply_enabled(True)
                elif se in ("0", "false", "no", "off"):
                    set_asr_voice_auto_reply_enabled(False)
                else:
                    return _json_resp(
                        400,
                        {
                            "ok": False,
                            "error": "invalid enabled; use 1/0 or true/false",
                        },
                    )
                logger.info(
                    "[HTTP] /api/asr_auto_reply set enabled=%s peer=%s",
                    get_asr_voice_auto_reply_enabled(),
                    peer,
                )
            return _json_resp(
                200,
                {"ok": True, "enabled": get_asr_voice_auto_reply_enabled()},
            )

        if path_only == "/api/pipeline_recent":
            dev = _extract_device_id(qargs)
            try:
                limit = int(qargs.get("limit") or str(device_pipeline_broker.max_events))
            except ValueError:
                limit = device_pipeline_broker.max_events
            limit = max(1, min(device_pipeline_broker.max_events, limit))
            items = device_pipeline_broker.snapshot_events(dev, limit)
            logger.info(
                "[HTTP] GET /api/pipeline_recent peer=%s device_id=%s limit=%d -> %d 条",
                peer,
                dev,
                limit,
                len(items),
            )
            return _json_resp(
                200,
                {
                    "items": items,
                    "device_id": dev,
                    "limit": limit,
                    "max_events": device_pipeline_broker.max_events,
                    "t": time.time(),
                },
            )

        if path_only == "/api/device_servo":
            dev = (qargs.get("device_id") or "").strip()
            if not dev:
                return _json_resp(
                    400,
                    {"error": "missing device_id", "t": time.time()},
                )
            try:
                dyaw = float(qargs.get("dyaw") or 0.0)
                dpitch = float(qargs.get("dpitch") or 0.0)
            except (TypeError, ValueError):
                return _json_resp(
                    400,
                    {"error": "invalid dyaw or dpitch", "t": time.time()},
                )
            try:
                ms = int(qargs.get("ms") or 400)
            except (TypeError, ValueError):
                ms = 400
            ms = max(50, min(ms, 10_000))
            try:
                # 固定镜头默认绝对定位；显式传 xm=1 时为相对增量
                xm = int(qargs.get("xm") if qargs.get("xm") is not None else 0)
            except (TypeError, ValueError):
                return _json_resp(
                    400,
                    {"error": "invalid xm (use 0=absolute|1=relative)", "t": time.time()},
                )
            try:
                ym = int(qargs.get("ym") if qargs.get("ym") is not None else xm)
            except (TypeError, ValueError):
                return _json_resp(
                    400,
                    {"error": "invalid ym (use 0=absolute|1=relative)", "t": time.time()},
                )
            if xm == 0:
                ix = int(round(max(0.0, min(180.0, dyaw))))
            else:
                ix = int(round(max(-90.0, min(90.0, dyaw))))
            if ym == 0:
                iy = int(round(max(0.0, min(180.0, dpitch))))
            else:
                iy = int(round(max(-90.0, min(90.0, dpitch))))
            if xm not in (0, 1) or ym not in (0, 1):
                return _json_resp(
                    400,
                    {
                        "error": "invalid xm/ym (debug UI: 0=absolute, 1=relative)",
                        "t": time.time(),
                    },
                )
            act = (qargs.get("action") or "opportunistic").strip().lower()
            if act not in ("replace", "append", "opportunistic"):
                return _json_resp(
                    400,
                    {
                        "error": "invalid action (use replace|append|opportunistic)",
                        "t": time.time(),
                    },
                )
            req_id = uuid.uuid4().hex[:16]
            payload = {
                "type": "pb_single",
                "req": req_id,
                "idx": 0,
                "chunk_ms": ms,
                "action": act,
                "servo": {
                    "xm": xm,
                    "ym": ym,
                    "x": ix,
                    "y": iy,
                    "ms": ms,
                },
            }

            with_scene = (qargs.get("with_scene") or qargs.get("append_scene") or "").strip()
            tail_frames: Optional[list[dict]] = None
            scene_req: Optional[str] = None
            if with_scene:
                doc_ws = _load_pb_scenes_document()
                if doc_ws and _pb_scene_entry_by_name(doc_ws, with_scene):
                    scene_req = uuid.uuid4().hex[:16]
                    tail_frames = _prepare_pb_scene_chain_frames(
                        with_scene, runtime_req=scene_req
                    )
                    if not tail_frames:
                        tail_frames = None
                        scene_req = None
                else:
                    logger.warning(
                        "[/api/device_servo] with_scene=%r 未知或空帧，仅下发 pb_single device_id=%s",
                        with_scene,
                        dev,
                    )

            try:
                logger.info(
                    "[/api/device_servo] 发往 device_id=%s（/asr_chat WebSocket）文本帧: %s%s",
                    dev,
                    json.dumps(payload, ensure_ascii=False),
                    f" +scene={with_scene!r} frames={len(tail_frames)}" if tail_frames else "",
                )
                if tail_frames:
                    n = await asr_chat_hub.send_pb_single_then_chain_ordered(
                        dev, payload, tail_frames
                    )
                else:
                    n = await asr_chat_hub.send(dev, payload)
            except Exception:
                logger.exception(
                    "[HTTP] /api/device_servo 下发异常 device_id=%s", dev
                )
                n = 0
            logger.info(
                "[HTTP] GET /api/device_servo peer=%s device_id=%s "
                "dyaw=%s dpitch=%s xm=%d ym=%d action=%s -> pb_single ix=%d iy=%d ms=%d delivered=%d%s",
                peer,
                dev,
                dyaw,
                dpitch,
                xm,
                ym,
                act,
                ix,
                iy,
                ms,
                n,
                f" with_scene={with_scene!r}" if with_scene else "",
            )
            return _json_resp(
                200,
                {
                    "ok": True,
                    "device_id": dev,
                    "type": "pb_single",
                    "action": act,
                    "servo": payload["servo"],
                    "req": req_id,
                    "delivered": n,
                    "with_scene": with_scene or None,
                    "scene_req": scene_req,
                    "scene_frames": len(tail_frames) if tail_frames else 0,
                    "t": time.time(),
                },
            )

        if path_only == "/api/device_pb_scenes":
            doc = _load_pb_scenes_document()
            keys = _pb_scene_keys_sorted(doc)
            logger.info(
                "[HTTP] GET /api/device_pb_scenes peer=%s -> %d scene(s)",
                peer,
                len(keys),
            )
            return _json_resp(
                200,
                {
                    "ok": True,
                    "scenes": keys,
                    "file": os.path.basename(PB_SCENES_FILE),
                    "t": time.time(),
                },
            )

        if path_only == "/api/device_tts":
            from deskbot_server.application.chat_flow import run_device_tts_only
            from deskbot_server.infrastructure.ws.downlink_adapter import WsDownlinkAdapter

            dev = ""
            text = ""
            scene_q = ""
            if method == "POST":
                try:
                    raw_body = (getattr(request, "body", None) or b"").decode("utf-8")
                    payload = json.loads(raw_body) if raw_body.strip() else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return _json_resp(
                        400,
                        {"ok": False, "error": "invalid JSON body", "t": time.time()},
                    )
                if isinstance(payload, dict):
                    dev = str(payload.get("device_id") or "").strip()
                    text = str(payload.get("text") or "").strip()
                    scene_q = str(payload.get("scene") or "").strip()
            else:
                dev = (qargs.get("device_id") or "").strip()
                text = (qargs.get("text") or "").strip()
                scene_q = (qargs.get("scene") or "").strip()
            if not dev:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing device_id", "t": time.time()},
                )
            if not text:
                return _json_resp(
                    400,
                    {"ok": False, "error": "missing text", "t": time.time()},
                )
            ws = await asr_chat_hub.first_ws(dev)
            if ws is None:
                return _json_resp(
                    200,
                    {
                        "ok": False,
                        "error": "device not connected on /asr_chat",
                        "device_id": dev,
                        "delivered": 0,
                        "hint": "请确认 ESP32 已用相同 device_id 连接 /asr_chat",
                        "t": time.time(),
                    },
                )
            req_id = uuid.uuid4().hex[:16]
            settings = chat.settings
            broker = device_pipeline_broker

            async def _device_tts_job() -> None:
                downlink = WsDownlinkAdapter(
                    ws,
                    settings=settings,
                    device_id=dev,
                    dp_broker=broker,
                )
                try:
                    turn = await run_device_tts_only(
                        downlink,
                        chat,
                        text,
                        request_id=req_id,
                        device_id=dev,
                        scenes=[scene_q] if scene_q else None,
                    )
                    ok = (turn.status or "ok") == "ok" and not turn.error
                    logger.info(
                        "[HTTP] /api/device_tts job done device_id=%s req=%s text=%r ok=%s err=%s",
                        dev,
                        req_id,
                        text[:120],
                        ok,
                        turn.error,
                    )
                except Exception:
                    logger.exception(
                        "[HTTP] /api/device_tts job failed device_id=%s req=%s",
                        dev,
                        req_id,
                    )

            asyncio.create_task(_device_tts_job())
            logger.info(
                "[HTTP] %s /api/device_tts accepted peer=%s device_id=%s req=%s text=%r",
                method,
                peer,
                dev,
                req_id,
                text[:120],
            )
            return _json_resp(
                200,
                {
                    "ok": True,
                    "accepted": True,
                    "device_id": dev,
                    "text": text,
                    "scene": scene_q or None,
                    "req": req_id,
                    "t": time.time(),
                },
            )

        if path_only == "/api/device_pb_scene":
            dev = (qargs.get("device_id") or "").strip()
            scene_q = (qargs.get("scene") or "").strip()
            if not dev:
                return _json_resp(
                    400,
                    {"error": "missing device_id", "t": time.time()},
                )
            if not scene_q:
                return _json_resp(
                    400,
                    {"error": "missing scene", "t": time.time()},
                )
            doc = _load_pb_scenes_document()
            if not doc:
                return _json_resp(
                    500,
                    {
                        "error": "cannot read or parse scenes file",
                        "path": PB_SCENES_FILE,
                        "t": time.time(),
                    },
                )
            valid = _pb_scene_keys_sorted(doc)
            sc_obj = _pb_scene_entry_by_name(doc, scene_q)
            if sc_obj is None:
                return _json_resp(
                    400,
                    {
                        "error": f"unknown or empty scene: {scene_q!r}",
                        "valid_scenes": valid,
                        "t": time.time(),
                    },
                )
            raw_frames = sc_obj.get("frames")
            if not isinstance(raw_frames, list) or not raw_frames:
                return _json_resp(
                    500,
                    {"error": f"scene {scene_q!r} has no frames", "t": time.time()},
                )
            req_id = uuid.uuid4().hex[:16]
            frames: list[dict] = []
            for fr in raw_frames:
                if not isinstance(fr, dict):
                    continue
                one = copy.deepcopy(fr)
                one["req"] = req_id
                frames.append(one)
            if not frames:
                return _json_resp(
                    500,
                    {"error": "no valid frame objects", "t": time.time()},
                )
            scene_log = scene_q.strip().lower()

            logger.info(
                "[HTTP] GET /api/device_pb_scene peer=%s device_id=%s scene=%s req=%s frames=%d",
                peer,
                dev,
                scene_log,
                req_id,
                len(frames),
            )
            try:
                n = await asr_chat_hub.send_pb_chain_ordered(dev, frames)
            except Exception:
                logger.exception(
                    "[HTTP] /api/device_pb_scene 下发异常 device_id=%s scene=%s",
                    dev,
                    scene_log,
                )
                return _json_resp(
                    500,
                    {
                        "ok": False,
                        "error": "send failed (see server log)",
                        "device_id": dev,
                        "scene": scene_q,
                        "t": time.time(),
                    },
                )
            logger.info(
                "[/api/device_pb_scene] 已顺序下发 scene=%s device_id=%s req=%s frames=%d ws_sends=%d",
                scene_log,
                dev,
                req_id,
                len(frames),
                n,
            )
            hint = None
            if n == 0:
                hint = (
                    "没有发往 WebSocket：该 device_id 当前无已连接的 /asr_chat，"
                    "或连接已断开。请确认 ESP32 使用相同 device_id 登录 /asr_chat。"
                )
            return _json_resp(
                200,
                {
                    "ok": True,
                    "device_id": dev,
                    "scene": scene_q,
                    "req": req_id,
                    "frames": len(frames),
                    "delivered": n,
                    "hint": hint,
                    "t": time.time(),
                },
            )

        if path_only.startswith("/api/"):
            logger.warning(
                "[HTTP] 未知 API peer=%s path=%s -> 404", peer, path_only
            )
            return _json_resp(404, {"error": "not found", "path": path_only})

        logger.debug("[HTTP] 非 API 请求忽略 peer=%s path=%s", peer, path_only)
        return _no_content()

    return handler
