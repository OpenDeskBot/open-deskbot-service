"""向后兼容：settings 辅助函数。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from deskbot_server.constants import ASR_CHAT_SUPPRESS_DEVICE_STAGES
from deskbot_server.core.settings import AppSettings

if TYPE_CHECKING:
    from deskbot_server.application.chat_service import ChatService


def _asr_chat_send_stage_to_device(pipeline: ChatService, stage: str) -> bool:
    settings = getattr(pipeline, "settings", None)
    if isinstance(settings, AppSettings):
        return settings.should_send_stage_to_device(stage)
    if getattr(pipeline, "asr_chat_device_pb_only", False):
        return False
    if getattr(pipeline, "asr_chat_minimal_device_downlink", False):
        return stage not in ASR_CHAT_SUPPRESS_DEVICE_STAGES
    return True


def _is_pb_downlink_payload(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    tp = str(payload.get("type") or "").strip()
    return tp in ("pb_start", "pb_chunk", "pb_end", "pb_single", "pb_cancel")


def _read_pb_random_servo_cfg(tts_cfg: dict) -> Optional[dict]:
    settings = AppSettings.from_config({"tts": tts_cfg or {}})
    return settings.pb_random_servo_cfg()


def _read_asr_chat_device_pb_only(config: dict) -> bool:
    return AppSettings.from_config(config).server.asr_chat_device_pb_only


def _read_send_face_info_to_asr_chat(config: dict) -> bool:
    s = AppSettings.from_config(config)
    return s.server.send_face_info_to_asr_chat and not s.server.asr_chat_device_pb_only
