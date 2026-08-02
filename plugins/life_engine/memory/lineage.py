"""记忆演化链路。

这里不把旧记忆判成“失效”或“删除”，而是记录它后来怎样被整理、
迁移、修正或重新解释。检索时可以同时看到当前理解和历史轨迹。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .edges import EdgeType, MemoryEdge, row_to_edge
from .sqlite_runtime import run_db


LINEAGE_EDGE_TYPES = {
    EdgeType.CONTINUES,
    EdgeType.REFINES,
    EdgeType.CORRECTS,
    EdgeType.RENAMES,
    EdgeType.REINTERPRETS,
}

CANONICAL_EDGE_TYPES = {
    EdgeType.RENAMES,
    EdgeType.REFINES,
    EdgeType.CORRECTS,
}


@dataclass
class MemoryEvidence:
    """一次检索中的证据文件。"""

    file_path: str
    title: str
    snippet: str
    relevance: float = 0.0
    source: str = "direct"
    relation: str = ""
    relation_reason: str = ""
    exists: bool = True


@dataclass
class MemoryTrace:
    """旧记忆和新理解之间的一段演化轨迹。"""

    relation: str
    file_path: str
    title: str
    snippet: str = ""
    reason: str = ""
    direction: str = "later"
    exists: bool = True


@dataclass
class MemoryCorrection:
    """用户或系统记录的显式修正。"""

    correction_id: str
    topic: str
    message: str
    source: str = "user"
    created_at: float = field(default_factory=time.time)
    related_node_id: Optional[str] = None
    query: str = ""
    stream_id: Optional[str] = None


@dataclass
class MemoryBundle:
    """围绕一个主题聚合出的可追溯记忆包。"""

    query: str
    current_understanding: str
    primary_path: str
    evidence: list[MemoryEvidence] = field(default_factory=list)
    history_trace: list[MemoryTrace] = field(default_factory=list)
    corrections: list[MemoryCorrection] = field(default_factory=list)
    uncertainty: str = ""


def row_to_correction(row: sqlite3.Row) -> MemoryCorrection:
    """将数据库行转换为 MemoryCorrection。"""
    return MemoryCorrection(
        correction_id=row["correction_id"],
        topic=row["topic"],
        message=row["message"],
        source=row["source"] or "user",
        created_at=row["created_at"],
        related_node_id=row["related_node_id"],
        query=row["query"] or "",
        stream_id=row["stream_id"],
    )


async def insert_memory_correction(
    db: sqlite3.Connection,
    topic: str,
    message: str,
    source: str = "user",
    related_node_id: str | None = None,
    query: str = "",
    stream_id: str | None = None,
) -> MemoryCorrection:
    """插入一条显式修正记录。"""
    correction = MemoryCorrection(
        correction_id=str(uuid.uuid4())[:12],
        topic=topic,
        message=message,
        source=source or "user",
        created_at=time.time(),
        related_node_id=related_node_id,
        query=query,
        stream_id=stream_id,
    )

    def _do_db_work() -> None:
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO memory_corrections
            (correction_id, topic, message, source, created_at, related_node_id, query, stream_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                correction.correction_id,
                correction.topic,
                correction.message,
                correction.source,
                correction.created_at,
                correction.related_node_id,
                correction.query,
                correction.stream_id,
            ),
        )
        db.commit()

    await run_db(_do_db_work)
    return correction


async def list_memory_corrections(
    db: sqlite3.Connection,
    query: str,
    related_node_ids: list[str] | None = None,
    limit: int = 5,
) -> list[MemoryCorrection]:
    """按查询词和相关节点取最近修正。"""
    related_node_ids = related_node_ids or []
    query_text = str(query or "").strip()

    def _do_db_work() -> list[MemoryCorrection]:
        cursor = db.cursor()
        rows: list[sqlite3.Row] = []
        seen: set[str] = set()

        if related_node_ids:
            placeholders = ",".join("?" for _ in related_node_ids)
            cursor.execute(
                f"""
                SELECT * FROM memory_corrections
                WHERE related_node_id IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*related_node_ids, limit),
            )
            for row in cursor.fetchall():
                if row["correction_id"] not in seen:
                    rows.append(row)
                    seen.add(row["correction_id"])

        if query_text and len(rows) < limit:
            like = f"%{query_text}%"
            cursor.execute(
                """
                SELECT * FROM memory_corrections
                WHERE topic LIKE ? OR message LIKE ? OR query LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (like, like, like, limit),
            )
            for row in cursor.fetchall():
                if row["correction_id"] not in seen:
                    rows.append(row)
                    seen.add(row["correction_id"])
                if len(rows) >= limit:
                    break

        return [row_to_correction(row) for row in rows[:limit]]

    return await run_db(_do_db_work)


async def get_lineage_edges(
    db: sqlite3.Connection,
    node_id: str,
    min_weight: float = 0.0,
) -> tuple[list[MemoryEdge], list[MemoryEdge]]:
    """获取某个节点的演化出边和入边。"""
    edge_values = [edge_type.value for edge_type in LINEAGE_EDGE_TYPES]
    placeholders = ",".join("?" for _ in edge_values)

    def _do_db_work() -> tuple[list[MemoryEdge], list[MemoryEdge]]:
        cursor = db.cursor()
        cursor.execute(
            f"""
            SELECT * FROM memory_edges
            WHERE source_id = ? AND weight >= ? AND edge_type IN ({placeholders})
            ORDER BY weight DESC, created_at DESC
            """,
            (node_id, min_weight, *edge_values),
        )
        outgoing = [row_to_edge(row) for row in cursor.fetchall()]

        cursor.execute(
            f"""
            SELECT * FROM memory_edges
            WHERE target_id = ? AND weight >= ? AND edge_type IN ({placeholders})
            ORDER BY weight DESC, created_at DESC
            """,
            (node_id, min_weight, *edge_values),
        )
        incoming = [row_to_edge(row) for row in cursor.fetchall()]
        return outgoing, incoming

    return await run_db(_do_db_work)
