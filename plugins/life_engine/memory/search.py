"""记忆检索数据结构与操作函数。

包含 SearchResult 数据类、混合检索、RRF 融合、
激活扩散等函数。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from src.app.plugin_system.api import log_api

if TYPE_CHECKING:
    pass

from .nodes import MemoryNode, row_to_node
from .edges import get_edges_from

logger = log_api.get_logger("life_engine.memory.search")


# ============================================================
# 常量
# ============================================================

RRF_K = 60  # RRF 融合参数
SPREAD_DECAY = 0.7  # 激活扩散衰减系数
SPREAD_THRESHOLD = 0.3  # 激活扩散阈值


# ============================================================
# 数据类型定义
# ============================================================


@dataclass
class SearchResult:
    """检索结果。"""

    file_path: str
    title: str
    snippet: str
    relevance: float
    source: str  # 'direct' | 'associated'
    association_path: List[str] = field(default_factory=list)
    association_reason: str = ""


# ============================================================
# 向量检索
# ============================================================


async def get_chroma_collection(db_path: str) -> Any:
    """获取 ChromaDB collection。

    Args:
        db_path: 向量数据库路径

    Returns:
        ChromaDB collection 对象
    """
    from src.kernel.vector_db import get_vector_db_service

    vector_service = get_vector_db_service(db_path)
    return await vector_service.get_or_create_collection("life_memory")


async def embed_text(text: str) -> List[float]:
    """生成文本的 embedding 向量。

    Args:
        text: 输入文本

    Returns:
        embedding 向量
    """
    from src.app.plugin_system.api.llm_api import create_embedding_request, get_model_set_by_task

    try:
        model_set = get_model_set_by_task("embedding")
        request = create_embedding_request(
            model_set=model_set,
            request_name="life_memory_embedding",
            inputs=[text],
        )
        response = await request.send()
        embeddings = getattr(response, "embeddings", None) or []
        if not embeddings:
            raise RuntimeError("Embedding 请求返回为空")
        return [float(v) for v in embeddings[0]]
    except Exception as e:
        logger.error(f"Embedding 生成失败: {e}")
        raise


async def sync_embedding(
    db: sqlite3.Connection,
    collection: Any,
    file_path: str,
    content: str,
    get_node_by_file_path_func: Any = None,
) -> None:
    """同步文件的 embedding 到向量数据库。

    Args:
        db: SQLite 数据库连接
        collection: ChromaDB collection
        file_path: 文件路径
        content: 文件内容
        get_node_by_file_path_func: 获取节点的函数
    """
    from .nodes import get_node_by_file_path

    if get_node_by_file_path_func:
        node = await get_node_by_file_path_func(file_path)
    else:
        node = await get_node_by_file_path(db, file_path)
    if not node:
        return

    try:
        embedding = await embed_text(content[:3000])

        collection.upsert(
            ids=[node.node_id],
            embeddings=[embedding],
            documents=[content[:500]],
            metadatas=[
                {
                    "file_path": file_path,
                    "title": node.title,
                    "created_at": node.created_at,
                }
            ],
        )

        def _do_db_work() -> None:
            cursor = db.cursor()
            cursor.execute(
                "UPDATE memory_nodes SET embedding_synced = 1 WHERE node_id = ?",
                (node.node_id,),
            )
            db.commit()

        await asyncio.to_thread(_do_db_work)
        logger.debug(f"已同步 embedding: {file_path}")
    except Exception as e:
        logger.error(f"同步 embedding 失败 ({file_path}): {e}")


async def vector_search(
    query: str,
    collection: Any,
    top_k: int = 10,
    filter_existing_func: Any = None,
) -> List[Tuple[str, float]]:
    """向量相似度检索。

    Args:
        query: 查询文本
        collection: ChromaDB collection
        top_k: 返回数量
        filter_existing_func: 过滤存在节点的函数

    Returns:
        (node_id, similarity) 列表
    """
    try:
        query_embedding = await embed_text(query)

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["distances"],
        )

        if not results["ids"] or not results["ids"][0]:
            return []

        # ChromaDB 返回的是距离，转换为相似度
        raw_pairs: List[Tuple[str, float]] = []
        for node_id, distance in zip(results["ids"][0], results["distances"][0]):
            similarity = 1.0 / (1.0 + distance)
            raw_pairs.append((node_id, similarity))

        # 过滤掉向量库存在但节点表不存在的脏 ID
        if filter_existing_func:
            filtered_pairs, stale_ids = await filter_existing_func(raw_pairs)
            if stale_ids:
                logger.warning(
                    f"向量检索命中 {len(stale_ids)} 个脏节点ID（节点表不存在），已忽略: {stale_ids[:5]}"
                )
                try:
                    collection.delete(ids=stale_ids)
                except Exception as cleanup_err:
                    logger.debug(f"清理向量库脏节点失败: {cleanup_err}")
            return filtered_pairs

        return raw_pairs
    except Exception as e:
        logger.error(f"向量检索失败: {e}")
        return []


# ============================================================
# 全文检索
# ============================================================


def _sanitize_fts_query(query: str) -> str:
    """将查询文本转义为合法的 SQLite FTS5 MATCH 参数。

    FTS5 会把裸数字解释为列号（导致 "no such column: N"），
    把含特殊运算符的字符串解释为查询语法。
    通过双引号包裹整个 query 可强制按字面量搜索。
    内部已有的双引号需先转义（"" 是 FTS5 的引号转义方式）。
    """
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


async def fts_search(db: sqlite3.Connection, query: str, top_k: int = 10) -> List[Tuple[str, float]]:
    """全文搜索。

    Args:
        db: SQLite 数据库连接
        query: 查询文本
        top_k: 返回数量

    Returns:
        (node_id, score) 列表
    """
    safe_query = _sanitize_fts_query(query)

    def _do_db_work() -> List[Tuple[str, float]]:
        cursor = db.cursor()
        # 使用 FTS5 的 BM25 排序
        cursor.execute(
            """
            SELECT node_id, bm25(memory_fts) as score
            FROM memory_fts
            WHERE memory_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (safe_query, top_k),
        )

        results: List[Tuple[str, float]] = []
        for row in cursor.fetchall():
            # BM25 返回负数，绝对值越大越相关
            score = abs(row["score"]) / 10.0  # 归一化
            results.append((row["node_id"], min(score, 1.0)))

        return results

    return await asyncio.to_thread(_do_db_work)


