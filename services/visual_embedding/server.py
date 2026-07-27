"""Qwen3-VL-Embedding-2B 视觉嵌入服务。

OpenAI 兼容的 /v1/embeddings 接口，支持文本与图像（base64/路径）嵌入，
将文本与图像映射到同一语义空间，用于表情包的纯视觉检索。

显存策略（按需加载 + 空闲自动卸载）：
- 启动时不加载模型，不占显存
- 首次请求时自动加载（约数秒）
- 空闲超过 idle-timeout（默认 180s）后自动卸载，释放显存给其他模型
- 下次请求再自动加载

启动：
    python server.py --model-path /root/models/Qwen3-VL-Embedding-2B --port 8848 --idle-timeout 180
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import logging
import os
import sys
import threading
import time
from typing import Any

import numpy as np
import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("visual_embedding")

# ── 全局状态 ──────────────────────────────────────────────────────

_MODEL_NAME = "Qwen3-VL-Embedding-2B"
_EMBEDDER: Any = None
_MODEL_PATH: str = ""
_IDLE_TIMEOUT: float = 180.0
_last_activity: float = 0.0
_lock = threading.Lock()  # 串行化加载/卸载/推理，避免显存竞态


# ── 模型加载 / 卸载 ───────────────────────────────────────────────

def _do_load() -> None:
    """加载模型到显存（阻塞，需在线程中调用）。"""
    global _EMBEDDER, _last_activity
    with _lock:
        if _EMBEDDER is not None:
            _last_activity = time.time()
            return
        scripts_dir = os.path.join(_MODEL_PATH, "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        try:
            from qwen3_vl_embedding import Qwen3VLEmbedder
        except ImportError as exc:
            raise RuntimeError(
                f"无法从 {scripts_dir} 导入 Qwen3VLEmbedder，请确认模型已完整下载: {exc}"
            ) from exc
        logger.info(f"按需加载模型: {_MODEL_PATH}")
        t0 = time.time()
        _EMBEDDER = Qwen3VLEmbedder(model_name_or_path=_MODEL_PATH)
        _last_activity = time.time()
        logger.info(f"模型加载完成（{time.time() - t0:.1f}s）")


def _do_unload() -> None:
    """从显存卸载模型（阻塞，需在线程中调用）。"""
    global _EMBEDDER
    with _lock:
        if _EMBEDDER is None:
            return
        logger.info("卸载视觉嵌入模型，释放显存")
        del _EMBEDDER
        _EMBEDDER = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def _touch() -> None:
    global _last_activity
    _last_activity = time.time()


async def _ensure_loaded() -> None:
    """确保模型已加载（首次请求触发）。"""
    if _EMBEDDER is None:
        await asyncio.to_thread(_do_load)
    _touch()


async def _idle_watcher() -> None:
    """后台监视：空闲超过阈值则卸载模型。"""
    while True:
        await asyncio.sleep(15)
        if _IDLE_TIMEOUT <= 0:
            continue
        if _EMBEDDER is not None and (time.time() - _last_activity) > _IDLE_TIMEOUT:
            idle = time.time() - _last_activity
            logger.info(f"空闲 {idle:.0f}s 超过阈值 {_IDLE_TIMEOUT:.0f}s，自动卸载")
            await asyncio.to_thread(_do_unload)


# ── 嵌入计算 ──────────────────────────────────────────────────────

def _decode_image(image_ref: str) -> Image.Image:
    """解析图像引用：base64（可带 data URI 前缀）或本地路径。"""
    if not image_ref:
        raise ValueError("image 为空")
    if image_ref.startswith("data:"):
        image_ref = image_ref.split(",", 1)[-1]
    if os.path.exists(image_ref):
        return Image.open(image_ref).convert("RGB")
    try:
        raw = base64.b64decode(image_ref)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise ValueError(f"无法解析图像（非 base64/路径）: {exc}") from exc


def _embed_one_sync(text: str | None, image_ref: str | None, instruction: str | None) -> list[float]:
    """对单条文本/图像计算嵌入（阻塞，需在线程中调用）。"""
    if not text and not image_ref:
        raise ValueError("text 与 image 至少提供一个")
    with _lock:
        if _EMBEDDER is None:
            raise RuntimeError("模型未加载")
        embedder = _EMBEDDER
        item: dict[str, Any] = {}
        if text:
            item["text"] = f"{instruction}\n{text}" if instruction else text
        if image_ref:
            item["image"] = _decode_image(image_ref)
        vec = embedder.process([item])[0]
    # process() 返回 torch tensor（可能在 CUDA、BFloat16），转 CPU + float32
    if hasattr(vec, "detach"):
        vec = vec.detach().cpu().float().numpy()
    arr = np.asarray(vec, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    return arr.tolist()


async def _embed_one(text: str | None, image_ref: str | None, instruction: str | None) -> list[float]:
    await _ensure_loaded()
    return await asyncio.to_thread(_embed_one_sync, text, image_ref, instruction)


# ── API ───────────────────────────────────────────────────────────

app = FastAPI(title="Visual Embedding Service", version="1.1.0")


class EmbeddingRequest(BaseModel):
    input: str | list[str] | None = None
    image: str | None = None
    text: str | None = None
    instruction: str | None = None
    model: str | None = None


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_idle_watcher())
    logger.info(f"服务启动（模型按需加载，空闲 {_IDLE_TIMEOUT:.0f}s 后自动卸载）")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "model_loaded": _EMBEDDER is not None,
        "model": _MODEL_NAME,
        "idle_timeout": _IDLE_TIMEOUT,
        "idle_seconds": round(time.time() - _last_activity, 1) if _last_activity else None,
    }


@app.post("/unload")
async def unload() -> dict[str, Any]:
    """手动卸载模型释放显存。"""
    await asyncio.to_thread(_do_unload)
    return {"status": "ok", "model_loaded": False}


@app.post("/v1/embeddings")
async def embeddings(req: EmbeddingRequest) -> dict[str, Any]:
    start = time.time()
    try:
        if req.image or (req.text and req.input is None):
            vec = await _embed_one(req.text, req.image, req.instruction)
            data = [{"object": "embedding", "index": 0, "embedding": vec}]
        elif req.input is not None:
            texts = [req.input] if isinstance(req.input, str) else list(req.input)
            data = []
            for i, t in enumerate(texts):
                vec = await _embed_one(t, None, req.instruction)
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
    global _MODEL_PATH, _IDLE_TIMEOUT
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", default=os.environ.get("VISUAL_EMBED_MODEL_PATH", "/root/models/Qwen3-VL-Embedding-2B"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("VISUAL_EMBED_PORT", "8848")))
    parser.add_argument("--idle-timeout", type=float, default=float(os.environ.get("VISUAL_EMBED_IDLE_TIMEOUT", "180")),
                        help="空闲多少秒后自动卸载模型释放显存（<=0 表示常驻不卸载）")
    args = parser.parse_args()

    _MODEL_PATH = args.model_path
    _IDLE_TIMEOUT = args.idle_timeout
    # 不预加载模型，按需加载
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
