"""记忆节点数据结构与操作函数。

包含 NodeType 枚举、MemoryNode 数据类，
以及节点的 CRUD 操作函数。
"""

from __future__ import annotations

import asyncio
import hashlib
import posixpath
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from src.app.plugin_system.api import log_api

logger = log_api.get_logger("life_engine.memory.nodes")


# ============================================================
# 数据类型定义
# ============================================================


class NodeType(Enum):
    """节点类型。"""

    FILE = "file"  # 文件节点：对应 workspace 中的实际文件
    CONCEPT = "concept"  # 概念节点：人物、地点、主题等抽象概念


@dataclass
class MemoryNode:
    """记忆节点。"""

    node_id: str
    node_type: NodeType
    file_path: Optional[str] = None  # 仅 FILE 类型有
    content_hash: Optional[str] = None
    title: str = ""

    # 激活相关
    activation_strength: float = 1.0
    access_count: int = 0
    last_accessed_at: Optional[float] = None

    # 情感标记
    emotional_valence: float = 0.0  # 情感效价 [-1, 1]
    emotional_arousal: float = 0.0  # 情感唤醒度 [0, 1]
    importance: float = 0.5  # 主观重要性 [0, 1]

    # 元数据
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    embedding_synced: bool = False


# ============================================================
# 辅助函数
# ============================================================


