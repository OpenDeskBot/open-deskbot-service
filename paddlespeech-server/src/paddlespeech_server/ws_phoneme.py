"""``/paddlespeech/tts/streaming_phoneme`` WebSocket 路由。"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, WebSocket
from paddlespeech.cli.log import logger
from starlette.websockets import WebSocketState

from .phoneme import (
    collect_pcm_int16,
    flatten_phone_ids,
    id_to_symbol_map,
    split_pcm_by_phonemes,
)

extra_router = APIRouter()


@extra_router.websocket("/paddlespeech/tts/streaming_phoneme")
async def websocket_streaming_phoneme(websocket: WebSocket):
    await websocket.accept()
    from paddlespeech.server.engine.engine_pool import get_engine_pool

    engine_pool = get_engine_pool()
    tts_engine = engine_pool.get("tts")
    if tts_engine is None:
        await websocket.send_json(
            {"status": -1, "message": "tts engine not initialized", "segments": []}
        )
        await websocket.close()
        return
    et = getattr(tts_engine, "engine_type", "")
    if et != "online-onnx":
        await websocket.send_json(
            {
                "status": -1,
                "message": f"streaming_phoneme only supports engine_type=online-onnx, got {et!r}",
                "segments": [],
            }
        )
        await websocket.close()
        return

    from paddlespeech.server.engine.tts.online.onnx.tts_engine import (
        PaddleTTSConnectionHandler,
    )

    connection_handler: Any = None
    session: str | None = None

    try:
        while True:
            assert websocket.application_state == WebSocketState.CONNECTED
            message = await websocket.receive()
            websocket._raise_on_disconnect(message)
            data = json.loads(message["text"])

            if "signal" in data:
                if data["signal"] == "start":
                    session = uuid.uuid1().hex
                    connection_handler = PaddleTTSConnectionHandler(tts_engine)
                    await websocket.send_json(
                        {
                            "status": 0,
                            "signal": "server ready",
                            "session": session,
                        }
                    )
                elif data["signal"] == "end":
                    connection_handler = None
                    await websocket.send_json(
                        {
                            "status": 0,
                            "signal": "connection will be closed",
                            "session": session or "",
                        }
                    )
                    break
                else:
                    await websocket.send_json(
                        {"status": 0, "signal": "no valid json data"}
                    )

            elif "text" in data:
                if connection_handler is None:
                    await websocket.send_json(
                        {"status": -1, "message": "send signal start first", "segments": []}
                    )
                    continue
                text = str(data["text"])
                spk_id = int(data.get("spk_id", 0))
                try:
                    phone_ids = flatten_phone_ids(connection_handler, text)
                    pcm = collect_pcm_int16(connection_handler, text, spk_id)
                    id_to_sym = id_to_symbol_map(connection_handler)
                    sr = int(tts_engine.sample_rate)
                    if not phone_ids:
                        await websocket.send_json(
                            {
                                "status": -1,
                                "message": "empty phone_ids for text",
                                "segments": [],
                            }
                        )
                        continue
                    segments = split_pcm_by_phonemes(
                        pcm, phone_ids, sample_rate=sr, id_to_sym=id_to_sym
                    )
                    await websocket.send_json({"status": 1, "segments": segments})
                    await websocket.send_json({"status": 2, "segments": []})
                except Exception as exc:  # pragma: no cover
                    logger.exception("streaming_phoneme synthesis failed")
                    await websocket.send_json(
                        {
                            "status": -1,
                            "message": str(exc),
                            "detail": type(exc).__name__,
                            "segments": [],
                        }
                    )
            else:
                logger.error("Invalid streaming_phoneme request JSON")
                await websocket.send_json(
                    {"status": -1, "message": "invalid request json", "segments": []}
                )

    except Exception as exc:  # pragma: no cover
        logger.error(exc)
