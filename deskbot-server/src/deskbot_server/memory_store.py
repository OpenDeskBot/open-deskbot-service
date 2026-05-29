"""用户长期记忆（``data/user_memory.json``），注入 LLM system prompt。"""
from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Optional

from deskbot_server.constants import USER_MEMORY_FILE

_MAX_ENTRIES = 200
_MAX_PROMPT_ENTRIES = 30


def _normalize_entry(raw: object, *, device_id: str = "") -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("entry must be object")
    text = str(raw.get("text") or raw.get("value") or "").strip()
    if not text:
        raise ValueError("text required")
    entry_id = str(raw.get("id") or "").strip() or uuid.uuid4().hex[:12]
    dev = str(raw.get("device_id") if raw.get("device_id") is not None else device_id).strip()
    created = raw.get("created_at")
    try:
        created_at = float(created) if created is not None else time.time()
    except (TypeError, ValueError):
        created_at = time.time()
    return {
        "id": entry_id,
        "device_id": dev,
        "text": text,
        "created_at": created_at,
    }


def load_memory_entries() -> list[dict[str, Any]]:
    if not os.path.isfile(USER_MEMORY_FILE):
        return []
    with open(USER_MEMORY_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        try:
            out.append(_normalize_entry(item))
        except ValueError:
            continue
    return out


def save_memory_entries(entries: list[dict[str, Any]]) -> None:
    norm = [_normalize_entry(e) for e in entries]
    os.makedirs(os.path.dirname(USER_MEMORY_FILE) or ".", exist_ok=True)
    with open(USER_MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"entries": norm}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def list_memory_for_device(device_id: Optional[str] = None, *, limit: int = _MAX_PROMPT_ENTRIES) -> list[dict[str, Any]]:
    """设备专属 + 全局（``device_id`` 为空）记忆，按时间倒序。"""
    dev = str(device_id or "").strip()
    cap = max(1, min(int(limit), _MAX_ENTRIES))
    rows = load_memory_entries()
    matched = [
        e
        for e in rows
        if not str(e.get("device_id") or "").strip() or str(e.get("device_id") or "").strip() == dev
    ]
    matched.sort(key=lambda e: float(e.get("created_at") or 0), reverse=True)
    return matched[:cap]


def add_memory(text: str, *, device_id: Optional[str] = None) -> dict[str, Any]:
    entries = load_memory_entries()
    entry = _normalize_entry({"text": text, "device_id": device_id or ""})
    entries.append(entry)
    if len(entries) > _MAX_ENTRIES:
        entries = sorted(entries, key=lambda e: float(e.get("created_at") or 0))[-_MAX_ENTRIES:]
    save_memory_entries(entries)
    return entry


def delete_memory(entry_id: str) -> bool:
    eid = str(entry_id or "").strip()
    if not eid:
        return False
    entries = load_memory_entries()
    kept = [e for e in entries if str(e.get("id") or "") != eid]
    if len(kept) == len(entries):
        return False
    save_memory_entries(kept)
    return True
