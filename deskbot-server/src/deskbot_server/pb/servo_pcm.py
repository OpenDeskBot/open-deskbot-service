"""舵机交错、PCM 对齐与 pb JSON wire 消息。"""

from __future__ import annotations

import copy
import logging
import random
from collections import deque
from typing import Any

from deskbot_server.pb.shapes import PB_ACTION_REPLACE, PB_LEVEL_TASK

logger = logging.getLogger("deskbot-server")

def _silence_phoneme_seg(ms: int, sample_rate: int) -> dict[str, Any]:
    """生成与 TTS 分片同结构的静音片（mono s16le），供 hold / 纯舵机片使用。"""
    ms = max(int(ms), 1)
    sr = max(int(sample_rate), 1)
    n_samples = sr * ms // 1000
    pcm = b"\x00" * (n_samples * 2)
    return {"phoneme": "", "ms": ms, "pcm": pcm}


def interleave_tts_phoneme_segs_with_servo_plan(
    segs: list[dict[str, Any]],
    servo_plan: list[dict[str, Any]] | None,
    sample_rate: int,
) -> tuple[list[dict[str, Any]], list[dict[str, int] | None]]:
    """把 ``servo`` 计划（hold + 位移）与 PaddleSpeech 音素分片按播放顺序交错。

    - ``{"_hold_ms": H}``：插入 H 毫秒静音片，并在对应 ``parallel_servo`` 写入 **hold**
      ``{xm:2, ym:2, x:0, y:0, ms:H}``（本包双轴不驱动，时长 ms 与 chunk 对齐）。
    - 普通 ``{xm, ym, x, y, ms}``：**优先**消费下一条尚未输出的 TTS 分片并附上该舵机；
      若 TTS 已耗尽，则追加一条 **仅承载该舵机** 的静音片（``chunk_ms`` 与 ``ms`` 对齐，由后续 ``align_pcm`` 修正）。

    处理完计划后，将 **剩余** TTS 分片依次追加（无舵机）。这样「点头 5 次」可写 10 条位移，
    不会因音素只有 2 片而被截断为 2 帧。
    """
    if not segs:
        return [], []
    if not servo_plan:
        return list(segs), [None] * len(segs)

    tokens: list[tuple[str, Any]] = []
    for it in servo_plan:
        if not isinstance(it, dict):
            continue
        if "_hold_ms" in it:
            try:
                h = int(it["_hold_ms"])
            except (TypeError, ValueError):
                continue
            if h > 0:
                tokens.append(("hold", min(h, 30_000)))
            continue
        if "xm" in it and "ym" in it:
            try:
                xm, ym = int(it["xm"]), int(it["ym"])
                x, y = int(it["x"]), int(it["y"])
                ms = int(it["ms"])
            except (TypeError, ValueError, KeyError):
                continue
            if xm not in (0, 1, 2) or ym not in (0, 1, 2) or ms <= 0:
                continue
            tokens.append(("move", {"xm": xm, "ym": ym, "x": x, "y": y, "ms": ms}))

    if not tokens:
        return list(segs), [None] * len(segs)

    pq: deque[dict[str, Any]] = deque(copy.deepcopy(s) for s in segs)
    out_segs: list[dict[str, Any]] = []
    parallel: list[dict[str, int] | None] = []

    for kind, payload in tokens:
        if kind == "hold":
            ms = int(payload)
            out_segs.append(_silence_phoneme_seg(ms, sample_rate))
            parallel.append({"xm": 2, "ym": 2, "x": 0, "y": 0, "ms": ms})
        else:
            cmd = payload
            if pq:
                out_segs.append(pq.popleft())
            else:
                cms = max(int(cmd["ms"]), 40)
                out_segs.append(_silence_phoneme_seg(cms, sample_rate))
            parallel.append(cmd)

    while pq:
        out_segs.append(pq.popleft())
        parallel.append(None)

    return out_segs, parallel