# ============================================================
# RRF 融合
# ============================================================


def rrf_fusion(
    fts_results: List[Tuple[str, float]],
    vector_results: List[Tuple[str, float]],
    k: int = RRF_K,
) -> List[Tuple[str, float]]:
    """Reciprocal Rank Fusion 融合。

    Args:
        fts_results: FTS 检索结果
        vector_results: 向量检索结果
        k: RRF 参数

    Returns:
        融合后的 (node_id, score) 列表
    """
    scores: Dict[str, float] = {}

    # FTS 结果
    for rank, (node_id, _) in enumerate(fts_results):
        scores[node_id] = scores.get(node_id, 0) + 1.0 / (k + rank + 1)

    # Vector 结果
    for rank, (node_id, _) in enumerate(vector_results):
        scores[node_id] = scores.get(node_id, 0) + 1.0 / (k + rank + 1)

    # 排序
    return sorted(scores.items(), key=lambda x: -x[1])


# ============================================================
# 激活扩散
# ============================================================


async def spread_activation(
    db: sqlite3.Connection,
    seed_ids: List[str],
    max_depth: int = 2,
    max_results: int = 10,
    spread_decay: float = SPREAD_DECAY,
    spread_threshold: float = SPREAD_THRESHOLD,
) -> List[Tuple[str, float, List[str], str]]:
    """激活扩散联想。

    Args:
        db: SQLite 数据库连接
        seed_ids: 种子节点 ID 列表
        max_depth: 最大扩散深度
        max_results: 最大返回数量
        spread_decay: 扩散衰减系数
        spread_threshold: 扩散阈值

    Returns:
        [(node_id, activation_score, path, reason), ...]
    """
    activation: Dict[str, float] = {seed: 1.0 for seed in seed_ids}
    paths: Dict[str, List[str]] = {seed: [seed] for seed in seed_ids}
    reasons: Dict[str, str] = {}
    visited = set(seed_ids)
    frontier = list(seed_ids)

    for depth in range(max_depth):
        next_frontier: List[str] = []
        decay = spread_decay ** (depth + 1)

        for node_id in frontier:
            current_activation = activation[node_id]
            edges = await get_edges_from(db, node_id, min_weight=spread_threshold)

            for edge in edges:
                neighbor = edge.target_id
                if neighbor in visited:
                    continue

                propagated = current_activation * edge.weight * decay
                if propagated >= spread_threshold:
                    if neighbor not in activation:
                        activation[neighbor] = 0
                        paths[neighbor] = paths[node_id] + [neighbor]
                        reasons[neighbor] = f"{edge.edge_type.value}: {edge.reason}"

                    activation[neighbor] += propagated
                    next_frontier.append(neighbor)
                    visited.add(neighbor)

        frontier = next_frontier
        if not frontier:
            break

    # 移除种子节点，返回联想到的节点
    for seed in seed_ids:
        activation.pop(seed, None)

    sorted_items = sorted(activation.items(), key=lambda x: -x[1])[:max_results]

    return [
        (node_id, score, paths.get(node_id, []), reasons.get(node_id, ""))
        for node_id, score in sorted_items
    ]


