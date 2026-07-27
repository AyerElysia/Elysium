"""视觉嵌入服务冒烟测试。

验证文本与图像在同一语义空间对齐：
- 与图像语义一致的文本 → cosine 高
- 与图像语义无关的文本 → cosine 低

用法（服务已启动）：
    python smoke_test.py --image /path/to/meme.png \
        --match "俏皮地吐舌头卖萌" \
        --mismatch "严肃的商务会议报表"
"""

from __future__ import annotations

import argparse
import base64
import sys

import numpy as np
import requests


def embed(endpoint: str, **payload) -> np.ndarray:
    resp = requests.post(endpoint, json=payload, timeout=120)
    resp.raise_for_status()
    data = resp.json()["data"][0]["embedding"]
    return np.asarray(data, dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8848/v1/embeddings")
    parser.add_argument("--image", required=True, help="表情包图片路径")
    parser.add_argument("--match", required=True, help="与图像语义一致的描述")
    parser.add_argument("--mismatch", required=True, help="与图像语义无关的描述")
    parser.add_argument("--margin", type=float, default=0.05, help="match 应比 mismatch 高出至少这么多")
    args = parser.parse_args()

    # 图像转 base64
    with open(args.image, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    print("嵌入图像 ...")
    img_vec = embed(args.endpoint, image=image_b64)
    print(f"  维度: {len(img_vec)}")

    print("嵌入文本 ...")
    match_vec = embed(args.endpoint, input=args.match)
    mismatch_vec = embed(args.endpoint, input=args.mismatch)

    sim_match = cosine(img_vec, match_vec)
    sim_mismatch = cosine(img_vec, mismatch_vec)

    print(f"\n图像 ↔ 匹配文本「{args.match}」:   {sim_match:.4f}")
    print(f"图像 ↔ 无关文本「{args.mismatch}」: {sim_mismatch:.4f}")
    print(f"差值: {sim_match - sim_mismatch:.4f}（要求 >= {args.margin}）")

    if sim_match > sim_mismatch + args.margin:
        print("\n[PASS] 视觉-文本语义对齐正常：匹配文本显著更相似。")
        sys.exit(0)
    else:
        print("\n[FAIL] 语义对齐不达预期，请评估模型或备选方案（Chinese-CLIP）。")
        sys.exit(1)


if __name__ == "__main__":
    main()
