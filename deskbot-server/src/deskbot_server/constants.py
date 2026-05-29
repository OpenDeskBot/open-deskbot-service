"""共享常量。"""
from __future__ import annotations

import os

from deskbot_server.paths import DATA_DIR, MODELS_DIR

LOG_FILE = os.environ.get("DESKBOT_SERVER_LOG_FILE", "app.log")
SAFE_SEND_TIMEOUT = float(os.environ.get("WS_SEND_TIMEOUT_SEC", "10.0"))

SERVO_CFG_FILE = str(DATA_DIR / "servo.json")
CAMERA_FACE_CFG_FILE = str(DATA_DIR / "camera_face.json")
FACE_PROFILES_FILE = str(DATA_DIR / "face_profiles.json")
FACE_MOUTH_BY_PHONEME_FILE = str(DATA_DIR / "face_mouth_by_phoneme.json")
FACE_EXPR_SCENES_FILE = str(DATA_DIR / "face_expr_scenes.json")
SCENE_PLAYBOOKS_FILE = str(DATA_DIR / "scene_playbooks.json")
USER_MEMORY_FILE = str(DATA_DIR / "user_memory.json")

ASR_CHAT_SUPPRESS_DEVICE_STAGES = frozenset({
    "asr_start",
    "asr_text",
    "asr_empty",
    "asr_rejected",
    "llm_text",
})

CAMERA_PATH = "/camera"
CAMERA_VIEW_PATH = "/camera_view"
DEVICE_PIPELINE_PATH = "/device_pipeline"
DEVICE_PIPELINE_MAX_EVENTS = 100

CAMERA_MODEL_DEFAULT_PATH = str(MODELS_DIR / "mediapipe" / "face_landmarker.task")
