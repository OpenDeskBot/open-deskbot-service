from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ChatTurnResult:
    """一轮 ASR→LLM→TTS/pb 的时序与结果摘要。"""

    llm_text: Optional[str] = None
    llm_raw: Optional[str] = None
    actions: list[Any] = field(default_factory=list)
    servo: list[Any] = field(default_factory=list)
    need_reply: bool = True
    json_ok: bool = False
    t_llm_end: Optional[float] = None
    t_tts_end: Optional[float] = None
    status: str = "ok"
    error: Optional[str] = None
    voice_auto_reply_off: bool = False
    scenes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "llm_text": self.llm_text,
            "llm_raw": self.llm_raw,
            "actions": self.actions,
            "servo": self.servo,
            "need_reply": self.need_reply,
            "json_ok": self.json_ok,
            "t_llm_end": self.t_llm_end,
            "t_tts_end": self.t_tts_end,
            "status": self.status,
            "error": self.error,
            "voice_auto_reply_off": self.voice_auto_reply_off,
            "scenes": self.scenes,
        }
