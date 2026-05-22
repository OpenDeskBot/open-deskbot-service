# deskbot-server

ESP32 语音机器人后端：VAD → FunASR → LLM → PaddleSpeech TTS，经 **pb 协议** 向设备下发 PCM + 屏幕动画 + 舵机指令；可选 `/camera` 人脸检测与 Flask 调试台。

环境准备与一键启动见仓库根目录 [README.md](../README.md)。

## 目录

```
deskbot-server/
├── src/deskbot_server/     # Python 包（pipeline / ws / pb / web …）
├── config.yaml         # 默认配置
├── data/               # pb 动画与场景 JSON（运行时必需）
├── prompts/            # LLM system prompt
├── scripts/            # download_model.py
├── tools/              # 本地联调客户端
└── start.sh            # SETUP_ONLY / SKIP_SETUP / FAST_START
```

## 配置

1. 在仓库根目录：`cp deskbot-server/.env.example deskbot-server/.env`，填写 **`LLM_API_KEY`**（必填）；字段说明见根 [README.md](../README.md#2-配置-env)。
2. **`ASR_MODEL_DIR`** 建议保持默认 `./models/SenseVoiceSmall`；根目录 `./start.sh` 会在缺失时自动下载模型。
3. `config.yaml` 要点：
   - 本地 `asr.model_dir` 留空即可，以 `.env` 的 `ASR_MODEL_DIR` 为准
   - `tts.ws_url` 指向 Paddle TTS（默认 `ws://127.0.0.1:8092/paddlespeech/tts/streaming`）
   - `server.asr_chat_device_pb_only: true` 时设备端仅收 `pb_*` + PCM（无 `ready`/阶段 JSON）

启动：

```bash
./start.sh                              # 装依赖并启动
SKIP_SETUP=1 ./start.sh                 # venv 已就绪时只启动
LLM_API_KEY=xxx FAST_START=1 ./start.sh # 跳过 pip 重装
```

仓库根 `./start.sh` 会同时启动 `paddlespeech-server` 与本服务。

## WebSocket 端点

| 路径 | 方向 | 说明 |
|------|------|------|
| `/asr_chat?device_id=` | 双向 | 主语音链路（音频上行 → ASR/LLM/TTS 下行） |
| `/camera?device_id=` | ESP32→服务端 | JPEG 帧上行；服务端做人脸检测 |
| `/camera_view?device_id=` | 订阅 | 调试页预览原始 JPEG + 检测元数据 |
| `/device_pipeline?role=subscriber&device=` | 订阅 | 流水线事件、人脸关键点（调试页） |

`device_id` 也兼容查询参数 `device` / `deviceid` / `id`。生产环境**务必**携带 `device_id`，否则不会进入设备表与 pipeline 广播。

### `/asr_chat` 上行（ESP32 → 服务端）

- **二进制**：opus 或 pcm16 音频帧（由 `config.yaml` `audio.input_codec` 决定）
- **JSON**：
  - `{"type":"audio","codec":"pcm16","data":"<base64>"}`
  - `{"type":"flush"}` — 结束当前语音段
  - `{"type":"user_text","text":"..."}` — 跳过 ASR 直接 LLM
  - `{"type":"ping"}` / `{"type":"pb_ack",...}` — 心跳与播放回压

### `/asr_chat` 下行（服务端 → ESP32）

默认 **`asr_chat_device_pb_only: true`** 时，设备侧主要收到：

- **`pb_start` / `pb_chunk` / `pb_end` / `pb_single`** + 紧随的 **binary s16le PCM**
- 可选 `pb_cancel`

完整字段、舵机、`action` 语义见 [docs/esp32_playback_protocol.md](../docs/esp32_playback_protocol.md)。

关闭 pb-only 时还会下发 `ready`、`asr_*`、`llm_text` 等阶段 JSON（调试协议，见 `config.yaml`）。

#### `face_info`（可选）

当 `server.send_face_info_to_asr_chat: true`（或 `DESKBOT_SEND_FACE_INFO=1`）且 `/camera` 检测到人脸时，向同 `device_id` 的 `/asr_chat` 下发朝向信息，供机器人舵机跟随：

```json
{
  "type": "face_info",
  "device_id": "deskbot_1",
  "yaw_deg": -12.3,
  "pitch_deg": 5.7,
  "is_frontal": true,
  "nose": {"x": 154.2, "y": 130.1},
  "frame_w": 320,
  "frame_h": 240
}
```

`pitch_deg` 在嘴角关键点缺失时可能省略；未检测到人脸时不发送。

### `/camera`

- URL：`ws://host:9000/camera?device_id=<id>`（**必填**）
- 上行：JPEG（或 PNG）二进制帧
- 下行：连接时 `ready`；可选 `ping`→`pong`。**不下发**每帧 `camera_ack`（省 ESP 收发与电量）

本地无 ESP32 时可使用 `tools/camera_test_client.py` 推图测试。

## HTTP API（与 WS 同端口 9000）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | `{"ok":true}` |
| GET | `/api/devices` | 在线设备列表 |
| GET | `/api/pipeline_recent?device_id=&limit=` | 最近 pipeline 事件 |
| GET | `/api/asr_auto_reply` | 是否启用自动应答 |
| GET/POST | `/api/device_servo` | 向设备下发 `pb_single` 舵机指令 |
| GET | `/api/device_pb_scenes` | 内置场景 id 列表 |

## Flask 调试台（`0.0.0.0:5050`，局域网可访问）

```bash
../start.sh                        # 根目录默认已启调试台
# 或单独：
DESKBOT_WEB_HOST=0.0.0.0 python -m deskbot_server.web
```

| 页面 | 用途 |
|------|------|
| `/debug/devices` | 设备列表、流水线、摄像头预览、场景下发 |
| `/debug/simulation` | 音素 TTS、pb 动画预览 |
| `/debug/llm` | LLM 试跑 |
| `/debug/paddlespeech` | TTS 试跑 |

## 本地联调工具

见 [tools/README.md](tools/README.md)。常用：

```bash
source .venv/bin/activate
python tools/test_client.py \
  --ws-url ws://127.0.0.1:9000/asr_chat \
  --input-wav demo_16k_mono.wav
```

输入 wav 须为 **16kHz / mono / 16-bit PCM**。

## 相关文档

- [docs/README.md](../docs/README.md) — 协议索引
- [docs/esp32_playback_protocol.md](../docs/esp32_playback_protocol.md) — pb 传输与舵机
- [docs/pb_face_bundle_and_shape_protocol.md](../docs/pb_face_bundle_and_shape_protocol.md) — 表情 JSON 与图元
