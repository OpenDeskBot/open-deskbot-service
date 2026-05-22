# opendesk-service

ESP32 桌面机器人开源后端：设备经 WebSocket 上传语音，服务端完成 **VAD → ASR → LLM → TTS**，并通过自研 **pb** 协议下发 PCM、屏幕动画与舵机指令；可选 **/camera** 做人脸检测与调试页预览。

```
ESP32  ──WebSocket(opus/pcm16)──▶  deskbot-server (VAD + FunASR + LLM)
                                        │
                                        ▼
                               paddlespeech-server (流式 TTS + 音素)
                                        │
                                        ▼
ESP32  ◀──── pb_* JSON + PCM binary ─────
```

| 目录 | 作用 | 默认端口 |
|------|------|----------|
| [`deskbot-server/`](deskbot-server/) | 主服务：语音链路、人脸、Flask 调试台 | WS `9000`，HTTP `5050` |
| [`paddlespeech-server/`](paddlespeech-server/) | TTS 侧车（PaddleSpeech + 音素对齐） | WS `8092` |
| [`docs/`](docs/) | pb 协议、表情 JSON 等固件/工具约定 | — |

设备接入：`ws://<host>:9000/asr_chat?device_id=<id>`（语音）、`ws://<host>:9000/camera?device_id=<id>`（JPEG）。协议与 API 见 [deskbot-server/README.md](deskbot-server/README.md)。

**License:** [MIT](LICENSE) · [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md)

---

## 运行

### 1. 系统依赖

#### Python

- **Python 3.11**（推荐 3.11.9；`python3.11 --version` 确认）
- 非默认解释器：`PYTHON_BIN=python3.11 ./start.sh`

#### ffmpeg（必填）

语音链路使用 **opus** 上行，服务端需 **ffmpeg** 做转码。安装后执行 `ffmpeg -version` 确认可用。

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y ffmpeg

# CentOS 7 / 旧版 RHEL（需 EPEL）
sudo yum install -y epel-release && sudo yum install -y ffmpeg

# Rocky / Alma / CentOS Stream 8+
sudo dnf install -y epel-release && sudo dnf install -y ffmpeg

# Fedora
sudo dnf install -y ffmpeg

# macOS
brew install ffmpeg

# Windows
winget install ffmpeg
```

未安装 ffmpeg 时 `./start.sh` 会警告；可临时 `SKIP_SYSTEM_CHECK=1 ./start.sh`，但 **opus 语音可能无法正常工作**。

#### /camera 人脸检测（MediaPipe，Linux 必填）

使用 `/camera` 上传 JPEG 做人脸检测时，MediaPipe 需要 **OpenGL ES** 运行时库（`libGLESv2.so.2`）。无图形界面的 Linux 服务器默认可能未安装。

```bash
# Debian / Ubuntu
sudo apt install -y libgles2-mesa libegl1-mesa

# CentOS 7 / 旧版 RHEL
sudo yum install -y mesa-libGLES mesa-libEGL

