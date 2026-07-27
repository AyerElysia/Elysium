#!/usr/bin/env python
"""下载 Qwen3-VL-Embedding-2B 模型（modelscope 优先，HF 镜像兜底）。"""

from __future__ import annotations

import argparse
import os
import sys


def download_from_modelscope(model_id: str, local_dir: str) -> bool:
    try:
        from modelscope import snapshot_download

        print(f"[modelscope] 下载 {model_id} → {local_dir}")
        snapshot_download(model_id, local_dir=local_dir)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[modelscope] 下载失败: {exc}")
        return False


def download_from_hf(model_id: str, local_dir: str) -> bool:
    try:
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from huggingface_hub import snapshot_download

        print(f"[huggingface] 下载 {model_id} → {local_dir}")
        snapshot_download(model_id, local_dir=local_dir)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[huggingface] 下载失败: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-id", default="Qwen/Qwen3-VL-Embedding-2B")
    parser.add_argument("--local-dir", default="/root/models/Qwen3-VL-Embedding-2B")
    args = parser.parse_args()

    os.makedirs(args.local_dir, exist_ok=True)

    # 已下载则跳过（检查关键文件）
    if os.path.exists(os.path.join(args.local_dir, "config.json")):
        print(f"模型已存在: {args.local_dir}，跳过下载")
        return

    if download_from_modelscope(args.model_id, args.local_dir):
        print("下载完成（modelscope）")
        return
    if download_from_hf(args.model_id, args.local_dir):
        print("下载完成（huggingface）")
        return

    print("所有下载途径均失败", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
