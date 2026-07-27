"""Qwen3-VL-Embedding-2B 视觉嵌入服务。

OpenAI 兼容的 /v1/embeddings 接口，支持文本与图像（base64/URL）嵌入，
将文本与图像映射到同一语义空间，用于表情包的纯视觉检索。

启动：
    python server.py --model-path /root/models/Qwen3-VL-Embedding-2B --port 8848
"""

from __future__ import annotations

import argparse
import base64
import io
import logging
import os
import sys
import time
from typing import Any

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("visual_embedding")

# ── 模型加载 ──────────────────────────────────────────────────────

_EMBEDDER = None
_MODEL_NAME = "Qwen3-VL-Embedding-2B"


def _load_embedder(model_path: str):
    """加载 Qwen3VLEmbedder（来自模型仓库自带的 scripts）。"""
    global _EMBEDDER
    # 模型仓库自带 scripts/qwen3_vl_embedding.py
    scripts_dir = os.path.join(model_path, "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    try:
        from qwen3_vl_embedding import Qwen3VLEmbedder
    except ImportError as exc:
        raise RuntimeError(
            f"无法从 {scripts_dir} 导入 Qwen3VLEmbedder，请确认模型已完整下载: {exc}"
        ) from exc

    logger.info(f"加载模型: {model_path}")
    _EMBEDDER = Qwen3VLEmbedder(model_name_or_path=model_path)
    logger.info("模型加载完成")
    return _EMBEDDER


def _decode_image(image_ref: str) -> Image.Image:
    """解析图像引用：base64（可带 data URI 前缀）或本地路径。"""
    if not image_ref:
        raise ValueError("image 为空")
    # data URI 前缀
    if image_ref.startswith("data:"):
        image_ref = image_ref.split(",", 1)[-1]
    # 本地路径
    if os.path.exists(image_ref):
        return Image.open(image_ref).convert("RGB")
    # base64
    try:
        raw = base64.b64decode(image_ref)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise ValueError(f"无法解析图像（非 base64/路径）: {exc}") from exc


def _embed_one(text: str | None, image_ref: str | None, instruction: str | None) -> list[float]:
    """对单条文本/图像输入计算嵌入。"""
    if _EMBEDDER is None:
        raise RuntimeError("模型未加载")
    if not text and not image_ref:
        raise ValueError("text 与 image 至少提供一个")

    item: dict[str, Any] = {}
    if text:
        # 指令感知：检索 query 加指令可提升效果
        item["text"] = f"{instruction}\n{text}" if instruction else text
    if image_ref:
        item["image"] = _decode_image(image_ref)

    vec = _EMBEDDER.process([item])[0]
    # process() 返回 torch tensor（可能在 CUDA 上、BFloat16），先转 CPU + float32 再转 numpy
    if hasattr(vec, "detach"):
        vec = vec.detach().cpu().float().numpy()
    arr = np.asarray(vec, dtype=np.float32)
    # L2 归一化，便于 cosine 直接用点积
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    return arr.tolist()


# ── API ───────────────────────────────────────────────────────────

app = FastAPI(title="Visual Embedding Service", version="1.0.0")


class EmbeddingRequest(BaseModel):
    # OpenAI 风格：input 为文本或文本列表
    input: str | list[str] | None = None
    # 扩展：单条图像（base64/路径/data URI）
    image: str | None = None
    # 扩展：单条文本（与 image 可同时提供）
    text: str | None = None
    # 指令（可选，用于检索 query）
    instruction: str | None = None
    model: str | None = None


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "model_loaded": _EMBEDDER is not None, "model": _MODEL_NAME}


@app.post("/v1/embeddings")
async def embeddings(req: EmbeddingRequest) -> dict[str, Any]:
    start = time.time()
    try:
        # 模式 1：扩展的单条 text+image
        if req.image or (req.text and req.input is None):
            vec = _embed_one(req.text, req.image, req.instruction)
            data = [{"object": "embedding", "index": 0, "embedding": vec}]
        # 模式 2：OpenAI 风格文本（单条或批量）
        elif req.input is not None:
            texts = [req.input] if isinstance(req.input, str) else list(req.input)
            data = []
            for i, t in enumerate(texts):
                vec = _embed_one(t, None, req.instruction)
                data.append({"object": "embedding", "index": i, "embedding": vec})
        else:
            raise HTTPException(status_code=400, detail="提供 input 或 text/image")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(f"嵌入失败: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    dim = len(data[0]["embedding"]) if data else 0
    return {
        "object": "list",
        "model": req.model or _MODEL_NAME,
        "data": data,
        "usage": {"prompt_tokens": 0, "total_tokens": 0, "dimensions": dim},
        "use_time": round(time.time() - start, 4),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", default=os.environ.get("VISUAL_EMBED_MODEL_PATH", "/root/models/Qwen3-VL-Embedding-2B"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("VISUAL_EMBED_PORT", "8848")))
    args = parser.parse_args()

    _load_embedder(args.model_path)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
