#!/usr/bin/env python3
import asyncio
import base64
import datetime as _dt
import io
import json
import os
import socket
import time
import wave

import yaml
from flask import Flask, jsonify, redirect, render_template, request, url_for

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


from deskbot_server.config import load_config as _load_config_yaml
from deskbot_server.env import load_dotenv
from deskbot_server.llm.utils import llm_pb_scenes_prompt_appendix, parse_llm_reply
from deskbot_server.paths import DEFAULT_CONFIG_PATH

CONFIG_PATH = DEFAULT_CONFIG_PATH

load_dotenv()

app = Flask(__name__, template_folder="templates")
# 调试期：模板修改立即生效（不依赖 debug=True 副作用），并且让浏览器不要缓存
# debug 页面 HTML——避免改了 .html 之后浏览器仍展示旧版（典型现象：「我改了
# 代码但页面看起来没变」），看 below `_no_cache_debug_pages`。
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


@app.after_request
def _no_cache_debug_pages(resp):
    """对 /debug/* 调试页面禁止任何缓存，避免开发时改了 HTML 浏览器仍用老版。"""
    p = (request.path or "")
    if p.startswith("/debug/") or p == "/":
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


def _prefer_access_host(raw_host: str) -> str:
    """将 0.0.0.0/127.0.0.1/localhost 替换为当前页面访问的主机 IP。"""
    host = (raw_host or "").strip()
    if host and host not in ("0.0.0.0", "::", "127.0.0.1", "localhost"):
        return host
    req_host = (request.host or "").split(":", 1)[0].strip()
    if req_host and req_host not in ("0.0.0.0", "::", "localhost"):
        return req_host
    return "127.0.0.1"


def _deskbot_ws_base() -> tuple[str, int]:
    cfg = load_config()
    srv = cfg.get("server") or {}
    public = (os.environ.get("DESKBOT_WEB_PUBLIC_HOST") or "").strip() or str(
        srv.get("web_public_host") or ""
    ).strip()
    if public:
        host = public
    else:
        host = _prefer_access_host(
            os.environ.get("DESKBOT_SERVER_HOST") or srv.get("host", "127.0.0.1")
        )
    port = int(os.environ.get("DESKBOT_SERVER_PORT") or srv.get("port", 9000))
    return host, port


def _deskbot_ws_default() -> str:
    cfg = load_config()
    host, port = _deskbot_ws_base()
    ws_path = os.environ.get("DESKBOT_WS_PATH") or cfg.get("server", {}).get("ws_path", "/asr_chat")
    if not str(ws_path).startswith("/"):
        ws_path = f"/{ws_path}"
    return f"ws://{host}:{port}{ws_path}"


def _device_pipeline_ws_base() -> str:
    host, port = _deskbot_ws_base()
    return f"ws://{host}:{port}/device_pipeline"


def _camera_view_ws_base() -> str:
    host, port = _deskbot_ws_base()
    return f"ws://{host}:{port}/camera_view"


def _deskbot_http_base() -> str:
    host, port = _deskbot_ws_base()
    return f"http://{host}:{port}"


def load_config():
    return _load_config_yaml(str(CONFIG_PATH))


def _pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm_bytes)
    return buf.getvalue()


def _tts_phoneme_streaming_url(cfg: dict) -> str:
    """由 ``tts.ws_url`` 推导音素对齐 WebSocket 地址（同 host:port，路径为 streaming_phoneme）。"""
    raw = os.environ.get("TTS_WS_URL") or (cfg.get("tts") or {}).get(
        "ws_url", "ws://127.0.0.1:8092/paddlespeech/tts/streaming"
    )
    raw = str(raw).strip()
    if "://" not in raw:
        raw = "ws://" + raw
    scheme, rest = raw.split("://", 1)
    slash = rest.find("/")
    if slash == -1:
        base = f"{scheme}://{rest}"
    else:
        base = f"{scheme}://{rest[:slash]}"
    return f"{base}/paddlespeech/tts/streaming_phoneme"