def apply_parallel_pb_servo(
    pairs: list[tuple[dict[str, Any], bytes]],
    parallel: list[dict[str, int] | None] | None,
) -> int:
    """按与 ``pairs`` 等长的 ``parallel`` 写入 ``servo``；``None`` 表示该片不附加舵机。"""
    if not parallel:
        return 0
    n = 0
    for i, (msg, _pcm) in enumerate(pairs):
        if i >= len(parallel):
            break
        cmd = parallel[i]
        if not isinstance(cmd, dict):
            continue
        msg["servo"] = {
            "xm": int(cmd["xm"]),
            "ym": int(cmd["ym"]),
            "x": int(cmd["x"]),
            "y": int(cmd["y"]),
            "ms": int(cmd["ms"]),
        }
        n += 1
    return n


def apply_llm_pb_servo_actions(
    pairs: list[tuple[dict[str, Any], bytes]],
    servo_cmds: list[dict[str, Any]] | None,
) -> int:
    """将 LLM 给出的舵机序列按分片下标与 ``pairs`` 对齐，写入各 ``msg`` 的 ``servo`` 字段。

    第 ``i`` 条舵机指令写到第 ``i`` 个 ``(msg, pcm)``；若指令多于分片则丢弃多余项并打日志。
    返回实际写入的分片数（``apply_random_pb_servo_actions`` 会跳过已有 ``servo`` 的片）。
    """
    if not servo_cmds:
        return 0
    n_pairs = len(pairs)
    if len(servo_cmds) > n_pairs:
        logger.warning(
            "[pb] LLM servo 条数 (%d) 多于音素分片 (%d)，已截断",
            len(servo_cmds),
            n_pairs,
        )
    n = 0
    for i, (msg, _pcm) in enumerate(pairs):
        if i >= len(servo_cmds):
            break
        cmd = servo_cmds[i]
        if not isinstance(cmd, dict):
            continue
        msg["servo"] = {
            "xm": int(cmd["xm"]),
            "ym": int(cmd["ym"]),
            "x": int(cmd["x"]),
            "y": int(cmd["y"]),
            "ms": int(cmd["ms"]),
        }
        n += 1
    return n


def apply_random_pb_servo_actions(
    pairs: list[tuple[dict[str, Any], bytes]],
    cfg: dict[str, Any] | None,
    *,
    rng: random.Random | None = None,
) -> int:
    """在含 PCM 的 pb 分片上按概率附加 ``servo``（双轴相对位移，``xm=ym=1``）。

    用于让 ESP32 在说话时偶尔点头/摆头；不改变 binary PCM。
    返回实际附加了 ``servo`` 的分片数。
    """
    if not cfg or not cfg.get("enabled"):
        return 0
    r = rng or random.Random()
    try:
        p_hit = float(cfg.get("probability", 0.3))
    except (TypeError, ValueError):
        p_hit = 0.3
    p_hit = max(0.0, min(1.0, p_hit))
    try:
        ms_min = int(cfg.get("ms_min", cfg.get("servo_ms_min", 120)))
        ms_max = int(cfg.get("ms_max", cfg.get("servo_ms_max", 280)))
    except (TypeError, ValueError):
        ms_min, ms_max = 120, 280
    if ms_max < ms_min:
        ms_min, ms_max = ms_max, ms_min

    def _irange(key: str, default: tuple[int, int]) -> tuple[int, int]:
        v = cfg.get(key)
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            try:
                a, b = int(v[0]), int(v[1])
                return (min(a, b), max(a, b))
            except (TypeError, ValueError):
                pass
        return default

    rx0, rx1 = _irange("rel_x_range", (-6, 6))
    ry0, ry1 = _irange("rel_y_range", (-6, 6))
    skip_first = bool(cfg.get("skip_first", True))
    skip_last = bool(cfg.get("skip_last", True))

    n = len(pairs)
    added = 0
    for i, (msg, pcm) in enumerate(pairs):
        if not pcm:
            continue
        if skip_first and i == 0:
            continue
        if skip_last and n > 1 and i == n - 1:
            continue
        if msg.get("servo"):
            continue
        if r.random() >= p_hit:
            continue
        dx = r.randint(rx0, rx1)
        dy = r.randint(ry0, ry1)
        if dx == 0 and dy == 0:
            if rx1 >= 1:
                dx = 1
            elif rx0 <= -1:
                dx = -1
            elif ry1 >= 1:
                dy = 1
            elif ry0 <= -1:
                dy = -1
            else:
                continue
        msg["servo"] = {
            "xm": 1,
            "ym": 1,
            "x": int(dx),
            "y": int(dy),
            "ms": int(r.randint(ms_min, ms_max)),
        }
        added += 1
    return added


