"""语义匹配工具：基于 Embedding 的洞察匹配算法。

使用 BAAI/bge-m3 通过 Nexus API 计算语义向量相似度，
精确识别"同一模式在不同情境的复现"。

降级策略：Embedding API 不可用时回退到 Jaccard 分词匹配。

阈值校准（2026-07-27，基于实际洞察数据）：
- 同一模式改述：cosine 0.67 ~ 0.91
- 相关但不同：cosine 0.44 ~ 0.49
- 强化阈值（reinforce）：0.65
- 合并阈值（merge）：0.75
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import Any

import numpy as np

logger = logging.getLogger("life_engine.learning.semantic_matcher")

# ── Embedding 配置 ──────────────────────────────────────────────

_EMBEDDING_API_URL = "http://localhost:3000/v1/embeddings"
_EMBEDDING_API_KEY = "sk-V2o9Ut2rBHFgkH4hCy53snYbQA5uAlkc25jlRzmtT9P3wapo"
_EMBEDDING_MODEL = "BAAI/bge-m3"
_EMBEDDING_TIMEOUT = 15.0
_EMBEDDING_BATCH_SIZE = 32  # 每批最多发送的文本数

# 匹配阈值
REINFORCE_THRESHOLD = 0.65  # cosine >= 此值 → 同一模式，可强化
MERGE_THRESHOLD = 0.75      # cosine >= 此值 → 高度重复，应合并

# ── Embedding 缓存 ──────────────────────────────────────────────

_embedding_cache: dict[str, np.ndarray] = {}
_cache_lock = threading.Lock()
_embedding_available: bool | None = None  # None = 未检测


def _text_key(text: str) -> str:
    """文本缓存键（MD5 前 16 位）。"""
    return hashlib.md5(text.strip().encode("utf-8")).hexdigest()[:16]


def _check_embedding_available() -> bool:
    """检测 Embedding API 是否可用（只检测一次）。"""
    global _embedding_available
    if _embedding_available is not None:
        return _embedding_available
    try:
        import httpx
        r = httpx.post(
            _EMBEDDING_API_URL,
            json={"model": _EMBEDDING_MODEL, "input": ["ping"]},
            timeout=5.0,
            headers={"Authorization": f"Bearer {_EMBEDDING_API_KEY}"},
        )
        _embedding_available = r.status_code == 200
    except Exception:
        _embedding_available = False
    if not _embedding_available:
        logger.warning("Embedding API 不可用，回退到 Jaccard 匹配")
    return _embedding_available


def embed_texts(texts: list[str]) -> list[np.ndarray] | None:
    """批量计算文本 embedding。返回 None 表示 API 不可用。

    结果会缓存到内存中，相同文本不重复请求。
    """
    if not texts:
        return []
    if not _check_embedding_available():
        return None

    results: list[np.ndarray | None] = [None] * len(texts)
    to_fetch: list[tuple[int, str]] = []  # (index, text)

    # 先查缓存
    with _cache_lock:
        for i, text in enumerate(texts):
            key = _text_key(text)
            if key in _embedding_cache:
                results[i] = _embedding_cache[key]
            else:
                to_fetch.append((i, text))

    # 批量请求未缓存的
    if to_fetch:
        try:
            import httpx
            # 分批
            for batch_start in range(0, len(to_fetch), _EMBEDDING_BATCH_SIZE):
                batch = to_fetch[batch_start:batch_start + _EMBEDDING_BATCH_SIZE]
                batch_texts = [t for _, t in batch]
                r = httpx.post(
                    _EMBEDDING_API_URL,
                    json={"model": _EMBEDDING_MODEL, "input": batch_texts},
                    timeout=_EMBEDDING_TIMEOUT,
                    headers={"Authorization": f"Bearer {_EMBEDDING_API_KEY}"},
                )
                if r.status_code != 200:
                    logger.warning(f"Embedding API 返回 {r.status_code}")
                    return None
                data = r.json()
                embeddings = [np.array(d["embedding"], dtype=np.float32) for d in data["data"]]
                with _cache_lock:
                    for (orig_idx, text), emb in zip(batch, embeddings):
                        results[orig_idx] = emb
                        _embedding_cache[_text_key(text)] = emb
        except Exception as exc:
            logger.warning(f"Embedding API 调用失败: {exc}")
            return None

    return [r for r in results if r is not None] if all(r is not None for r in results) else None


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """计算两个向量的余弦相似度。"""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def embedding_similarity(text1: str, text2: str) -> float | None:
    """计算两段文本的 embedding 余弦相似度。返回 None 表示不可用。"""
    embs = embed_texts([text1, text2])
    if embs is None or len(embs) < 2:
        return None
    return cosine_similarity(embs[0], embs[1])


# ── Jaccard 降级方案 ──────────────────────────────────────────────

try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False

STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没",
    "看", "好", "自己", "这", "那", "什么", "可以", "这个", "现在", "知道",
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "should", "could", "may", "might", "must", "can", "i", "you",
    "he", "she", "it", "we", "they", "my", "your", "his", "her", "its",
}


def tokenize_semantic(text: str) -> list[str]:
    """语义分词：支持中英混合，提取有意义的词。"""
    if not text or not text.strip():
        return []
    text = re.sub(r'[^\w\s]', ' ', text)
    text = text.lower().strip()
    if not text:
        return []
    tokens: list[str] = []
    if JIEBA_AVAILABLE:
        for word in jieba.cut(text):
            word = word.strip()
            if len(word) < 2:
                continue
            if word in STOP_WORDS:
                continue
            tokens.append(word)
    else:
        for segment in text.split():
            segment = segment.strip()
            if not segment:
                continue
            if any('\u4e00' <= c <= '\u9fff' for c in segment):
                for i in range(len(segment) - 1):
                    bigram = segment[i:i+2]
                    if bigram not in STOP_WORDS:
                        tokens.append(bigram)
            else:
                if len(segment) >= 2 and segment not in STOP_WORDS:
                    tokens.append(segment)
    return tokens


def semantic_overlap(text1: str, text2: str) -> float:
    """Jaccard 语义重叠度（降级方案）。"""
    tokens1 = set(tokenize_semantic(text1))
    tokens2 = set(tokenize_semantic(text2))
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    union = tokens1 | tokens2
    jaccard = len(intersection) / len(union) if union else 0.0
    if intersection:
        avg_common_len = sum(len(w) for w in intersection) / len(intersection)
        weight_bonus = min(0.2, (avg_common_len - 2) * 0.05)
    else:
        weight_bonus = 0.0
    return min(1.0, jaccard + weight_bonus)


# ── 主匹配接口 ──────────────────────────────────────────────────

def match_insight_pattern(
    claim1: str,
    claim2: str,
    *,
    topic1: str = "",
    topic2: str = "",
    same_topic_threshold: float = REINFORCE_THRESHOLD,
    diff_topic_threshold: float = REINFORCE_THRESHOLD,
) -> float:
    """匹配两条洞察是否为同一模式。

    优先使用 Embedding cosine similarity（精确），
    API 不可用时降级到 Jaccard（粗糙但无依赖）。

    Returns:
        匹配度 [0.0, 1.0]，超过阈值则认为是同一模式。
        返回 0.0 表示未达阈值。
    """
    # 尝试 Embedding
    sim = embedding_similarity(claim1, claim2)
    if sim is not None:
        # Embedding 可用：用 cosine 阈值判断
        threshold = same_topic_threshold  # 统一用 0.65
        return sim if sim >= threshold else 0.0

    # 降级：Jaccard
    overlap = semantic_overlap(claim1, claim2)
    # Jaccard 阈值需要更低（因为 Jaccard 本身数值偏低）
    jaccard_threshold = 0.35
    both_have_topic = bool(topic1) and bool(topic2)
    if both_have_topic and topic1 != topic2:
        jaccard_threshold = 0.45
    return overlap if overlap >= jaccard_threshold else 0.0


def batch_match(claims: list[str]) -> np.ndarray | None:
    """批量计算 claims 之间的 cosine 相似度矩阵。

    返回 NxN 矩阵，或 None（API 不可用）。
    用于 merge_duplicates 等需要全配对比较的场景。
    """
    embs = embed_texts(claims)
    if embs is None:
        return None
    # 归一化
    norms = np.linalg.norm(embs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized = np.array(embs) / norms
    return normalized @ normalized.T


# ── 工具函数 ──────────────────────────────────────────────────

def extract_keywords(text: str, top_n: int = 5) -> list[str]:
    """提取文本关键词（按出现频率）。"""
    tokens = tokenize_semantic(text)
    if not tokens:
        return []
    freq: dict[str, int] = {}
    for token in tokens:
        freq[token] = freq.get(token, 0) + 1
    sorted_tokens = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [token for token, _ in sorted_tokens[:top_n]]


def format_insight_summary(claim: str, max_length: int = 60) -> str:
    """格式化洞察摘要（用于日志）。"""
    if len(claim) <= max_length:
        return claim
    return claim[:max_length-1] + "…"
