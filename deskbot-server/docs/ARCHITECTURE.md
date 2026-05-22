# deskbot-server 分层架构（Hexagonal / 端口-适配器）

## 目录与依赖规则

当前 **实际** 包布局（`domain/`、`interfaces/` 为规划名，尚未物理迁移）：

```
src/deskbot_server/
├── core/                      # 配置、类型、端口 Protocol（零外部 IO）
│   ├── settings.py            # AppSettings
│   ├── types.py               # ChatTurnResult
│   └── ports/                 # AsrPort, LlmPort, TtsPort, DownlinkPort, PipelineEventsPort
├── application/               # 用例编排（不 import ws 处理器）
│   ├── chat_service.py        # ChatService（组合 ASR+LLM+TTS 端口）
│   ├── chat_flow.py           # run_chat_turn / publish_chat_turn
│   ├── camera_broker.py       # CameraImageBroker（JPEG pub/sub，注入 send_fn）
│   ├── camera_frame.py        # 人脸几何分析、face_info 组装
│   └── face_detector.py       # MediaPipe CameraFaceDetector
├── infrastructure/            # 端口实现 + Composition Root 装配
│   ├── bootstrap.py           # build_chat_service(config)
│   ├── asr/funasr.py
│   ├── llm/openai_compat.py
│   ├── tts/paddle_phoneme.py
│   └── ws/downlink_adapter.py # WsDownlinkAdapter, WsPipelineEventsAdapter
├── pb/                        # pb 协议领域（图元、脸包、组帧；无 ws 依赖）
│   ├── shapes.py
│   ├── face_bundle.py
│   ├── phoneme_anim.py
│   ├── servo_pcm.py
│   ├── wire.py
│   ├── scenes.py
│   ├── anim_defaults.py       # 公共 API 聚合（向后兼容 re-export）
│   └── bundle.py
├── vision/                    # 镜头几何、畸变校正
├── ws/                        # WebSocket / HTTP 协议薄层（入口）
│   ├── router.py
│   ├── asr_chat.py / asr_chat_hub.py
│   ├── camera.py
│   ├── device_pipeline.py
│   ├── http_api.py
│   ├── ws_send.py
│   └── pb_idle_registry.py    # ws_send ↔ AsrChatHub 空闲打盹注册表
├── web/                       # Flask 调试台（独立进程 python -m deskbot_server.web）
├── pipeline/                  # 向后兼容别名（flow → application.chat_flow）
├── llm/utils.py               # LLM 解析、prompt 附录（web 与 infrastructure 共用）
├── tts/phoneme.py             # Paddle 音素 TTS 客户端
├── main.py                    # Composition Root（装配 + websockets.serve）
└── …                          # config, constants, settings, paths, util 等横切模块
```

**逻辑分层（概念上）：**

```
ws / web / main  →  application  →  core  ←  infrastructure
                        ↓
                   pb / vision
```

**依赖方向（严禁违反）：**

- `application` **不得**直接依赖 `ws.*` 处理器（经 `DownlinkPort` / 注入 `send_fn` 解耦）
- `core` **不得**依赖 funasr、openai、websockets
- `pb`、`vision` **不得**依赖 `ws`、`application`

## Composition Root（main.py）

`main.py` 负责装配，典型顺序：

1. `build_chat_service(config)` — `infrastructure/bootstrap.py` 创建 `ChatService` + 适配器
2. `AsrChatHub`、`DevicePipelineBroker`、`DeviceRegistry`
3. `set_pb_idle_hub(asr_chat_hub)` — 供 `ws_send` 刷新空闲打盹计时
4. `CameraImageBroker(send_fn=_safe_send)` — 显式注入 WebSocket 发送函数
5. `websockets.serve(..., handle_client, process_request=http_handler)`

## 主链路

### 语音对话

```
/asr_chat → ConnectionSession → ChatService.asr()
         → run_chat_turn(WsDownlinkAdapter, ChatService)   # application/chat_flow
              → LlmPort → TtsPort
              → pb.wire.build_pb_wire_pairs()
              → DownlinkPort.send_pb_wire()
```

`pipeline/flow.py` 仅为薄转发，指向 `application/chat_flow`。

### 摄像头

```
/camera → CameraFaceDetector.detect_5pt()     [application/face_detector]
       → analyze_face_detection()              [application/camera_frame]
       → CameraImageBroker.publish()           [application/camera_broker]
       → build_face_info_message() → AsrChatHub [ws/camera.py 编排]
```

## 向后兼容

| 旧 import | 现状 |
|-----------|------|
| `BotPipeline` | `ChatService` 别名（`pipeline/pipeline.py`） |
| `ChatService.from_config` | 已移除；改用 `infrastructure.bootstrap.build_chat_service` |
| `pb.anim_defaults.*` | 仍可用；实现分布在 `pb/shapes`、`face_bundle` 等 |
| `from ws.camera import CameraImageBroker` | `ws/camera.py` 的 `__all__` re-export，实现仍在 `application` |

## 测试

```bash
cd deskbot-server
PYTHONPATH=src pytest tests/ -q
```

## 后续（可选重构）

- 将 `pb/`、`vision/` 物理迁入 `domain/`（或仅文档统一命名，不改 import）
- 将 `ws/`、`web/` 迁入 `interfaces/`（同上）
- `http_api.py` 拆薄：HTTP 路由留 ws 层，业务命令下沉 application
- 逐步收敛顶层 `settings.py`、`pipeline/` 等兼容层
