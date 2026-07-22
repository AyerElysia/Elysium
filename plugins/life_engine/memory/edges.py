"""记忆边数据结构与操作函数。

包含 EdgeType 枚举、MemoryEdge 数据类，
以及边的 CRUD 操作、Hebbian 强化函数。
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from src.app.plugin_system.api import log_api

from .eligibility import assess_document_path

logger = log_api.get_logger("life_engine.memory.edges")


# ============================================================
# 数据类型定义
# ============================================================


class EdgeType(Enum):
    """边类型。"""

    # 文件 ↔ 文件（显式关联）
    RELATES = "relates"  # 相关（默认双向）
    CAUSES = "causes"  # 因果（A导致B）
    CONTINUES = "continues"  # 延续（A是B的后续）
    CONTRASTS = "contrasts"  # 对比（A和B观点不同）
    REFINES = "refines"  # 精炼（B 是 A 的后来整理）
    CORRECTS = "corrects"  # 修正（B 修正 A 的过时理解）
    RENAMES = "renames"  # 路径/命名迁移（A 后来变成 B）
    REINTERPRETS = "reinterprets"  # 重新解释（B 给 A 新语境）

    # 文件 → 概念（自动/半自动）
    MENTIONS = "mentions"  # 文件提及某概念

    # 任意节点间（动态增强）
    ASSOCIATES = "associates"  # 联想边（显式学习时产生）


# 默认只用于检索扩散的显式关系；ASSOCIATES 仅在显式学习场景中参与。
EXPLICIT_RELATION_EDGE_TYPES = frozenset(
    {
        EdgeType.RELATES,
        EdgeType.CAUSES,
        EdgeType.CONTINUES,
        EdgeType.CONTRASTS,
        EdgeType.REFINES,
        EdgeType.CORRECTS,
        EdgeType.RENAMES,
        EdgeType.REINTERPRETS,
    }
)


DIRECTIONAL_EDGE_TYPES = {
    EdgeType.CAUSES,
    EdgeType.CONTINUES,
    EdgeType.MENTIONS,
    EdgeType.REFINES,
    EdgeType.CORRECTS,
    EdgeType.RENAMES,
    EdgeType.REINTERPRETS,
}


@dataclass
class MemoryEdge:
    """记忆边（关联）。"""

    edge_id: str
    source_id: str
    target_id: str
    edge_type: EdgeType

    # 连接强度
    weight: float = 0.5
    base_strength: float = 0.5
    reinforcement: float = 0.0

    # 激活统计
    activation_count: int = 0
    last_activated_at: Optional[float] = None

    # 元数据
    reason: str = ""  # 关联原因
    created_at: float = field(default_factory=time.time)
    bidirectional: bool = True


# ============================================================
# 辅助函数
# ============================================================


def row_to_edge(row: sqlite3.Row) -> MemoryEdge:
    """将数据库行转换为 MemoryEdge。"""
    return MemoryEdge(
        edge_id=row["edge_id"],
        source_id=row["source_id"],
        target_id=row["target_id"],
        edge_type=EdgeType(row["edge_type"]),
        weight=row["weight"],
        base_strength=row["base_strength"],
        reinforcement=row["reinforcement"],
        activation_count=row["activation_count"],
        last_activated_at=row["last_activated_at"],
        reason=row["reason"] or "",
        created_at=row["created_at"],
        bidirectional=bool(row["bidirectional"]),
    )


# ============================================================
# 边操作
# ============================================================


_EDGE_WRITE_LOCK = threading.RLock()


def _validate_strength(value: float, *, name: str = "strength") -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是 [0, 1] 范围内的有限数值")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是 [0, 1] 范围内的有限数值") from exc
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ValueError(f"{name} 必须在 [0, 1] 范围内")
    return normalized


def _start_savepoint(cursor: sqlite3.Cursor, prefix: str) -> str:
    name = f"{prefix}_{uuid.uuid4().hex}"
    cursor.execute(f"SAVEPOINT {name}")
    return name


def _finish_savepoint(
    cursor: sqlite3.Cursor,
    name: str,
    error: BaseException | None,
) -> None:
    if error is not None:
        cursor.execute(f"ROLLBACK TO SAVEPOINT {name}")
    cursor.execute(f"RELEASE SAVEPOINT {name}")


def _load_edge_row(
    cursor: sqlite3.Cursor,
    source_id: str,
    target_id: str,
    edge_type: EdgeType,
) -> sqlite3.Row | None:
    cursor.execute(
        """
        SELECT * FROM memory_edges
        WHERE source_id = ? AND target_id = ? AND edge_type = ?
        """,
        (source_id, target_id, edge_type.value),
    )
    return cursor.fetchone()


def _insert_or_sync_edge(
    cursor: sqlite3.Cursor,
    *,
    source_id: str,
    target_id: str,
    edge_type: EdgeType,
    weight: float,
    base_strength: float,
    reinforcement: float,
    activation_count: int,
    last_activated_at: float | None,
    reason: str,
    created_at: float,
    bidirectional: bool,
) -> tuple[MemoryEdge, bool]:
    row = _load_edge_row(cursor, source_id, target_id, edge_type)
    if row is not None:
        cursor.execute(
            """
            UPDATE memory_edges
            SET weight = ?, base_strength = ?, reinforcement = ?,
                activation_count = ?, last_activated_at = ?, reason = ?,
                bidirectional = ?
            WHERE edge_id = ?
            """,
            (
                weight,
                base_strength,
                reinforcement,
                activation_count,
                last_activated_at,
                reason,
                1 if bidirectional else 0,
                row["edge_id"],
            ),
        )
        updated = _load_edge_row(cursor, source_id, target_id, edge_type)
        assert updated is not None
        return row_to_edge(updated), False

    edge_id = str(uuid.uuid4())[:8]
    cursor.execute(
        """
        INSERT INTO memory_edges
        (edge_id, source_id, target_id, edge_type, weight, base_strength,
         reinforcement, activation_count, last_activated_at, reason, created_at, bidirectional)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            edge_id,
            source_id,
            target_id,
            edge_type.value,
            weight,
            base_strength,
            reinforcement,
            activation_count,
            last_activated_at,
            reason,
            created_at,
            1 if bidirectional else 0,
        ),
    )
    inserted = _load_edge_row(cursor, source_id, target_id, edge_type)
    assert inserted is not None
    return row_to_edge(inserted), True


