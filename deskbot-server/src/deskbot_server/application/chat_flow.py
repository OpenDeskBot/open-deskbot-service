from __future__ import annotations

import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Optional

from deskbot_server.application.llm_tool_runner import execute_llm_tools
from deskbot_server.auto_reply import get_asr_voice_auto_reply_enabled
from deskbot_server.core.ports.downlink import DownlinkPort, PipelineEventsPort
from deskbot_server.core.types import ChatTurnResult
from deskbot_server.llm.utils import parse_llm_reply
from deskbot_server.pb.scenes import (
    _pb_scene_entry_by_name,
    _prepare_pb_scene_chain_frames,
)
from deskbot_server.pb.wire import build_pb_wire_pairs
from deskbot_server.util import _json_msg, _ms_between, format_exc_detail

if TYPE_CHECKING:
    from deskbot_server.application.chat_service import ChatService
    from deskbot_server.ws.registry import DeviceRegistry

logger = logging.getLogger("deskbot-server")


async def run_chat_turn(
    downlink: DownlinkPort,
    chat: ChatService,
    user_text: str,
    *,
    request_id: Optional[str] = None,
    device_id: Optional[str] = None,
    registry: Optional[DeviceRegistry] = None,
    t_asr_start: Optional[float] = None,
    t_asr_text: Optional[float] = None,
) -> ChatTurnResult:
    """在已有用户侧文本后执行 LLM + TTS/pb 管道（应用层，不依赖 WebSocket 类型）。"""
    result = ChatTurnResult()
    settings = chat.settings

    try:
        if not get_asr_voice_auto_reply_enabled():
            now_m = time.monotonic()
            result.t_llm_end = now_m
            result.t_tts_end = now_m
            result.voice_auto_reply_off = True
            logger.info(
                "[asr] 自动应答已关闭，跳过 LLM/TTS device_id=%s req=%s user=%r",
                device_id,
                request_id,
                (user_text or "")[:120],
            )
            send_v = settings.should_send_stage_to_device("llm_text")
            await downlink.emit_stage(
                "voice_auto_reply_off",
                request_id=request_id,
                client_fields={
                    "message": "自动应答已关闭（后台调试页可重新开启）",
                    "user_text": user_text,
                },
                event_fields={"skipped": True, "reason": "voice_auto_reply_off"},
                send_client=send_v,
            )
            await downlink.emit_stage(
                "playback_done",
                request_id=request_id,
                client_fields={"playback": "skipped", "reason": "voice_auto_reply_off", "pb_segments": 0},
                event_fields={
                    "tts_ms": 0.0,
                    "e2e_ms": _ms_between(t_asr_start, result.t_tts_end),
                    "playback": "skipped",
                },
                send_client=False,
            )
            return result

        ack_ctx = None
        if registry is not None and device_id:
            ack_ctx = await registry.pb_ack_llm_context(device_id)

        answer = await chat.llm(user_text, device_context=ack_ctx, device_id=device_id)
        parsed = parse_llm_reply(answer)
        reply_text = parsed["reply"]
        llm_scenes = list(parsed.get("scenes") or [])
        llm_moves = list(parsed.get("moves") or [])
        llm_anims = list(parsed.get("anims") or [])
        llm_tools = list(parsed.get("tools") or [])
        need_reply = bool(parsed.get("need_reply", True))

        tool_results: list[dict] = []
        if llm_tools and device_id:
            tool_results = execute_llm_tools(llm_tools, device_id=device_id)
            if tool_results:
                logger.info(
                    "[LLM] tools device_id=%s req=%s results=%s",
                    device_id,
                    request_id,
                    tool_results,
                )

        result.llm_text = reply_text
        result.llm_raw = parsed["raw"]
        result.scenes = llm_scenes
        result.moves = llm_moves
        result.anims = llm_anims
        result.tools = llm_tools
        result.tool_results = tool_results
        result.servo = list(parsed.get("servo") or [])
        result.need_reply = need_reply
        result.json_ok = parsed["json_ok"]
        result.t_llm_end = time.monotonic()

        llm_ms = _ms_between(t_asr_text, result.t_llm_end)
        logger.info(
            "[LLM] 回复 device_id=%s req=%s llm_ms=%s json_ok=%s need_reply=%s json=%s",
            device_id,
            request_id,
            llm_ms,
            parsed["json_ok"],
            need_reply,
            parsed["raw"],
        )

        if not parsed["json_ok"]:
            logger.warning(
                "[LLM] 输出未通过 JSON 解析，按整段文本走 TTS。device_id=%s req=%s",
                device_id,
                request_id,
            )

        send_llm = settings.should_send_stage_to_device("llm_text") and need_reply
        await downlink.emit_stage(
            "llm_text",
            request_id=request_id,
            client_fields={
                "text": reply_text,
                "raw": parsed["raw"],
                "json_ok": parsed["json_ok"],
                "need_reply": need_reply,
                "moves": llm_moves,
                "anims": llm_anims,
                "tools": llm_tools,
                "tool_results": tool_results,
                "scenes": llm_scenes,
                "servo": result.servo,
            },
            event_fields={
                "llm_ms": llm_ms,
                "text": reply_text,
                "raw": parsed["raw"],
                "moves": llm_moves,
                "anims": llm_anims,
                "tools": llm_tools,
                "tool_results": tool_results,
                "scenes": llm_scenes,
                "servo": result.servo,
                "json_ok": parsed["json_ok"],
                "need_reply": need_reply,
            },
            send_client=send_llm,
        )

        if tool_results:
            await downlink.emit_stage(
                "llm_tools",
                request_id=request_id,
                client_fields={"tools": llm_tools, "results": tool_results},
                event_fields={"tools": llm_tools, "results": tool_results},
                send_client=False,
            )

        if not need_reply:
            logger.info(
                "[LLM] need_reply=false，跳过 TTS/pb。device_id=%s req=%s",
                device_id,
                request_id,
            )
            result.t_tts_end = time.monotonic()
            await downlink.emit_stage(
                "playback_done",
                request_id=request_id,
                client_fields={"playback": "skipped", "need_reply": False, "pb_segments": 0},
                event_fields={
                    "tts_ms": 0.0,
                    "e2e_ms": _ms_between(t_asr_start, result.t_tts_end),
                    "playback": "skipped",
                    "need_reply": False,
                },
                send_client=False,
            )
            return result

        try:
            await _run_pb_playback(
                downlink,
                chat,
                reply_text=reply_text,
                parsed=parsed,
                llm_scenes=llm_scenes if not llm_anims else [],
                request_id=request_id,
                device_id=device_id,
                result=result,
                t_asr_start=t_asr_start,
            )
        except Exception as tts_exc:
            detail = format_exc_detail(tts_exc)
            logger.exception("TTS 流程失败")
            result.status = "error"
            result.error = f"tts: {tts_exc}"
            await downlink.emit_stage(
                "tts_error",
                request_id=request_id,
                client_fields={"message": str(tts_exc), "detail": detail},
                event_fields={"status": "error", "error": str(tts_exc)},
                send_client=settings.should_send_stage_to_device("tts_error"),
            )
    except Exception as llm_exc:
        detail = format_exc_detail(llm_exc)
        logger.exception("LLM 流程失败")
        result.status = "error"
        result.error = f"llm: {llm_exc}"
        await downlink.emit_stage(
            "error",
            request_id=request_id,
            client_fields={"message": str(llm_exc), "detail": detail},
            event_fields={"status": "error", "error": str(llm_exc)},
            send_client=settings.should_send_stage_to_device("error"),
        )

    return result


