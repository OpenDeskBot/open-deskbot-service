"""WebSocket 接口：/camera 与 /camera_view（处理器薄层）。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from websockets.exceptions import ConnectionClosed

from deskbot_server.application.camera_broker import CameraImageBroker
from deskbot_server.application.camera_frame import (
    analyze_face_detection,
    build_face_info_message,
    build_face_pos_payload,
)
from deskbot_server.application.face_detector import CameraFaceDetector
from deskbot_server.util import (
    _extract_device_id,
    _json_msg,
    _parse_query,
    _peer_str,
    _split_path,
    _ws_request_path,
)
from deskbot_server.vision.geometry import FACE_FRAME_HEIGHT, FACE_FRAME_WIDTH, FACE_KEYPOINT_NAMES
from deskbot_server.vision.undistort import CameraFaceRuntime
from deskbot_server.ws.asr_chat_hub import AsrChatHub
from deskbot_server.ws.device_pipeline import DevicePipelineBroker
from deskbot_server.ws.registry import DeviceRegistry
from deskbot_server.ws.ws_send import _safe_send

logger = logging.getLogger("deskbot-server")

__all__ = ["CameraImageBroker", "CameraFaceDetector", "handle_camera", "handle_camera_view"]


async def handle_camera(
    websocket,
    registry: DeviceRegistry,
    asr_chat_hub: AsrChatHub,
    dp_broker: DevicePipelineBroker,
    image_broker: CameraImageBroker,
    camera_face_runtime: CameraFaceRuntime,
    *,
    send_face_info_to_asr_chat: bool = False,
) -> None:
    req_path = _ws_request_path(websocket)
    _, query = _split_path(req_path)
    qargs = _parse_query(query)
    url_device = _extract_device_id(qargs)
    peer = _peer_str(websocket)

    if not url_device:
        logger.warning("[/camera] 拒绝接入：缺失 device_id peer=%s path=%s", peer, req_path)
        await _safe_send(
            websocket,
            _json_msg({"type": "error", "message": "/camera 必须在 URL 中携带 device_id"}),
        )
        await websocket.close(code=1008, reason="device_id required")
        return

    try:
        detector = await asyncio.to_thread(
            CameraFaceDetector,
            undistorter=camera_face_runtime.undistorter,
            min_face_detection_confidence=camera_face_runtime.min_face_detection_confidence,
            min_face_presence_confidence=camera_face_runtime.min_face_presence_confidence,
        )
    except Exception as exc:
        detail = str(exc)
        if "libGLESv2" in detail or "libEGL" in detail:
            detail = (
                f"{detail} — Linux 请安装 OpenGL ES 库，例如: "
                "CentOS/RHEL: yum install mesa-libGLES mesa-libEGL; "
                "Debian/Ubuntu: apt install libgles2-mesa libegl1-mesa"
            )
        logger.error("[/camera] MediaPipe 初始化失败 device_id=%s: %s", url_device, detail)
        await _safe_send(
            websocket,
            _json_msg({"type": "error", "message": "服务端人脸检测器初始化失败", "detail": detail}),
        )
        await websocket.close(code=1011, reason="detector init failed")
        return

    await _safe_send(
        websocket,
        _json_msg(
            {
                "type": "ready",
                "channel": "camera",
                "width": FACE_FRAME_WIDTH,
                "height": FACE_FRAME_HEIGHT,
                "keypoints": list(FACE_KEYPOINT_NAMES),
                "device_id": url_device,
                "expects": "binary JPEG frames (no per-frame camera_ack)",
            }
        ),
    )

    logger.info("[/camera] 生产者接入 device_id=%s peer=%s", url_device, peer)
    await registry.connect(url_device, "camera", websocket)

    frame_count = 0
    detected_count = 0
    _stat_interval = 5.0
    _stat = {
        "t0": time.monotonic(),
        "frames_in": 0,
        "frames_decoded": 0,
        "frames_face": 0,
        "view_attempted": 0,
        "view_sent": 0,
        "infer_ms_total": 0.0,
    }

    def _flush_stat() -> None:
        elapsed = max(time.monotonic() - _stat["t0"], 1e-6)
        fps_in = _stat["frames_in"] / elapsed
        fps_face = _stat["frames_face"] / elapsed
        avg_infer = (
            _stat["infer_ms_total"] / _stat["frames_decoded"] if _stat["frames_decoded"] else 0.0
        )
        drop = _stat["view_attempted"] - _stat["view_sent"]
        drop_pct = drop / _stat["view_attempted"] * 100.0 if _stat["view_attempted"] else 0.0
        logger.info(
            "[/camera][stat] device_id=%s 最近 %.1fs: 收 %d 帧(%.1f fps) "
            "检出 %d 帧(%.1f fps) 推理均时 %.1fms; /camera_view 投递 %d/%d (丢 %.0f%%)",
            url_device,
            elapsed,
            _stat["frames_in"],
            fps_in,
            _stat["frames_face"],
            fps_face,
            avg_infer,
            _stat["view_sent"],
            _stat["view_attempted"],
            drop_pct,
        )
        _stat.update(
            {
                "t0": time.monotonic(),
                "frames_in": 0,
                "frames_decoded": 0,
                "frames_face": 0,
                "view_attempted": 0,
                "view_sent": 0,
                "infer_ms_total": 0.0,
            }
        )

    async def _stat_loop() -> None:
        try:
            while True:
                await asyncio.sleep(_stat_interval)
                _flush_stat()
        except asyncio.CancelledError:
            pass

    stat_task = asyncio.create_task(_stat_loop())

    async def _on_no_face(infer_ms: float, *, error: Optional[str] = None) -> None:
        if error:
            logger.debug(
                "[/camera] frame=%d device_id=%s no_face error=%s infer_ms=%.1f",
                frame_count,
                url_device,
                error,
                infer_ms,
            )
        _s, _a = await image_broker.publish(url_device, frame_bytes, detected=False)
        _stat["view_sent"] += _s
        _stat["view_attempted"] += _a
        _pb_idle = getattr(asr_chat_hub, "pb_idle_snore", None)
        if _pb_idle is not None:
            _pb_idle.on_camera_gaze_tick(url_device, False)

    try:
        async for message in websocket:
            if isinstance(message, str):
                try:
                    data = json.loads(message)
                except Exception:
                    continue
                if isinstance(data, dict) and data.get("type") == "ping":
                    await _safe_send(websocket, _json_msg({"type": "pong"}))
                continue

            if not isinstance(message, (bytes, bytearray)) or not message:
                continue

            frame_count += 1
            _stat["frames_in"] += 1
            frame_bytes = bytes(message)
            t0 = time.monotonic()
            try:
                detect = await asyncio.to_thread(detector.detect_5pt, frame_bytes)
            except Exception as exc:
                logger.warning(
                    "[/camera] 解码/推理失败 device_id=%s frame=%d: %s",
                    url_device,
                    frame_count,
                    exc,
                )
                infer_ms = (time.monotonic() - t0) * 1000.0
                await _on_no_face(infer_ms, error="decode_or_infer_failed")
                continue

            infer_ms = (time.monotonic() - t0) * 1000.0
            _stat["frames_decoded"] += 1
            _stat["infer_ms_total"] += infer_ms

            if not detect or not detect.get("points"):
                await _on_no_face(infer_ms)
                continue

            detected_count += 1
            _stat["frames_face"] += 1
            analysis = analyze_face_detection(detect)
            _pb_idle = getattr(asr_chat_hub, "pb_idle_snore", None)
            if _pb_idle is not None:
                _pb_idle.on_camera_gaze_tick(url_device, analysis["is_frontal"])

            await dp_broker.broadcast_to_device(url_device, build_face_pos_payload(url_device, analysis))
            _s, _a = await image_broker.publish(
                url_device,
                frame_bytes,
                detected=True,
                landmarks=analysis["landmarks"],
                frame_w=analysis["image_w"],
                frame_h=analysis["image_h"],
                yaw_deg=analysis["yaw_deg"],
                pitch_deg=analysis["pitch_deg"],
                iris_offsets=analysis["iris_offsets"],
                frontal_score=analysis["frontal_score"],
                is_frontal=analysis["is_frontal"],
                points=analysis["points"],
            )
            _stat["view_sent"] += _s
            _stat["view_attempted"] += _a

            face_info = build_face_info_message(
                url_device, analysis, send_face_info=send_face_info_to_asr_chat
            )
            if face_info is not None:
                await asr_chat_hub.send(url_device, face_info)
    except ConnectionClosed as closed:
        logger.info(
            "/camera WebSocket 已关闭: device_id=%s frames=%d detected=%d %s",
            url_device,
            frame_count,
            detected_count,
            closed,
        )
    finally:
        stat_task.cancel()
        try:
            await stat_task
        except (asyncio.CancelledError, Exception):
            pass
        if _stat["frames_in"] or _stat["view_attempted"] or _stat["frames_decoded"]:
            _flush_stat()
        await registry.disconnect(websocket)
        await asyncio.to_thread(detector.close)


async def handle_camera_view(
    websocket,
    image_broker: CameraImageBroker,
) -> None:
    req_path = _ws_request_path(websocket)
    _, query = _split_path(req_path)
    qargs = _parse_query(query)
    url_device = _extract_device_id(qargs)
    peer = _peer_str(websocket)
    logger.info("[/camera_view] 订阅者接入 peer=%s device_filter=%s", peer, url_device)

    await _safe_send(
        websocket,
        _json_msg(
            {
                "type": "ready",
                "channel": "camera_view",
                "device_filter": url_device,
                "expects": "binary JPEG frames preceded by camera_frame meta",
            }
        ),
    )

    await image_broker.add_subscriber(websocket, url_device)
    try:
        async for msg in websocket:
            if isinstance(msg, (bytes, bytearray)):
                continue
            try:
                d = json.loads(msg)
            except Exception:
                continue
            if isinstance(d, dict) and d.get("type") == "ping":
                await _safe_send(websocket, _json_msg({"type": "pong"}))
    except ConnectionClosed as closed:
        logger.info(
            "/camera_view WebSocket 已关闭 peer=%s device_filter=%s: %s",
            peer,
            url_device,
            closed,
        )
    finally:
        await image_broker.remove_subscriber(websocket)
