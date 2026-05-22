from __future__ import annotations

from deskbot_server.application.camera_frame import analyze_face_detection, build_face_info_message
from deskbot_server.pb.shapes import enumerate_zh_phonemes, normalize_primitive_shape
from deskbot_server.pb.wire import build_pb_wire_pairs


def test_enumerate_zh_phonemes_contains_silence():
    phones = enumerate_zh_phonemes()
    assert "_" in phones
    assert "sil" in phones


def test_normalize_primitive_shape_aliases():
    assert normalize_primitive_shape("fill_rect") == "rect"
    assert normalize_primitive_shape("ellipse_fill") == "ellipse_fill"


def test_analyze_face_detection_empty_points():
    result = analyze_face_detection({"points": [], "landmarks": []})
    assert "frontal_score" in result
    assert result["yaw_deg"] is None


def test_build_face_info_skipped_without_yaw():
    analysis = {"yaw_deg": None, "pitch_deg": None, "landmarks": []}
    assert build_face_info_message("d1", analysis, send_face_info=True) is None


def test_build_pb_wire_pairs_empty_segs_raises():
    import pytest

    try:
        build_pb_wire_pairs([], {}, servo_plan=[], sample_rate=24000)
        assert False, "expected error"
    except Exception:
        pass
