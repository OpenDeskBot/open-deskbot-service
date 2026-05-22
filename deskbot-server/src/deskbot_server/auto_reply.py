# 调试页「启用自动应答」：为 False 时 /asr_chat 不执行 LLM+TTS（见 GET/GET?enabled= /api/asr_auto_reply）
_asr_voice_auto_reply_enabled = True


def get_asr_voice_auto_reply_enabled() -> bool:
    return _asr_voice_auto_reply_enabled


def set_asr_voice_auto_reply_enabled(enabled: bool) -> None:
    global _asr_voice_auto_reply_enabled
    _asr_voice_auto_reply_enabled = bool(enabled)