async def _phoneme_tts_ws_call(ws_url: str, text: str, spk_id: int) -> tuple[bytes, list[dict]]:
    import websockets

    async with websockets.connect(ws_url, max_size=None, open_timeout=60) as ws:
        await ws.send(json.dumps({"task": "tts", "signal": "start"}))
        r0 = json.loads(await ws.recv())
        if r0.get("status") != 0:
            raise RuntimeError(f"PaddleSpeech 握手失败: {r0}")
        session = r0.get("session")
        await ws.send(json.dumps({"text": text, "spk_id": spk_id}))

        segments: list[dict] = []
        while True:
            pkt = json.loads(await ws.recv())
            st = pkt.get("status")
            if st == -1:
                raise RuntimeError(str(pkt.get("message") or pkt))
            if st == 1 and isinstance(pkt.get("segments"), list):
                segments = pkt["segments"]
                continue
            if st == 2:
                break

        await ws.send(
            json.dumps({"task": "tts", "signal": "end", "session": session})
        )
        try:
            await asyncio.wait_for(ws.recv(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass

    pcm = bytearray()
    display: list[dict] = []
    for s in segments:
        b64 = s.get("audio") or ""
        chunk = base64.b64decode(b64) if b64 else b""
        pcm.extend(chunk)
        display.append(
            {
                "phoneme_id": s.get("phoneme_id"),
                "phoneme": s.get("phoneme"),
                "ms": s.get("ms"),
                "pcm_bytes": len(chunk),
            }
        )
    return bytes(pcm), display


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def tcp_alive(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@app.get("/")
def index():
    return redirect(url_for("debug_online"), code=302)


@app.get("/health")
def health_ok():
    return ("ok", 200)


@app.get("/debug/online")
def debug_online():
    return render_template(
        "debug_online.html",
        active_nav="online",
        deskbot_ws_default=_deskbot_ws_default(),
    )


@app.get("/debug/devices")
def debug_devices():
    return render_template(
        "debug_devices.html",
        active_nav="devices",
        device_pipeline_ws_base=_device_pipeline_ws_base(),
        camera_view_ws_base=_camera_view_ws_base(),
        deskbot_http_base=_deskbot_http_base(),
    )


@app.get("/debug/llm")
def debug_llm():
    cfg = load_config()
    llm_cfg = cfg.get("llm", {}) or {}
    return render_template(
        "debug_llm.html",
        active_nav="llm",
        llm_model=llm_cfg.get("model_name", ""),
        llm_base_url=llm_cfg.get("base_url", ""),
        llm_system_prompt=llm_cfg.get("system_prompt", ""),
    )


@app.get("/debug/paddlespeech")
def debug_paddlespeech():
    cfg = load_config()
    tts = cfg.get("tts") or {}
    return render_template(
        "debug_paddlespeech.html",
        active_nav="paddle",
        default_spk=int(tts.get("spk_id", 0)),
        sample_rate=int(tts.get("sample_rate", 24000)),
        phoneme_ws_url=_tts_phoneme_streaming_url(cfg),
    )


@app.get("/debug/simulation")
def debug_simulation():
    cfg = load_config()
    tts = cfg.get("tts") or {}
    return render_template(
        "debug_simulation.html",
        active_nav="sim",
        default_spk=int(tts.get("spk_id", 0)),
        sample_rate=int(tts.get("sample_rate", 24000)),
    )


@app.get("/settings")
def settings():
    return render_template("settings.html", active_nav="settings")


@app.get("/api/config")
def get_config():
    cfg = load_config()
    return jsonify(cfg)


@app.post("/api/paddlespeech/phoneme_tts")
def api_paddlespeech_phoneme_tts():
    """服务端代理调用 ``streaming_phoneme``，返回音素表与整段 WAV（base64）供页面播放。"""
    payload = request.get_json(force=True, silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "空文本"}), 400
    spk_id = int(payload.get("spk_id", 0))
    cfg = load_config()
    ws_url = _tts_phoneme_streaming_url(cfg)
    sr = int((cfg.get("tts") or {}).get("sample_rate", 24000))
    try:
        pcm, display = asyncio.run(_phoneme_tts_ws_call(ws_url, text, spk_id))
    except Exception as exc:  # pragma: no cover
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
    wav = _pcm_to_wav_bytes(pcm, sr)
    return jsonify(
        {
            "ok": True,
            "ws_url_used": ws_url,
            "sample_rate": sr,
            "segments": display,
            "wav_base64": base64.b64encode(wav).decode("ascii"),
            "pcm_total_bytes": len(pcm),
        }
    )


@app.post("/api/config")
def update_config():
    payload = request.get_json(force=True)
    cfg = load_config()

    # 仅更新常用调试项，避免误改全量配置结构
    cfg.setdefault("server", {})
    cfg.setdefault("asr", {})
    cfg.setdefault("tts", {})
    cfg.setdefault("vad", {})

    if "server_port" in payload:
        cfg["server"]["port"] = int(payload["server_port"])
    if "asr_language" in payload:
        cfg["asr"]["language"] = str(payload["asr_language"])
    if "vad_mode" in payload:
        cfg["vad"]["mode"] = int(payload["vad_mode"])
    if "tts_ws_url" in payload:
        cfg["tts"]["ws_url"] = str(payload["tts_ws_url"])
    if "tts_spk_id" in payload:
        cfg["tts"]["spk_id"] = int(payload["tts_spk_id"])

    save_config(cfg)
    return jsonify({"ok": True})


def _beijing_time_str() -> str:
    if ZoneInfo is not None:
        now = _dt.datetime.now(ZoneInfo("Asia/Shanghai"))
    else:
        now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return now.strftime("%Y-%m-%d %H:%M:%S") + " " + weekdays[now.weekday()]


def _resolve_llm_api_key() -> str:
    return (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("DASHSCOPE_API_KEY")
        or os.environ.get("QWEN_API_KEY")
        or ""
    )


_ALLOWED_ROLES = {"system", "user", "assistant"}


@app.post("/api/llm/chat")
def llm_chat():
    payload = request.get_json(force=True, silent=True) or {}
    user_text = str(payload.get("text") or "").strip()
    raw_history = payload.get("history") or []

    if not user_text:
        return jsonify({"ok": False, "error": "空文本"}), 400

    cfg = load_config()
    llm_cfg = cfg.get("llm", {}) or {}
    base_url = llm_cfg.get("base_url")
    model_name = llm_cfg.get("model_name", "qwen-flash")
    default_system_prompt = llm_cfg.get(
        "system_prompt", "你是中文助手，请简洁回答。每次回答不超过50字"
    )
    raw_sys = payload.get("system_prompt")
    if isinstance(raw_sys, str) and raw_sys.strip():
        system_prompt = raw_sys
    else:
        system_prompt = default_system_prompt

    api_key = _resolve_llm_api_key()
    if not api_key or "请替换" in api_key:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "LLM API Key 未配置（环境变量 LLM_API_KEY / DASHSCOPE_API_KEY）",
                }
            ),
            400,
        )

    sys_content = (
        f"{system_prompt}\n当前时间是: {_beijing_time_str()}（北京时间，东八区）"
    )
    px = llm_pb_scenes_prompt_appendix()
    if px:
        sys_content += "\n" + px

    messages = [
        {
            "role": "system",
            "content": sys_content,
        }
    ]
    for item in raw_history:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "")
        if role not in _ALLOWED_ROLES or not content:
            continue
        if role == "system":
            continue
        messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_text})

    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover
        return jsonify({"ok": False, "error": f"openai 未安装: {exc}"}), 500

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
        t0 = time.monotonic()
        completion = client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=float(payload.get("temperature", 0.7)),
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        raw = (completion.choices[0].message.content or "").strip()
        parsed = parse_llm_reply(raw)
        usage = getattr(completion, "usage", None)
        usage_dict = None
        if usage is not None:
            try:
                usage_dict = {
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }
            except Exception:
                usage_dict = None
        return jsonify(
            {
                "ok": True,
                "reply": parsed["reply"],
                "raw": parsed["raw"],
                "actions": parsed["actions"],
                "servo": parsed.get("servo") or [],
                "scenes": parsed.get("scenes") or [],
                "json_ok": parsed["json_ok"],
                "need_reply": parsed.get("need_reply", True),
                "model": model_name,
                "elapsed_ms": elapsed_ms,
                "usage": usage_dict,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@app.get("/api/health")
def health():
    cfg = load_config()
    deskbot_host = os.environ.get("DESKBOT_SERVER_HOST") or cfg.get("server", {}).get("host", "127.0.0.1")
    if deskbot_host == "0.0.0.0":
        deskbot_host = "127.0.0.1"
    deskbot_port = int(os.environ.get("DESKBOT_SERVER_PORT") or cfg.get("server", {}).get("port", 9000))

    tts_url = os.environ.get("TTS_WS_URL") or cfg.get(
        "tts", {}
    ).get("ws_url", "ws://127.0.0.1:8092/paddlespeech/tts/streaming")
    # 简单解析 ws://host:port/path
    tts_host = "127.0.0.1"
    tts_port = 8092
    try:
        remain = tts_url.split("://", 1)[1]
        host_port = remain.split("/", 1)[0]
        tts_host = host_port.split(":")[0]
        tts_port = int(host_port.split(":")[1])
    except Exception:
        pass

    return jsonify(
        {
            "deskbot_server": tcp_alive(deskbot_host, deskbot_port),
            "tts_server": tcp_alive(tts_host, tts_port),
            "deskbot_target": f"{deskbot_host}:{deskbot_port}",
            "tts_target": f"{tts_host}:{tts_port}",
        }
    )


if __name__ == "__main__":
    host = (os.environ.get("DESKBOT_WEB_HOST") or "0.0.0.0").strip()
    port = int(os.environ.get("DESKBOT_WEB_PORT") or "5050")
    app.run(host=host, port=port, debug=True)
