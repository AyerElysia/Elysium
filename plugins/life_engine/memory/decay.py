"""记忆衰减与做梦系统接口。

包含 Ebbinghaus 遗忘曲线计算、衰减应用、
做梦游走、弱边修剪等函数。
"""

from __future__ import annotations

import asyncio
import inspect
import math
import sqlite3
import time
from typing import Any, Dict, List, Optional

import numpy as np

from src.app.plugin_system.api import log_api

from .eligibility import (
    assess_document_path,
    assess_indexed_document_path,
    eligible_document_path_sql,
    is_eligible_indexed_document_path,
    register_indexed_path_sql_function,
)
from .indexing import transaction as _index_transaction
from .nodes import MemoryNode, NodeType, row_to_node
from .edges import (
    EdgeType,
    _EDGE_WRITE_LOCK,
    _reinforce_associations_sync,
    get_edges_from,
)

logger = log_api.get_logger("life_engine.memory.decay")

# NOTE: Every ``register_indexed_path_sql_function(db)`` call below is
# deliberately unconditional and repeated per query, mirroring the call sites
# in ``search.py``. This is safe because
# ``eligibility.register_indexed_path_sql_function`` serializes installation
# with its own ``_SQL_FUNCTION_LOCK`` and probes the connection before calling
# ``sqlite3.Connection.create_function`` again, so concurrent callers never
# race on ``create_function`` and never deadlock against
# ``indexing._TRANSACTION_LOCK`` or ``edges._EDGE_WRITE_LOCK`` (neither is held
# while registering here).


# ============================================================
# 常量
# ============================================================

DECAY_LAMBDA = 0.05  # 遗忘衰减系数（约14天半衰期）
PRUNE_THRESHOLD = 0.1  # 边剪枝阈值
DREAM_LEARNING_RATE = 0.05  # REM 做梦学习率


def _run_edge_maintenance(
    db: sqlite3.Connection,
    prefix: str,
    operation: Any,
) -> Any:
    """在边写锁和统一事务边界内运行维护操作，保留调用方外层事务。

    ``prefix`` 不再直接命名一个裸 ``SAVEPOINT``：维护操作改为进入
    ``indexing.transaction()``，与 ``upsert_document_rows`` 等写路径共享同一把
    ``indexing._TRANSACTION_LOCK``。这避免了旧实现的竞态——``apply_decay`` 曾经
    只靠 ``_EDGE_WRITE_LOCK`` 保护自己的裸 SAVEPOINT，与仅受
    ``_TRANSACTION_LOCK`` 保护的 ``transaction()`` 根事务互不知晓；当两者在同一
    共享连接上并发运行时，后进入的一方会被 SQLite 视为前者根事务下的嵌套
    SAVEPOINT，前者失败回滚就会连带抹掉后者已提交的写入。现在两条路径共享同一
    把事务锁，根事务与嵌套 SAVEPOINT 的身份始终由锁的持有顺序决定，不会被误判。
    """
    with _EDGE_WRITE_LOCK, _index_transaction(db):
        return operation()


# ============================================================
# 遗忘曲线
# ============================================================


def compute_memory_strength(node: MemoryNode, decay_lambda: float = DECAY_LAMBDA) -> float:
    """计算记忆强度，结合 Ebbinghaus 遗忘曲线和多种保护因素。

    Args:
        node: MemoryNode 实例
        decay_lambda: 遗忘衰减系数

    Returns:
        记忆强度 [0, 1]
    """
    if not node.last_accessed_at:
        return node.activation_strength

    now = time.time()
    days_since = (now - node.last_accessed_at) / 86400

    # 基础时间衰减 (Ebbinghaus-inspired)
    time_decay = math.exp(-decay_lambda * days_since)

    # 提取练习效应 (Testing Effect)
    retrieval_bonus = math.log(1 + node.access_count) * 0.1

    # 情感保护 (Emotional Enhancement)
    emotional_shield = node.emotional_arousal * 0.2

    # 重要性保护
    importance_shield = node.importance * 0.1

    # 最终强度
    strength = time_decay + retrieval_bonus + emotional_shield + importance_shield
    return min(max(strength, 0.0), 1.0)


