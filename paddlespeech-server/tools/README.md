# 联调工具

| 脚本 | 说明 |
|------|------|
| [test_phoneme_client.py](./test_phoneme_client.py) | 连接 `streaming_phoneme`，打印分片摘要并可导出 WAV |

前置：本目录上级已 `./start.sh` 或 `./start-local.sh`，且 `.venv` 已安装依赖（含 `websockets`，由 paddlespeech 间接带入；若缺失可 `pip install websockets`）。

```bash
cd paddlespeech-server
source .venv/bin/activate
python tools/test_phoneme_client.py --text "测试一下"
```
