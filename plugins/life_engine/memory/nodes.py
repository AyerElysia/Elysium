"""记忆节点数据结构与操作函数。

包含 NodeType 枚举、MemoryNode 数据类，
以及节点的 CRUD 操作函数。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from src.app.plugin_system.api import log_api

from .eligibility import assess_document_path, assess_indexed_document_path
from .sqlite_runtime import run_db

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
    fts_content_hash: str | None = None
    embedding_content_hash: str | None = None
    embedding_model: str = ""
    legacy_fts_present: bool = False
    subject_document_id: str = ""
    subject_version_id: str = ""
    subject_document_revision: int = 0
    subject_binding_revision: int = 0
    subject_content_sha256: str = ""
    is_deleted: bool = False


@dataclass(frozen=True, slots=True)
class ManagedDocumentIndexSnapshot:
    """A fenced, exact subject head projected into the rebuildable index.

    The caller must hold the subject namespace fence until this projection
    commits. ``content=None`` means the current bytes are not text-indexable;
    it never authorizes changing or deleting the authoritative document.
    """

    document_id: str
    version_id: str
    path: str
    document_revision: int
    binding_revision: int
    content_sha256: str
    content: str | None
    deleted: bool = False
    title: str = ""

    def validate(self) -> None:
        """Reject incomplete source identity instead of inventing provenance."""
        for value in (self.document_id, self.version_id):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or not value.isascii()
                or any(not (char.isalnum() or char in "_-.:@") for char in value)
            ):
                raise ValueError("ManagedDocumentSourceIdentityInvalid")
        if (
            not isinstance(self.path, str)
            or not self.path
            or len(self.path) > 2048
            or "\\" in self.path
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
            or ":" in self.path
        ):
            raise ValueError("ManagedDocumentPathInvalid")
        if (
            type(self.document_revision) is not int
            or self.document_revision <= 0
            or type(self.binding_revision) is not int
            or self.binding_revision <= 0
            or type(self.deleted) is not bool
            or (self.content is not None and not isinstance(self.content, str))
            or not isinstance(self.title, str)
            or (self.deleted and self.content is not None)
        ):
            raise ValueError("ManagedDocumentProjectionStateInvalid")
        if (
            not isinstance(self.content_sha256, str)
            or len(self.content_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.content_sha256)
        ):
            raise ValueError("ManagedDocumentContentHashInvalid")
        if self.content is not None and not assess_indexed_document_path(self.path).eligible:
            raise ValueError("ManagedDocumentTextPathNotIndexable")

    @property
    def projection_sha256(self) -> str:
        """Pin text, title, and source metadata without persisting another body."""
        self.validate()
        body = asdict(self)
        body["content"] = (
            hashlib.sha256(self.content.encode("utf-8")).hexdigest()
            if self.content is not None else None
        )
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ManagedDocumentIndexResult:
    """Content-free receipt for a rebuildable managed-document projection."""

    node_id: str
    document_id: str
    version_id: str
    document_revision: int
    indexed: bool
    idempotent_replay: bool = False


def generate_subject_file_node_id(document_id: str) -> str:
    """Keep one node across renames without ever hashing its current path."""
    value = str(document_id)
    if not value or len(value) > 128 or not value.isascii():
        raise ValueError("ManagedDocumentSourceIdentityInvalid")
    return f"subject-file:{value}"


# ============================================================
# 辅助函数
# ============================================================


def generate_file_node_id(file_path: str) -> str:
    """Return a deterministic ID for an already-canonical file path.

    Deliberately do not normalize here: callers that accept external input must
    first use ``canonical_file_node_id``. Hashing a noncanonical spelling as-is
    prevents an absolute or traversal alias from silently claiming the stored
    identity of a valid document.
    """
    path = str(file_path or "")
    return f"file:{hashlib.md5(path.encode()).hexdigest()[:12]}"


def canonical_file_node_id(file_path: str) -> tuple[str, str]:
    """Validate one document path and return its canonical path and node ID."""
    eligibility = assess_document_path(file_path)
    if not eligibility.eligible:
        raise ValueError(f"不支持索引的记忆文档路径: {eligibility.reason}")
    path = eligibility.path
    return path, generate_file_node_id(path)


def generate_legacy_file_node_id(file_path: str) -> str:
    """兼容旧实现（直接使用原始字符串）的节点 ID 生成规则。"""
    return f"file:{hashlib.md5(str(file_path).encode()).hexdigest()[:12]}"


def compute_content_hash(content: str) -> str:
    """计算内容 hash。"""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def row_to_node(row: sqlite3.Row) -> MemoryNode:
    """将数据库行转换为 MemoryNode。"""
    columns = set(row.keys())
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
        fts_content_hash=(
            str(row["fts_content_hash"])
            if "fts_content_hash" in columns
            and row["fts_content_hash"] is not None
            else None
        ),
        embedding_content_hash=(
            str(row["embedding_content_hash"])
            if "embedding_content_hash" in columns
            and row["embedding_content_hash"] is not None
            else None
        ),
        embedding_model=(
            str(row["embedding_model"] or "")
            if "embedding_model" in columns
            else ""
        ),
        legacy_fts_present=(
            bool(row["legacy_fts_present"])
            if "legacy_fts_present" in columns
            else False
        ),
        subject_document_id=str(row["subject_document_id"] or "") if "subject_document_id" in columns else "",
        subject_version_id=str(row["subject_version_id"] or "") if "subject_version_id" in columns else "",
        subject_document_revision=int(row["subject_document_revision"] or 0) if "subject_document_revision" in columns else 0,
        subject_binding_revision=int(row["subject_binding_revision"] or 0) if "subject_binding_revision" in columns else 0,
        subject_content_sha256=str(row["subject_content_sha256"] or "") if "subject_content_sha256" in columns else "",
        is_deleted=bool(row["is_deleted"]) if "is_deleted" in columns else False,
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
    """Return a canonical file node through the SQLite document authority.

    A supplied document body is always committed through the chunk/FTS/outbox
    transaction. Empty-content calls only ensure a reference node exists and
    never clear an already indexed document. Legacy migration callbacks are
    intentionally ignored here: node lookup and ordinary writes must not turn
    into implicit identity repair or vector-store work.
    """
    del emit_visual_event, update_fts_func, migrate_node_identity_func
    normalized_path, _ = canonical_file_node_id(file_path)
    text = str(content or "")

    if text:
        from .indexing import upsert_document_rows

        await run_db(
            upsert_document_rows,
            db,
            normalized_path,
            text,
            title,
        )
    else:
        from .indexing import ensure_document_reference_rows

        await run_db(
            ensure_document_reference_rows,
            db,
            normalized_path,
            title,
        )

    node = await get_node_by_file_path(db, normalized_path)
    if node is None:
        raise RuntimeError(f"文档节点写入后未找到: {normalized_path}")
    return node


async def get_node_by_file_path(
    db: sqlite3.Connection,
    file_path: str,
    migrate_node_identity_func: Any = None,
) -> Optional[MemoryNode]:
    """Read one canonical file node without repairing legacy identities.

    The optional migration callback is retained for source compatibility but is
    deliberately ignored. A legacy ID may be returned only when its persisted
    path is already the exact canonical spelling for the requested document.
    """
    del migrate_node_identity_func
    eligibility = assess_document_path(file_path)
    if not eligibility.eligible:
        return None
    normalized_path = eligibility.path

    def _valid_file_row(row: sqlite3.Row) -> Optional[MemoryNode]:
        if "is_deleted" in set(row.keys()) and bool(row["is_deleted"]):
            return None
        if str(row["node_type"] or "file").lower() != NodeType.FILE.value:
            return None
        stored = assess_indexed_document_path(row["file_path"])
        if not stored.eligible or stored.path != normalized_path:
            return None
        return row_to_node(row)

    def _lookup_node() -> Optional[MemoryNode]:
        # Do not select a canonical-ID row until the path has also been proven
        # unique. A historical duplicate must remain quarantined from reads.
        rows = db.execute(
            "SELECT * FROM memory_nodes WHERE lower(COALESCE(node_type, 'file')) = ? "
            "AND file_path = ? ORDER BY node_id",
            (NodeType.FILE.value, normalized_path),
        ).fetchall()
        valid_rows = [
            node
            for row in rows
            if (node := _valid_file_row(row)) is not None
        ]
        if len(valid_rows) != 1:
            return None
        return valid_rows[0]

    return await run_db(_lookup_node)


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
        from .indexing import transaction

        with transaction(db, immediate=True) as cursor:
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
            cursor.execute(
                "SELECT activation_strength, access_count, last_accessed_at FROM memory_nodes WHERE node_id = ?",
                (node_id,),
            )
            row = cursor.fetchone()
            if row:
                return (
                    float(row["activation_strength"] or 0.0),
                    int(row["access_count"] or 0),
                    row["last_accessed_at"],
                )
            return None

    row_data = await run_db(_do_db_work)
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
