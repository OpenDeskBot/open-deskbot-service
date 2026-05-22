"""音素对齐 TTS 的 PCM 切分与 frontend 辅助（纯逻辑，便于单测）。"""

from __future__ import annotations

import base64
from typing import Any, Dict, List

import numpy as np


def flatten_phone_ids(handler: Any, text: str) -> List[int]:
    fe = handler.executor.frontend
    lang = handler.config.lang
    if lang == "zh":
        input_ids = fe.get_input_ids(
            text, merge_sentences=False, get_tone_ids=False
        )
    elif lang == "en":
        input_ids = fe.get_input_ids(text, merge_sentences=False)
    else:
        raise ValueError(f"unsupported TTS lang: {lang}")
    phone_ids = input_ids.get("phone_ids") or []
    out: List[int] = []
    for t in phone_ids:
        out.extend(np.asarray(t.numpy(), dtype=np.int64).flatten().tolist())
    return [int(x) for x in out]


def id_to_symbol_map(handler: Any) -> Dict[int, str]:
    vocab = handler.executor.frontend.vocab_phones
    return {int(v): str(k) for k, v in vocab.items()}


def collect_pcm_int16(handler: Any, text: str, spk_id: int) -> np.ndarray:
    chunks: List[np.ndarray] = []
    for wav_b64 in handler.run(sentence=text, spk_id=spk_id):
        raw = base64.b64decode(wav_b64)
        chunks.append(np.frombuffer(raw, dtype=np.int16))
    if not chunks:
        return np.array([], dtype=np.int16)
    return np.concatenate(chunks, axis=0)


def split_pcm_by_phonemes(
    pcm: np.ndarray,
    phone_ids: List[int],
    sample_rate: int,
    id_to_sym: Dict[int, str],
) -> List[Dict[str, Any]]:
    n = len(phone_ids)
    if n == 0:
        return []
    total = int(pcm.shape[0])
    segs: List[Dict[str, Any]] = []
    for i, pid in enumerate(phone_ids):
        start = total * i // n
        end = total * (i + 1) // n
        if i == n - 1:
            end = total
        chunk = pcm[start:end]
        # 与 ``chunk_ms * sr // 1000 * 2`` 字节对齐（均分边界可能多几个 int16，设备会按 chunk_ms 算 expect_len）
        if sample_rate > 0 and chunk.size > 0:
            ns = int(chunk.size)
            ms_floor = ns * 1000 // sample_rate
            exp_samples = ms_floor * sample_rate // 1000
            if exp_samples == 0:
                exp_samples = ns
            elif exp_samples < ns:
                chunk = chunk[:exp_samples]
        ms = (
            max(1, int(chunk.size * 1000 // sample_rate))
            if sample_rate and chunk.size > 0
            else 0
        )
        sym = id_to_sym.get(int(pid), str(int(pid)))
        b64 = base64.b64encode(chunk.tobytes()).decode("ascii")
        segs.append(
            {
                "audio": b64,
                "phoneme_id": int(pid),
                "phoneme": sym,
                "ms": ms,
            }
        )
    return segs
