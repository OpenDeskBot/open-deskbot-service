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

## 运行（Ubuntu）

以下说明面向 **Ubuntu 22.04 / 24.04 LTS（amd64）**。默认使用 **CPU** 推理（`USE_CPU_TORCH=1`），无需 NVIDIA 显卡。

### 电脑配置建议

| 项目 | 最低 | 推荐 | 说明 |
|------|------|------|------|
| **CPU** | 4 核 x86_64 | 8 核及以上 | 同时跑本地 ASR（FunASR）、TTS（PaddleSpeech ONNX）与人脸推理 |
| **内存** | 8 GB | 16 GB | 两个 Python venv + 模型加载后占用较高 |
| **磁盘** | 10 GB 可用 | 20 GB+ | 含双 venv、ASR/TTS/人脸模型与 pip 缓存；首次 `./start.sh` 会联网下载 |
| **网络** | 首次必需 | 稳定外网 | 下载 pip 包、SenseVoiceSmall（约 900MB）、Paddle TTS 权重；对话需访问 DashScope |
| **GPU** | 不需要 | — | 默认 CPU 版 torch；有 CUDA 可自行改 `USE_CPU_TORCH=0`（非文档重点） |

首次完整启动后，仓库内典型占用：**deskbot-server** 与 **paddlespeech-server** 各一套 `.venv`（合计约数 GB）+ `deskbot-server/models/`（ASR 约 900MB）+ Paddle TTS ONNX 权重（体积较大，在 paddlespeech venv 缓存中）。启用 `/camera` 人脸身份时，还会首次下载 InsightFace `buffalo_s`（约 120MB，到 `~/.insightface/`）。

---

### 快速开始

#### 1. 安装系统依赖

```bash
sudo apt update
sudo apt install -y \
  python3.11 python3.11-venv python3.11-dev \
  ffmpeg curl git \
  libgles2-mesa libegl1-mesa
```

| 包 | 用途 |
|----|------|
| `python3.11` + `venv` | 创建两个独立虚拟环境（与 CI 一致，勿用 3.12 跑 Paddle） |
| `ffmpeg` | **必填**：ESP32 **opus** 上行转码；缺失时语音链路异常 |
| `libgles2-mesa` / `libegl1-mesa` | **用 `/camera` 时必填**：MediaPipe 需要 `libGLESv2.so.2`；仅语音可暂不装 |
| `curl` / `git` | 模型下载与克隆仓库 |

非默认 Python：`PYTHON_BIN=python3.11 ./start.sh`。

#### 2. 配置密钥

```bash
cd opendesk-service          # 仓库根目录
cp deskbot-server/.env.example deskbot-server/.env
# 编辑 deskbot-server/.env，填写 LLM_API_KEY=sk-...
```

#### 3. 一键启动

```bash
chmod +x start.sh
./start.sh
```

脚本会自动：创建 **deskbot-server** 与 **paddlespeech-server** 的 venv 并安装依赖（pip 默认清华源）、检查/下载 ASR 与人脸模型、启动 TTS（`8092`）+ 主服务（`9000`）+ Flask 调试台（`5050`）。

| 服务 | 地址 |
|------|------|
| 主链路（ESP32） | `ws://<主机局域网IP>:9000/asr_chat?device_id=<id>` |
| 摄像头 | `ws://<主机IP>:9000/camera?device_id=<id>` |
| TTS（本机） | `ws://127.0.0.1:8092/paddlespeech/tts/streaming` |
| 音素 TTS（口型同步） | `ws://127.0.0.1:8092/paddlespeech/tts/streaming_phoneme` |
| 调试台 | `http://<主机局域网IP>:5050/` |

二次启动（依赖已装好）可加速：

```bash
SKIP_SETUP=1 FAST_START=1 ./start.sh
```

| 环境变量 | 说明 |
|----------|------|
| `SKIP_SETUP=1` | 跳过 venv/依赖安装 |
| `FAST_START=1` | 跳过 pip 重装 |
| `DESKBOT_START_WEB=0` | 不启动 Flask 调试台 |
| `SKIP_MODEL_DOWNLOAD=1` | 不下载模型（需已放到 `deskbot-server/models/`） |
| `SKIP_SYSTEM_CHECK=1` | 跳过 ffmpeg 等缺失警告（不推荐） |

仅跑主服务：`cd deskbot-server && ./start.sh`（需另起 [paddlespeech-server](paddlespeech-server/README.md) 或改 `TTS_WS_URL`）。

---

### 关键配置

配置分两层：**环境变量**（`deskbot-server/.env`，含密钥）与 **业务默认值**（`deskbot-server/config.yaml`，可热改部分项）。

#### `deskbot-server/.env`（优先改这里）

| 变量 | 必填 | 说明 |
|------|------|------|
| **`LLM_API_KEY`** | **是** | 阿里云 DashScope OpenAI 兼容 Key；也可用 `DASHSCOPE_API_KEY` / `QWEN_API_KEY` |
| **`ASR_MODEL_DIR`** | 建议默认 | 本地 ASR 目录，默认 `./models/SenseVoiceSmall`；缺失时 `./start.sh` 自动下载 |
| `DESKBOT_SERVER_HOST` / `PORT` | 否 | 主 WebSocket，默认 `0.0.0.0:9000` |
| `DESKBOT_WEB_HOST` / `PORT` | 否 | 调试台，默认 `0.0.0.0:5050` |
| `DESKBOT_WEB_PUBLIC_HOST` | 否 | 多网卡/NAT 时填局域网 IP，调试页生成的设备 WS 地址才正确 |
| `TTS_WS_URL` | 否 | 默认连本机 `8092`；TTS 跑在别的机器时改此项 |
| `DESKBOT_ASR_CHAT_DEVICE_PB_ONLY` | 否 | `1`（默认）：设备只收 `pb_*` + PCM，无阶段 JSON |
| `DESKBOT_SEND_FACE_INFO` | 否 | `1` 时向 `/asr_chat` 转发 `face_info`（舵机跟随，与 pb-only 互斥） |

`.env` 勿提交 git。

#### `deskbot-server/config.yaml`（行为与算法）

| 区块 | 常用项 | 说明 |
|------|--------|------|
| `audio` | `input_codec: opus` | 与 ESP32 上行编码一致；调试可改 `pcm16` |
| `asr` | `model_dir` 留空 | 以 `.env` 的 `ASR_MODEL_DIR` 为准 |
| `llm` | `model_name: qwen-flash` | DashScope 模型名；`system_prompt_file` 为人设 |
| `tts` | `ws_url`、`pb_face_bundle_json` | TTS 地址与默认表情动画 JSON |
| `server` | `asr_chat_device_pb_only` | 生产设备下行协议开关 |
| `server` | `pb_idle_snore_sec` | 空闲多久自动打盹场景 |
| `camera_face` | 检测阈值、`face_embedding_enabled` | `/camera` 人脸检测与身份识别；数据在 `data/camera_face.json` 可持久化 |

更细的 WebSocket / HTTP API 见 [deskbot-server/README.md](deskbot-server/README.md)。

---

### 手动下载模型（可选）

一般无需执行；离线或 `SKIP_MODEL_DOWNLOAD=1` 时：

```bash
cd deskbot-server && source .venv/bin/activate
pip install -U modelscope && python scripts/download_model.py
mkdir -p models/mediapipe
curl -L --fail -o models/mediapipe/face_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
deactivate && cd ..
```

表情与场景 JSON 已随仓库在 [`deskbot-server/data/`](deskbot-server/data/)。

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