def align_pcm_s16le_mono_to_chunk_ms(
    pcm: bytes, chunk_ms: int, sample_rate: int
) -> tuple[bytes, int]:
    """把 mono s16le PCM 对齐到 ``chunk_ms * sample_rate // 1000 * 2`` 字节。

    与常见设备校验公式一致：``expect_len = chunk_ms * sr / 1000 * 2``（整除）。
    音素均分切片可能多出几个采样，不处理会导致 ``binary length mismatch``。

    若 ``chunk_ms<=0`` 但有 PCM，则用 PCM 长度反推 ``chunk_ms``（floor ms）。
    """
    pcm = pcm[: len(pcm) & ~1]
    if sample_rate <= 0:
        return pcm, max(0, chunk_ms)
    if chunk_ms <= 0:
        if not pcm:
            return pcm, 0
        chunk_ms = max(1, (len(pcm) // 2) * 1000 // sample_rate)
    expected = (chunk_ms * sample_rate // 1000) * 2
    if expected <= 0:
        return pcm, chunk_ms
    if len(pcm) > expected:
        return pcm[:expected], chunk_ms
    if len(pcm) < expected:
        return pcm + b"\x00" * (expected - len(pcm)), chunk_ms
    return pcm, chunk_ms


def pb_json_messages(
    *,
    pb_req: str,
    sample_rate: int,
    fmt: str,
    channels: int,
    anim_rows: list[dict[str, Any]],
    pcm_per_idx: list[bytes],
    action: str = PB_ACTION_REPLACE,
    level: int = PB_LEVEL_TASK,
) -> list[tuple[dict[str, Any], bytes]]:
    """生成 ``(pb 字典, 紧随其后的 PCM 或 b'')`` 列表；有 PCM 时字典内含 ``audio.next_bin``。

    单片 ``n == 1`` 使用 ``pb_single``；多片为 ``pb_start`` → ``pb_chunk``* → ``pb_end``。

    ``action``：``replace`` / ``append`` / ``default``；``level``：0–3，语义见协议文档。
    缺省 ``replace`` + ``level=1``（任务态）。"""
    n = len(anim_rows)
    if n == 0:
        return []
    pairs: list[tuple[dict[str, Any], bytes]] = []
    for i in range(n):
        row = anim_rows[i]
        is_first = i == 0
        is_last = i == n - 1
        if n == 1:
            # 单片自成一轮：下位机要求 type=pb_single，勿单发 pb_end（多片仍 start→…→end）
            typ = "pb_single"
        elif is_first:
            typ = "pb_start"
        elif is_last:
            typ = "pb_end"
        else:
            typ = "pb_chunk"
        pcm = pcm_per_idx[i] if i < len(pcm_per_idx) else b""
        msg: dict[str, Any] = {
            "type": typ,
            "req": pb_req,
            "idx": row.get("idx", i),
            "chunk_ms": int(row.get("chunk_ms") or 0),
            "anim": row["anim"],
            "phoneme": row.get("phoneme", ""),
            "pb_ver": 2,
            "action": action,
            "level": int(level),
        }
        if is_first or n == 1:
            msg["sr"] = int(sample_rate)
            msg["fmt"] = fmt
            msg["ch"] = int(channels)
        if pcm:
            msg["audio"] = {"next_bin": 1}
        pairs.append((msg, pcm if pcm else b""))
    return pairs