def normalize_file_path(file_path: str) -> str:
    """规范化文件路径字符串，避免同一路径多种写法导致的节点分裂。"""
    raw = str(file_path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    normalized = posixpath.normpath(raw)
    if normalized == ".":
        return ""
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def generate_file_node_id(file_path: str) -> str:
    """根据文件路径生成节点 ID。"""
    normalized = normalize_file_path(file_path)
    return f"file:{hashlib.md5(normalized.encode()).hexdigest()[:12]}"


def generate_legacy_file_node_id(file_path: str) -> str:
    """兼容旧实现（直接使用原始字符串）的节点 ID 生成规则。"""
    return f"file:{hashlib.md5(str(file_path).encode()).hexdigest()[:12]}"


def generate_concept_node_id(concept: str) -> str:
    """根据概念名称生成节点 ID。"""
    return f"concept:{hashlib.md5(concept.encode()).hexdigest()[:12]}"


def compute_content_hash(content: str) -> str:
    """计算内容 hash。"""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def row_to_node(row: sqlite3.Row) -> MemoryNode:
    """将数据库行转换为 MemoryNode。"""
    return MemoryNode(
        node_id=row["node_id"],
        node_type=NodeType(row["node_type"]),
        file_path=row["file_path"],
        content_hash=row["content_hash"],
        title=row["title"] or "",
        activation_strength=row["activation_strength"],
        access_count=row["access_count"],
        last_accessed_at=row["last_accessed_at"],
        emotional_valence=row["emotional_valence"],
        emotional_arousal=row["emotional_arousal"],
        importance=row["importance"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        embedding_synced=bool(row["embedding_synced"]),
    )


# ============================================================
# 节点操作（依赖 Service 实例）
# ============================================================


async def get_or_create_file_node(
    db: sqlite3.Connection,
    file_path: str,
    title: str = "",
    content: str = "",
    emit_visual_event: Any = None,
    update_fts_func: Any = None,
    migrate_node_identity_func: Any = None,
) -> MemoryNode:
    """获取或创建文件节点。

    Args:
        db: SQLite 数据库连接
        file_path: 文件路径
        title: 标题
        content: 内容（用于生成 hash 和 FTS）
        emit_visual_event: 可视化事件发射函数
        update_fts_func: FTS 更新函数
        migrate_node_identity_func: 节点身份迁移函数

    Returns:
        MemoryNode 实例
    """
    normalized_path = normalize_file_path(file_path)
    if not normalized_path:
        raise ValueError("file_path 不能为空")
    node_id = generate_file_node_id(normalized_path)
    legacy_node_id = generate_legacy_file_node_id(file_path)

    def _lookup_node(nid: str) -> Optional[MemoryNode]:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM memory_nodes WHERE node_id = ?", (nid,))
        row = cursor.fetchone()
        return row_to_node(row) if row else None

    row_result = await asyncio.to_thread(_lookup_node, node_id)
    if row_result is None and legacy_node_id != node_id:
        legacy_row_result = await asyncio.to_thread(_lookup_node, legacy_node_id)
        if legacy_row_result is not None and migrate_node_identity_func:
            await migrate_node_identity_func(
                old_node_id=legacy_node_id,
                new_node_id=node_id,
                new_file_path=normalized_path,
            )
            row_result = await asyncio.to_thread(_lookup_node, node_id)

    now = time.time()
    content_hash = compute_content_hash(content) if content else None

    if row_result:
        node = row_result

        if content_hash and node.content_hash != content_hash:
            def _update_node_hash() -> None:
                cursor = db.cursor()
                cursor.execute(
                    """
                    UPDATE memory_nodes
                    SET content_hash = ?, title = ?, updated_at = ?, embedding_synced = 0
                    WHERE node_id = ?
                    """,
                    (content_hash, title or node.title, now, node_id),
                )
                db.commit()

            await asyncio.to_thread(_update_node_hash)
            node.content_hash = content_hash
            node.title = title or node.title
            node.updated_at = now
            node.embedding_synced = False

            if update_fts_func:
                await update_fts_func(node_id, title, content[:2000])

        return node

    node = MemoryNode(
        node_id=node_id,
        node_type=NodeType.FILE,
        file_path=normalized_path,
        content_hash=content_hash,
        title=title,
        created_at=now,
        updated_at=now,
    )

    def _insert_node() -> None:
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO memory_nodes
            (node_id, node_type, file_path, content_hash, title,
             activation_strength, access_count, last_accessed_at,
             emotional_valence, emotional_arousal, importance,
             created_at, updated_at, embedding_synced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node.node_id,
                node.node_type.value,
                node.file_path,
                node.content_hash,
                node.title,
                node.activation_strength,
                node.access_count,
                node.last_accessed_at,
                node.emotional_valence,
                node.emotional_arousal,
                node.importance,
                node.created_at,
                node.updated_at,
                0,
            ),
        )
        db.commit()

    await asyncio.to_thread(_insert_node)

    if content and update_fts_func:
        await update_fts_func(node_id, title, content[:2000])

    if emit_visual_event:
        emit_visual_event(
            "memory.nodes.created",
            {
                "node": {
                    "id": node.node_id,
                    "type": node.node_type.value.upper(),
                    "title": node.title,
                    "path": node.file_path,
                    "activation": node.activation_strength,
                    "importance": node.importance,
                }
            },
        )

    logger.debug(f"创建文件节点: {file_path}")
    return node


async def get_node_by_file_path(
    db: sqlite3.Connection,
    file_path: str,
    migrate_node_identity_func: Any = None,
) -> Optional[MemoryNode]:
    """根据文件路径获取节点。

    Args:
        db: SQLite 数据库连接
        file_path: 文件路径
        migrate_node_identity_func: 节点身份迁移函数

    Returns:
        MemoryNode 或 None
    """
    normalized_path = normalize_file_path(file_path)
    if not normalized_path:
        return None
    node_id = generate_file_node_id(normalized_path)

    def _lookup_node(nid: str) -> Optional[MemoryNode]:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM memory_nodes WHERE node_id = ?", (nid,))
        row = cursor.fetchone()
        return row_to_node(row) if row else None

    row_result = await asyncio.to_thread(_lookup_node, node_id)
    if row_result is None:
        legacy_node_id = generate_legacy_file_node_id(file_path)
        if legacy_node_id != node_id:
            legacy_row_result = await asyncio.to_thread(_lookup_node, legacy_node_id)
            if legacy_row_result is not None and migrate_node_identity_func:
                await migrate_node_identity_func(
                    old_node_id=legacy_node_id,
                    new_node_id=node_id,
                    new_file_path=normalized_path,
                )
                row_result = await asyncio.to_thread(_lookup_node, node_id)
    return row_result


async def migrate_node_identity(
    db: sqlite3.Connection,
    old_node_id: str,
    new_node_id: str,
    new_file_path: str,
    emit_visual_event: Any = None,
    migrate_vector_identity_func: Any = None,
) -> bool:
    """将节点身份从 old_node_id 迁移到 new_node_id，并保留关联与检索数据。

    Args:
        db: SQLite 数据库连接
        old_node_id: 旧节点 ID
        new_node_id: 新节点 ID
        new_file_path: 新文件路径
        emit_visual_event: 可视化事件发射函数
        migrate_vector_identity_func: 向量身份迁移函数

    Returns:
        是否迁移成功
    """

    if old_node_id == new_node_id:
        def _update_path() -> bool:
            cursor = db.cursor()
            cursor.execute(
                "UPDATE memory_nodes SET file_path = ?, updated_at = ? WHERE node_id = ?",
                (new_file_path, time.time(), old_node_id),
            )
            db.commit()
            return cursor.rowcount > 0

        return await asyncio.to_thread(_update_path)

    def _load_node(nid: str) -> Optional[sqlite3.Row]:
        cursor = db.cursor()
        cursor.execute("SELECT * FROM memory_nodes WHERE node_id = ?", (nid,))
        return cursor.fetchone()

    old_row = await asyncio.to_thread(_load_node, old_node_id)
    if not old_row:
        return False

    new_row = await asyncio.to_thread(_load_node, new_node_id)
    now = time.time()

    if new_row:
        merged_title = (new_row["title"] or "").strip() or (old_row["title"] or "")
        merged_content_hash = new_row["content_hash"] or old_row["content_hash"]
        merged_activation = max(
            float(new_row["activation_strength"] or 0.0),
            float(old_row["activation_strength"] or 0.0),
        )
        merged_access_count = int(new_row["access_count"] or 0) + int(old_row["access_count"] or 0)
        new_last = new_row["last_accessed_at"]
        old_last = old_row["last_accessed_at"]
        merged_last_accessed = (
            max(v for v in (new_last, old_last) if v is not None)
            if (new_last is not None or old_last is not None)
            else None
        )
        merged_emotional_valence = (
            float(new_row["emotional_valence"] or 0.0) + float(old_row["emotional_valence"] or 0.0)
        ) / 2.0
        merged_emotional_arousal = max(
            float(new_row["emotional_arousal"] or 0.0),
            float(old_row["emotional_arousal"] or 0.0),
        )
        merged_importance = max(
            float(new_row["importance"] or 0.0),
            float(old_row["importance"] or 0.0),
        )
        merged_created_at = min(
            float(new_row["created_at"] or now),
            float(old_row["created_at"] or now),
        )

        def _update_merged() -> None:
            cursor = db.cursor()
            cursor.execute(
                """
                UPDATE memory_nodes
                SET file_path = ?,
                    content_hash = ?,
                    title = ?,
                    activation_strength = ?,
                    access_count = ?,
                    last_accessed_at = ?,
                    emotional_valence = ?,
                    emotional_arousal = ?,
                    importance = ?,
                    created_at = ?,
                    updated_at = ?,
                    embedding_synced = 0
                WHERE node_id = ?
                """,
                (
                    new_file_path,
                    merged_content_hash,
                    merged_title,
                    merged_activation,
                    merged_access_count,
                    merged_last_accessed,
                    merged_emotional_valence,
                    merged_emotional_arousal,
                    merged_importance,
                    merged_created_at,
                    now,
                    new_node_id,
                ),
            )

        await asyncio.to_thread(_update_merged)
    else:
        def _insert_merged() -> None:
            cursor = db.cursor()
            cursor.execute(
                """
                INSERT INTO memory_nodes
                (node_id, node_type, file_path, content_hash, title,
                 activation_strength, access_count, last_accessed_at,
                 emotional_valence, emotional_arousal, importance,
                 created_at, updated_at, embedding_synced)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_node_id,
                    old_row["node_type"],
                    new_file_path,
                    old_row["content_hash"],
                    old_row["title"],
                    old_row["activation_strength"],
                    old_row["access_count"],
                    old_row["last_accessed_at"],
                    old_row["emotional_valence"],
                    old_row["emotional_arousal"],
                    old_row["importance"],
                    old_row["created_at"],
                    now,
                    0,
                ),
            )

        await asyncio.to_thread(_insert_merged)

    # Migrate edges and FTS data (sync DB, single transaction)
    def _migrate_edges_and_fts() -> None:
        cursor = db.cursor()
        cursor.execute(
            "SELECT * FROM memory_edges WHERE source_id = ? OR target_id = ?",
            (old_node_id, old_node_id),
        )
        old_edges = cursor.fetchall()
        for edge in old_edges:
            mapped_source = new_node_id if edge["source_id"] == old_node_id else edge["source_id"]
            mapped_target = new_node_id if edge["target_id"] == old_node_id else edge["target_id"]
            if mapped_source == mapped_target:
                continue

            cursor.execute(
                """
                INSERT INTO memory_edges
                (edge_id, source_id, target_id, edge_type, weight, base_strength,
                 reinforcement, activation_count, last_activated_at, reason, created_at, bidirectional)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id, edge_type) DO UPDATE SET
                    weight = MAX(weight, excluded.weight),
                    base_strength = MAX(base_strength, excluded.base_strength),
                    reinforcement = reinforcement + excluded.reinforcement,
                    activation_count = activation_count + excluded.activation_count,
                    last_activated_at = CASE
                        WHEN last_activated_at IS NULL THEN excluded.last_activated_at
                        WHEN excluded.last_activated_at IS NULL THEN last_activated_at
                        ELSE MAX(last_activated_at, excluded.last_activated_at)
                    END,
                    reason = CASE
                        WHEN reason IS NULL OR reason = '' THEN excluded.reason
                        ELSE reason
                    END,
                    bidirectional = MAX(bidirectional, excluded.bidirectional)
                """,
                (
                    str(uuid.uuid4())[:8],
                    mapped_source,
                    mapped_target,
                    edge["edge_type"],
                    edge["weight"],
                    edge["base_strength"],
                    edge["reinforcement"],
                    edge["activation_count"],
                    edge["last_activated_at"],
                    edge["reason"],
                    edge["created_at"],
                    edge["bidirectional"],
                ),
            )

        cursor.execute(
            "SELECT title, content FROM memory_fts WHERE node_id = ? LIMIT 1",
            (old_node_id,),
        )
        old_fts_row = cursor.fetchone()
        if old_fts_row:
            cursor.execute("DELETE FROM memory_fts WHERE node_id = ?", (new_node_id,))
            cursor.execute(
                "INSERT INTO memory_fts (node_id, title, content) VALUES (?, ?, ?)",
                (new_node_id, old_fts_row["title"], old_fts_row["content"]),
            )

        cursor.execute(
            "DELETE FROM memory_edges WHERE source_id = ? OR target_id = ?",
            (old_node_id, old_node_id),
        )
        cursor.execute("DELETE FROM memory_fts WHERE node_id = ?", (old_node_id,))
        cursor.execute("DELETE FROM memory_nodes WHERE node_id = ?", (old_node_id,))
        db.commit()

    await asyncio.to_thread(_migrate_edges_and_fts)

    if migrate_vector_identity_func:
        await migrate_vector_identity_func(
            old_node_id=old_node_id,
            new_node_id=new_node_id,
            new_file_path=new_file_path,
        )

    return True


async def migrate_file_path(
    db: sqlite3.Connection,
    old_path: str,
    new_path: str,
    migrate_node_identity_func: Any = None,
) -> bool:
    """迁移文件路径对应的记忆身份，避免移动文件后节点断裂。

    Args:
        db: SQLite 数据库连接
        old_path: 旧路径
        new_path: 新路径
        migrate_node_identity_func: 节点身份迁移函数

    Returns:
        是否迁移成功
    """
    old_norm = normalize_file_path(old_path)
    new_norm = normalize_file_path(new_path)
    if not old_norm or not new_norm:
        return False
    if old_norm == new_norm:
        return True

    old_node_id = generate_file_node_id(old_norm)
    new_node_id = generate_file_node_id(new_norm)
    if migrate_node_identity_func:
        migrated = await migrate_node_identity_func(
            old_node_id=old_node_id,
            new_node_id=new_node_id,
            new_file_path=new_norm,
        )
        if migrated:
            logger.info(f"已迁移记忆路径: {old_norm} -> {new_norm}")
        return migrated
    return False


async def update_fts(db: sqlite3.Connection, node_id: str, title: str, content: str) -> None:
    """更新全文搜索索引。

    Args:
        db: SQLite 数据库连接
        node_id: 节点 ID
        title: 标题
        content: 内容
    """
    def _do_db_work() -> None:
        cursor = db.cursor()
        cursor.execute("DELETE FROM memory_fts WHERE node_id = ?", (node_id,))
        cursor.execute(
            "INSERT INTO memory_fts (node_id, title, content) VALUES (?, ?, ?)",
            (node_id, title, content),
        )
        db.commit()

    await asyncio.to_thread(_do_db_work)


async def increment_access(
    db: sqlite3.Connection,
    node_id: str,
    emit_visual_event: Any = None,
) -> None:
    """增加节点访问计数并更新激活强度。

    Args:
        db: SQLite 数据库连接
        node_id: 节点 ID
        emit_visual_event: 可视化事件发射函数
    """
    now = time.time()

    def _do_db_work() -> Optional[tuple]:
        cursor = db.cursor()
        cursor.execute(
            """
            UPDATE memory_nodes
            SET access_count = access_count + 1,
                last_accessed_at = ?,
                activation_strength = MIN(1.0, activation_strength + 0.1)
            WHERE node_id = ?
            """,
            (now, node_id),
        )
        db.commit()
        cursor.execute(
            "SELECT activation_strength, access_count, last_accessed_at FROM memory_nodes WHERE node_id = ?",
            (node_id,),
        )
        row = cursor.fetchone()
        if row:
            return (float(row["activation_strength"] or 0.0), int(row["access_count"] or 0), row["last_accessed_at"])
        return None

    row_data = await asyncio.to_thread(_do_db_work)
    if row_data and emit_visual_event:
        emit_visual_event(
            "memory.nodes.updated",
            {
                "nodes": [
                    {
                        "id": node_id,
                        "activation": row_data[0],
                        "access_count": row_data[1],
                        "last_accessed_at": row_data[2],
                    }
                ]
            },
        )