# ============================================================
# 辅助函数
# ============================================================


async def filter_existing_scores(
    db: sqlite3.Connection,
    scores: List[Tuple[str, float]],
) -> Tuple[List[Tuple[str, float]], List[str]]:
    """仅保留节点表中存在的结果。

    Args:
        db: SQLite 数据库连接
        scores: (node_id, score) 列表

    Returns:
        (filtered_scores, stale_node_ids)
    """
    if not scores:
        return [], []

    ordered_ids: List[str] = []
    seen = set()
    for node_id, _ in scores:
        if node_id not in seen:
            ordered_ids.append(node_id)
            seen.add(node_id)

    placeholders = ",".join("?" for _ in ordered_ids)

    def _do_db_work() -> set:
        cursor = db.cursor()
        cursor.execute(
            f"SELECT node_id FROM memory_nodes WHERE node_id IN ({placeholders})",
            ordered_ids,
        )
        return {row["node_id"] for row in cursor.fetchall()}

    existing_ids = await asyncio.to_thread(_do_db_work)
    stale_ids = [node_id for node_id in ordered_ids if node_id not in existing_ids]
    filtered_scores = [(node_id, score) for node_id, score in scores if node_id in existing_ids]
    return filtered_scores, stale_ids


async def get_node_by_id(db: sqlite3.Connection, node_id: str) -> Optional[MemoryNode]:
    """根据 ID 获取节点。

    Args:
        db: SQLite 数据库连接
        node_id: 节点 ID

    Returns:
        MemoryNode 或 None
    """
    def _do_db_work() -> Optional[MemoryNode]:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM memory_nodes WHERE node_id = ?", (node_id,))
        row = cursor.fetchone()
        return row_to_node(row) if row else None

    return await asyncio.to_thread(_do_db_work)


async def get_snippet(db: sqlite3.Connection, node_id: str) -> str:
    """获取节点内容摘要。

    Args:
        db: SQLite 数据库连接
        node_id: 节点 ID

    Returns:
        内容摘要
    """
    def _do_db_work() -> str:
        cursor = db.cursor()
        cursor.execute("SELECT content FROM memory_fts WHERE node_id = ?", (node_id,))
        row = cursor.fetchone()
        if row:
            content = row["content"]
            return content[:150] + "..." if len(content) > 150 else content
        return ""

    return await asyncio.to_thread(_do_db_work)


async def filter_results(
    db: sqlite3.Connection,
    results: List[Tuple[str, float]],
    file_types: Optional[List[str]] = None,
    time_range_days: int = 0,
) -> List[Tuple[str, float]]:
    """过滤检索结果。

    Args:
        db: SQLite 数据库连接
        results: 检索结果
        file_types: 文件类型过滤
        time_range_days: 时间范围过滤

    Returns:
        过滤后的结果
    """
    filtered: List[Tuple[str, float]] = []
    now = time.time()
    cutoff = now - time_range_days * 86400 if time_range_days > 0 else 0

    for node_id, score in results:
        node = await get_node_by_id(db, node_id)
        if not node:
            continue

        # 时间过滤
        if cutoff > 0 and node.created_at < cutoff:
            continue

        # 类型过滤
        if file_types and node.file_path:
            path_lower = node.file_path.lower()
            match = False
            for ft in file_types:
                if ft in path_lower or path_lower.startswith(ft):
                    match = True
                    break
            if not match:
                continue

        filtered.append((node_id, score))

    return filtered


