# paddlespeech-server

PaddleSpeech **流式 TTS 侧车服务**：在官方 [`paddlespeech_server`](https://github.com/PaddlePaddle/PaddleSpeech) 之上，额外提供音素对齐 WebSocket，供 [deskbot-server](../deskbot-server/) 将 TTS 与口型 / 舵机动画同步。

环境准备与一键启动见仓库根目录 [README.md](../README.md)。

## 特性

| 端点 | 说明 |
|------|------|
| `/paddlespeech/tts/streaming` | 官方 PaddleSpeech 流式 TTS（与 upstream 行为一致） |
| `/paddlespeech/tts/streaming_phoneme` | **本仓库扩展**：整句合成后按音素序列均分 PCM，返回 `phoneme_id` / `phoneme` / `ms` / `audio` |

音素对齐为**启发式均分**（流式 voc 不暴露逐音素真实时长），详见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

## 目录

```
paddlespeech-server/
├── src/paddlespeech_server/   # Python 包（phoneme 切分 + WS 路由 + 启动入口）
├── conf/                      # PaddleSpeech yaml（默认 8092 / online-onnx）
├── docs/PROTOCOL.md           # streaming_phoneme 协议
├── tools/                     # 本地联调客户端
├── server_extended.py         # 兼容旧启动方式（等价 python -m paddlespeech_server）
├── start.sh                   # SETUP_ONLY / SKIP_SETUP / FAST_START
└── start-local.sh             # 等价 SKIP_SETUP=1 ./start.sh
```

## 要求

| 项 | 建议 |
|----|------|
| Python | **3.11**（与仓库 CI、根 `start.sh` 一致；3.12 上 Paddle 生态风险更高） |
| OS | Linux / macOS 优先；Windows 上 PaddleSpeech 支持有限 |
| 网络 | 首次启动会从 Paddle 模型源下载 ONNX 权重（体积较大） |

本目录**不包含** PaddleSpeech 上游源码；运行时通过 pip 安装 `paddlespeech==1.5.0`。如需阅读 upstream 实现，见 [PaddleSpeech GitHub](https://github.com/PaddlePaddle/PaddleSpeech) 或本地 venv 的 `site-packages/paddlespeech/`。

## 快速开始

### 与 deskbot-server 一起启动（推荐）

在仓库根目录：

```bash
./start.sh
```

### 仅启动 TTS

```bash
cd paddlespeech-server
./start.sh                    # 创建 .venv、安装依赖并启动
SKIP_SETUP=1 ./start.sh       # venv 已就绪时只启动
SETUP_ONLY=1 ./start.sh       # 只装依赖，不启动进程
./start-local.sh              # 同 SKIP_SETUP=1 ./start.sh
```

启动成功后默认监听 **8092**：

| 服务 | 地址 |
|------|------|
| 官方流式 TTS | 服务监听 `0.0.0.0:8092`；本机 `ws://127.0.0.1:8092/paddlespeech/tts/streaming` |
| 音素对齐 TTS | 本机 `ws://127.0.0.1:8092/paddlespeech/tts/streaming_phoneme` |

### 环境变量

复制 `cp .env.example .env` 后按需修改（`.env` 已 gitignore）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `PADDLE_VENV` | `./.venv` | 虚拟环境路径 |
| `PADDLESPEECH_CONFIG` | `./conf/tts_online_application.yaml` | PaddleSpeech 配置 |
| `PIP_INDEX_URL` | 清华镜像（可改） | 加速大 wheel 下载 |

`start.sh` 还支持：`PYTHON_BIN`、`FAST_START`、`SKIP_SETUP`、`SETUP_ONLY`（语义同 [deskbot-server/start.sh](../deskbot-server/start.sh)）。

## 配置

[`conf/tts_online_application.yaml`](conf/tts_online_application.yaml) 为服务主配置：

- `host` / `port`：默认 `0.0.0.0:8092`
- `engine_list: ['tts_online-onnx']`：CPU ONNX 流式中文 TTS
- `tts_online-onnx.lang: zh`：与 deskbot-server 默认中文链路一致

修改引擎、采样率或 block 参数时，建议同步检查 deskbot-server 的 `config.yaml` `tts` 段。

## 命令行入口（venv 激活后）

```bash
cd paddlespeech-server
source .venv/bin/activate          # Windows Git Bash: source .venv/Scripts/activate

paddlespeech-server --config_file conf/tts_online_application.yaml
python -m paddlespeech_server --config_file conf/tts_online_application.yaml
python server_extended.py --config_file conf/tts_online_application.yaml
```

## 联调工具

```bash
cd paddlespeech-server
source .venv/bin/activate
python tools/test_phoneme_client.py --text "你好，我是桌面机器人。"
```

见 [tools/README.md](tools/README.md)。

## 开发

```bash
cd paddlespeech-server
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"

ruff check src tests
python -m compileall -q src
PYTHONPATH=src pytest tests/ -q
```

CI 对 `src/` 做 ruff + compileall + 纯逻辑单测；**不**在 GitHub Actions 里安装完整 Paddle 栈（体积与耗时过大）。

## 与 deskbot-server 集成

deskbot-server 通过 `config.yaml` 的 `tts.ws_url` 连接官方流式端点；音素动画链路会自动推导 `streaming_phoneme` 地址（见 `deskbot_server.tts.phoneme.phoneme_streaming_url_from_tts_ws`）。

调试页：`DESKBOT_START_WEB=1 ../start.sh` 后访问 `http://127.0.0.1:5050/debug/paddlespeech`。

## 协议文档

- [docs/PROTOCOL.md](docs/PROTOCOL.md) — `streaming_phoneme` 消息格式与状态码
- [docs/esp32_playback_protocol.md](../docs/esp32_playback_protocol.md) — 设备侧 pb 播放（由 deskbot-server 下发）

## 版本

当前组件版本见 [`.paddlespeech-server_version`](.paddlespeech-server_version)（扩展层；底层 PaddleSpeech 版本见 `requirements.txt` / `pyproject.toml`）。

## License

[MIT](../LICENSE)
