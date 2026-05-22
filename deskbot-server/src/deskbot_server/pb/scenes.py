from __future__ import annotations

import copy
import json
import os
from typing import Optional

from deskbot_server.constants import PB_SCENES_FILE

_pb_scenes_doc_cache: Optional[tuple[float, dict]] = None


def _load_pb_scenes_document() -> dict:
    """读取 ``pb_scenes_*.json`` 根对象；按 mtime 缓存。"""
    global _pb_scenes_doc_cache
    try:
        mtime = os.path.getmtime(PB_SCENES_FILE)
    except OSError:
        _pb_scenes_doc_cache = None
        return {}
    if _pb_scenes_doc_cache is not None and _pb_scenes_doc_cache[0] == mtime:
        return _pb_scenes_doc_cache[1]
    try:
        with open(PB_SCENES_FILE, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    _pb_scenes_doc_cache = (mtime, doc)
    return doc


def _pb_scenes_root(doc: dict) -> dict:
    sc = doc.get("scenes")
    return sc if isinstance(sc, dict) else {}


def _pb_scene_entry_by_name(doc: dict, scene_lower: str) -> Optional[dict]:
    """按 **不区分大小写** 匹配 ``scenes`` 下的场景名。"""
    want = (scene_lower or "").strip().lower()
    if not want:
        return None
    for k, v in _pb_scenes_root(doc).items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        if k.strip().lower() == want and isinstance(v, dict):
            return v
    return None


def _pb_scene_keys_sorted(doc: dict) -> list[str]:
    """返回含非空 ``frames`` 的场景 id 列表（原始大小写，排序）。"""
    out: list[str] = []
    for k, v in _pb_scenes_root(doc).items():
        if not isinstance(k, str) or k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(v.get("frames"), list) and len(v["frames"]) > 0:
            out.append(k.strip())
    out.sort(key=lambda s: (s.lower(), s))
    return out


def _prepare_pb_scene_chain_frames(scene_name: str, *, runtime_req: str) -> list[dict]:
    """从场景文档复制一链 pb 帧，写入 ``req``，并设 ``append`` / ``opportunistic``。"""
    doc = _load_pb_scenes_document()
    ent = _pb_scene_entry_by_name(doc, scene_name)
    if ent is None:
        return []
    raw_frames = ent.get("frames")
    if not isinstance(raw_frames, list) or not raw_frames:
        return []
    frames: list[dict] = []
    for fr in raw_frames:
        if not isinstance(fr, dict):
            continue
        one = copy.deepcopy(fr)
        one["req"] = runtime_req
        frames.append(one)
    if not frames:
        return []
    chain_action = "append" if len(frames) > 1 else "opportunistic"
    for one in frames:
        one["action"] = chain_action
    return frames