async def run_device_tts_only(
    downlink: DownlinkPort,
    chat: "ChatService",
    text: str,
    *,
    request_id: Optional[str] = None,
    device_id: Optional[str] = None,
    scenes: Optional[list] = None,
    moves: Optional[list] = None,
    anims: Optional[list] = None,
) -> ChatTurnResult:
    """跳过 LLM，将给定文本走音素 TTS 并下发 pb；可选在同一条链锁内追加场景 pb 帧。"""
    reply_text = (text or "").strip()
    result = ChatTurnResult()
    result.llm_text = reply_text
    result.t_llm_end = time.monotonic()
    parsed = {
        "reply": reply_text,
        "servo": [],
        "scenes": [],
        "json_ok": True,
        "need_reply": True,
        "raw": reply_text,
        "moves": list(moves or []),
        "anims": list(anims or []),
    }
    if not reply_text:
        result.status = "error"
        result.error = "empty text"
        return result
    try:
        scene_list = [
            str(s).strip()
            for s in (scenes or [])
            if isinstance(s, str) and str(s).strip()
        ]
        if parsed["moves"] or parsed["anims"]:
            scene_list = []
        await _run_pb_playback(
            downlink,
            chat,
            reply_text=reply_text,
            parsed=parsed,
            llm_scenes=scene_list,
            request_id=request_id,
            device_id=device_id,
            result=result,
            t_asr_start=result.t_llm_end,
        )
    except Exception as tts_exc:
        detail = format_exc_detail(tts_exc)
        logger.exception("[device_tts] TTS 流程失败 device_id=%s", device_id)
        result.status = "error"
        result.error = f"tts: {tts_exc}"
        await downlink.emit_stage(
            "tts_error",
            request_id=request_id,
            client_fields={"message": str(tts_exc), "detail": detail},
            event_fields={"status": "error", "error": str(tts_exc)},
            send_client=chat.settings.should_send_stage_to_device("tts_error"),
        )
    return result