# ============================================================
# 混合检索
# ============================================================


async def search_memory(
    db: sqlite3.Connection,
    query: str,
    collection: Any,
    top_k: int = 5,
    enable_association: bool = True,
    file_types: Optional[List[str]] = None,
    time_range_days: int = 0,
    emit_visual_event: Any = None,
    increment_access_func: Any = None,
    reinforce_coactivated_func: Any = None,
) -> List[SearchResult]:
    """混合检索 + 联想。

    Args:
        db: SQLite 数据库连接
        query: 查询文本
        collection: ChromaDB collection
        top_k: 返回数量
        enable_association: 是否启用联想
        file_types: 文件类型过滤
        time_range_days: 时间范围过滤
        emit_visual_event: 可视化事件发射函数
        increment_access_func: 增加访问计数的函数
        reinforce_coactivated_func: Hebbian 强化函数

    Returns:
        SearchResult 列表
    """
    if emit_visual_event:
        emit_visual_event(
            "memory.search.started",
            {
                "query": query,
                "top_k": top_k,
                "enable_association": enable_association,
            },
        )

    # 并行执行关键词和语义检索
    fts_results = await fts_search(db, query, top_k * 2)

    async def _bound_filter(raw_pairs):
        return await filter_existing_scores(db, raw_pairs)

    vector_results = await vector_search(query, collection, top_k * 2, _bound_filter)

    # RRF 融合
    seed_scores = rrf_fusion(fts_results, vector_results)

    # 过滤文件类型和时间范围
    if file_types or time_range_days > 0:
        seed_scores = await filter_results(db, seed_scores, file_types, time_range_days)

    # 取 top-k 作为种子
    seeds = seed_scores[:top_k]
    seed_ids = [node_id for node_id, _ in seeds]

    # 激活扩散联想
    associated: List[Tuple[str, float, List[str], str]] = []
    if enable_association and seeds:
        associated = await spread_activation(db, seed_ids, max_depth=2)

    # 更新访问计数并强化边
    if increment_access_func:
        for node_id, _ in seeds:
            await increment_access_func(node_id)

    if reinforce_coactivated_func and len(seed_ids) > 1:
        await reinforce_coactivated_func(seed_ids)

    # 构建结果
    results: List[SearchResult] = []
    seed_payload: List[Dict[str, Any]] = []
    associated_payload: List[Dict[str, Any]] = []

    # 直接命中
    for node_id, score in seeds:
        node = await get_node_by_id(db, node_id)
        if node and node.file_path:
            snippet = await get_snippet(db, node_id)
            results.append(
                SearchResult(
                    file_path=node.file_path,
                    title=node.title,
                    snippet=snippet,
                    relevance=score,
                    source="direct",
                )
            )
            seed_payload.append({
                "id": node_id,
                "title": node.title,
                "path": node.file_path,
                "score": score,
            })

    # 联想结果
    for node_id, score, path, reason in associated:
        node = await get_node_by_id(db, node_id)
        if node and node.file_path:
            if any(r.file_path == node.file_path for r in results):
                continue
            snippet = await get_snippet(db, node_id)
            results.append(
                SearchResult(
                    file_path=node.file_path,
                    title=node.title,
                    snippet=snippet,
                    relevance=score * 0.8,
                    source="associated",
                    association_path=path,
                    association_reason=reason,
                )
            )
            associated_payload.append({
                "id": node_id,
                "title": node.title,
                "path": node.file_path,
                "score": score,
                "association_path": path,
                "association_reason": reason,
            })

    if emit_visual_event:
        emit_visual_event(
            "memory.search.seeds",
            {
                "query": query,
                "seed_ids": seed_ids,
                "results": seed_payload,
            },
        )
        if associated_payload:
            emit_visual_event(
                "memory.activation.spread",
                {
                    "query": query,
                    "seed_ids": seed_ids,
                    "results": associated_payload,
                },
            )
        emit_visual_event(
            "memory.search.finished",
            {
                "query": query,
                "total_found": len(results),
            },
        )

    return results[:top_k * 2]