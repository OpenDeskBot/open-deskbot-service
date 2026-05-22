"""共享常量。"""
from __future__ import annotations

import os

from deskbot_server.paths import DATA_DIR, MODELS_DIR

LOG_FILE = os.environ.get("DESKBOT_SERVER_LOG_FILE", "app.log")
SAFE_SEND_TIMEOUT = float(os.environ.get("WS_SEND_TIMEOUT_SEC", "10.0"))

PB_SCENES_FILE = str(DATA_DIR / "pb_scenes_idle_sleep_guard.json")

ASR_CHAT_SUPPRESS_DEVICE_STAGES = frozenset({
    "asr_start",
    "asr_text",
    "asr_empty",
    "asr_rejected",
    "llm_text",
    "actions",
})

CAMERA_PATH = "/camera"
CAMERA_VIEW_PATH = "/camera_view"
DEVICE_PIPELINE_PATH = "/device_pipeline"
DEVICE_PIPELINE_MAX_EVENTS = 100

CAMERA_MODEL_DEFAULT_PATH = str(MODELS_DIR / "mediapipe" / "face_landmarker.task")