async def run_device_playbook(
    downlink: DownlinkPort,
    chat: "ChatService",
    playbook: dict,
    *,
    request_id: Optional[str] = None,
    device_id: Optional[str] = None,
) -> ChatTurnResult:
    """场景编排：TTS 与表情/舵机轨在同一条 pb 链内交错下发（非 TTS 结束后再播）。"""
    from deskbot_server.scene_playbook_runner import playbook_to_llm_plan

    text, moves, anims = playbook_to_llm_plan(playbook)
    if not text.strip():
        if moves or anims:
            text = "。"
        else:
            result = ChatTurnResult()
            result.status = "error"
            result.error = "empty playbook"
            return result
    return await run_device_tts_only(
        downlink,
        chat,
        text,
        request_id=request_id,
        device_id=device_id,
        scenes=None,
        moves=moves,
        anims=anims,
    )


async def _run_pb_playback(
    downlink: DownlinkPort,
    chat: ChatService,
    *,
    reply_text: str,
    parsed: dict,
    llm_scenes: list,
    request_id: Optional[str],
    device_id: Optional[str],
    result: ChatTurnResult,
    t_asr_start: Optional[float],
) -> None:
    sr_pb, segs = await chat.tts_phoneme_segments(reply_text)
    pcm_ok = any(len(s.get("pcm") or b"") > 0 for s in segs)
    if not segs or not pcm_ok:
        raise RuntimeError("phoneme TTS 无分片或无 PCM")

    pairs, pb_req, n_pb, sr_pb = build_pb_wire_pairs(
        segs,
        chat.tts_cfg,
        servo_plan=list(parsed.get("servo") or []) if not parsed.get("moves") else None,
        moves=list(parsed.get("moves") or []),
        anims=list(parsed.get("anims") or []),
        sample_rate=sr_pb,
        request_id=request_id,
        random_servo_cfg=chat.settings.pb_random_servo_cfg(),
    )

    frame_overview = [
        {
            "i": i,
            "type": m.get("type"),
            "idx": m.get("idx"),
            "chunk_ms": m.get("chunk_ms"),
            "phoneme": m.get("phoneme"),
            "action": m.get("action"),
            "pcm_bytes": len(p) if p else 0,
            "has_audio_next_bin": bool(m.get("audio")),
        }
        for i, (m, p) in enumerate(pairs)
    ]
    logger.info(
        "[pb TX] 开始下发 device_id=%s request_id=%s pb_req=%s segments=%d sr=%s",
        device_id,
        request_id,
        pb_req,
        n_pb,
        sr_pb,
    )
    logger.info("[pb TX] 帧序一览 %s", json.dumps(frame_overview, ensure_ascii=False))

    n_scene_pb = 0
    scenes_applied: list[str] = []
    async with downlink.pb_serial_chain():
        for i, (msg, pcm) in enumerate(pairs):
            wire_text = _json_msg(msg)
            logger.info("[pb TX] %d/%d wire_json %s", i + 1, n_pb, wire_text)
            if pcm:
                logger.info("[pb TX] %d/%d binary idx=%s bytes=%d", i + 1, n_pb, msg.get("idx"), len(pcm))
            await downlink.send_pb_wire(wire_text, pcm)

        for sc_name in llm_scenes:
            if not isinstance(sc_name, str):
                continue
            sc_key = sc_name.strip()
            if not sc_key or _pb_scene_entry_by_name({}, sc_key) is None:
                if sc_key:
                    logger.warning(
                        "[pb TX] LLM scenes 跳过未知场景 %r device_id=%s req=%s",
                        sc_key,
                        device_id,
                        request_id,
                    )
                continue
            sreq = uuid.uuid4().hex[:16]
            sframes = _prepare_pb_scene_chain_frames(sc_key, runtime_req=sreq)
            if not sframes:
                continue
            scenes_applied.append(sc_key)
            for fi, one in enumerate(sframes):
                await downlink.send_pb_wire(_json_msg(one), None)
                n_scene_pb += 1

    logger.info(
        "[pb TX] 下发结束 device_id=%s pb_req=%s 语音 JSON=%d%s",
        device_id,
        pb_req,
        n_pb,
        f"；LLM scenes 追加 {n_scene_pb} 条" if n_scene_pb else "",
    )
    result.t_tts_end = time.monotonic()
    await downlink.emit_stage(
        "playback_done",
        request_id=request_id,
        client_fields={
            "playback": "pb",
            "pb_req": pb_req,
            "pb_segments": len(pairs),
            "pb_llm_scene_extra": n_scene_pb,
            "scenes_applied": scenes_applied,
        },
        event_fields={
            "tts_ms": _ms_between(result.t_llm_end, result.t_tts_end),
            "e2e_ms": _ms_between(t_asr_start, result.t_tts_end),
            "playback": "pb",
        },
        send_client=False,
    )