# Rocky / Alma / CentOS Stream 8+
sudo dnf install -y mesa-libGLES mesa-libEGL
```

安装后可用 `ldconfig -p | grep libGLESv2` 确认。缺失时 **仅 `/camera` 失败**，`/asr_chat` 语音链路不受影响。

---

### 2. 配置 `.env`

在仓库根目录执行（**首次部署必做**）：

```bash
cp deskbot-server/.env.example deskbot-server/.env
```

用编辑器打开 `deskbot-server/.env`，至少修改下表中的字段：

| 变量 | 是否必填 | 说明 |
|------|----------|------|
| **`LLM_API_KEY`** | **必填** | 阿里云 DashScope OpenAI 兼容 API Key；也可用 `DASHSCOPE_API_KEY` 或 `QWEN_API_KEY`（三选一） |
| **`ASR_MODEL_DIR`** | 推荐保留默认 | 本地 ASR 模型目录，默认 `./models/SenseVoiceSmall`（相对 `deskbot-server/`）；`./start.sh` 会在缺失时**自动下载** |
| `DESKBOT_SERVER_HOST` | 可选 | WebSocket 监听地址，默认 `0.0.0.0`（局域网设备可连） |
| `DESKBOT_SERVER_PORT` | 可选 | 主服务端口，默认 `9000` |
| `DESKBOT_WEB_HOST` | 可选 | Flask 调试台监听地址，默认 `0.0.0.0`（局域网可访问） |
| `DESKBOT_WEB_PORT` | 可选 | Flask 调试台端口，默认 `5050` |
| `TTS_WS_URL` | 可选 | TTS WebSocket；默认由 `config.yaml` 指向本机 `8092` |
| `DESKBOT_WEB_PUBLIC_HOST` | 可选 | 调试页生成的对外 WS/HTTP 主机名（多网卡/NAT 时填写局域网 IP） |

`.env` 含真实密钥，**勿提交到 git**（已在 `.gitignore` 中忽略）。

---

### 3. 启动

仓库根目录一键启动（创建 venv、检查/下载模型、启动 TTS + 主服务 + **调试台**）：

```bash
chmod +x start.sh    # 首次
./start.sh
```

`./start.sh` 会自动：

1. 若不存在则 `cp deskbot-server/.env.example` → `deskbot-server/.env`（仍需你填写 `LLM_API_KEY`）
2. 若缺失则下载 **SenseVoiceSmall** ASR 模型（约 900MB）到 `deskbot-server/models/SenseVoiceSmall/`
3. 若缺失则下载 **MediaPipe** 人脸模型（约 3.6MB）到 `deskbot-server/models/mediapipe/`
4. 默认启动 **Flask 调试台**（`DESKBOT_START_WEB=1`）

| 服务 | 地址 |
|------|------|
| 主链路（ESP32） | `ws://<主机IP>:9000/asr_chat?device_id=<id>` |
| TTS | 监听 `0.0.0.0:8092`；本机 `ws://127.0.0.1:8092/paddlespeech/tts/streaming` |
| 音素 TTS | 本机 `ws://127.0.0.1:8092/paddlespeech/tts/streaming_phoneme` |
| **调试台（默认开启）** | `http://<主机局域网IP>:5050/`（服务监听 `0.0.0.0:5050`） |

常用环境变量：

| 变量 | 说明 |
|------|------|
| `SKIP_SETUP=1` | venv 已就绪，跳过依赖安装 |
| `FAST_START=1` | 跳过 pip 重装 |
| `DESKBOT_START_WEB=0` | 不启动 Flask 调试台 |
| `SKIP_MODEL_DOWNLOAD=1` | 不自动下载模型（需已手动放到 `models/`） |
| `SKIP_SYSTEM_CHECK=1` | 跳过 ffmpeg 缺失警告 |

仅跑主服务（不经过根 `start.sh`）：`cd deskbot-server && ./start.sh`（需另起 [paddlespeech-server](paddlespeech-server/README.md) 或配置 `TTS_WS_URL`）。

---

### 4. 手动下载模型（可选）

一般无需手动执行；仅在 `SKIP_MODEL_DOWNLOAD=1` 或离线环境时参考：

```bash
cd deskbot-server && source .venv/bin/activate
pip install -U modelscope && python scripts/download_model.py
mkdir -p models/mediapipe
curl -L --fail -o models/mediapipe/face_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
deactivate && cd ..
```

动画数据已在 [`deskbot-server/data/`](deskbot-server/data/)。

---

## 开发

```
opendesk-service/
├── deskbot-server/       # src/deskbot_server/
├── paddlespeech-server/
├── docs/
└── start.sh
```

```bash
cd deskbot-server && source .venv/bin/activate
pip install -e ".[dev]"

ruff check src
PYTHONPATH=src pytest tests/ -q
```

本地联调（服务已启动后）：

```bash
python tools/test_client.py --ws-url ws://127.0.0.1:9000/asr_chat --input-wav demo_16k_mono.wav
python tools/live_mic_client.py --ws-url ws://127.0.0.1:9000/asr_chat
```

| 文档 | 说明 |
|------|------|
| [deskbot-server/README.md](deskbot-server/README.md) | WebSocket / HTTP API、配置 |
| [deskbot-server/docs/ARCHITECTURE.md](deskbot-server/docs/ARCHITECTURE.md) | 分层架构 |
| [docs/esp32_playback_protocol.md](docs/esp32_playback_protocol.md) | pb 下行播放 |
| [docs/pb_face_bundle_and_shape_protocol.md](docs/pb_face_bundle_and_shape_protocol.md) | 表情 JSON、图元 |
| [paddlespeech-server/README.md](paddlespeech-server/README.md) | TTS 侧车 |
| [paddlespeech-server/docs/PROTOCOL.md](paddlespeech-server/docs/PROTOCOL.md) | `streaming_phoneme` 协议 |
