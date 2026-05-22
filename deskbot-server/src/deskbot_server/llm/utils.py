"""LLM 输出解析等纯文本工具，独立于 funasr/torch 等重依赖，
供 ``deskbot_server`` 主服务与 ``web/app.py`` 共享使用。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from deskbot_server.paths import DATA_DIR

_PB_SCENES_JSON = str(DATA_DIR / "pb_scenes_idle_sleep_guard.json")
_LLM_SCENES_APPENDIX_CACHE: Optional[tuple[float, str]] = None


def _pb_scene_keys_from_doc(doc: dict) -> list[str]:
    sc = doc.get("scenes")
    if not isinstance(sc, dict):
        return []
    out: list[str] = []
    for k, v in sc.items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(v.get("frames"), list) and len(v["frames"]) > 0:
            out.append(k.strip())
    out.sort(key=lambda s: (s.lower(), s))
    return out


def llm_pb_scenes_prompt_appendix() -> str:
    """供 system prompt 追加：合法 ``scenes`` id 列表与窗帘语义（与 ``pb_scenes_idle_sleep_guard.json`` 同步）。"""
    global _LLM_SCENES_APPENDIX_CACHE
    try:
        mtime = os.path.getmtime(_PB_SCENES_JSON)
    except OSError:
        return ""
    if _LLM_SCENES_APPENDIX_CACHE is not None and _LLM_SCENES_APPENDIX_CACHE[0] == mtime:
        return _LLM_SCENES_APPENDIX_CACHE[1]
    try:
        with open(_PB_SCENES_JSON, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(doc, dict):
        return ""
    keys = _pb_scene_keys_from_doc(doc)
    if not keys:
        return ""
    names = ", ".join(keys)
    text = (
        "    - scenes: 字符串数组。在语音 TTS 的 pb 链（口型+PCM）**全部下发完成后**，"
        "服务端会按数组顺序追加下发 ``data/pb_scenes_idle_sleep_guard.json`` 里各场景的 pb 帧链；"
        f"合法场景 id（须完全一致）为：{names}。不需要时写 []。\n"
        "    窗帘：用户明确要**关上窗帘**时 ``scenes`` 须含 ``curtain_close``；要**打开窗帘**时须含 ``curtain_open``。"
        "其它情绪/待机画面从上述 id 中按需选用。可与 ``action`` 里的智能家居字符串同时使用。\n"
    )
    _LLM_SCENES_APPENDIX_CACHE = (mtime, text)
    return text


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
        # ms 无效时若同条还带舵机字段则按位移解析
    if "hold_ms" in obj:
        try:
            h = int(obj["hold_ms"])
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            return {"_hold_ms": min(h, 30_000)}
        # hold_ms 为 0 或无效时，若同条还带舵机字段则仍按位移解析
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


def parse_llm_reply(raw: str) -> dict:
    """把 LLM 输出尝试解析为约定 JSON。

    优先识别 ``{"tts": str, "servo": [...], "scenes": [...]}``；兼容旧版
    ``{"reply": str, "action"|"actions": [...]}``。

    失败时把整段文本当作 ``reply``、``actions=[]``、``servo=[]``、``need_reply=True`` 返回，**不抛异常**。

    返回字段：

      - ``reply``  : 用于 TTS 的文本（``tts`` 优先，其次 ``reply``）
      - ``actions``: 动作字符串列表（已去重保序，仅保留非空字符串）
      - ``scenes`` : 场景 id 字符串列表（顺序保留；与 ``pb_scenes_idle_sleep_guard.json`` 中 ``scenes`` 的 key 对应）
      - ``servo``  : 舵机计划列表；每项为 ``normalize_pb_servo_dict`` 结果，或 ``{"_hold_ms": n}``（由 ``hold_ms`` / ``hold``+``ms`` 解析而来）
      - ``need_reply``: 是否走 TTS/pb 与向设备下发语音相关阶段；缺省 ``True``
      - ``json_ok``: 是否成功解析出 JSON
      - ``raw``    : LLM 原始输出（去掉首尾空白）
    """
    text = (raw or "").strip()
    parsed: Optional[dict] = None

    candidates = []
    if text:
        candidates.append(text)
        m = _LLM_JSON_FENCE_RE.search(text)
        if m:
            candidates.append(m.group(1))
        # 退化：找首个 '{' 与最后一个 '}' 之间的内容，覆盖 LLM 在 JSON 前后多
        # 写一两句解释的常见情况
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

    actions: list = []
    servo_out: list[Any] = []
    if isinstance(parsed, dict):
        raw_actions = parsed.get("action")
        if raw_actions is None:
            raw_actions = parsed.get("actions")
        if isinstance(raw_actions, str):
            raw_actions = [raw_actions]
        if isinstance(raw_actions, (list, tuple)):
            seen: set = set()
            for a in raw_actions:
                if not isinstance(a, str):
                    continue
                v = a.strip()
                if not v or v in seen:
                    continue
                seen.add(v)
                actions.append(v)
        raw_servo = parsed.get("servo")
        if isinstance(raw_servo, dict):
            raw_servo = [raw_servo]
        if isinstance(raw_servo, (list, tuple)):
            for item in raw_servo:
                ent = parse_servo_plan_item(item)
                if ent:
                    servo_out.append(ent)
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
            "actions": actions,
            "scenes": scenes_out,
            "servo": servo_out,
            "need_reply": _parsed_json_need_reply(parsed),
            "json_ok": True,
            "raw": text,
        }

    return {
        "reply": text,
        "actions": [],
        "scenes": [],
        "servo": [],
        "need_reply": True,
        "json_ok": False,
        "raw": text,
    }
