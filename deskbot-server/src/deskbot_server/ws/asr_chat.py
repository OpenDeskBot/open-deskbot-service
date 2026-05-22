from __future__ import annotations

import base64
import json
import logging
import time
from typing import Optional

from websockets.exceptions import ConnectionClosed

from deskbot_server.pipeline.audio import AudioConfig, ConnectionSession
from deskbot_server.pipeline.flow import _flow_llm_tts, _publish_asr_chat_turn
from deskbot_server.pipeline.pipeline import BotPipeline
from deskbot_server.settings import _asr_chat_send_stage_to_device
from deskbot_server.util import (
    _format_ts,
    _json_msg,
    _ms_between,
    _new_request_id,
    _normalize_incoming_pb_ack,
    _peer_str,
    format_exc_detail,
)
from deskbot_server.ws.asr_chat_hub import AsrChatHub
from deskbot_server.ws.device_pipeline import DevicePipelineBroker
from deskbot_server.ws.registry import DeviceRegistry
from deskbot_server.ws.stages import _emit_stage
from deskbot_server.ws.ws_send import _safe_send

logger = logging.getLogger("deskbot-server")

async def handle_asr_chat(
    websocket,
    pipeline: BotPipeline,
    audio_cfg: AudioConfig,
    device_id: Optional[str],
    registry: DeviceRegistry,
    dp_broker: DevicePipelineBroker,
    asr_chat_hub: AsrChatHub,
) -> None:
    """/asr_chat WS：处理音频/文本输入，并把每轮 ASR/LLM/TTS 指标推送到 pipeline 广播器。

    - ``device_id`` 来自 URL ``?device_id=xxx``；若为空，连接仍可用，但不会进入
      DeviceRegistry / AsrChatHub，也不会推送 pipeline 事件。
    - 每一轮请求（用户文本或 ASR 识别出的文本）都会生成唯一 ``request_id``，
      跨阶段（``asr_start`` / ``asr_text`` / ``llm_text`` / ``playback_done`` /
      ``tts_error`` / ``error``）注入；客户端与页面订阅者均可据此
      关联同一轮对话。
    """
    session = ConnectionSession(pipeline, audio_cfg)
    peer = _peer_str(websocket)
    if device_id:
        await registry.connect(device_id, "asr_chat", websocket)
        await asr_chat_hub.attach(device_id, websocket)
        logger.info(
            "[/asr_chat] 接入 device_id=%s peer=%s (已登记到 DeviceRegistry)",
            device_id,
            peer,
        )
    else:
        logger.warning(
            "[/asr_chat] 接入缺失 device_id peer=%s —— 不会出现在 /api/devices 设备列表，"
            "请改用 ws://host:9000/asr_chat?device_id=<设备ID>",
            peer,
        )
    try:
        if not getattr(pipeline, "asr_chat_device_pb_only", False):
            await _safe_send(
                websocket, _json_msg({"type": "ready", "device_id": device_id})
            )

        async for message in websocket:
            try:
                codec = None
                if isinstance(message, bytes):
                    payload = message
                else:
                    data = json.loads(message)
                    msg_type = data.get("type")
                    if msg_type == "ping":
                        if not getattr(pipeline, "asr_chat_device_pb_only", False):
                            await _safe_send(websocket, _json_msg({"type": "pong"}))
                        continue
                    if msg_type == "pb_ack":
                        norm = _normalize_incoming_pb_ack(data)
                        if norm is not None and device_id:
                            await registry.record_pb_ack(device_id, norm)
                            logger.info(
                                "[pb_ack] device_id=%s req=%r idx=%s audio_buf_ms=%s servo=%s",
                                device_id,
                                norm.get("req"),
                                norm.get("idx"),
                                norm.get("audio_buf_ms"),
                                norm.get("servo"),
                            )
                            if dp_broker is not None:
                                now_ts = time.time()
                                await dp_broker.broadcast_to_device(
                                    device_id,
                                    {
                                        "type": "pipeline_stage",
                                        "event": {
                                            "device_id": device_id,
                                            "request_id": None,
                                            "stage": "pb_ack",
                                            "ack": norm,
                                            "ts": now_ts,
                                            "t_mono": time.monotonic(),
                                            "received_at": _format_ts(now_ts),
                                        },
                                    },
                                )
                        elif norm is not None and not device_id:
                            logger.info(
                                "[pb_ack] 已解析但连接无 device_id，未入库 peer=%s",
                                peer,
                            )
                        continue
                    if msg_type == "user_text":
                        ut = (data.get("text") or "").strip()
                        if not ut:
                            if not (
                                getattr(
                                    pipeline, "asr_chat_minimal_device_downlink", False
                                )
                                or getattr(
                                    pipeline, "asr_chat_device_pb_only", False
                                )
                            ):
                                await _safe_send(
                                    websocket,
                                    _json_msg({"type": "error", "message": "空文本"}),
                                )
                            continue
                        if not pipeline.is_valid_asr_text(ut):
                            if not (
                                getattr(
                                    pipeline, "asr_chat_minimal_device_downlink", False
                                )
                                or getattr(
                                    pipeline, "asr_chat_device_pb_only", False
                                )
                            ):
                                await _safe_send(
                                    websocket,
                                    _json_msg({"type": "asr_rejected", "text": ut}),
                                )
                            continue
                        request_id = _new_request_id()
                        t_asr_start = time.monotonic()
                        await _emit_stage(
                            websocket,
                            dp_broker,
                            device_id,
                            request_id,
                            "asr_start",
                            event_fields={"source": "text"},
                            send_client=_asr_chat_send_stage_to_device(
                                pipeline, "asr_start"
                            ),
                        )
                        await _emit_stage(
                            websocket,
                            dp_broker,
                            device_id,
                            request_id,
                            "asr_text",
                            client_fields={"text": ut, "source": "text"},
                            event_fields={"asr_ms": None},
                            send_client=_asr_chat_send_stage_to_device(
                                pipeline, "asr_text"
                            ),
                        )
                        t_asr_text = time.monotonic()
                        flow = await _flow_llm_tts(
                            websocket,
                            pipeline,
                            ut,
                            request_id=request_id,
                            dp_broker=dp_broker,
                            registry=registry,
                            device_id=device_id,
                            t_asr_start=t_asr_start,
                            t_asr_text=t_asr_text,
                        )
                        await _publish_asr_chat_turn(
                            dp_broker,
                            registry,
                            device_id,
                            source="text",
                            asr_text=ut,
                            t_asr_start=t_asr_start,
                            t_asr_text=t_asr_text,
                            flow=flow,
                            request_id=request_id,
                        )
                        continue
                    if msg_type == "flush":
                        pcm_segment = session.flush()
                        if pcm_segment is None:
                            continue
                    elif msg_type == "audio":
                        payload = base64.b64decode(data["data"])
                        codec = data.get("codec")
                    else:
                        continue

                if isinstance(message, bytes) or data.get("type") == "audio":
                    pcm_segment = await session.feed_audio(payload, codec)
                    if pcm_segment is None:
                        continue

                request_id = _new_request_id()
                seg_duration_ms = int(
                    len(pcm_segment) / 2 / audio_cfg.sample_rate * 1000
                )
                t_asr_start = time.monotonic()
                await _emit_stage(
                    websocket,
                    dp_broker,
                    device_id,
                    request_id,
                    "asr_start",
                    event_fields={"source": "asr"},
                    send_client=_asr_chat_send_stage_to_device(pipeline, "asr_start"),
                )
                text = await pipeline.asr(
                    pcm_segment, sample_rate=audio_cfg.sample_rate
                )
                t_asr_text = time.monotonic()
                asr_ms = _ms_between(t_asr_start, t_asr_text)
                if not text:
                    logger.info(
                        "[ASR] 结果为空 device_id=%s req=%s audio_ms=%d asr_ms=%s",
                        device_id,
                        request_id,
                        seg_duration_ms,
                        asr_ms,
                    )
                    await _emit_stage(
                        websocket,
                        dp_broker,
                        device_id,
                        request_id,
                        "asr_empty",
                        event_fields={
                            "status": "error",
                            "error": "empty",
                            "asr_ms": asr_ms,
                        },
                        send_client=_asr_chat_send_stage_to_device(
                            pipeline, "asr_empty"
                        ),
                    )
                    continue
                if not pipeline.is_valid_asr_text(text):
                    logger.info(
                        "[ASR] 结果被过滤 device_id=%s req=%s audio_ms=%d asr_ms=%s text=%r",
                        device_id,
                        request_id,
                        seg_duration_ms,
                        asr_ms,
                        text,
                    )
                    await _emit_stage(
                        websocket,
                        dp_broker,
                        device_id,
                        request_id,
                        "asr_rejected",
                        client_fields={"text": text},
                        event_fields={
                            "status": "error",
                            "error": "rejected",
                            "asr_ms": asr_ms,
                        },
                        send_client=_asr_chat_send_stage_to_device(
                            pipeline, "asr_rejected"
                        ),
                    )
                    continue
                logger.info(
                    "[ASR] 识别成功 device_id=%s req=%s audio_ms=%d asr_ms=%s text=%r",
                    device_id,
                    request_id,
                    seg_duration_ms,
                    asr_ms,
                    text,
                )
                await _emit_stage(
                    websocket,
                    dp_broker,
                    device_id,
                    request_id,
                    "asr_text",
                    client_fields={"text": text, "source": "asr"},
                    event_fields={"asr_ms": asr_ms},
                    send_client=_asr_chat_send_stage_to_device(pipeline, "asr_text"),
                )
                flow = await _flow_llm_tts(
                    websocket,
                    pipeline,
                    text,
                    request_id=request_id,
                    dp_broker=dp_broker,
                    registry=registry,
                    device_id=device_id,
                    t_asr_start=t_asr_start,
                    t_asr_text=t_asr_text,
                )
                await _publish_asr_chat_turn(
                    dp_broker,
                    registry,
                    device_id,
                    source="asr",
                    asr_text=text,
                    t_asr_start=t_asr_start,
                    t_asr_text=t_asr_text,
                    flow=flow,
                    request_id=request_id,
                )
            except Exception as exc:
                detail = format_exc_detail(exc)
                logger.exception("处理客户端消息失败")
                if not getattr(pipeline, "asr_chat_device_pb_only", False):
                    await _safe_send(
                        websocket,
                        _json_msg(
                            {"type": "error", "message": str(exc), "detail": detail}
                        ),
                    )
    except ConnectionClosed as closed:
        logger.info("WebSocket 已关闭: %s", closed)
    finally:
        if device_id:
            await asr_chat_hub.detach(device_id, websocket)
            await registry.disconnect(websocket)
