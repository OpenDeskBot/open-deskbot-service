from __future__ import annotations

import logging

from deskbot_server.constants import CAMERA_VIEW_PATH, DEVICE_PIPELINE_PATH
from deskbot_server.pipeline.audio import AudioConfig
from deskbot_server.pipeline.pipeline import BotPipeline
from deskbot_server.util import (
    _extract_device_id,
    _parse_query,
    _peer_str,
    _split_path,
    _ws_request_path,
)
from deskbot_server.vision.geometry import CAMERA_PATH
from deskbot_server.vision.undistort import CameraFaceRuntime
from deskbot_server.ws.asr_chat import handle_asr_chat
from deskbot_server.ws.asr_chat_hub import AsrChatHub
from deskbot_server.application.camera_broker import CameraImageBroker
from deskbot_server.ws.camera import handle_camera, handle_camera_view
from deskbot_server.ws.device_pipeline import DevicePipelineBroker, handle_device_pipeline
from deskbot_server.ws.registry import DeviceRegistry

logger = logging.getLogger("deskbot-server")

async def handle_client(
    websocket,
    pipeline: BotPipeline,
    audio_cfg: AudioConfig,
    ws_path: str,
    device_pipeline_broker: DevicePipelineBroker,
    registry: DeviceRegistry,
    asr_chat_hub: AsrChatHub,
    camera_image_broker: "CameraImageBroker",
    camera_face_runtime: CameraFaceRuntime,
    *,
    send_face_info_to_asr_chat: bool = False,
):
    raw_path = _ws_request_path(websocket)
    path_only, query = _split_path(raw_path)
    peer = _peer_str(websocket)
    logger.info("[WS] 收到连接 peer=%s path=%s", peer, raw_path)

    if path_only == CAMERA_PATH:
        await handle_camera(
            websocket,
            registry,
            asr_chat_hub,
            device_pipeline_broker,
            camera_image_broker,
            camera_face_runtime,
            send_face_info_to_asr_chat=send_face_info_to_asr_chat,
        )
        return

    if path_only == CAMERA_VIEW_PATH:
        await handle_camera_view(websocket, camera_image_broker)
        return

    if path_only == DEVICE_PIPELINE_PATH:
        await handle_device_pipeline(websocket, device_pipeline_broker, registry)
        return

    if path_only and path_only != ws_path:
        logger.warning(
            "[WS] 拒绝非法路径 peer=%s path=%s "
            "(期望 asr_chat=%s, camera=%s, camera_view=%s, device_pipeline=%s)",
            peer,
            raw_path,
            ws_path,
            CAMERA_PATH,
            CAMERA_VIEW_PATH,
            DEVICE_PIPELINE_PATH,
        )
        await websocket.close(code=1008, reason=f"unsupported path: {raw_path}")
        return

    qargs = _parse_query(query)
    device_id = _extract_device_id(qargs)

    await handle_asr_chat(
        websocket,
        pipeline,
        audio_cfg,
        device_id,
        registry,
        device_pipeline_broker,
        asr_chat_hub,
    )
