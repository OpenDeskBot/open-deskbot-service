"""pb 下行 wire 组帧：音素分片 → anim → JSON+binary 对。"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from deskbot_server.pb.face_bundle import resolve_pb_face_bundle
from deskbot_server.pb.llm_plan import (
    build_anim_rows_for_llm_plan,
    expand_llm_anims,
    expand_llm_moves,
    interleave_tts_segs_with_llm_plan,
)
from deskbot_server.pb.phoneme_anim import phoneme_seq_to_anim_seq
from deskbot_server.pb.servo_pcm import (
    align_pcm_s16le_mono_to_chunk_ms,
    apply_parallel_pb_servo,
    apply_random_pb_servo_actions,
    interleave_tts_phoneme_segs_with_servo_plan,
    pb_json_messages,
)

logger = logging.getLogger("deskbot-server")

__all__ = [
    "align_pcm_s16le_mono_to_chunk_ms",
    "apply_parallel_pb_servo",
    "apply_random_pb_servo_actions",
    "build_pb_wire_pairs",
    "interleave_tts_phoneme_segs_with_servo_plan",
    "pb_json_messages",
    "phoneme_seq_to_anim_seq",
    "resolve_pb_face_bundle",
]


def build_pb_wire_pairs(
    segs: list[dict[str, Any]],
    tts_cfg: dict[str, Any],
    *,
    servo_plan: list[dict[str, Any]] | None = None,
    moves: list[dict[str, Any]] | None = None,
    anims: list[dict[str, Any]] | None = None,
    sample_rate: int,
    request_id: Optional[str] = None,
    random_servo_cfg: Optional[dict[str, Any]] = None,
) -> tuple[list[tuple[dict[str, Any], bytes]], str, int, int]:
    """音素 TTS 分片 → pb wire (msg, pcm) 列表。返回 (pairs, pb_req, n_pb, sample_rate)。"""
    face_bundle = resolve_pb_face_bundle(tts_cfg)
    move_steps = expand_llm_moves(moves)
    anim_frames = expand_llm_anims(anims)
    parallel_anim: list[dict[str, Any] | None] | None = None

    if move_steps or anim_frames:
        segs, parallel_servo, parallel_anim = interleave_tts_segs_with_llm_plan(
            segs, move_steps, anim_frames, sample_rate
        )
        logger.info(
            "[pb TX] LLM moves/anims 交错后 segments=%d（move_steps=%d anim_frames=%d）",
            len(segs),
            len(move_steps),
            len(anim_frames),
        )
    else:
        segs, parallel_servo = interleave_tts_phoneme_segs_with_servo_plan(
            segs, servo_plan, sample_rate
        )
        logger.info(
            "[pb TX] 音素分片与 servo 计划交错后 segments=%d（含 hold/补静音承载的多余舵机）",
            len(segs),
        )

    if parallel_anim is not None:
        anim_rows = build_anim_rows_for_llm_plan(segs, parallel_anim, face_bundle)
    else:
        anim_rows = phoneme_seq_to_anim_seq(segs, face_bundle)
    pcm_list: list[bytes] = []
    for i, s in enumerate(segs):
        raw = bytes(s.get("pcm") or b"")
        cms = int(anim_rows[i].get("chunk_ms") or s.get("ms") or 0)
        aligned, cms2 = align_pcm_s16le_mono_to_chunk_ms(raw, cms, sample_rate)
        if cms2 != cms:
            anim_rows[i]["chunk_ms"] = cms2
        pcm_list.append(aligned)

    pb_req = request_id or uuid.uuid4().hex[:16]
    pairs = pb_json_messages(
        pb_req=pb_req,
        sample_rate=sample_rate,
        fmt="s16le",
        channels=1,
        anim_rows=anim_rows,
        pcm_per_idx=pcm_list,
    )
    n_llm_servo = apply_parallel_pb_servo(pairs, parallel_servo)
    if n_llm_servo:
        logger.info(
            "[pb TX] 已将 %d 条 pb 分片附上舵机/hold（parallel 与交错后分片对齐）",
            n_llm_servo,
        )
    if random_servo_cfg:
        n_ra = apply_random_pb_servo_actions(pairs, random_servo_cfg)
        if n_ra:
            logger.info("[pb TX] 随机舵机动作：%d 片附加 servo", n_ra)

    return pairs, pb_req, len(pairs), sample_rate
