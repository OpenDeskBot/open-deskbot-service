"""MediaPipe 人脸检测（/domain 层，无 WebSocket 依赖）。"""

from __future__ import annotations

import io
import logging
import os
from typing import Optional

import numpy as np

from deskbot_server.constants import CAMERA_MODEL_DEFAULT_PATH
from deskbot_server.vision.geometry import (
    FACE_FRAME_HEIGHT,
    FACE_FRAME_WIDTH,
    FACE_KEYPOINT_NAMES,
    MP_FACE_5PT_INDICES,
    MP_FACE_DETAIL_INDICES,
    MP_FACE_DETAIL_NAMES,
)
from deskbot_server.vision.undistort import CameraUndistorter

logger = logging.getLogger("deskbot-server")


def resolve_camera_model_path() -> str:
    env = os.environ.get("CAMERA_FACE_LANDMARKER_PATH")
    if env and os.path.isfile(env):
        return env
    return CAMERA_MODEL_DEFAULT_PATH


class CameraFaceDetector:
    """单条 `/camera` 连接独占一个 MediaPipe FaceLandmarker。"""

    def __init__(
        self,
        *,
        num_faces: int = 1,
        model_path: Optional[str] = None,
        undistorter: Optional[CameraUndistorter] = None,
        min_face_detection_confidence: float = 0.5,
        min_face_presence_confidence: float = 0.5,
    ) -> None:
        import mediapipe as mp  # type: ignore
        from mediapipe.tasks import python as mp_py  # type: ignore
        from mediapipe.tasks.python import vision as mp_vision  # type: ignore

        self._mp = mp
        self._mp_vision = mp_vision
        self._undistorter = undistorter

        path = model_path or resolve_camera_model_path()
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"找不到 face_landmarker.task（{path}）。请下载到 "
                "deskbot-server/models/mediapipe/face_landmarker.task，"
                "或设置 CAMERA_FACE_LANDMARKER_PATH。"
            )

        options = mp_vision.FaceLandmarkerOptions(
            base_options=mp_py.BaseOptions(model_asset_path=path),
            running_mode=mp_vision.RunningMode.IMAGE,
            num_faces=num_faces,
            output_face_blendshapes=False,
            output_facial_transformation_matrixes=False,
            min_face_detection_confidence=min_face_detection_confidence,
            min_face_presence_confidence=min_face_presence_confidence,
        )
        self._landmarker = mp_vision.FaceLandmarker.create_from_options(options)
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._landmarker.close()
        except Exception:
            pass
        self._closed = True

    @staticmethod
    def _decode_jpeg_to_rgb(buf: bytes):
        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(buf)) as im:
            im = im.convert("RGB")
            return np.array(im, dtype=np.uint8)

    def detect_5pt(self, image_bytes: bytes) -> Optional[dict]:
        if self._closed:
            return None
        rgb = self._decode_jpeg_to_rgb(image_bytes)
        if self._undistorter is not None:
            rgb = self._undistorter.apply(rgb)
        h, w = rgb.shape[:2]
        mp_image = self._mp.Image(
            image_format=self._mp.ImageFormat.SRGB,
            data=rgb,
        )
        result = self._landmarker.detect(mp_image)
        face_landmarks = getattr(result, "face_landmarks", None)
        if not face_landmarks:
            return None
        face = face_landmarks[0]
        sx = float(FACE_FRAME_WIDTH)
        sy = float(FACE_FRAME_HEIGHT)

        def _scaled(nx: float, ny: float) -> tuple:
            x = max(0.0, min(sx, nx * sx))
            y = max(0.0, min(sy, ny * sy))
            return round(x, 2), round(y, 2)

        points: list = []
        for name in FACE_KEYPOINT_NAMES:
            idxs = MP_FACE_5PT_INDICES.get(name)
            if not idxs:
                continue
            try:
                xs = [face[i].x for i in idxs]
                ys = [face[i].y for i in idxs]
            except IndexError:
                continue
            nx = sum(xs) / len(xs)
            ny = sum(ys) / len(ys)
            x, y = _scaled(nx, ny)
            points.append({"name": name, "x": x, "y": y})
        if not points:
            return None

        landmarks: list = []
        for name in MP_FACE_DETAIL_NAMES:
            idx = MP_FACE_DETAIL_INDICES.get(name)
            if idx is None:
                continue
            try:
                lm = face[idx]
            except IndexError:
                continue
            nx = max(0.0, min(1.0, float(lm.x)))
            ny = max(0.0, min(1.0, float(lm.y)))
            landmarks.append({
                "name": name,
                "x": round(nx * float(w), 2),
                "y": round(ny * float(h), 2),
            })

        return {
            "points": points,
            "landmarks": landmarks,
            "image_w": int(w),
            "image_h": int(h),
        }