async def apply_decay(db: sqlite3.Connection) -> int:
    """应用遗忘衰减（定期任务）。

    Args:
        db: SQLite 数据库连接

    Returns:
        更新的节点数量
    """
    logger.info("Starting memory decay process")
    start_time = time.time()

    def _do_db_work() -> int:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM memory_nodes")
        rows = cursor.fetchall()

        updated = 0
        for row in rows:
            if "is_deleted" in row.keys() and bool(row["is_deleted"]):
                continue
            node = row_to_node(row)
            if (
                node.node_type == NodeType.FILE
                and not is_eligible_indexed_document_path(node.file_path)
            ):
                continue
            new_strength = compute_memory_strength(node)

            if abs(new_strength - node.activation_strength) > 0.01:
                cursor.execute(
                    "UPDATE memory_nodes SET activation_strength = ? WHERE node_id = ?",
                    (new_strength, node.node_id),
                )
                updated += 1

        # 双向 ASSOCIATES 以逻辑关系对为单位衰减，避免镜像行逐渐分叉。
        association_rows = cursor.execute(
            "SELECT * FROM memory_edges WHERE edge_type = ?",
            (EdgeType.ASSOCIATES.value,),
        ).fetchall()
        association_pairs: Dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in association_rows:
            pair_key = tuple(sorted((str(row["source_id"]), str(row["target_id"]))))
            association_pairs.setdefault(pair_key, []).append(row)

        decay_now = time.time()
        for (node_a, node_b), rows in association_pairs.items():
            is_bidirectional_pair = any(bool(row["bidirectional"]) for row in rows)
            timestamps = [
                float(row["last_activated_at"])
                for row in rows
                if row["last_activated_at"] is not None
            ]
            is_complete_bidirectional_pair = is_bidirectional_pair and len(rows) == 2
            if is_complete_bidirectional_pair:
                if not timestamps:
                    continue
                base_strength = max(float(row["base_strength"] or 0.0) for row in rows)
                reinforcement = max(float(row["reinforcement"] or 0.0) for row in rows)
                activation_count = max(int(row["activation_count"] or 0) for row in rows)
                last_activated_at = max(timestamps)
                reason = next(
                    (str(row["reason"] or "") for row in rows if row["reason"]),
                    "",
                )
                days_since = (decay_now - last_activated_at) / 86400
                decay_factor = math.exp(-DECAY_LAMBDA * days_since)
                new_weight = base_strength + reinforcement * decay_factor

                if new_weight < PRUNE_THRESHOLD:
                    cursor.execute(
                        """
                        DELETE FROM memory_edges
                        WHERE edge_type = ?
                          AND ((source_id = ? AND target_id = ?)
                               OR (source_id = ? AND target_id = ?))
                        """,
                        (EdgeType.ASSOCIATES.value, node_a, node_b, node_b, node_a),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE memory_edges
                        SET weight = ?, base_strength = ?, reinforcement = ?,
                            activation_count = ?, last_activated_at = ?, reason = ?,
                            bidirectional = 1
                        WHERE edge_type = ?
                          AND ((source_id = ? AND target_id = ?)
                               OR (source_id = ? AND target_id = ?))
                        """,
                        (
                            new_weight,
                            base_strength,
                            reinforcement,
                            activation_count,
                            last_activated_at,
                            reason,
                            EdgeType.ASSOCIATES.value,
                            node_a,
                            node_b,
                            node_b,
                            node_a,
                        ),
                    )
                continue

            for row in rows:
                if row["last_activated_at"] is None:
                    if is_bidirectional_pair:
                        cursor.execute(
                            "UPDATE memory_edges SET bidirectional = 0 WHERE edge_id = ?",
                            (row["edge_id"],),
                        )
                    continue
                days_since = (decay_now - float(row["last_activated_at"])) / 86400
                decay_factor = math.exp(-DECAY_LAMBDA * days_since)
                new_weight = (
                    float(row["base_strength"] or 0.0)
                    + float(row["reinforcement"] or 0.0) * decay_factor
                )
                if new_weight < PRUNE_THRESHOLD:
                    cursor.execute(
                        "DELETE FROM memory_edges WHERE edge_id = ?",
                        (row["edge_id"],),
                    )
                elif (
                    abs(new_weight - float(row["weight"] or 0.0)) > 0.01
                    or is_bidirectional_pair
                ):
                    cursor.execute(
                        "UPDATE memory_edges SET weight = ?, bidirectional = ? WHERE edge_id = ?",
                        (new_weight, 0 if is_bidirectional_pair else row["bidirectional"], row["edge_id"]),
                    )

        return updated

    updated = await asyncio.to_thread(
        _run_edge_maintenance,
        db,
        "apply_decay",
        _do_db_work,
    )

    elapsed = time.time() - start_time
    logger.info(
        f"Memory decay completed: {updated} nodes updated in {elapsed:.2f}s"
    )
    return updated


# ============================================================
# 做梦系统接口
# ============================================================


async def dream_walk(
    db: sqlite3.Connection,
    num_seeds: int = 5,
    seed_ids: Optional[List[str]] = None,
    max_depth: int = 3,
    decay_factor: float = 0.6,
    learning_rate: float = DREAM_LEARNING_RATE,
    emit_visual_event: Any = None,
    persist_learning: bool = False,
) -> Dict[str, Any]:
    """REM 做梦游走：从随机种子出发进行激活扩散。

    与搜索时的 spread_activation 的区别：
    - 种子是随机选取的（不是查询驱动的）
    - 衰减更慢（decay_factor=0.6 vs 0.7），扩散更远
    - 学习率更低（0.05 vs 0.1），梦中学习更温和
    - 不需要查询，不消耗 embedding API

    Args:
        db: SQLite 数据库连接
        num_seeds: 种子数量
        seed_ids: 指定的种子节点 ID（可选）
        max_depth: 最大扩散深度
        decay_factor: 扩散衰减系数
        learning_rate: 学习率
        emit_visual_event: 可视化事件发射函数
        persist_learning: 是否持久化 ASSOCIATES 学习边，默认关闭

    Returns:
        {"nodes_activated": int, "new_edges_created": int, "seed_ids": list}
    """
    if not db:
        return {"nodes_activated": 0, "new_edges_created": 0, "seed_ids": []}

    # Step 1: Load only active, eligible file nodes (sync DB).
    def _load_nodes() -> List[tuple]:
        cursor = db.cursor()
        register_indexed_path_sql_function(db)
        columns = {str(row[1]) for row in cursor.execute("PRAGMA table_info(memory_nodes)")}
        eligibility_sql, eligibility_params = eligible_document_path_sql("file_path")
        clauses = [
            "node_type = ?",
            "activation_strength > 0.05",
            "file_path IS NOT NULL",
            "TRIM(file_path) <> ''",
            eligibility_sql,
        ]
        if "is_deleted" in columns:
            clauses.append("COALESCE(is_deleted, 0) = 0")
        cursor.execute(
            "SELECT node_id, activation_strength FROM memory_nodes WHERE "
            + " AND ".join(clauses)
            + " ORDER BY activation_strength DESC",
            [NodeType.FILE.value, *eligibility_params],
        )
        return [(r["node_id"], r["activation_strength"]) for r in cursor.fetchall()]

    node_rows = await asyncio.to_thread(_load_nodes)
    if not node_rows:
        return {"nodes_activated": 0, "new_edges_created": 0, "seed_ids": []}

    node_ids = [r[0] for r in node_rows]
    eligible_node_ids = set(node_ids)
    strengths = np.array([r[1] for r in node_rows], dtype=np.float64)
    total_strength = float(strengths.sum())
    if total_strength <= 0:
        strengths = np.ones(len(node_ids), dtype=np.float64) / max(len(node_ids), 1)
    else:
        strengths /= total_strength

    requested_seed_ids = [
        str(node_id or "").strip()
        for node_id in (seed_ids or [])
        if str(node_id or "").strip()
    ]
    actual_seed_ids = [node_id for node_id in requested_seed_ids if node_id in node_ids]
    remaining_pool = [node_id for node_id in node_ids if node_id not in actual_seed_ids]

    missing_count = max(0, min(num_seeds, len(node_ids)) - len(actual_seed_ids))
    if missing_count > 0 and remaining_pool:
        pool_indices = [node_ids.index(node_id) for node_id in remaining_pool]
        pool_strengths = strengths[pool_indices]
        pool_total = float(pool_strengths.sum())
        if pool_total <= 0:
            pool_strengths = np.ones(len(pool_indices), dtype=np.float64) / max(len(pool_indices), 1)
        else:
            pool_strengths = pool_strengths / pool_total
        sampled_indices = np.random.choice(
            len(pool_indices),
            size=min(missing_count, len(pool_indices)),
            replace=False,
            p=pool_strengths,
        )
        actual_seed_ids.extend(remaining_pool[idx] for idx in sampled_indices)

    if not actual_seed_ids:
        return {"nodes_activated": 0, "new_edges_created": 0, "seed_ids": []}

    # 梦游走式激活扩散
    activation: Dict[str, float] = {sid: 1.0 for sid in actual_seed_ids}
    visited = set(actual_seed_ids)
    frontier = list(actual_seed_ids)

    for depth in range(max_depth):
        next_frontier: List[str] = []
        decay = decay_factor ** (depth + 1)

        for node_id in frontier:
            current_act = activation[node_id]
            edges = await get_edges_from(db, node_id, min_weight=0.05)

            for edge in edges:
                neighbor = edge.target_id
                if neighbor not in eligible_node_ids or neighbor in visited:
                    continue

                propagated = current_act * edge.weight * decay
                # 梦中阈值更低，允许更远的联想
                if propagated >= 0.1:
                    activation[neighbor] = activation.get(neighbor, 0) + propagated
                    next_frontier.append(neighbor)
                    visited.add(neighbor)

        frontier = next_frontier

        if emit_visual_event:
            emit_visual_event(
                "memory.dream.walk",
                {
                    "depth": depth,
                    "seed_ids": actual_seed_ids,
                    "activated_ids": list(activation.keys()),
                    "frontier_ids": next_frontier,
                },
                source="dream",
            )

        if not frontier:
            break

    all_activated = list(activation.keys())

    if not persist_learning:
        logger.info(
            f"REM dream_walk 只读完成: seeds={len(actual_seed_ids)} "
            f"activated={len(all_activated)}"
        )
        return {
            "nodes_activated": len(all_activated),
            "new_edges_created": 0,
            "seed_ids": actual_seed_ids,
        }

    top_activated = sorted(activation.items(), key=lambda item: -item[1])[:15]
    top_ids = [node_id for node_id, _ in top_activated]
    _, created_pairs, stale_ids = await asyncio.to_thread(
        _reinforce_associations_sync,
        db,
        top_ids,
        learning_rate,
        initial_strength=0.15,
        reason="REM 做梦联想",
    )
    if stale_ids:
        logger.warning(f"REM 学习跳过不存在节点: {stale_ids[:5]}")

    logger.info(
        f"REM dream_walk 完成: seeds={len(actual_seed_ids)} "
        f"activated={len(all_activated)} new_edges={created_pairs}"
    )

    return {
        "nodes_activated": len(all_activated),
        "new_edges_created": created_pairs,
        "seed_ids": actual_seed_ids,
    }


async def list_dream_candidate_nodes(
    db: sqlite3.Connection,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """列出适合做梦选种的长期主题候选节点。

    Args:
        db: SQLite 数据库连接
        limit: 返回数量

    Returns:
        候选节点信息列表
    """
    if not db:
        return []

    def _do_db_work() -> List[Dict[str, Any]]:
        cursor = db.cursor()
        register_indexed_path_sql_function(db)
        columns = {str(row[1]) for row in cursor.execute("PRAGMA table_info(memory_nodes)")}
        eligibility_sql, eligibility_params = eligible_document_path_sql("file_path")
        clauses = [
            "node_type = ?",
            "file_path IS NOT NULL",
            "TRIM(file_path) <> ''",
            eligibility_sql,
        ]
        if "is_deleted" in columns:
            clauses.append("COALESCE(is_deleted, 0) = 0")
        cursor.execute(
            "SELECT node_id, file_path, title, activation_strength, access_count, "
            "emotional_valence, emotional_arousal, importance, updated_at "
            "FROM memory_nodes WHERE "
            + " AND ".join(clauses)
            + " ORDER BY importance DESC, emotional_arousal DESC, access_count DESC, "
            "activation_strength DESC, updated_at DESC LIMIT ?",
            [NodeType.FILE.value, *eligibility_params, max(1, int(limit))],
        )
        results: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            eligibility = assess_indexed_document_path(row["file_path"])
            if not eligibility.eligible:
                continue
            results.append({
                "node_id": row["node_id"],
                "file_path": eligibility.path,
                "title": row["title"] or "",
                "activation_strength": float(row["activation_strength"] or 0.0),
                "access_count": int(row["access_count"] or 0),
                "emotional_valence": float(row["emotional_valence"] or 0.0),
                "emotional_arousal": float(row["emotional_arousal"] or 0.0),
                "importance": float(row["importance"] or 0.0),
                "updated_at": float(row["updated_at"] or 0.0),
            })
        return results

    return await asyncio.to_thread(_do_db_work)


async def list_random_file_nodes(
    db: sqlite3.Connection,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    """随机采样文件节点，供做梦系统自由联想使用。

    与 list_dream_candidate_nodes 不同，此方法使用 ORDER BY RANDOM()
    从全图谱均匀采样，让任何记忆都有机会成为做梦素材。

    Args:
        db: SQLite 数据库连接
        limit: 返回数量

    Returns:
        随机节点信息列表
    """
    if not db:
        return []

    def _do_db_work() -> List[Dict[str, Any]]:
        cursor = db.cursor()
        register_indexed_path_sql_function(db)
        columns = {str(row[1]) for row in cursor.execute("PRAGMA table_info(memory_nodes)")}
        eligibility_sql, eligibility_params = eligible_document_path_sql("file_path")
        clauses = [
            "node_type = ?",
            "file_path IS NOT NULL",
            "TRIM(file_path) <> ''",
            eligibility_sql,
        ]
        if "is_deleted" in columns:
            clauses.append("COALESCE(is_deleted, 0) = 0")
        cursor.execute(
            "SELECT node_id, file_path, title, activation_strength, access_count, "
            "emotional_valence, emotional_arousal, importance, updated_at "
            "FROM memory_nodes WHERE "
            + " AND ".join(clauses)
            + " ORDER BY RANDOM() LIMIT ?",
            [NodeType.FILE.value, *eligibility_params, max(1, int(limit))],
        )
        results: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            eligibility = assess_indexed_document_path(row["file_path"])
            if not eligibility.eligible:
                continue
            results.append({
                "node_id": row["node_id"],
                "file_path": eligibility.path,
                "title": row["title"] or "",
                "activation_strength": float(row["activation_strength"] or 0.0),
                "access_count": int(row["access_count"] or 0),
                "emotional_valence": float(row["emotional_valence"] or 0.0),
                "emotional_arousal": float(row["emotional_arousal"] or 0.0),
                "importance": float(row["importance"] or 0.0),
                "updated_at": float(row["updated_at"] or 0.0),
            })
        return results

    return await asyncio.to_thread(_do_db_work)


async def prune_weak_edges(
    db: sqlite3.Connection,
    threshold: float = PRUNE_THRESHOLD,
) -> int:
    """修剪弱 ASSOCIATES 边（仅自动生成的联想边，保护手动关联）。

    Args:
        db: SQLite 数据库连接
        threshold: 剪枝阈值

    Returns:
        被修剪的边数量
    """
    if not db:
        return 0

    def _do_db_work() -> int:
        cursor = db.cursor()
        rows = cursor.execute(
            "SELECT * FROM memory_edges WHERE edge_type = ?",
            (EdgeType.ASSOCIATES.value,),
        ).fetchall()
        if not rows:
            return 0

        pairs: Dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            pair_key = tuple(sorted((str(row["source_id"]), str(row["target_id"]))))
            pairs.setdefault(pair_key, []).append(row)

        edge_ids: set[str] = set()
        for pair_rows in pairs.values():
            is_bidirectional_pair = any(bool(row["bidirectional"]) for row in pair_rows)
            if is_bidirectional_pair:
                if any(float(row["weight"] or 0.0) < threshold for row in pair_rows):
                    edge_ids.update(str(row["edge_id"]) for row in pair_rows)
                continue
            edge_ids.update(
                str(row["edge_id"])
                for row in pair_rows
                if float(row["weight"] or 0.0) < threshold
            )

        if not edge_ids:
            return 0

        placeholders = ",".join("?" for _ in edge_ids)
        cursor.execute(
            f"DELETE FROM memory_edges WHERE edge_id IN ({placeholders})",
            list(edge_ids),
        )
        return len(edge_ids)

    count = await asyncio.to_thread(
        _run_edge_maintenance,
        db,
        "prune_weak_edges",
        _do_db_work,
    )
    if count > 0:
        logger.info(f"REM 弱边修剪完成: pruned={count} threshold={threshold}")
    return count


# ============================================================
# 关联图谱
# ============================================================


async def _call_relation_callback(
    callback: Any,
    db: sqlite3.Connection,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """兼容模块函数（需要 db）和 service bound callback（不需要 db）。"""
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        signature = None

    if signature is None:
        call_args = args if getattr(callback, "__self__", None) is not None else (db, *args)
    else:
        def accepts(*candidate_args: Any) -> bool:
            try:
                signature.bind(*candidate_args, **kwargs)
            except TypeError:
                return False
            return True

        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
        ]
        first_name = positional[0].name.lower() if positional else ""
        accepts_without_db = accepts(*args)
        accepts_with_db = accepts(db, *args)
        is_bound_method = getattr(callback, "__self__", None) is not None

        if first_name in {"db", "conn", "connection", "database"} and accepts_with_db:
            call_args = (db, *args)
        elif is_bound_method and accepts_without_db:
            call_args = args
        elif accepts_without_db and not accepts_with_db:
            call_args = args
        elif accepts_with_db and not accepts_without_db:
            call_args = (db, *args)
        elif accepts_without_db:
            call_args = args
        else:
            raise TypeError("关系回调参数不兼容")

    result = callback(*call_args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _relation_lookup_kwargs(callback: Any) -> Dict[str, Any]:
    """Disable service lookup migration for a read-only relation request."""
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return {}
    accepts_migration_flag = "migrate_identity" in signature.parameters or any(
        parameter.kind == parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    return {"migrate_identity": False} if accepts_migration_flag else {}


def _relation_node_is_file(node: Any) -> bool:
    value = getattr(getattr(node, "node_type", None), "value", getattr(node, "node_type", None))
    return str(value or "").lower() == NodeType.FILE.value


async def get_file_relations(
    db: sqlite3.Connection,
    file_path: str,
    depth: int = 1,
    min_strength: float = 0.2,
    get_node_by_file_path_func: Any = None,
    get_edges_from_func: Any = None,
    get_edges_to_func: Any = None,
    get_node_by_id_func: Any = None,
) -> Dict[str, Any]:
    """按层遍历文件关联图，同时返回出/入方向和实际深度。"""
    from .nodes import get_node_by_file_path
    from .edges import get_edges_from, get_edges_to
    from .search import get_node_by_id

    max_depth = max(0, int(depth))
    get_node_func = get_node_by_file_path_func or get_node_by_file_path
    get_from_func = get_edges_from_func or get_edges_from
    get_to_func = get_edges_to_func or get_edges_to
    get_id_func = get_node_by_id_func or get_node_by_id

    root_eligibility = assess_document_path(file_path)
    if not root_eligibility.eligible:
        return {"error": f"不是可操作的记忆文档: {root_eligibility.reason}"}
    file_path = root_eligibility.path
    node = await _call_relation_callback(
        get_node_func,
        db,
        file_path,
        **_relation_lookup_kwargs(get_node_func),
    )
    if not node:
        return {"error": f"未找到文件: {file_path}"}
    node_eligibility = assess_indexed_document_path(node.file_path)
    if not _relation_node_is_file(node) or not node_eligibility.eligible:
        return {"error": f"不是可操作的记忆文档: {node_eligibility.reason}"}

    relations: Dict[str, Any] = {
        "center": {
            "file_path": node.file_path or file_path,
            "title": node.title,
            "activation_strength": node.activation_strength,
            "access_count": node.access_count,
        },
        "outgoing": [],
        "incoming": [],
    }
    frontier: list[tuple[Any, int]] = [(node, 0)]
    node_depths: Dict[str, int] = {node.node_id: 0}
    seen_relations: set[tuple[str, str, str, str]] = set()

    while frontier:
        current, current_depth = frontier.pop(0)
        next_depth = current_depth + 1
        if next_depth > max_depth:
            continue
        out_edges = await _call_relation_callback(
            get_from_func, db, current.node_id, min_strength
        )
        in_edges = await _call_relation_callback(
            get_to_func, db, current.node_id, min_strength
        )

        for direction, edges in (("outgoing", out_edges), ("incoming", in_edges)):
            for edge in edges:
                neighbor_id = edge.target_id if direction == "outgoing" else edge.source_id
                relation_key = (edge.edge_id, direction, current.node_id, neighbor_id)
                if relation_key in seen_relations:
                    continue
                seen_relations.add(relation_key)
                neighbor = await _call_relation_callback(get_id_func, db, neighbor_id)
                if neighbor is None or not _relation_node_is_file(neighbor):
                    continue
                neighbor_eligibility = assess_indexed_document_path(neighbor.file_path)
                if not neighbor_eligibility.eligible:
                    continue
                known_depth = node_depths.get(neighbor.node_id)
                if known_depth is not None and known_depth < next_depth:
                    continue
                relations[direction].append(
                    {
                        "file_path": neighbor.file_path,
                        "title": neighbor.title,
                        "relation_type": edge.edge_type.value,
                        "strength": edge.weight,
                        "reason": edge.reason,
                        "depth": next_depth,
                        "edge_id": edge.edge_id,
                    }
                )
                if known_depth is None:
                    node_depths[neighbor.node_id] = next_depth
                    frontier.append((neighbor, next_depth))

    return relations


# ============================================================
# 统计信息
# ============================================================


async def get_stats(db: sqlite3.Connection) -> Dict[str, Any]:
    """获取记忆系统统计信息。

    Args:
        db: SQLite 数据库连接

    Returns:
        统计信息字典
    """
    def _do_db_work() -> Dict[str, Any]:
        cursor = db.cursor()

        cursor.execute(
            "SELECT COUNT(*) as cnt FROM memory_nodes WHERE node_type = ?",
            (NodeType.FILE.value,),
        )
        file_count = cursor.fetchone()["cnt"]

        cursor.execute(
            "SELECT COUNT(*) as cnt FROM memory_nodes WHERE node_type = ?",
            (NodeType.CONCEPT.value,),
        )
        concept_count = cursor.fetchone()["cnt"]

        cursor.execute("SELECT COUNT(*) as cnt FROM memory_edges")
        edge_count = cursor.fetchone()["cnt"]

        cursor.execute("SELECT AVG(activation_strength) as avg FROM memory_nodes")
        avg_activation = cursor.fetchone()["avg"] or 0

        return {
            "file_nodes": file_count,
            "concept_nodes": concept_count,
            "total_edges": edge_count,
            "avg_activation": round(avg_activation, 3),
        }

    return await asyncio.to_thread(_do_db_work)