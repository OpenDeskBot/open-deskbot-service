"""服务端摄像头人脸舵机跟随（不依赖调试页）。"""
from __future__ import annotations

import math
import time
import uuid
from typing import Any, Optional

from deskbot_server.camera_face_tune import (
    get_frontal_angle_threshold_deg,
    get_gaze_pitch_threshold_deg,
    get_gaze_yaw_threshold_deg,
    get_horizontal_fov_deg,
)
from deskbot_server.debug_prefs_store import get_camera_servo_auto_mode
from deskbot_server.pb.scenes import _pb_scene_entry_by_name, _prepare_pb_scene_chain_frames
from deskbot_server.pb.shapes import PB_ACTION_REPLACE, PB_LEVEL_DEBUG
from deskbot_server.vision.geometry import FRONTAL_YAW_THRESHOLD_DEG
from deskbot_server.ws.asr_chat_hub import AsrChatHub

_SERVO_CENTER_X = 90
_SERVO_CENTER_Y = 90
_MAP_YAW_SIGN = -1
_MAP_PITCH_SIGN = 1
_FOLLOW_PITCH_OFFSET = -15
_GAZE_PITCH_OFFSET = -15
_GAZE_SMILE_SCENE = "happy_smile"
_GAZE_SMILE_MIN_MS = 10_000
_SERVO_MS = 500

_device_state: dict[str, dict[str, Any]] = {}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _screen_angles_from_analysis(analysis: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    landmarks = analysis.get("landmarks") or []
    nose = next(
        (p for p in landmarks if isinstance(p, dict) and p.get("name") == "nose"),
        None,
    )
    w = int(analysis.get("image_w") or 0)
    h = int(analysis.get("image_h") or 0)
    if not nose or w <= 0 or h <= 0:
        return None, None
    try:
        nx = float(nose["x"])
        ny = float(nose["y"])
    except (TypeError, ValueError, KeyError):
        return None, None

    hfov_rad = math.radians(get_horizontal_fov_deg())
    vfov_rad = 2 * math.atan(math.tan(hfov_rad / 2) * (h / w))
    dx = nx - w / 2
    dy = ny - h / 2
    r2d = 180 / math.pi
    screen_yaw = math.atan((2 * dx * math.tan(hfov_rad / 2)) / w) * r2d
    screen_pitch = math.atan((2 * dy * math.tan(vfov_rad / 2)) / h) * r2d
    return round(screen_yaw, 1), round(screen_pitch, 1)


def _may_send_servo(mode: str, analysis: dict[str, Any]) -> bool:
    if not analysis.get("points"):
        return False
    if mode == "follow":
        return True
    if mode == "follow_frontal":
        if isinstance(analysis.get("is_frontal_angle"), bool):
            return bool(analysis["is_frontal_angle"])
        fa = analysis.get("frontal_angle_deg")
        if fa is None:
            return False
        try:
            return float(fa) <= get_frontal_angle_threshold_deg(FRONTAL_YAW_THRESHOLD_DEG)
        except (TypeError, ValueError):
            return False
    if mode == "gaze":
        if isinstance(analysis.get("is_looking_at_camera"), bool):
            return bool(analysis["is_looking_at_camera"])
        yaw_thr = get_gaze_yaw_threshold_deg(FRONTAL_YAW_THRESHOLD_DEG)
        pitch_thr = get_gaze_pitch_threshold_deg(FRONTAL_YAW_THRESHOLD_DEG)
        gy = analysis.get("gaze_yaw_deg")
        gp = analysis.get("gaze_pitch_deg")
        if gy is not None:
            try:
                if abs(float(gy)) >= yaw_thr:
                    return False
            except (TypeError, ValueError):
                return False
        if gp is not None:
            try:
                if abs(float(gp)) >= pitch_thr:
                    return False
            except (TypeError, ValueError):
                return False
        return True
    return False


def _pitch_offset_for_mode(mode: str) -> float:
    if mode == "gaze":
        return _GAZE_PITCH_OFFSET
    if mode in ("follow", "follow_frontal"):
        return _FOLLOW_PITCH_OFFSET
    return 0.0


async def camera_servo_follower_tick(
    asr_chat_hub: AsrChatHub,
    device_id: str,
    analysis: dict[str, Any],
) -> None:
    """按 ``debug.camera_servo_auto_mode`` 向设备 /asr_chat 下发绝对舵机定位。"""
    mode = get_camera_servo_auto_mode()
    if mode not in ("follow", "follow_frontal", "gaze"):
        return
    if not device_id or not _may_send_servo(mode, analysis):
        return

    screen_yaw, screen_pitch = _screen_angles_from_analysis(analysis)
    if screen_yaw is None or screen_pitch is None:
        return

    dead = 0.5 if mode == "gaze" else 0.15
    if abs(screen_yaw) <= dead and abs(screen_pitch) <= dead:
        return

    pitch_off = _pitch_offset_for_mode(mode)
    ix = int(round(_clamp(_SERVO_CENTER_X + _MAP_YAW_SIGN * screen_yaw, 0, 180)))
    iy = int(round(_clamp(_SERVO_CENTER_Y + _MAP_PITCH_SIGN * screen_pitch + pitch_off, 0, 180)))

    st = _device_state.setdefault(device_id, {})
    now_ms = time.monotonic() * 1000.0
    min_gap = 350.0 if mode == "gaze" else 400.0
    last_send = float(st.get("last_send_ms") or 0.0)
    if now_ms - last_send < min_gap:
        return
    if (
        st.get("last_ix") == ix
        and st.get("last_iy") == iy
        and (now_ms - last_send) < 1600.0
    ):
        return

    req_id = uuid.uuid4().hex[:16]
    payload: dict[str, Any] = {
        "type": "pb_single",
        "req": req_id,
        "idx": 0,
        "chunk_ms": _SERVO_MS,
        "pb_ver": 2,
        "action": PB_ACTION_REPLACE,
        "level": PB_LEVEL_DEBUG,
        "servo": {
            "xm": 0,
            "ym": 0,
            "x": ix,
            "y": iy,
            "ms": _SERVO_MS,
        },
    }

    tail_frames: Optional[list[dict]] = None
    can_bundle_smile = (
        mode == "gaze"
        and _pb_scene_entry_by_name({}, _GAZE_SMILE_SCENE)
        and (now_ms - float(st.get("last_smile_ms") or 0.0) >= _GAZE_SMILE_MIN_MS)
    )
    if can_bundle_smile:
        scene_req = uuid.uuid4().hex[:16]
        tail_frames = _prepare_pb_scene_chain_frames(
            _GAZE_SMILE_SCENE, runtime_req=scene_req
        )
        if not tail_frames:
            tail_frames = None

    if tail_frames:
        delivered = await asr_chat_hub.send_pb_single_then_chain_ordered(
            device_id, payload, tail_frames
        )
    else:
        delivered = await asr_chat_hub.send(device_id, payload)

    if delivered <= 0:
        return

    st["last_send_ms"] = now_ms
    st["last_ix"] = ix
    st["last_iy"] = iy
    if can_bundle_smile and tail_frames:
        st["last_smile_ms"] = now_ms
