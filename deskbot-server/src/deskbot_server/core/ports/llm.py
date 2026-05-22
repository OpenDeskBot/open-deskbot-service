from __future__ import annotations

from typing import Optional, Protocol


class LlmPort(Protocol):
    async def complete(self, user_text: str, *, device_context: Optional[str] = None) -> str: ...
