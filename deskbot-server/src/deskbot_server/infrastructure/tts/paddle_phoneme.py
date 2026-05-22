from __future__ import annotations

from deskbot_server.core.ports.tts import PhonemeSegment
from deskbot_server.core.settings import AppSettings
from deskbot_server.tts.phoneme import fetch_phoneme_tts, phoneme_streaming_url_from_tts_ws


class PaddlePhonemeTtsAdapter:
    """PaddleSpeech streaming_phoneme TTS 适配器。"""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    async def synthesize_phoneme_segments(self, text: str) -> tuple[int, list[PhonemeSegment]]:
        url = phoneme_streaming_url_from_tts_ws(self._settings.tts.ws_url)
        spk_id = self._settings.tts.spk_id
        sample_rate = self._settings.tts.sample_rate
        segments, _full = await fetch_phoneme_tts(url, text, spk_id)
        return sample_rate, [
            PhonemeSegment(
                phoneme=str(s.get("phoneme") or ""),
                ms=int(s.get("ms") or 0),
                pcm=bytes(s.get("pcm") or b""),
                phoneme_id=s.get("phoneme_id"),
            )
            for s in segments
        ]
