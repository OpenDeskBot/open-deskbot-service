"""摄像头帧处理：几何量计算与 face_info 组装（无 WebSocket）。"""

from __future__ import annotations

import time
from typing import Any, Optional

from deskbot_server.vision.geometry import (
    FACE_FRAME_HEIGHT,
    FACE_FRAME_WIDTH,
    FRONTAL_THRESHOLD,
    FRONTAL_YAW_THRESHOLD_DEG,
    compute_eye_iris_offsets,
    compute_face_pitch_deg,
    compute_face_yaw_deg,
    compute_frontal_score,
)


def analyze_face_detection(detect: dict) -> dict[str, Any]:
    """从 ``CameraFaceDetector.detect_5pt`` 结果计算面向角、正脸分等。"""
    points = detect.get("points") or []
    landmarks = detect.get("landmarks") or []
    frontal_score = compute_frontal_score(points)
    is_frontal = frontal_score >= FRONTAL_THRESHOLD
    yaw_deg = compute_face_yaw_deg(landmarks)
    pitch_deg = compute_face_pitch_deg(landmarks)
    iris_offsets = compute_eye_iris_offsets(landmarks)
    return {
        "points": points,
        "landmarks": landmarks,
        "frontal_score": frontal_score,
        "is_frontal": is_frontal,
        "yaw_deg": yaw_deg,
        "pitch_deg": pitch_deg,
        "iris_offsets": iris_offsets,
        "image_w": int(detect.get("image_w") or 0) or FACE_FRAME_WIDTH,
        "image_h": int(detect.get("image_h") or 0) or FACE_FRAME_HEIGHT,
    }


def build_face_pos_payload(device_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
    now = time.time()
    mono = time.monotonic()
    return {
        "type": "face_pos",
        "device_id": device_id,
        "points": analysis["points"],
        "width": FACE_FRAME_WIDTH,
        "height": FACE_FRAME_HEIGHT,
        "frontal_score": analysis["frontal_score"],
        "is_frontal": analysis["is_frontal"],
        "ts": now,
        "t_mono": mono,
        "source": "camera",
        "image_w": analysis["image_w"],
        "image_h": analysis["image_h"],
    }


def build_face_info_message(
    device_id: str,
    analysis: dict[str, Any],
    *,
    send_face_info: bool,
) -> Optional[dict[str, Any]]:
    if not send_face_info:
        return None
    yaw_deg = analysis.get("yaw_deg")
    if yaw_deg is None:
        return None
    pitch_deg = analysis.get("pitch_deg")
    is_frontal_dir = (
        abs(yaw_deg) < FRONTAL_YAW_THRESHOLD_DEG
        and (pitch_deg is None or abs(pitch_deg) < FRONTAL_YAW_THRESHOLD_DEG)
    )
    now = time.time()
    mono = time.monotonic()
    face_info: dict[str, Any] = {
        "type": "face_info",
        "device_id": device_id,
        "action": "opportunistic",
        "yaw_deg": yaw_deg,
        "is_frontal": is_frontal_dir,
        "ts": now,
        "t_mono": mono,
    }
    if pitch_deg is not None:
        face_info["pitch_deg"] = pitch_deg
    landmarks = analysis.get("landmarks") or []
    nose_pt = next(
        (p for p in landmarks if isinstance(p, dict) and p.get("name") == "nose"),
        None,
    )
    if nose_pt is not None:
        try:
            face_info["nose"] = {
                "x": round(float(nose_pt["x"]), 2),
                "y": round(float(nose_pt["y"]), 2),
            }
            face_info["frame_w"] = analysis["image_w"]
            face_info["frame_h"] = analysis["image_h"]
        except (TypeError, ValueError, KeyError):
            pass
    return face_info