def _existing_node_ids(cursor: sqlite3.Cursor, node_ids: List[str]) -> set[str]:
    ordered = list(dict.fromkeys(node_ids))
    if not ordered:
        return set()
    placeholders = ",".join("?" for _ in ordered)
    cursor.execute(
        f"SELECT node_id FROM memory_nodes WHERE node_id IN ({placeholders})",
        ordered,
    )
    return {str(row["node_id"]) for row in cursor.fetchall()}


def _reinforce_associations_sync(
    db: sqlite3.Connection,
    node_ids: List[str],
    learning_rate: float,
    *,
    initial_strength: float,
    reason: str,
    now: float | None = None,
) -> tuple[List[Dict[str, Any]], int, List[str]]:
    """原子强化 ASSOCIATES 逻辑边，并让正反两行保持一致。"""
    rate = _validate_strength(learning_rate, name="learning_rate")
    initial = _validate_strength(initial_strength, name="initial_strength")
    deduped_ids = list(dict.fromkeys(str(node_id) for node_id in node_ids if node_id))
    if len(deduped_ids) < 2:
        return [], 0, []

    now_ts = time.time() if now is None else float(now)
    with _EDGE_WRITE_LOCK:
        cursor = db.cursor()
        savepoint = _start_savepoint(cursor, "reinforce_associations")
        error: BaseException | None = None
        try:
            existing = _existing_node_ids(cursor, deduped_ids)
            stale_ids = [node_id for node_id in deduped_ids if node_id not in existing]
            valid_ids = [node_id for node_id in deduped_ids if node_id in existing]
            events: List[Dict[str, Any]] = []
            created_pairs = 0

            for index, node_a in enumerate(valid_ids):
                for node_b in valid_ids[index + 1:]:
                    if node_a == node_b:
                        continue
                    forward = _load_edge_row(cursor, node_a, node_b, EdgeType.ASSOCIATES)
                    reverse = _load_edge_row(cursor, node_b, node_a, EdgeType.ASSOCIATES)
                    rows = [row for row in (forward, reverse) if row is not None]
                    if not rows:
                        created_pairs += 1
                    old_weight = max((float(row["weight"] or 0.0) for row in rows), default=initial)
                    delta = rate * (1.0 - old_weight) if rows else 0.0
                    new_weight = min(1.0, old_weight + delta)
                    base_strength = max(
                        (float(row["base_strength"] or 0.0) for row in rows),
                        default=initial,
                    )
                    reinforcement = max(
                        (float(row["reinforcement"] or 0.0) for row in rows),
                        default=0.0,
                    ) + delta
                    activation_count = max(
                        (int(row["activation_count"] or 0) for row in rows),
                        default=0,
                    ) + 1
                    effective_reason = str(reason or "").strip() or next(
                        (str(row["reason"] or "") for row in rows if row["reason"]),
                        "",
                    )
                    logical_created_at = min(
                        (float(row["created_at"] or now_ts) for row in rows),
                        default=now_ts,
                    )

                    for source, target, existing_row in (
                        (node_a, node_b, forward),
                        (node_b, node_a, reverse),
                    ):
                        edge, _ = _insert_or_sync_edge(
                            cursor,
                            source_id=source,
                            target_id=target,
                            edge_type=EdgeType.ASSOCIATES,
                            weight=new_weight,
                            base_strength=base_strength,
                            reinforcement=reinforcement,
                            activation_count=activation_count,
                            last_activated_at=now_ts,
                            reason=effective_reason,
                            created_at=(
                                float(existing_row["created_at"])
                                if existing_row is not None
                                else logical_created_at
                            ),
                            bidirectional=True,
                        )
                        events.append(
                            {
                                "id": edge.edge_id,
                                "source": source,
                                "target": target,
                                "type": EdgeType.ASSOCIATES.value,
                                "weight": new_weight,
                                "delta": delta,
                            }
                        )
        except BaseException as exc:
            error = exc
            raise
        finally:
            _finish_savepoint(cursor, savepoint, error)

    return events, created_pairs, stale_ids


