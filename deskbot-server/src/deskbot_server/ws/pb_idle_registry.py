"""PB 空闲计时：ws_send 与 AsrChatHub 之间的注册表，避免循环 import。"""

from __future__ import annotations

from typing import Any, Optional, Protocol

_PB_IDLE_SCHED_ATTRS = ("pb_idle_snore", "pb_idle_silence")


class _PbIdleHub(Protocol):
    pb_idle_snore: Any
    pb_idle_silence: Any

    def ws_asr_device_id(self, ws) -> Optional[str]: ...


_hub: Optional[_PbIdleHub] = None


def set_pb_idle_hub(hub: Optional[_PbIdleHub]) -> None:
    global _hub
    _hub = hub


def _notify_idle_schedulers(device_id: str) -> None:
    hub = _hub
    if hub is None or not device_id:
        return
    for attr in _PB_IDLE_SCHED_ATTRS:
        sched = getattr(hub, attr, None)
        if sched is not None:
            sched.note_activity(device_id)


def note_pb_idle_after_successful_asr_send(websocket) -> None:
    """成功下行到某条 WebSocket 后刷新各 idle 计时（仅已登记为 /asr_chat 的连接）。"""
    hub = _hub
    if hub is None:
        return
    dev = hub.ws_asr_device_id(websocket)
    if dev:
        _notify_idle_schedulers(dev)


def note_pb_idle_for_device(device_id: str) -> None:
    """按 device_id 刷新 idle 计时（设备刚接入 /asr_chat 时调用）。"""
    _notify_idle_schedulers(device_id)
