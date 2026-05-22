from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]

from openai import OpenAI

from deskbot_server.core.settings import AppSettings
from deskbot_server.llm.utils import llm_pb_scenes_prompt_appendix

logger = logging.getLogger("deskbot-server")


class OpenAiLlmAdapter:
    """OpenAI 兼容 Chat API（DashScope Qwen 等）。"""

    def __init__(self, settings: AppSettings) -> None:
        api_key = (
            os.environ.get("LLM_API_KEY")
            or os.environ.get("DASHSCOPE_API_KEY")
            or os.environ.get("QWEN_API_KEY")
        )
        if not api_key or "请替换" in api_key:
            raise ValueError(
                "LLM API Key 未配置。请通过环境变量 LLM_API_KEY 或 DASHSCOPE_API_KEY 传入。"
            )
        try:
            api_key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("LLM API Key 包含非 ASCII 字符，请使用真实英文 API Key。") from exc

        self._client = OpenAI(api_key=api_key, base_url=settings.llm.base_url)
        self._model = settings.llm.model_name
        self._system_prompt = settings.llm.system_prompt or (
            '你是中文语音助手，请简洁回答。每次只输出 JSON：{"tts":"…","servo":[]}。'
        )

    @staticmethod
    def _beijing_time_str() -> str:
        if ZoneInfo is not None:
            now = dt.datetime.now(ZoneInfo("Asia/Shanghai"))
        else:
            now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
        weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        return now.strftime("%Y-%m-%d %H:%M:%S") + " " + weekdays[now.weekday()]

    def _build_system_prompt(self, device_context: Optional[str] = None) -> str:
        base = f"{self._system_prompt}\n当前时间是: {self._beijing_time_str()}（北京时间，东八区）"
        px = llm_pb_scenes_prompt_appendix()
        if px:
            base += "\n" + px
        if device_context:
            base += (
                "\n\n以下为本机 ESP32 最近一次上报的播放/舵机状态（pb_ack，JSON）；"
                "若无本段则设备尚未上报。生成 servo 时请结合其中舵机位置与边界，使用相对位移（xm=ym=1），避免撞边：\n"
                f"{device_context}"
            )
        return base

    async def complete(self, user_text: str, *, device_context: Optional[str] = None) -> str:
        system_content = self._build_system_prompt(device_context)

        def _chat() -> str:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.7,
            )
            return completion.choices[0].message.content or ""

        return (await asyncio.to_thread(_chat)).strip()