async def create_or_update_edge(
    db: sqlite3.Connection,
    source_id: str,
    target_id: str,
    edge_type: EdgeType,
    reason: str = "",
    strength: float = 0.5,
    bidirectional: bool = True,
    emit_visual_event: Any = None,
) -> MemoryEdge:
    """原子创建或更新边，并维护非方向型双向边的镜像行。"""
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("memory edge source_id 不能为空")
    if not isinstance(target_id, str) or not target_id.strip():
        raise ValueError("memory edge target_id 不能为空")
    if source_id == target_id:
        raise ValueError("memory edge 不允许 source_id 与 target_id 相同")
    if not isinstance(edge_type, EdgeType):
        raise ValueError("edge_type 必须是 EdgeType")
    normalized_strength = _validate_strength(strength)
    effective_bidirectional = bool(
        bidirectional and edge_type not in DIRECTIONAL_EDGE_TYPES
    )
    now = time.time()

    def _do_db_work() -> tuple[MemoryEdge, bool]:
        with _EDGE_WRITE_LOCK:
            cursor = db.cursor()
            savepoint = _start_savepoint(cursor, "upsert_edge")
            error: BaseException | None = None
            try:
                existing_nodes = _existing_node_ids(cursor, [source_id, target_id])
                missing = [
                    node_id
                    for node_id in (source_id, target_id)
                    if node_id not in existing_nodes
                ]
                if missing:
                    raise ValueError(
                        "memory edge endpoint 不存在: " + ", ".join(missing)
                    )

                forward_row = _load_edge_row(cursor, source_id, target_id, edge_type)
                reverse_row = (
                    _load_edge_row(cursor, target_id, source_id, edge_type)
                    if edge_type not in DIRECTIONAL_EDGE_TYPES
                    else None
                )
                reverse_is_mirror = bool(
                    reverse_row is not None and reverse_row["bidirectional"]
                )
                is_update = bool(
                    forward_row is not None
                    or (effective_bidirectional and reverse_row is not None)
                    or reverse_is_mirror
                )
                existing_rows = [row for row in (forward_row,) if row is not None]
                if effective_bidirectional or reverse_is_mirror:
                    existing_rows.extend(
                        row for row in (reverse_row,) if row is not None
                    )
                effective_reason = str(reason or "").strip() or next(
                    (str(row["reason"] or "") for row in existing_rows if row["reason"]),
                    "",
                )
                last_activated_at = now if existing_rows else None
                logical_created_at = min(
                    (float(row["created_at"] or now) for row in existing_rows),
                    default=now,
                )
                logical_reinforcement = max(
                    (float(row["reinforcement"] or 0.0) for row in existing_rows),
                    default=0.0,
                )
                logical_activation_count = max(
                    (int(row["activation_count"] or 0) for row in existing_rows),
                    default=0,
                )

                forward, _ = _insert_or_sync_edge(
                    cursor,
                    source_id=source_id,
                    target_id=target_id,
                    edge_type=edge_type,
                    weight=normalized_strength,
                    base_strength=normalized_strength,
                    reinforcement=(
                        float(forward_row["reinforcement"] or 0.0)
                        if forward_row is not None and not effective_bidirectional
                        else logical_reinforcement
                    ),
                    activation_count=(
                        int(forward_row["activation_count"] or 0)
                        if forward_row is not None and not effective_bidirectional
                        else logical_activation_count
                    ),
                    last_activated_at=last_activated_at,
                    reason=effective_reason,
                    created_at=(
                        float(forward_row["created_at"])
                        if forward_row is not None
                        else logical_created_at
                    ),
                    bidirectional=effective_bidirectional,
                )

                if effective_bidirectional:
                    _insert_or_sync_edge(
                        cursor,
                        source_id=target_id,
                        target_id=source_id,
                        edge_type=edge_type,
                        weight=normalized_strength,
                        base_strength=normalized_strength,
                        reinforcement=logical_reinforcement,
                        activation_count=logical_activation_count,
                        last_activated_at=last_activated_at,
                        reason=effective_reason,
                        created_at=(
                            float(reverse_row["created_at"])
                            if reverse_row is not None
                            else logical_created_at
                        ),
                        bidirectional=True,
                    )
                elif reverse_row is not None and bool(reverse_row["bidirectional"]):
                    cursor.execute(
                        "DELETE FROM memory_edges WHERE edge_id = ?",
                        (reverse_row["edge_id"],),
                    )
            except BaseException as exc:
                error = exc
                raise
            finally:
                _finish_savepoint(cursor, savepoint, error)
        return forward, is_update

    edge, is_update = await asyncio.to_thread(_do_db_work)

    if emit_visual_event:
        event_type = "memory.edges.updated" if is_update else "memory.edges.created"
        emit_visual_event(
            event_type,
            {
                "edge": {
                    "id": edge.edge_id,
                    "source": edge.source_id,
                    "target": edge.target_id,
                    "type": edge.edge_type.value,
                    "weight": edge.weight,
                    "reason": edge.reason,
                    "last_activated_at": edge.last_activated_at,
                }
            },
        )
    logger.debug(f"创建边: {source_id} --[{edge_type.value}]--> {target_id}")
    return edge


