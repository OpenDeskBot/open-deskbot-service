from __future__ import annotations

from deskbot_server.llm.utils import parse_llm_reply
from deskbot_server.pb.llm_plan import (
    expand_llm_anims,
    expand_llm_moves,
    interleave_tts_segs_with_llm_plan,
    merge_llm_plan_anim_rows,
)


def test_parse_llm_reply_moves_anims():
    raw = (
        '{"need_reply": true, "tts": "你好", '
        '"moves": [{"move": "nod_head", "ms": 540}], '
        '"anims": [{"anim": "default", "ms": 1500}]}'
    )
    parsed = parse_llm_reply(raw)
    assert parsed["json_ok"] is True
    assert parsed["reply"] == "你好"
    assert parsed["moves"] == [{"move": "nod_head", "ms": 540}]
    assert parsed["anims"] == [{"anim": "default", "ms": 1500}]


def test_expand_llm_moves_scales_preset_steps():
    steps = expand_llm_moves([{"move": "nod_head", "ms": 1080}])
    assert len(steps) == 3
    assert sum(s["ms"] for s in steps) == 1080


def test_expand_llm_anims_fallback_default():
    frames = expand_llm_anims([{"anim": "__no_such_anim__", "ms": 800}])
    assert frames
    assert sum(f["ms"] for f in frames) == 800
    assert isinstance(frames[0].get("elements"), dict)


def test_interleave_tts_with_llm_plan_parallel():
    segs = [{"phoneme": "n", "ms": 100, "pcm": b"\x00" * 4800}]
    move_steps = [{"xm": 1, "ym": 1, "x": 0, "y": 10, "ms": 200}]
    anim_frames = [{"ms": 150, "elements": {"mouth": [], "eye_l": [], "eye_r": [], "nose": [], "extra": []}}]
    out, servo, anim = interleave_tts_segs_with_llm_plan(segs, move_steps, anim_frames, 24000)
    assert len(out) == 1
    assert out[0]["ms"] == 100
    assert servo[0]["ms"] == 200
    assert anim[0] is not None


def test_merge_llm_plan_anim_rows_keeps_phoneme_mouth():
    segs = [{"phoneme": "a", "ms": 100, "pcm": b"\x00" * 4800}]
    phoneme_rows = [
        {
            "idx": 0,
            "chunk_ms": 100,
            "phoneme": "a",
            "anim": {"elements": {"mouth": [{"shape": "rect", "x": 1, "y": 2, "w": 3, "h": 4}], "eye_l": [], "eye_r": [], "nose": [], "extra": []}},
        }
    ]
    plan_el = {"mouth": [{"shape": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 1}], "eye_l": [{"shape": "circle", "x": 1, "y": 2, "r": 3}], "eye_r": [], "nose": [], "extra": []}
    merged = merge_llm_plan_anim_rows(segs, phoneme_rows, [plan_el])
    mouth = merged[0]["anim"]["elements"]["mouth"]
    assert mouth == phoneme_rows[0]["anim"]["elements"]["mouth"]
    assert merged[0]["anim"]["elements"]["eye_l"] == plan_el["eye_l"]


def test_llm_face_context_prompt_appendix():
    from deskbot_server.face_snapshot_cache import update_device_faces
    from deskbot_server.llm.utils import llm_face_context_prompt_appendix

    dev = "test_device_faces_prompt"
    update_device_faces(
        dev,
        [
            {
                "face_id": 1,
                "person_name": "小明",
                "identity_score": 0.82,
                "person_id": 1,
                "image_w": 320,
                "image_h": 240,
                "landmarks": [{"name": "nose", "x": 200, "y": 140}],
                "points": [],
            },
            {
                "face_id": 2,
                "person_name": "小红",
                "identity_score": 0.91,
                "image_w": 320,
                "image_h": 240,
                "landmarks": [{"name": "nose", "x": 80, "y": 60}],
                "points": [],
            },
        ],
    )
    text = llm_face_context_prompt_appendix(dev)
    assert "face_id=2" in text and "小红" in text
    assert "画面" in text
    assert "register_face" in text
    assert "长期记忆" in text
    empty_dev = llm_face_context_prompt_appendix("")
    assert "register_face" in empty_dev
    assert "face_id=" not in empty_dev


def test_parse_llm_tools():
    raw = '{"tts":"好","tools":[{"tool":"memory_add","text":"喜欢猫"}]}'
    parsed = parse_llm_reply(raw)
    assert parsed["tools"] == [{"tool": "memory_add", "text": "喜欢猫"}]


def test_memory_store_roundtrip(tmp_path, monkeypatch):
    from deskbot_server import memory_store as ms

    mem_file = tmp_path / "user_memory.json"
    monkeypatch.setattr(ms, "USER_MEMORY_FILE", str(mem_file))
    e1 = ms.add_memory("主人喜欢猫", device_id="dev1")
    assert e1["text"] == "主人喜欢猫"
    rows = ms.list_memory_for_device("dev1")
    assert len(rows) == 1
    assert ms.delete_memory(e1["id"])
    assert ms.list_memory_for_device("dev1") == []
