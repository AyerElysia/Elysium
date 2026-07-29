#!/usr/bin/env python
"""下载路由决策模型（ModelScope 优先，HF 镜像兜底）。

默认下载 Qwen3.5-2B（bf16，单分片 4.55GB）。vLLM 启动时现场做 fp8 量化，
所以不需要另外下量化仓库。

几个坑，改之前先看：
  * 仓库名没有 -Instruct 后缀。Qwen3.5 这代的对话模型就叫 Qwen/Qwen3.5-2B，
    写成 Qwen/Qwen3.5-2B-Instruct 会 404（实测 ModelScope 返回 404，
    HF 那边则是一路重定向到登录页，看起来像权限问题，其实是仓库不存在）。
  * ModelScope 这次是通的（Qwen/Qwen3.5-2B 返回 200），所以顺序改成 ModelScope 优先。
    想换回来用 ROUTER_DOWNLOAD_SOURCE=hf,modelscope。
  * huggingface.co 的 resolve/main 在本机被墙，必须走 hf-mirror.com。
    这里强制覆盖 HF_ENDPOINT 而不是 setdefault——继承来的旧值会让下载一直卡住。
  * 「模型已存在」不能只看 config.json：下载中断时 config.json 早就落地了，
    权重却缺一半，等 vLLM 起来才报错。这里按 index.json 的分片清单逐个核对。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_HF_ENDPOINT = "https://hf-mirror.com"


def missing_files(local_dir: str) -> list[str]:
    """返回缺失/明显不完整的文件清单；空列表表示模型可用。"""
    root = Path(local_dir)
    if not root.is_dir():
        return ["<目录不存在>"]

    missing = [
        name
        for name in ("config.json", "tokenizer_config.json")
        if not (root / name).is_file()
    ]
    # 下载中断会留下这些临时文件，留着就说明上次没下完
    missing.extend(sorted(p.name for p in root.glob("*.incomplete")))

    index = root / "model.safetensors.index.json"
    if index.is_file():
        try:
            weight_map = json.loads(index.read_text(encoding="utf-8")).get("weight_map", {})
        except (OSError, ValueError) as exc:  # noqa: BLE001
            return [*missing, f"model.safetensors.index.json 不可读: {exc}"]
        for shard in sorted(set(weight_map.values())):
            path = root / shard
            if not path.is_file() or path.stat().st_size == 0:
                missing.append(shard)
    elif not any(root.glob("*.safetensors")):
        missing.append("*.safetensors")

    return missing


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
        endpoint = os.environ.get("HF_ENDPOINT") or DEFAULT_HF_ENDPOINT
        # 覆盖而非 setdefault：继承来的空值/直连地址会让下载一直重试到超时
        os.environ["HF_ENDPOINT"] = endpoint
        from huggingface_hub import snapshot_download

        print(f"[huggingface] 下载 {model_id} → {local_dir}（endpoint={endpoint}）")
        # huggingface_hub 自带断点续传，中断后重跑会接着下
        snapshot_download(model_id, local_dir=local_dir)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[huggingface] 下载失败: {exc}")
        return False


SOURCES = {"hf": download_from_hf, "modelscope": download_from_modelscope}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-id",
        default=os.environ.get("ROUTER_MODEL_ID", "Qwen/Qwen3.5-2B"),
    )
    parser.add_argument(
        "--local-dir",
        default=os.environ.get("ROUTER_MODEL_PATH", "/root/models/Qwen3.5-2B"),
    )
    parser.add_argument(
        "--source",
        default=os.environ.get("ROUTER_DOWNLOAD_SOURCE", "modelscope,hf"),
        help="下载源顺序，逗号分隔（可选 hf / modelscope）",
    )
    parser.add_argument("--force", action="store_true", help="即使本地完整也重新下载")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只检查本地模型是否完整，不下载（完整退出 0，缺文件退出 1）",
    )
    args = parser.parse_args()

    missing = missing_files(args.local_dir)
    if args.check_only:
        if missing:
            print(f"模型不完整: {args.local_dir}", file=sys.stderr)
            for name in missing:
                print(f"  缺: {name}", file=sys.stderr)
            sys.exit(1)
        print(f"模型完整: {args.local_dir}")
        return

    if not missing and not args.force:
        print(f"模型已存在且完整: {args.local_dir}，跳过下载")
        return
    if missing:
        preview = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        print(f"需要下载（缺 {len(missing)} 项: {preview}）")

    os.makedirs(args.local_dir, exist_ok=True)

    order = [name for name in (s.strip() for s in args.source.split(",")) if name in SOURCES]
    if not order:
        print(f"--source 无有效下载源: {args.source!r}", file=sys.stderr)
        sys.exit(2)

    for name in order:
        if SOURCES[name](args.model_id, args.local_dir):
            still_missing = missing_files(args.local_dir)
            if still_missing:
                # 下载器返回成功但文件没齐，继续换下一个源，别让 vLLM 去撞
                print(f"[{name}] 报告成功但文件仍不完整，缺 {len(still_missing)} 项")
                continue
            print(f"下载完成（{name}）")
            return

    print("所有下载途径均失败", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
