"""LLM 输出解析等纯文本工具，独立于 funasr/torch 等重依赖，
供 ``deskbot_server`` 主服务与 ``web/app.py`` 共享使用。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from deskbot_server.constants import FACE_EXPR_SCENES_FILE, SERVO_CFG_FILE
from deskbot_server.face_expr_scenes_store import load_face_expr_scenes_file
from deskbot_server.servo_config_store import load_servo_cfg_file

_LLM_APPENDIX_CACHE: dict[str, tuple[float, str]] = {}


def _face_expr_scene_entries() -> list[dict[str, Any]]:
    try:
        rows = load_face_expr_scenes_file(seed_if_missing=False)
    except (OSError, ValueError, json.JSONDecodeError):
        rows = None
    if rows is None:
        try:
            with open(FACE_EXPR_SCENES_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        rows = raw
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        frames = row.get("frames")
        if name and isinstance(frames, list) and frames:
            out.append(row)
    out.sort(key=lambda r: (str(r.get("name") or "").lower(), str(r.get("name") or "")))
    return out


def _cached_appendix(cache_key: str, mtime_path: str, build_fn) -> str:
    global _LLM_APPENDIX_CACHE
    try:
        mtime = os.path.getmtime(mtime_path)
    except OSError:
        return ""
    cached = _LLM_APPENDIX_CACHE.get(cache_key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    text = build_fn()
    _LLM_APPENDIX_CACHE[cache_key] = (mtime, text)
    return text


def llm_pb_moves_prompt_appendix() -> str:
    """供 system prompt 追加：合法 ``moves`` 预设 id、label 与默认时长。"""
    def _build() -> str:
        try:
            cfg = load_servo_cfg_file()
        except (OSError, ValueError):
            return ""
        if not cfg:
            return ""
        lines: list[str] = []
        for preset in cfg.get("presets") or []:
            if not isinstance(preset, dict):
                continue
            pid = str(preset.get("id") or "").strip()
            label = str(preset.get("label") or "").strip()
            if not pid:
                continue
            default_ms = sum(max(1, int(s.get("ms") or 0)) for s in (preset.get("steps") or []))
            lines.append(f"      - {pid}: {label or pid}（默认 {default_ms} ms）")
        if not lines:
            return ""
        body = "\n".join(lines)
        return (
            "  - moves: 数组。每项 ``{\"move\": \"预设动作id\", \"ms\": 执行时长}``。"
            "``move`` 须从下列预设中选取；``ms`` 为该动作整体期望时长（毫秒），"
            "服务端会按预设各 step 默认时长比例缩放，**ms 越大越慢、越小越快**。\n"
            f"    可用预设动作：\n{body}\n"
            "    不需要动作时写 []。\n"
        )

    return _cached_appendix("moves", SERVO_CFG_FILE, _build)


def llm_pb_anims_prompt_appendix() -> str:
    """供 system prompt 追加：合法 ``anims`` 场景 name、title 与默认时长。"""
    def _build() -> str:
        rows = _face_expr_scene_entries()
        if not rows:
            return ""
        lines: list[str] = []
        for row in rows:
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            title = str(row.get("title") or name).strip()
            default_ms = sum(max(1, int(fr.get("ms") or 0)) for fr in (row.get("frames") or []))
            lines.append(f"      - {name}: {title}（默认 {default_ms} ms）")
        body = "\n".join(lines)
        return (
            "  - anims: 数组。每项 ``{\"anim\": \"场景name\", \"ms\": 执行时长}``。"
            "``anim`` 须与 ``data/face_expr_scenes.json`` 中 ``name`` 一致；"
            "``ms`` 为该段表情动画整体期望时长，服务端按各帧默认时长比例缩放，"
            "**ms 越大越慢、越小越快**。未知名会回退 ``default``，仍无则跳过。\n"
            f"    可用表情动画：\n{body}\n"
            "    不需要时写 []。有 TTS 音素时分片口型仍由音素驱动，其它图层用所选 anim。\n"
        )

    return _cached_appendix("anims", FACE_EXPR_SCENES_FILE, _build)


def llm_pb_plan_prompt_appendix() -> str:
    """moves + anims 附录合并（替代旧 ``scenes`` / ``servo`` 直写说明）。"""
    parts = [llm_pb_moves_prompt_appendix(), llm_pb_anims_prompt_appendix()]
    return "".join(p for p in parts if p)


def llm_pb_scenes_prompt_appendix() -> str:
    """兼容旧调用名；返回 moves/anims 计划附录。"""
    return llm_pb_plan_prompt_appendix()


def llm_memory_prompt_appendix(device_id: Optional[str] = None) -> str:
    """长期记忆列表，注入 system prompt。"""
    from deskbot_server.memory_store import list_memory_for_device

    rows = list_memory_for_device(device_id, limit=30)
    if not rows:
        return "长期记忆：暂无。"
    lines: list[str] = []
    for e in rows:
        eid = str(e.get("id") or "")
        text = str(e.get("text") or "").strip()
        if text:
            lines.append(f"  - [{eid}] {text}")
    return "长期记忆（可用 memory_delete 删除，id 见方括号）：\n" + "\n".join(lines)


def llm_tools_prompt_appendix() -> str:
    """LLM 可返回的 tools 数组说明。"""
    return (
        "可用工具（可选 ``tools`` 数组，服务端立即执行；不需要时写 []）：\n"
        "  - register_face: {\"tool\":\"register_face\",\"name\":\"姓名\",\"face_id\":1}\n"
        "    将当前画面 face_id 的人脸注册/更新到档案（embedding 512 维）；"
        "仅一张脸时可省略 face_id；多人须指定 face_id 或先向用户澄清。\n"
        "  - set_camera_follow: {\"tool\":\"set_camera_follow\",\"mode\":\"follow|follow_frontal|gaze|off\"}\n"
        "    开启/关闭人脸舵机跟随（follow=跟随人脸，follow_frontal=跟随正脸，gaze=注视感知，off=关闭）。\n"
        "  - memory_add: {\"tool\":\"memory_add\",\"text\":\"要记住的内容\"}\n"
        "  - memory_delete: {\"tool\":\"memory_delete\",\"id\":\"记忆id\"}\n"
    )


def llm_face_context_prompt_appendix(device_id: Optional[str] = None) -> str:
    """人脸场景 + 记忆 + 工具说明。"""
    from deskbot_server.llm.face_scene import llm_face_scene_prompt_appendix

    parts: list[str] = []
    dev = str(device_id or "").strip()
    if dev:
        parts.append(llm_face_scene_prompt_appendix(dev))
    parts.append(llm_memory_prompt_appendix(device_id))
    parts.append(llm_tools_prompt_appendix())
    return "\n\n".join(p for p in parts if p)


def llm_recognized_faces_prompt_appendix(device_id: Optional[str] = None) -> str:
    """兼容旧调用名。"""
    return llm_face_context_prompt_appendix(device_id)


_LLM_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)


def _parse_need_reply_value(v: Any) -> bool:
    """JSON 里 ``need_reply`` 的宽松解析；缺省由调用方视为需要回复。"""
    if v is False or v == 0:
        return False
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("false", "0", "no", "否", "不需要", "不用", "none"):
            return False
        if s in ("true", "1", "yes", "是", "需要"):
            return True
        return bool(s)
    return bool(v)


def _parsed_json_need_reply(parsed: dict) -> bool:
    if "need_reply" not in parsed:
        return True
    return _parse_need_reply_value(parsed.get("need_reply"))


def parse_servo_plan_item(obj: Any) -> Optional[dict[str, Any]]:
    """解析 ``servo`` 数组单条：延时 ``hold_ms`` / ``hold``+``ms``，或标准 ``xm``…``ms``。"""
    if not isinstance(obj, dict):
        return None
    if obj.get("hold") is True or obj.get("hold") == 1:
        try:
            h = int(obj.get("ms", obj.get("hold_ms", 0)))
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            return {"_hold_ms": min(h, 30_000)}
    if "hold_ms" in obj:
        try:
            h = int(obj["hold_ms"])
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            return {"_hold_ms": min(h, 30_000)}
    return normalize_pb_servo_dict(obj)


def normalize_pb_servo_dict(obj: Any) -> Optional[dict[str, int]]:
    """校验并归一化单条 pb 舵机指令（``xm``/``ym``/``x``/``y``/``ms``），非法则 ``None``。"""
    if not isinstance(obj, dict):
        return None
    try:
        xm = int(obj.get("xm", 0))
        ym = int(obj.get("ym", 0))
        x = int(obj.get("x", 0))
        y = int(obj.get("y", 0))
        ms = int(obj.get("ms", 0))
    except (TypeError, ValueError):
        return None
    if xm not in (0, 1, 2) or ym not in (0, 1, 2):
        return None
    if ms <= 0:
        return None
    return {"xm": xm, "ym": ym, "x": x, "y": y, "ms": ms}


def _parse_llm_move_items(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        move_id = str(item.get("move") or "").strip()
        try:
            ms = int(item.get("ms", 0))
        except (TypeError, ValueError):
            continue
        if not move_id or ms <= 0:
            continue
        out.append({"move": move_id, "ms": ms})
    return out


def _parse_llm_anim_items(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        anim_name = str(item.get("anim") or "").strip()
        try:
            ms = int(item.get("ms", 0))
        except (TypeError, ValueError):
            continue
        if not anim_name or ms <= 0:
            continue
        out.append({"anim": anim_name, "ms": ms})
    return out


def _parse_llm_tool_items(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or item.get("name") or "").strip()
        if not tool:
            continue
        row = dict(item)
        row["tool"] = tool
        out.append(row)
    return out


def parse_llm_reply(raw: str) -> dict:
    """把 LLM 输出尝试解析为约定 JSON。

    格式 ``{"need_reply", "tts", "moves", "anims", "tools": [...]}``；
    仍兼容旧版 ``servo`` / ``scenes`` 与 ``reply`` 字段。

    失败时把整段文本当作 ``reply`` 返回，**不抛异常**。
    """
    text = (raw or "").strip()
    parsed: Optional[dict] = None

    candidates = []
    if text:
        candidates.append(text)
        m = _LLM_JSON_FENCE_RE.search(text)
        if m:
            candidates.append(m.group(1))
        try:
            i = text.index("{")
            j = text.rindex("}")
            if j > i:
                candidates.append(text[i : j + 1])
        except ValueError:
            pass

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict):
            parsed = obj
            break

    servo_out: list[Any] = []
    moves_out: list[dict[str, Any]] = []
    anims_out: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        raw_servo = parsed.get("servo")
        if isinstance(raw_servo, dict):
            raw_servo = [raw_servo]
        if isinstance(raw_servo, (list, tuple)):
            for item in raw_servo:
                ent = parse_servo_plan_item(item)
                if ent:
                    servo_out.append(ent)
        moves_out = _parse_llm_move_items(parsed.get("moves"))
        anims_out = _parse_llm_anim_items(parsed.get("anims"))
        tools_out = _parse_llm_tool_items(parsed.get("tools"))
        reply_tts = parsed.get("tts")
        reply_legacy = parsed.get("reply")
        reply: str
        if isinstance(reply_tts, str) and reply_tts.strip():
            reply = reply_tts.strip()
        elif isinstance(reply_legacy, str) and reply_legacy.strip():
            reply = reply_legacy.strip()
        else:
            reply = text
        scenes_out: list[str] = []
        raw_scenes = parsed.get("scenes")
        if isinstance(raw_scenes, str):
            raw_scenes = [raw_scenes]
        if isinstance(raw_scenes, (list, tuple)):
            for x in raw_scenes:
                if isinstance(x, str):
                    v = x.strip()
                    if v:
                        scenes_out.append(v)
        return {
            "reply": reply,
            "moves": moves_out,
            "anims": anims_out,
            "tools": tools_out,
            "scenes": scenes_out,
            "servo": servo_out,
            "need_reply": _parsed_json_need_reply(parsed),
            "json_ok": True,
            "raw": text,
        }

    return {
        "reply": text,
        "moves": [],
        "anims": [],
        "tools": [],
        "scenes": [],
        "servo": [],
        "need_reply": True,
        "json_ok": False,
        "raw": text,
    }


__all__ = [
    "llm_face_context_prompt_appendix",
    "llm_memory_prompt_appendix",
    "llm_pb_anims_prompt_appendix",
    "llm_pb_moves_prompt_appendix",
    "llm_pb_plan_prompt_appendix",
    "llm_pb_scenes_prompt_appendix",
    "llm_recognized_faces_prompt_appendix",
    "llm_tools_prompt_appendix",
    "normalize_pb_servo_dict",
    "parse_llm_reply",
    "parse_servo_plan_item",
]
