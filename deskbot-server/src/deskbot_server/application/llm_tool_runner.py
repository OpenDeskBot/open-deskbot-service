"""执行 LLM JSON 中的 ``tools`` 指令。"""
from __future__ import annotations

import logging
from typing import Any, Optional

from deskbot_server.application.face_registration import register_face_for_device
from deskbot_server.debug_prefs_store import persist_camera_servo_auto_mode
from deskbot_server.memory_store import add_memory, delete_memory

logger = logging.getLogger("deskbot-server")

_FOLLOW_ALIASES = {
    "": "",
    "off": "",
    "none": "",
    "关闭": "",
    "关": "",
    "follow": "follow",
    "跟随": "follow",
    "跟随人脸": "follow",
    "follow_frontal": "follow_frontal",
    "正脸": "follow_frontal",
    "跟随正脸": "follow_frontal",
    "gaze": "gaze",
    "注视": "gaze",
    "注视感知": "gaze",
}


def _normalize_follow_mode(raw: object) -> str:
    key = str(raw or "").strip().lower()
    if key in _FOLLOW_ALIASES:
        return _FOLLOW_ALIASES[key]
    return str(raw or "").strip()


def execute_llm_tools(
    tools: list[dict[str, Any]],
    *,
    device_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """逐条执行工具，返回结果摘要（供日志与 pipeline 事件）。"""
    results: list[dict[str, Any]] = []
    dev = str(device_id or "").strip()
    for raw in tools or []:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or raw.get("name") or "").strip()
        if not tool:
            continue
        try:
            if tool == "register_face":
                name = str(raw.get("name") or raw.get("person_name") or "").strip()
                fid_raw = raw.get("face_id")
                face_id = int(fid_raw) if fid_raw is not None else None
                out = register_face_for_device(dev, name, face_id=face_id)
                results.append(
                    {
                        "tool": tool,
                        "ok": True,
                        "person_id": out["profile"].get("person_id"),
                        "name": out["profile"].get("name"),
                        "face_id": out.get("face_id"),
                    }
                )
            elif tool in ("set_camera_follow", "set_camera_follow_mode", "camera_follow"):
                mode = _normalize_follow_mode(raw.get("mode") or raw.get("value"))
                if mode not in ("", "follow", "follow_frontal", "gaze"):
                    raise ValueError(f"未知跟随模式: {mode!r}")
                norm = persist_camera_servo_auto_mode(mode)
                results.append({"tool": tool, "ok": True, "mode": norm})
            elif tool == "memory_add":
                text = str(raw.get("text") or raw.get("value") or "").strip()
                if not text:
                    raise ValueError("memory_add 需要 text")
                entry = add_memory(text, device_id=dev or None)
                results.append({"tool": tool, "ok": True, "id": entry["id"], "text": entry["text"]})
            elif tool == "memory_delete":
                eid = str(raw.get("id") or "").strip()
                if not eid:
                    raise ValueError("memory_delete 需要 id")
                ok = delete_memory(eid)
                if not ok:
                    raise ValueError(f"未找到记忆 id={eid}")
                results.append({"tool": tool, "ok": True, "id": eid})
            else:
                results.append({"tool": tool, "ok": False, "error": f"未知工具: {tool}"})
        except Exception as exc:
            logger.warning("[LLM tools] %s 失败: %s", tool, exc)
            results.append({"tool": tool, "ok": False, "error": str(exc)})
    return results
