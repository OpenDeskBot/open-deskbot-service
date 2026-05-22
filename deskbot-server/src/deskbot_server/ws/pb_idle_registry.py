"""PB 空闲打盹：ws_send 与 AsrChatHub 之间的注册表，避免循环 import。"""

from __future__ import annotations

from typing import Any, Optional, Protocol


class _PbIdleHub(Protocol):
    pb_idle_snore: Any

    def ws_asr_device_id(self, ws) -> Optional[str]: ...


_hub: Optional[_PbIdleHub] = None


def set_pb_idle_hub(hub: Optional[_PbIdleHub]) -> None:
    global _hub
    _hub = hub


def note_pb_idle_after_successful_asr_send(websocket) -> None:
    """成功下行到某条 WebSocket 后刷新「空闲打盹」计时（仅已登记为 /asr_chat 的连接）。"""
    hub = _hub
    if hub is None:
        return
    sched = getattr(hub, "pb_idle_snore", None)
    if sched is None:
        return
    dev = hub.ws_asr_device_id(websocket)
    if dev:
        sched.note_activity(dev)