async def publish_chat_turn(
    events: PipelineEventsPort,
    device_id: Optional[str],
    *,
    source: str,
    asr_text: Optional[str],
    t_asr_start: Optional[float],
    t_asr_text: Optional[float],
    turn: ChatTurnResult,
    request_id: Optional[str] = None,
) -> None:
    if not device_id:
        return
    flow = turn.as_dict()
    t_llm_end = flow.get("t_llm_end")
    t_tts_end = flow.get("t_tts_end")
    end_t = t_tts_end or t_llm_end or t_asr_text
    evt = {
        "device_id": device_id,
        "request_id": request_id,
        "asr_text": asr_text,
        "asr_ms": _ms_between(t_asr_start, t_asr_text) if source == "asr" else None,
        "llm_text": flow.get("llm_text"),
        "llm_raw": flow.get("llm_raw"),
        "moves": list(flow.get("moves") or []),
        "anims": list(flow.get("anims") or []),
        "tools": list(flow.get("tools") or []),
        "tool_results": list(flow.get("tool_results") or []),
        "scenes": list(flow.get("scenes") or []),
        "json_ok": bool(flow.get("json_ok")),
        "need_reply": bool(flow.get("need_reply", True)),
        "voice_auto_reply_off": bool(flow.get("voice_auto_reply_off")),
        "llm_ms": _ms_between(t_asr_text, t_llm_end),
        "tts_text": flow.get("llm_text"),
        "tts_ms": _ms_between(t_llm_end, t_tts_end),
        "e2e_ms": _ms_between(t_asr_start, end_t),
        "status": flow.get("status") or "ok",
        "error": flow.get("error"),
        "source": source,
    }
    await events.publish_turn(evt)
    await events.touch_device(device_id, evt["status"])
