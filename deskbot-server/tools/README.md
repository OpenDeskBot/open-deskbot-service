# 本地调试工具

在 `deskbot-server/.venv` 激活后运行：

```bash
# 推送 wav 测 /asr_chat 全链路
python tools/test_client.py --ws-url ws://127.0.0.1:9000/asr_chat --input-wav demo_16k_mono.wav

# 本机麦克风
python tools/live_mic_client.py --ws-url ws://127.0.0.1:9000/asr_chat

# 推图片测 /camera
python tools/camera_test_client.py --ws-url ws://127.0.0.1:9000/camera --image-dir ./samples
```
