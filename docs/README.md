# 协议文档

面向 **ESP32 固件**、**Web 调试页** 与 **动画 JSON 编辑** 的约定说明。

| 文档 | 读者 | 内容 |
|------|------|------|
| [esp32_playback_protocol.md](./esp32_playback_protocol.md) | 固件 | pb 下行：`pb_start` / `pb_chunk` / `pb_end` / `pb_single`、PCM 二进制、舵机、`pb_ack` |
| [pb_face_bundle_and_shape_protocol.md](./pb_face_bundle_and_shape_protocol.md) | 固件 / 美术 / 服务端 | `face_bundle` JSON、`anim.elements` 图元 shape、音素 offset、编写与热重载 |

**运行时 WebSocket 端点、HTTP API、配置项** 见 [deskbot-server/README.md](../deskbot-server/README.md)。

**TTS 侧车（PaddleSpeech + 音素对齐 WS）** 见 [paddlespeech-server/README.md](../paddlespeech-server/README.md) 与 [paddlespeech-server/docs/PROTOCOL.md](../paddlespeech-server/docs/PROTOCOL.md)。

**环境搭建与一键启动** 见仓库根 [README.md](../README.md)。
