"""记忆索引修复维护模块。

提供可重跑、幂等的修复能力：

- 重建内容哈希已漂移的文档行（节点、FTS、chunk、向量化 outbox 任务）；
- 清理历史遗留的自环边，并确保数据库级自环防线存在；
- 只依据当前工作区真实文件重建，不删除原始经历与历史版本；
  旧内容版本的索引任务保留为 ``stale``/``failed`` 历史，不做静默擦除。

设计约束：

- 幂等：重复执行只处理仍然漂移的文档；
- 可审计：返回结构化报告，调用方可记录或展示；
- 低耦合：只依赖 eligibility/indexing/nodes 的既有原子函数。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from src.app.plugin_system.api import log_api

from .eligibility import read_workspace_document, scan_workspace_documents
from .indexing import transaction, upsert_document_rows
from .nodes import compute_content_hash

logger = log_api.get_logger("life_engine.memory.repair")


SELF_LOOP_TRIGGERS: tuple[str, ...] = (
    """
    CREATE TRIGGER IF NOT EXISTS memory_edges_no_self_loop_insert
    BEFORE INSERT ON memory_edges
    WHEN NEW.source_id = NEW.target_id
    BEGIN
        SELECT RAISE(ABORT, 'MemoryEdgeSelfLoop');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS memory_edges_no_self_loop_update
    BEFORE UPDATE ON memory_edges
    WHEN NEW.source_id = NEW.target_id
    BEGIN
        SELECT RAISE(ABORT, 'MemoryEdgeSelfLoop');
    END
    """,
)


@dataclass(frozen=True, slots=True)
class MemoryIndexRepairReport:
    """一次索引修复执行的结构化结果。"""

    scanned_documents: int = 0
    rebuilt_documents: int = 0
    rebuilt_paths: tuple[str, ...] = ()
    removed_self_loops: int = 0
    integrity_check: str = ""
    foreign_key_errors: int = 0
    pending_jobs: int = 0
    stale_jobs: int = 0
    failed_jobs: int = 0
    remaining_self_loops: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)


def ensure_self_loop_guards(db: sqlite3.Connection) -> None:
    """确保 memory_edges 的自环防线触发器存在（幂等）。"""
    if db.row_factory is None:
        db.row_factory = sqlite3.Row
    with transaction(db):
        for statement in SELF_LOOP_TRIGGERS:
            db.execute(statement)


def remove_self_loop_edges(db: sqlite3.Connection) -> int:
    """删除历史遗留自环边，返回删除数量。

    自环边在任何边类型下都没有有效语义，只会污染演化链与检索扩散。
    """
    if db.row_factory is None:
        db.row_factory = sqlite3.Row
    with transaction(db):
        rows = db.execute(
            "SELECT edge_id FROM memory_edges WHERE source_id = target_id"
        ).fetchall()
        if rows:
            db.executemany(
                "DELETE FROM memory_edges WHERE edge_id = ?",
                [(str(row["edge_id"]),) for row in rows],
            )
    if rows:
        logger.warning(f"已清理 {len(rows)} 条历史自环边")
    return len(rows)


def repair_document_index(
    db: sqlite3.Connection,
    workspace_path: Path | str,
) -> MemoryIndexRepairReport:
    """按当前工作区文件重建漂移的文档索引行，并清理自环边。

    重复执行是安全的：只有内容哈希仍不一致的文档会被重建；
    空文档只更新节点与 FTS，不会制造新的向量化任务。
    """
    if db.row_factory is None:
        db.row_factory = sqlite3.Row
    workspace = Path(workspace_path).resolve()
    scan = scan_workspace_documents(workspace)

    rebuilt: list[str] = []
    notes: list[str] = []
    for doc in scan.documents:
        content, mtime, _ = read_workspace_document(workspace, doc.path)
        actual_hash = compute_content_hash(content) if content else None
        row = db.execute(
            "SELECT content_hash FROM memory_nodes "
            "WHERE file_path = ? AND is_deleted = 0",
            (doc.path,),
        ).fetchone()
        stored_hash = None if row is None else row["content_hash"]
        if row is not None and stored_hash == actual_hash:
            continue
        upsert_document_rows(
            db,
            doc.path,
            content,
            Path(doc.path).stem,
            source_mtime=mtime,
        )
        rebuilt.append(doc.path)

    removed_loops = remove_self_loop_edges(db)
    ensure_self_loop_guards(db)

    integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
    fk_errors = len(db.execute("PRAGMA foreign_key_check").fetchall())
    job_counts = {
        str(row["status"]): int(row["count"])
        for row in db.execute(
            "SELECT status, COUNT(*) AS count FROM memory_index_jobs GROUP BY status"
        ).fetchall()
    }
    remaining_loops = int(
        db.execute(
            "SELECT COUNT(*) FROM memory_edges WHERE source_id = target_id"
        ).fetchone()[0]
    )
    if rebuilt:
        notes.append("rebuilt documents enqueue fresh embedding jobs")
    if job_counts.get("failed"):
        notes.append("historical failed jobs preserved for audit")

    report = MemoryIndexRepairReport(
        scanned_documents=len(scan.documents),
        rebuilt_documents=len(rebuilt),
        rebuilt_paths=tuple(rebuilt),
        removed_self_loops=removed_loops,
        integrity_check=integrity,
        foreign_key_errors=fk_errors,
        pending_jobs=job_counts.get("pending", 0),
        stale_jobs=job_counts.get("stale", 0),
        failed_jobs=job_counts.get("failed", 0),
        remaining_self_loops=remaining_loops,
        notes=tuple(notes),
    )
    logger.info(
        "记忆索引修复完成: "
        f"扫描 {report.scanned_documents}，重建 {report.rebuilt_documents}，"
        f"清理自环 {report.removed_self_loops}，完整性 {report.integrity_check}"
    )
    return report
