#!/usr/bin/env python3
"""deskbot-server `/camera` 烟囱客户端：把本地 JPEG/PNG（或一组图）按指定 FPS
推到 ``ws://host:9000/camera?device_id=...``（服务端不再下发每帧 ``camera_ack``）。

用法示例：

    # 把 ./photo.jpg 反复推 30 帧 @5fps
    python camera_test_client.py --image photo.jpg --device-id deskbot_dev --fps 5 --frames 30

    # 指定一个目录，按字典序循环推
    python camera_test_client.py --image-dir ./frames --device-id deskbot_dev --fps 8

    # 也支持把整段 mjpeg 拆成一个目录（ffmpeg -i in.mp4 -vf fps=5 frames/%04d.jpg）

需要的依赖：``websockets``（项目已有）。不需要 mediapipe / Pillow。
"""
import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import List

import websockets


SUPPORTED_EXT = (".jpg", ".jpeg", ".png")


def _list_images(image: str | None, image_dir: str | None) -> List[Path]:
    if image:
        p = Path(image)
        if not p.is_file():
            raise FileNotFoundError(f"图片不存在: {p}")
        return [p]
    if image_dir:
        d = Path(image_dir)
        if not d.is_dir():
            raise FileNotFoundError(f"目录不存在: {d}")
        items = sorted(
            f
            for f in d.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_EXT
        )
        if not items:
            raise FileNotFoundError(
                f"目录里没有 {SUPPORTED_EXT} 文件: {d}"
            )
        return items
    raise ValueError("--image 或 --image-dir 至少要给一个")


async def _recv_loop(ws):
    try:
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                continue
            try:
                d = json.loads(msg)
            except Exception:
                print(f"[server] (non-json) {msg!r}")
                continue
            t = d.get("type")
            if t == "ready":
                print(f"[server] ready: {d}")
            elif t == "camera_ack":
                print(f"[server] (legacy camera_ack) {d}")
            elif t == "error":
                print(f"[server][error] {d}")
            else:
                print(f"[server] {d}")
    except websockets.exceptions.ConnectionClosed as e:
        print(f"[server] connection closed: {e}")


async def run(args):
    images = _list_images(args.image, args.image_dir)
    print(
        f"准备推 {args.frames} 帧 @ {args.fps} fps，"
        f"循环 {len(images)} 张图：{[p.name for p in images[:5]]}"
        + (" ..." if len(images) > 5 else "")
    )

    interval = 1.0 / max(0.1, args.fps)
    url = args.ws_url
    if "?" in url:
        sep = "&"
    else:
        sep = "?"
    url = f"{url}{sep}device_id={args.device_id}"

    async with websockets.connect(url, max_size=None) as ws:
        recv_task = asyncio.create_task(_recv_loop(ws))
        try:
            for i in range(args.frames):
                p = images[i % len(images)]
                buf = p.read_bytes()
                t0 = time.monotonic()
                await ws.send(buf)
                rt = (time.monotonic() - t0) * 1000.0
                print(
                    f"[push] frame={i + 1:>4} file={p.name} "
                    f"size={len(buf)} send_ms={rt:.1f}"
                )
                await asyncio.sleep(interval)
            # 给服务端一点时间回最后几个 ack
            await asyncio.sleep(0.5)
        finally:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass


def main():
    parser = argparse.ArgumentParser(
        description="deskbot-server /camera 烟囱客户端：推 JPEG → 看 5 点检测结果",
    )
    parser.add_argument(
        "--ws-url",
        default="ws://127.0.0.1:9000/camera",
        help="deskbot-server /camera WebSocket 地址",
    )
    parser.add_argument(
        "--device-id",
        required=True,
        help="自动作为 URL ?device_id= 拼接（必填）",
    )
    parser.add_argument(
        "--image",
        help="单张 JPEG/PNG 路径，会被反复推 --frames 帧",
    )
    parser.add_argument(
        "--image-dir",
        help="目录，按字典序循环推（适合预先 ffmpeg 拆好的帧序列）",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=10,
        help="一共推多少帧（默认 10）",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=5.0,
        help="推送速率（默认 5fps）",
    )
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