async def get_edges_from(
    db: sqlite3.Connection,
    node_id: str,
    min_weight: float = 0.0,
) -> List[MemoryEdge]:
    """获取从指定节点出发的边。

    Args:
        db: SQLite 数据库连接
        node_id: 节点 ID
        min_weight: 最小权重过滤

    Returns:
        边列表
    """
    def _do_db_work() -> List[MemoryEdge]:
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT * FROM memory_edges
            WHERE source_id = ? AND weight >= ?
            ORDER BY weight DESC
            """,
            (node_id, min_weight),
        )
        return [row_to_edge(row) for row in cursor.fetchall()]

    return await asyncio.to_thread(_do_db_work)


async def get_edges_to(
    db: sqlite3.Connection,
    node_id: str,
    min_weight: float = 0.0,
) -> List[MemoryEdge]:
    """获取指向指定节点的边。

    Args:
        db: SQLite 数据库连接
        node_id: 节点 ID
        min_weight: 最小权重过滤

    Returns:
        边列表
    """
    def _do_db_work() -> List[MemoryEdge]:
        cursor = db.cursor()
        cursor.execute(
            """
            SELECT * FROM memory_edges
            WHERE target_id = ? AND weight >= ?
            ORDER BY weight DESC
            """,
            (node_id, min_weight),
        )
        return [row_to_edge(row) for row in cursor.fetchall()]

    return await asyncio.to_thread(_do_db_work)


async def delete_edge(
    db: sqlite3.Connection,
    source_path: str,
    target_path: str,
    edge_type: Optional[EdgeType] = None,
    generate_file_node_id_func: Any = None,
) -> bool:
    """删除边。

    Args:
        db: SQLite 数据库连接
        source_path: 源文件路径
        target_path: 目标文件路径
        edge_type: 边类型（可选）
        generate_file_node_id_func: 节点 ID 生成函数

    Returns:
        是否删除成功
    """
    from .nodes import generate_file_node_id

    source_eligibility = assess_document_path(source_path)
    target_eligibility = assess_document_path(target_path)
    if not source_eligibility.eligible or not target_eligibility.eligible:
        return False

    gen_func = generate_file_node_id_func or generate_file_node_id
    source_id = gen_func(source_eligibility.path)
    target_id = gen_func(target_eligibility.path)

    def _do_db_work() -> bool:
        with _EDGE_WRITE_LOCK:
            cursor = db.cursor()
            savepoint = _start_savepoint(cursor, "delete_edge")
            error: BaseException | None = None
            deleted_count = 0
            try:
                if edge_type is None:
                    cursor.execute(
                        """
                        DELETE FROM memory_edges
                        WHERE (source_id = ? AND target_id = ?)
                           OR (source_id = ? AND target_id = ?)
                        """,
                        (source_id, target_id, target_id, source_id),
                    )
                    deleted_count += max(0, cursor.rowcount)
                else:
                    cursor.execute(
                        """
                        DELETE FROM memory_edges
                        WHERE source_id = ? AND target_id = ? AND edge_type = ?
                        """,
                        (source_id, target_id, edge_type.value),
                    )
                    deleted_count += max(0, cursor.rowcount)
                    if edge_type not in DIRECTIONAL_EDGE_TYPES:
                        cursor.execute(
                            """
                            DELETE FROM memory_edges
                            WHERE source_id = ? AND target_id = ? AND edge_type = ?
                            """,
                            (target_id, source_id, edge_type.value),
                        )
                        deleted_count += max(0, cursor.rowcount)
            except BaseException as exc:
                error = exc
                raise
            finally:
                _finish_savepoint(cursor, savepoint, error)
            return deleted_count > 0

    return await asyncio.to_thread(_do_db_work)


# ============================================================
# Hebbian 强化
# ============================================================


async def reinforce_coactivated(
    db: sqlite3.Connection,
    node_ids: List[str],
    learning_rate: float = 0.1,
    filter_existing_func: Any = None,
    emit_visual_event: Any = None,
) -> None:
    """强化共同激活的节点之间的边 (Hebbian Learning)。

    Args:
        db: SQLite 数据库连接
        node_ids: 节点 ID 列表
        learning_rate: 学习率
        filter_existing_func: 过滤存在节点的函数
        emit_visual_event: 可视化事件发射函数
    """
    deduped_ids = list(dict.fromkeys(str(node_id) for node_id in node_ids if node_id))
    if filter_existing_func and deduped_ids:
        _, stale_ids = await filter_existing_func(
            [(node_id, 1.0) for node_id in deduped_ids]
        )
        stale_set = set(stale_ids)
        deduped_ids = [node_id for node_id in deduped_ids if node_id not in stale_set]

    reinforced_edges, _, stale_ids = await asyncio.to_thread(
        _reinforce_associations_sync,
        db,
        deduped_ids,
        learning_rate,
        initial_strength=0.2,
        reason="共同检索激活",
    )
    if stale_ids:
        logger.warning(
            f"Hebbian 强化跳过 {len(stale_ids)} 个不存在节点ID: {stale_ids[:5]}"
        )
    if reinforced_edges and emit_visual_event:
        emit_visual_event(
            "memory.edges.reinforced",
            {"edges": reinforced_edges},
        )
