"""Read-only health diagnostics for the Life Engine memory index.

The synchronous collector only performs SQLite and workspace reads.  The async
entry point keeps those reads, as well as Chroma reads, off the event loop.
No function in this module creates, deletes, or repairs memory data.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .eligibility import (
    SUPPORTED_DOCUMENT_SUFFIXES,
    assess_indexed_document_path,
    assess_workspace_document,
    read_workspace_document,
    scan_workspace_documents,
    summarize_rejections,
)
from .indexing import INDEX_SCHEMA_NAME
from .nodes import compute_content_hash, generate_file_node_id
from .sqlite_runtime import run_db

# Kept as a compatibility alias for callers that imported the former constant.
SUPPORTED_WORKSPACE_SUFFIXES = SUPPORTED_DOCUMENT_SUFFIXES
MAX_REPORTED_PATHS = 1000
MAX_VECTOR_IDS = 5000
# Outbox jobs that remain pending/processing longer than this are reported as
# stalled even when the index worker is expected to be running.
OUTBOX_STALL_SECONDS = 3600.0
# Named owner for the index outbox, so a degradation report says who to ask.
OUTBOX_OWNER = "life_engine.memory_index worker"


def _safe_error_reason(s: str | None, max_len: int = 64) -> str:
    """Return a sanitised, length-capped copy of an error reason string.

    Avoids importing ``re`` — only alphanumerics, spaces, and a small set of
    safe punctuation characters are preserved.
    """
    if not s:
        return ""
    safe_chars = set(" _-.:/()")
    cleaned = "".join(
        c for c in str(s) if c.isalnum() or c in safe_chars
    )
    return cleaned[:max_len]


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    try:
        return (
            db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type IN ('table', 'virtual table') AND name = ? LIMIT 1",
                (name,),
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        return False


def _table_columns(db: sqlite3.Connection, name: str) -> set[str]:
    if not _table_exists(db, name):
        return set()
    try:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({name})").fetchall()}
    except sqlite3.Error:
        return set()


def _count(db: sqlite3.Connection, table: str) -> int:
    if not _table_exists(db, table):
        return 0
    try:
        return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
    except (sqlite3.Error, TypeError, ValueError):
        return 0


def _is_deleted(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return bool(value)


def _stored_path_text(row: dict[str, Any]) -> str:
    """Render a persisted path for reporting without changing its identity."""
    value = row.get("file_path")
    return "" if value is None else str(value)


def _active_file_identity(row: dict[str, Any]) -> tuple[str | None, str]:
    """Return a strict active file identity or its diagnostic reason."""
    if _is_deleted(row.get("is_deleted")):
        return None, "deleted_node"
    if str(row.get("node_type") or "file").lower() != "file":
        return None, "not_file_node"

    decision = assess_indexed_document_path(row.get("file_path"))
    if not decision.eligible:
        return None, decision.reason or "invalid_path"

    node_id = row.get("node_id")
    if not isinstance(node_id, str) or node_id != generate_file_node_id(decision.path):
        return None, "node_id_mismatch"
    return decision.path, ""


def _bounded_paths(values: set[str] | list[str]) -> list[str]:
    return sorted(str(value) for value in values)[:MAX_REPORTED_PATHS]


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def _schema_snapshot(db: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": INDEX_SCHEMA_NAME,
        "version": None,
        "tokenizer": None,
    }
    if _table_exists(db, "memory_schema"):
        columns = _table_columns(db, "memory_schema")
        try:
            select_columns = [column for column in ("schema_name", "version", "tokenizer") if column in columns]
            if select_columns:
                where = " WHERE schema_name = ?" if "schema_name" in columns else ""
                params: tuple[Any, ...] = (INDEX_SCHEMA_NAME,) if where else ()
                row = db.execute(
                    f"SELECT {', '.join(select_columns)} FROM memory_schema{where} LIMIT 1",
                    params,
                ).fetchone()
                if row is not None:
                    values = dict(zip(select_columns, row))
                    if values.get("version") is not None:
                        result["version"] = int(values["version"])
                    if values.get("tokenizer"):
                        result["tokenizer"] = str(values["tokenizer"])
        except (sqlite3.Error, TypeError, ValueError):
            result["error_type"] = "SchemaReadError"

    if not result["tokenizer"]:
        for table in ("memory_chunks_fts", "memory_fts"):
            if not _table_exists(db, table):
                continue
            try:
                sql_row = db.execute(
                    "SELECT sql FROM sqlite_master WHERE name = ? LIMIT 1", (table,)
                ).fetchone()
                sql = str(sql_row[0] if sql_row else "").lower()
            except sqlite3.Error:
                sql = ""
            if "trigram" in sql:
                result["tokenizer"] = "trigram"
                break
            if "unicode61" in sql:
                result["tokenizer"] = "unicode61"
                break
    return result


def _integrity_snapshot(db: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": True,
        "integrity_check": "error",
        "integrity_ok": False,
        "foreign_key_check_count": 0,
        "foreign_key_check_ok": True,
    }
    try:
        row = db.execute("PRAGMA integrity_check").fetchone()
        value = str(row[0] if row else "error")
        result["integrity_check"] = "ok" if value.lower() == "ok" else value
        result["integrity_ok"] = value.lower() == "ok"
        if not result["integrity_ok"]:
            result["integrity_error_type"] = "IntegrityCheckFailed"
    except sqlite3.Error as exc:
        result["integrity_error_type"] = type(exc).__name__

    try:
        violations = db.execute("PRAGMA foreign_key_check").fetchall()
        result["foreign_key_check_count"] = len(violations)
        result["foreign_key_check_ok"] = not violations
    except sqlite3.Error as exc:
        result["foreign_key_check_error_type"] = type(exc).__name__
        result["foreign_key_check_ok"] = False

    try:
        result["foreign_keys_enabled"] = bool(
            int(db.execute("PRAGMA foreign_keys").fetchone()[0] or 0)
        )
    except (sqlite3.Error, TypeError, ValueError):
        result["foreign_keys_enabled"] = None
    return result


def _load_node_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(db, "memory_nodes"):
        return []
    columns = _table_columns(db, "memory_nodes")
    fields = [
        "node_id",
        "file_path",
        "content_hash",
        "node_type",
        "is_deleted",
        "embedding_synced",
    ]
    select = [column if column in columns else f"NULL AS {column}" for column in fields]
    try:
        rows = db.execute(f"SELECT {', '.join(select)} FROM memory_nodes").fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "node_id": row[0],
            "file_path": row[1],
            "content_hash": row[2],
            "node_type": row[3],
            "is_deleted": row[4],
            "embedding_synced": row[5],
        }
        for row in rows
    ]


def _workspace_files(
    workspace: Path,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Read only bodies selected by the shared metadata-only eligibility scan."""
    scan = scan_workspace_documents(workspace)
    files: list[dict[str, Any]] = []
    read_errors: list[str] = []
    for document in scan.documents:
        try:
            content, _, _ = read_workspace_document(workspace, document.path)
            content_hash = compute_content_hash(content) if content else None
        except (OSError, UnicodeError, ValueError):
            content_hash = None
            read_errors.append(document.path)
        files.append({"path": document.path, "content_hash": content_hash})

    rejected_paths = [
        decision.path
        for decision in scan.rejected
        if decision.path
    ]
    eligibility = {
        "rejected_count": len(scan.rejected),
        "rejected_reason_counts": summarize_rejections(scan.rejected),
        # A short alias makes the section convenient for generic consumers.
        "reason_counts": summarize_rejections(scan.rejected),
        "rejected_paths": _bounded_paths(rejected_paths),
    }
    return files, sorted(read_errors), eligibility


def _relation_snapshot(db: sqlite3.Connection, node_ids: set[str]) -> dict[str, Any]:
    result = {
        "total": 0,
        "orphan_count": 0,
        "self_loop_count": 0,
        "associates_count": 0,
        "associates_ratio": 0.0,
    }
    if not _table_exists(db, "memory_edges"):
        return result
    columns = _table_columns(db, "memory_edges")
    if not {"source_id", "target_id"}.issubset(columns):
        return result
    try:
        result["total"] = int(db.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0] or 0)
        result["self_loop_count"] = int(
            db.execute(
                "SELECT COUNT(*) FROM memory_edges WHERE source_id = target_id"
            ).fetchone()[0]
            or 0
        )
        if "edge_type" in columns:
            result["associates_count"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM memory_edges WHERE lower(edge_type) = 'associates'"
                ).fetchone()[0]
                or 0
            )
        if node_ids:
            result["orphan_count"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM memory_edges e "
                    "LEFT JOIN memory_nodes s ON s.node_id = e.source_id "
                    "LEFT JOIN memory_nodes t ON t.node_id = e.target_id "
                    "WHERE s.node_id IS NULL OR t.node_id IS NULL"
                ).fetchone()[0]
                or 0
            )
        else:
            result["orphan_count"] = result["total"]
    except sqlite3.Error:
        result["error_type"] = "EdgeReadError"
    result["associates_ratio"] = _safe_ratio(
        int(result["associates_count"]), int(result["total"])
    )
    return result


def _fts_orphan_snapshot(db: sqlite3.Connection) -> dict[str, int]:
    result = {
        "legacy_fts_orphan_count": 0,
        "chunk_orphan_count": 0,
        "chunk_fts_orphan_count": 0,
    }
    if _table_exists(db, "memory_fts"):
        try:
            if _table_exists(db, "memory_nodes"):
                result["legacy_fts_orphan_count"] = int(
                    db.execute(
                        "SELECT COUNT(*) FROM memory_fts f "
                        "LEFT JOIN memory_nodes n ON n.node_id = f.node_id "
                        "WHERE n.node_id IS NULL"
                    ).fetchone()[0]
                    or 0
                )
            else:
                result["legacy_fts_orphan_count"] = _count(db, "memory_fts")
        except sqlite3.Error:
            result["legacy_fts_orphan_count"] = 0

    if _table_exists(db, "memory_chunks"):
        try:
            if _table_exists(db, "memory_nodes"):
                result["chunk_orphan_count"] = int(
                    db.execute(
                        "SELECT COUNT(*) FROM memory_chunks c "
                        "LEFT JOIN memory_nodes n ON n.node_id = c.node_id "
                        "WHERE n.node_id IS NULL"
                    ).fetchone()[0]
                    or 0
                )
            else:
                result["chunk_orphan_count"] = _count(db, "memory_chunks")
        except sqlite3.Error:
            result["chunk_orphan_count"] = 0

    if _table_exists(db, "memory_chunks_fts"):
        try:
            if _table_exists(db, "memory_chunks"):
                result["chunk_fts_orphan_count"] = int(
                    db.execute(
                        "SELECT COUNT(*) FROM memory_chunks_fts f "
                        "LEFT JOIN memory_chunks c "
                        "ON c.chunk_id = f.chunk_id AND c.node_id = f.node_id "
                        "WHERE c.chunk_id IS NULL"
                    ).fetchone()[0]
                    or 0
                )
            else:
                result["chunk_fts_orphan_count"] = _count(db, "memory_chunks_fts")
        except sqlite3.Error:
            result["chunk_fts_orphan_count"] = 0
    return result


def _correction_snapshot(db: sqlite3.Connection) -> dict[str, int]:
    """Report correction rows whose optional related node no longer exists."""
    result = {"total": 0, "orphan_related_node_count": 0}
    if not _table_exists(db, "memory_corrections"):
        return result
    columns = _table_columns(db, "memory_corrections")
    try:
        result["total"] = _count(db, "memory_corrections")
        if "related_node_id" not in columns:
            return result
        if _table_exists(db, "memory_nodes"):
            result["orphan_related_node_count"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM memory_corrections c "
                    "LEFT JOIN memory_nodes n ON n.node_id = c.related_node_id "
                    "WHERE c.related_node_id IS NOT NULL AND n.node_id IS NULL"
                ).fetchone()[0]
                or 0
            )
        else:
            result["orphan_related_node_count"] = int(
                db.execute(
                    "SELECT COUNT(*) FROM memory_corrections "
                    "WHERE related_node_id IS NOT NULL"
                ).fetchone()[0]
                or 0
            )
    except sqlite3.Error:
        result["error_type"] = "CorrectionReadError"
    return result


def _outbox_snapshot(db: sqlite3.Connection, now: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "pending": 0,
        "processing": 0,
        "failed": 0,
        "stale": 0,
        "total": 0,
        "orphan_node_count": 0,
        "status_counts": {},
        "oldest_pending_created_at": None,
        "oldest_pending_age_seconds": None,
        "newest_pending_created_at": None,
        "last_completed_at": None,
        "stale_reason_counts": {},
    }
    if not _table_exists(db, "memory_index_jobs"):
        return result
    columns = _table_columns(db, "memory_index_jobs")
    if "status" not in columns:
        return result
    try:
        rows = db.execute(
            "SELECT lower(COALESCE(status, 'unknown')), COUNT(*) "
            "FROM memory_index_jobs GROUP BY lower(COALESCE(status, 'unknown'))"
        ).fetchall()
        for status, count in rows:
            status_name = str(status)
            status_count = int(count or 0)
            result["status_counts"][status_name] = status_count
            if status_name in {"pending", "processing", "failed", "stale"}:
                result[status_name] = status_count
        result["total"] = _count(db, "memory_index_jobs")
        if "node_id" in columns:
            if _table_exists(db, "memory_nodes"):
                result["orphan_node_count"] = int(
                    db.execute(
                        "SELECT COUNT(*) FROM memory_index_jobs j "
                        "LEFT JOIN memory_nodes n ON n.node_id = j.node_id "
                        "WHERE j.node_id IS NULL OR n.node_id IS NULL"
                    ).fetchone()[0]
                    or 0
                )
            else:
                result["orphan_node_count"] = result["total"]

        # Pending age signals — require created_at column (Unix epoch float/int).
        if "created_at" in columns:
            age_row = db.execute(
                "SELECT MIN(created_at), MAX(created_at) FROM memory_index_jobs "
                "WHERE lower(COALESCE(status, '')) = 'pending'"
            ).fetchone()
            if age_row and age_row[0] is not None:
                oldest_ts = float(age_row[0])
                newest_ts = float(age_row[1])
                result["oldest_pending_created_at"] = oldest_ts
                result["newest_pending_created_at"] = newest_ts
                result["oldest_pending_age_seconds"] = max(0.0, now - oldest_ts)

        # Last completed timestamp.  The shipped schema uses ``updated_at``;
        # ``last_updated`` is tolerated for forward compatibility only.
        last_completed_col = (
            "updated_at" if "updated_at" in columns else
            "last_updated" if "last_updated" in columns else
            None
        )
        if last_completed_col:
            done_row = db.execute(
                f"SELECT MAX({last_completed_col}) FROM memory_index_jobs "
                "WHERE lower(COALESCE(status, '')) = 'completed'"
            ).fetchone()
            if done_row and done_row[0] is not None:
                result["last_completed_at"] = float(done_row[0])

        # Stale reason breakdown.  The shipped schema stores the reason in
        # ``error``; ``error_reason`` is tolerated for forward compatibility.
        reason_col = (
            "error" if "error" in columns else
            "error_reason" if "error_reason" in columns else
            None
        )
        if reason_col:
            stale_rows = db.execute(
                f"SELECT COALESCE({reason_col}, ''), COUNT(*) FROM memory_index_jobs "
                "WHERE lower(COALESCE(status, '')) = 'stale' "
                f"GROUP BY COALESCE({reason_col}, '')"
            ).fetchall()
            for raw_reason, cnt in stale_rows:
                key = _safe_error_reason(raw_reason) or "unknown"
                result["stale_reason_counts"][key] = (
                    result["stale_reason_counts"].get(key, 0) + int(cnt or 0)
                )

    except sqlite3.Error:
        result["error_type"] = "OutboxReadError"
    return result


def _living_memory_snapshot(db: sqlite3.Connection) -> dict[str, Any]:
    """Read immutable-ledger coverage and rebuildable-projection drift signals."""

    tables = (
        "memory_artifact_versions",
        "memory_artifact_derivations",
        "memory_artifact_heads",
        "memory_interpretations",
        "memory_interpretation_fts",
        "memory_interpretation_sources",
        "memory_semantic_relations",
        "memory_recall_sessions",
        "memory_recall_events",
        "memory_corecall_events",
        "memory_association_projection",
        "memory_claims",
        "memory_claim_evidence",
        "memory_beliefs",
        "memory_epistemic_conflicts",
        "memory_state_events",
    )
    counts = {table: _count(db, table) for table in tables}
    head_mismatch_count = 0
    if _table_exists(db, "memory_artifact_heads") and _table_exists(
        db,
        "memory_artifact_versions",
    ):
        head_mismatch_count = int(
            db.execute(
                """SELECT COUNT(*) FROM memory_artifact_heads h
                LEFT JOIN memory_artifact_versions v ON v.artifact_id = h.artifact_id
                WHERE v.artifact_id IS NULL OR v.logical_key <> h.logical_key"""
            ).fetchone()[0]
            or 0
        )

    expected_pair_observations = 0
    invalid_corecall_payload_count = 0
    if _table_exists(db, "memory_corecall_events"):
        for row in db.execute(
            "SELECT entity_refs_json FROM memory_corecall_events"
        ).fetchall():
            try:
                refs = tuple(
                    dict.fromkeys(
                        str(item).strip()
                        for item in json.loads(str(row[0] or "[]"))
                        if str(item).strip()
                    )
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                invalid_corecall_payload_count += 1
                continue
            expected_pair_observations += len(refs) * (len(refs) - 1) // 2

    projected_pair_observations = 0
    if _table_exists(db, "memory_association_projection"):
        projected_pair_observations = int(
            db.execute(
                "SELECT COALESCE(SUM(event_count), 0) FROM memory_association_projection"
            ).fetchone()[0]
            or 0
        )

    claims_without_evidence = 0
    if _table_exists(db, "memory_claims") and _table_exists(
        db,
        "memory_claim_evidence",
    ):
        claims_without_evidence = int(
            db.execute(
                """SELECT COUNT(*) FROM memory_claims c
                WHERE NOT EXISTS (
                    SELECT 1 FROM memory_claim_evidence e
                    WHERE e.claim_id = c.claim_id
                )"""
            ).fetchone()[0]
            or 0
        )

    return {
        "counts": counts,
        "artifact_head_mismatch_count": head_mismatch_count,
        "invalid_corecall_payload_count": invalid_corecall_payload_count,
        "expected_pair_observations": expected_pair_observations,
        "projected_pair_observations": projected_pair_observations,
        "association_projection_drift": (
            expected_pair_observations != projected_pair_observations
        ),
        "claims_without_evidence": claims_without_evidence,
        "retrieval_is_not_truth": True,
    }


def collect_health_snapshot(
    db: sqlite3.Connection | None,
    workspace_path: str | Path,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Collect SQLite/workspace health data synchronously without mutations."""
    _now = now if now is not None else time.time()
    workspace = Path(workspace_path)
    empty_sqlite = {
        "available": db is not None,
        "integrity_check": "unavailable" if db is None else "error",
        "integrity_ok": False if db is not None else None,
        "foreign_key_check_count": 0,
        "foreign_key_check_ok": None if db is None else False,
        "foreign_keys_enabled": None,
    }
    schema = {"name": INDEX_SCHEMA_NAME, "version": None, "tokenizer": None}
    counts = {
        "nodes": 0,
        "file_nodes": 0,
        "concept_nodes": 0,
        "chunks": 0,
        "chunk_fts": 0,
        "legacy_fts": 0,
        "edges": 0,
        "index_jobs": 0,
    }
    node_rows: list[dict[str, Any]] = []
    orphans = {
        "legacy_fts_orphan_count": 0,
        "chunk_orphan_count": 0,
        "chunk_fts_orphan_count": 0,
    }
    outbox: dict[str, Any] = {
        "pending": 0,
        "processing": 0,
        "failed": 0,
        "stale": 0,
        "total": 0,
        "orphan_node_count": 0,
        "status_counts": {},
        "oldest_pending_created_at": None,
        "oldest_pending_age_seconds": None,
        "newest_pending_created_at": None,
        "last_completed_at": None,
        "stale_reason_counts": {},
    }
    corrections: dict[str, Any] = {
        "total": 0,
        "orphan_related_node_count": 0,
    }
    edges = {
        "total": 0,
        "orphan_count": 0,
        "self_loop_count": 0,
        "associates_count": 0,
        "associates_ratio": 0.0,
    }
    living_memory: dict[str, Any] = {
        "counts": {},
        "artifact_head_mismatch_count": 0,
        "invalid_corecall_payload_count": 0,
        "expected_pair_observations": 0,
        "projected_pair_observations": 0,
        "association_projection_drift": False,
        "claims_without_evidence": 0,
        "retrieval_is_not_truth": True,
    }
    if db is not None:
        try:
            integrity = _integrity_snapshot(db)
            empty_sqlite.update(integrity)
            schema = _schema_snapshot(db)
            node_rows = _load_node_rows(db)
            counts.update(
                {
                    "nodes": _count(db, "memory_nodes"),
                    "chunks": _count(db, "memory_chunks"),
                    "chunk_fts": _count(db, "memory_chunks_fts"),
                    "legacy_fts": _count(db, "memory_fts"),
                    "edges": _count(db, "memory_edges"),
                    "index_jobs": _count(db, "memory_index_jobs"),
                }
            )
            counts["file_nodes"] = sum(
                1
                for row in node_rows
                if str(row.get("node_type") or "file").lower() == "file"
            )
            counts["concept_nodes"] = sum(
                1 for row in node_rows if str(row.get("node_type") or "").lower() == "concept"
            )
            node_ids = {
                str(row["node_id"])
                for row in node_rows
                if row.get("node_id") is not None
            }
            orphans = _fts_orphan_snapshot(db)
            outbox = _outbox_snapshot(db, _now)
            corrections = _correction_snapshot(db)
            edges = _relation_snapshot(db, node_ids)
            living_memory = _living_memory_snapshot(db)
        except sqlite3.Error as exc:
            empty_sqlite["error_type"] = type(exc).__name__

    files, read_errors, eligibility = _workspace_files(workspace)
    workspace_paths = {str(item["path"]) for item in files}
    active_file_nodes: dict[str, dict[str, Any]] = {}
    missing_node_paths: set[str] = set()
    ineligible_indexed_node_count = 0
    ineligible_indexed_node_paths: list[str] = []
    ineligible_indexed_reason_counts: dict[str, int] = {}
    for row in node_rows:
        # Deleted and concept rows are not active document-index candidates, but
        # remain visible through the aggregate node counters.  Every active file
        # row must retain its exact stored path and deterministic node identity.
        if _is_deleted(row.get("is_deleted")):
            continue
        if str(row.get("node_type") or "file").lower() != "file":
            continue
        path, identity_error = _active_file_identity(row)
        if path is None:
            ineligible_indexed_node_count += 1
            report_path = _stored_path_text(row)
            if report_path:
                ineligible_indexed_node_paths.append(report_path)
            ineligible_indexed_reason_counts[identity_error] = (
                ineligible_indexed_reason_counts.get(identity_error, 0) + 1
            )
            continue

        workspace_decision = assess_workspace_document(workspace, path)
        # A path that no longer exists remains valid historical evidence. A
        # present symlink, oversized file, special file, or escaped path must
        # not remain eligible merely because its spelling is permitted.
        if not workspace_decision.eligible and not (
            workspace_decision.reason == "stat_error" and not (workspace / path).exists()
        ):
            ineligible_indexed_node_count += 1
            report_path = workspace_decision.path or path
            if report_path:
                ineligible_indexed_node_paths.append(report_path)
            if workspace_decision.reason:
                ineligible_indexed_reason_counts[workspace_decision.reason] = (
                    ineligible_indexed_reason_counts.get(workspace_decision.reason, 0) + 1
                )
            continue

        active_file_nodes.setdefault(path, row)
        if not workspace_decision.eligible:
            missing_node_paths.add(path)

    indexed_paths = set(active_file_nodes)
    unindexed_paths = workspace_paths - indexed_paths
    indexed_existing_paths = workspace_paths & indexed_paths
    hash_mismatch_paths: set[str] = set()
    for item in files:
        path = str(item["path"])
        row = active_file_nodes.get(path)
        if row is None or path in read_errors:
            continue
        if row.get("content_hash") != item.get("content_hash"):
            hash_mismatch_paths.add(path)

    file_count = len(files)
    indexed_count = len(indexed_existing_paths)
    index = {
        "workspace_file_count": file_count,
        "indexed_file_count": indexed_count,
        "coverage": _safe_ratio(indexed_count, file_count),
        "coverage_ratio": _safe_ratio(indexed_count, file_count),
        "unindexed_path_count": len(unindexed_paths),
        "unindexed_paths": _bounded_paths(unindexed_paths),
        "missing_node_path_count": len(missing_node_paths),
        "missing_node_paths": _bounded_paths(missing_node_paths),
        "hash_mismatch_count": len(hash_mismatch_paths),
        "hash_mismatch_paths": _bounded_paths(hash_mismatch_paths),
        "read_error_count": len(read_errors),
        "read_error_paths": _bounded_paths(read_errors),
        "ineligible_indexed_node_count": ineligible_indexed_node_count,
        "ineligible_indexed_node_paths": _bounded_paths(ineligible_indexed_node_paths),
        "ineligible_indexed_reason_counts": dict(
            sorted(ineligible_indexed_reason_counts.items())
        ),
    }
    workspace_snapshot = {
        "file_count": file_count,
        "supported_file_count": file_count,
        "missing_node_paths": _bounded_paths(missing_node_paths),
        "missing_node_path_count": len(missing_node_paths),
        "unindexed_paths": _bounded_paths(unindexed_paths),
    }

    snapshot: dict[str, Any] = {
        "status": "degraded",
        "sqlite": empty_sqlite,
        "schema": schema,
        "counts": counts,
        "workspace": workspace_snapshot,
        "fts": {
            **orphans,
            "legacy_orphan_count": orphans["legacy_fts_orphan_count"],
            "orphan_count": sum(orphans.values()),
        },
        "outbox": outbox,
        "corrections": corrections,
        "edges": edges,
        "living_memory": living_memory,
        "eligibility": eligibility,
        "index": index,
        # A synchronous snapshot has no collection handle.  The async entry
        # point replaces this placeholder after its optional vector read.
        "vector_degraded": True,
    }
    # Convenient scalar aliases for callers that do not need nested sections.
    snapshot.update(
        {
            "integrity_check": empty_sqlite.get("integrity_check"),
            "foreign_key_check_count": empty_sqlite.get("foreign_key_check_count", 0),
            "schema_version": schema.get("version"),
            "tokenizer": schema.get("tokenizer"),
            "node_count": counts["nodes"],
            "chunk_count": counts["chunks"],
            "chunk_fts_count": counts["chunk_fts"],
            "legacy_fts_count": counts["legacy_fts"],
            "workspace_file_count": file_count,
            "missing_node_path_count": len(missing_node_paths),
            "missing_paths": _bounded_paths(missing_node_paths),
            "fts_orphan_count": sum(orphans.values()),
            "legacy_fts_orphan_count": orphans["legacy_fts_orphan_count"],
            "chunk_orphan_count": orphans["chunk_orphan_count"],
            "chunk_fts_orphan_count": orphans["chunk_fts_orphan_count"],
            "pending_jobs": outbox["pending"],
            "processing_jobs": outbox["processing"],
            "failed_jobs": outbox["failed"],
            "outbox_orphan_node_count": outbox["orphan_node_count"],
            "correction_orphan_related_node_count": corrections["orphan_related_node_count"],
            "edge_orphan_count": edges["orphan_count"],
            "edge_self_loop_count": edges["self_loop_count"],
            "associates_ratio": edges["associates_ratio"],
            "index_coverage": index["coverage"],
            "hash_mismatch_count": index["hash_mismatch_count"],
        }
    )
    return snapshot


def _is_enabled(value: Any) -> bool:
    """Interpret SQLite flag values without treating textual ``'0'`` as true."""
    return _is_deleted(value)


def _valid_chunk_ids(
    db: sqlite3.Connection,
    valid_node_ids: set[str],
) -> dict[str, str]:
    """Return chunk IDs mapped to strict file-node owners."""
    if not valid_node_ids or not _table_exists(db, "memory_chunks"):
        return {}
    placeholders = ",".join("?" for _ in valid_node_ids)
    try:
        rows = db.execute(
            "SELECT chunk_id, node_id, chunk_index, content_hash, content "
            "FROM memory_chunks WHERE node_id IN ("
            + placeholders
            + ") ORDER BY node_id, chunk_index, chunk_id",
            sorted(valid_node_ids),
        ).fetchall()
    except sqlite3.Error:
        return {}

    chunks_by_node: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        node_id = row[1]
        if isinstance(node_id, str):
            chunks_by_node.setdefault(node_id, []).append(tuple(row))

    valid_chunk_ids: dict[str, str] = {}
    for node_id, node_chunks in chunks_by_node.items():
        node_chunk_ids: list[str] = []
        for expected_index, row in enumerate(node_chunks):
            try:
                chunk_index = int(row[2])
            except (TypeError, ValueError):
                node_chunk_ids = []
                break
            content = row[4]
            chunk_hash = str(row[3] or "")
            chunk_id = row[0]
            if (
                chunk_index != expected_index
                or not isinstance(content, str)
                or not content
                or chunk_hash != compute_content_hash(content)
                or not isinstance(chunk_id, str)
                or chunk_id != f"{node_id}:{chunk_index}:{chunk_hash}"
            ):
                node_chunk_ids = []
                break
            node_chunk_ids.append(chunk_id)
        valid_chunk_ids.update({chunk_id: node_id for chunk_id in node_chunk_ids})
    return valid_chunk_ids


def _vector_id_sets(
    db: sqlite3.Connection | None,
) -> tuple[set[str], set[str], set[str], set[str]]:
    """Return vector owners that still have strict active SQLite identities."""
    if db is None:
        return set(), set(), set(), set()
    rows = _load_node_rows(db)
    valid_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        path, _ = _active_file_identity(row)
        node_id = row.get("node_id")
        if path is not None and isinstance(node_id, str):
            valid_rows[node_id] = row

    node_ids = set(valid_rows)
    embedded_node_ids = {
        node_id
        for node_id, row in valid_rows.items()
        if _is_enabled(row.get("embedding_synced"))
    }
    chunk_owner_by_id = _valid_chunk_ids(db, node_ids)
    chunk_ids = set(chunk_owner_by_id)
    expected_embedded_chunk_ids = {
        chunk_id
        for chunk_id, node_id in chunk_owner_by_id.items()
        if node_id in embedded_node_ids
    }
    return node_ids, embedded_node_ids, chunk_ids, expected_embedded_chunk_ids


def _flatten_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_ids(item))
        return result
    return [str(value)]


def _collection_ids(collection: Any) -> list[str]:
    """Read IDs only; never request documents, embeddings, or metadata."""
    try:
        result = collection.get(limit=MAX_VECTOR_IDS, include=[])
    except TypeError:
        try:
            result = collection.get(limit=MAX_VECTOR_IDS)
        except TypeError:
            result = collection.get()
    if isinstance(result, dict):
        return list(dict.fromkeys(_flatten_ids(result.get("ids"))))
    return []


def _vector_collection_kind(collection: Any) -> str:
    if collection is None:
        return "unavailable"
    metadata = getattr(collection, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    kind = str(metadata.get("collection_kind") or metadata.get("kind") or "").lower()
    name = str(getattr(collection, "name", "") or "").lower()
    if (
        "chunk" in kind
        or metadata.get("chunk_index_version") is not None
        or "life_memory_chunks" in name
    ):
        return "chunk"
    return "legacy_node"


def _read_vector_collection(collection: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "kind": _vector_collection_kind(collection),
        "count": None,
        "id_count": 0,
        "ids_complete": False,
        "ids": [],
        "error_types": [],
        "degraded": True,
    }
    if collection is None:
        result["error_types"] = ["CollectionUnavailable"]
        return result

    count: int | None = None
    try:
        count = int(collection.count())
        result["count"] = count
        result["available"] = True
    except Exception as exc:  # vector backends are optional and third-party
        result["error_types"].append(type(exc).__name__)

    try:
        # Keep every returned ID for consistency checks.  Only the displayed
        # orphan-ID list is bounded later, after comparison is complete.
        ids = _collection_ids(collection)
        result["ids"] = ids
        result["id_count"] = len(ids)
        result["ids_complete"] = (
            count is not None and count <= MAX_VECTOR_IDS and len(ids) == count
        )
        result["available"] = True
    except Exception as exc:  # pragma: no cover - backend-specific failures
        result["error_types"].append(type(exc).__name__)

    result["degraded"] = bool(result["error_types"]) or not result["ids_complete"]
    return result


_VectorIdSets = tuple[set[str], set[str], set[str], set[str]]


def _collect_async_db_snapshot(
    db: sqlite3.Connection,
    workspace_path: str | Path,
) -> tuple[dict[str, Any], _VectorIdSets]:
    # Probe before the collector's defensive SQLite error handling so a
    # thread-bound test connection can be handled by the caller.
    db.execute("SELECT 1").fetchone()
    return collect_health_snapshot(db, workspace_path), _vector_id_sets(db)


def _collect_empty_db_snapshot(
    workspace_path: str | Path,
) -> tuple[dict[str, Any], _VectorIdSets]:
    return collect_health_snapshot(None, workspace_path), (set(), set(), set(), set())


def _collect_read_only_db_snapshot(
    db_path: str | Path,
    workspace_path: str | Path,
) -> tuple[dict[str, Any], _VectorIdSets]:
    """Collect one consistent committed snapshot on an isolated read-only handle."""
    path = Path(db_path).expanduser().resolve()
    uri = f"{path.as_uri()}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA busy_timeout = 5000")
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        try:
            return collect_health_snapshot(db, workspace_path), _vector_id_sets(db)
        finally:
            db.rollback()
    finally:
        db.close()


async def _async_db_snapshot(
    db: sqlite3.Connection | None,
    workspace_path: str | Path,
) -> tuple[dict[str, Any], _VectorIdSets]:
    if db is None:
        return await run_db(_collect_empty_db_snapshot, workspace_path)
    try:
        return await run_db(_collect_async_db_snapshot, db, workspace_path)
    except sqlite3.ProgrammingError as exc:
        # Small unit-test connections may retain SQLite's default thread guard.
        # Production connections are created with check_same_thread=False, so
        # this compatibility path is only for callers that own the connection.
        if "created in a thread" not in str(exc):
            raise
        return collect_health_snapshot(db, workspace_path), _vector_id_sets(db)


async def _finish_health_snapshot(
    snapshot: dict[str, Any],
    id_sets: _VectorIdSets,
    collection: Any,
    *,
    vector_expected: bool = True,
    index_worker_expected: bool = True,
    outbox_stall_seconds: float = OUTBOX_STALL_SECONDS,
) -> dict[str, Any]:
    """Attach vector diagnostics and compute one final snapshot status."""
    node_ids, embedded_node_ids, chunk_ids, expected_chunk_ids = id_sets
    vector = await asyncio.to_thread(_read_vector_collection, collection)
    vector["expected"] = bool(vector_expected)
    vector["disabled"] = not bool(vector_expected)

    ids_complete = bool(vector.get("ids_complete"))
    vector_ids = set(str(value) for value in vector.get("ids", []))
    if vector.get("kind") == "chunk":
        orphan_ids = vector_ids - chunk_ids
        missing_ids = expected_chunk_ids - vector_ids if ids_complete else set()
        vector["orphan_chunk_count"] = len(orphan_ids) if ids_complete else None
        vector["orphan_chunk_ids"] = _bounded_paths(orphan_ids)
        vector["missing_chunk_count"] = len(missing_ids) if ids_complete else None
        vector["missing_chunk_ids"] = _bounded_paths(missing_ids)
        vector["orphan_count"] = vector["orphan_chunk_count"]
        vector["orphan_ids"] = vector["orphan_chunk_ids"]
        vector["missing_embedded_node_count"] = None
    else:
        orphan_ids = vector_ids - node_ids
        missing_ids = embedded_node_ids - vector_ids if ids_complete else set()
        vector["orphan_count"] = len(orphan_ids) if ids_complete else None
        vector["orphan_ids"] = _bounded_paths(orphan_ids)
        vector["missing_embedded_node_count"] = (
            len(missing_ids) if ids_complete else None
        )
    vector.pop("ids", None)
    if not vector_expected:
        vector["degraded"] = False
    vector["vector_degraded"] = bool(vector.get("degraded"))
    snapshot["vector"] = vector
    snapshot["vector_count"] = vector.get("count")
    snapshot["vector_id_count"] = vector.get("id_count", 0)
    snapshot["vector_degraded"] = bool(vector.get("degraded"))

    sqlite_section = snapshot.get("sqlite", {})
    index_section = snapshot.get("index", {})
    fts_section = snapshot.get("fts", {})
    edge_section = snapshot.get("edges", {})
    outbox_section = snapshot.get("outbox", {})
    correction_section = snapshot.get("corrections", {})
    sqlite_ok = sqlite_section.get("integrity_ok") is True
    fk_ok = sqlite_section.get("foreign_key_check_ok") is True

    # --- Outbox consumer visibility (AGENTS.md §8) ---
    # The producer enqueues index jobs unconditionally inside the write
    # transaction, while the consumer loop is gated on `memory_index.enabled`.
    # A disabled consumer is an expected absence, but a disabled consumer with
    # a growing backlog is a degradation that must stay visible instead of
    # being reported as a silent `ok`.
    degradations: list[dict[str, Any]] = []
    outbox_section["owner"] = OUTBOX_OWNER
    outbox_section["index_worker_expected"] = bool(index_worker_expected)
    outbox_section["stall_threshold_seconds"] = float(outbox_stall_seconds)
    pending_count = int(outbox_section.get("pending", 0) or 0)
    stale_count = int(outbox_section.get("stale", 0) or 0)
    backlog_count = pending_count + stale_count
    outbox_section["backlog"] = backlog_count
    oldest_age = outbox_section.get("oldest_pending_age_seconds")
    backlog_age = float(oldest_age) if oldest_age is not None else None
    backlog_is_old = backlog_age is not None and backlog_age > float(outbox_stall_seconds)

    if not index_worker_expected:
        backlog_status = "disabled_backlog" if backlog_count else "disabled"
    elif backlog_count == 0:
        backlog_status = "empty"
    elif backlog_is_old:
        backlog_status = "stalled"
    else:
        backlog_status = "ok"
    outbox_section["backlog_status"] = backlog_status
    outbox_degraded = backlog_status in {"disabled_backlog", "stalled"}
    outbox_section["degraded"] = outbox_degraded
    outbox_section["disabled"] = not bool(index_worker_expected)

    degradation_reason: str | None = None
    if backlog_status == "disabled_backlog":
        degradation_reason = (
            "index worker disabled while the outbox still holds "
            f"{backlog_count} job(s); vector projections cannot catch up "
            "until the consumer is re-enabled"
        )
    elif backlog_status == "stalled":
        degradation_reason = (
            f"index worker expected but the oldest pending job is "
            f"{backlog_age:.0f}s old (threshold {float(outbox_stall_seconds):.0f}s); "
            "the consumer is not draining the outbox"
        )
    outbox_section["degradation_reason"] = degradation_reason
    # Re-attach in case the collector produced a snapshot without the section.
    snapshot["outbox"] = outbox_section
    if degradation_reason is not None:
        degradations.append(
            {
                "component": "outbox",
                "owner": OUTBOX_OWNER,
                "status": backlog_status,
                "backlog": backlog_count,
                "last_success_at": outbox_section.get("last_completed_at"),
                "reason": degradation_reason,
            }
        )
    snapshot["degradations"] = degradations

    issue_count = sum(
        int(index_section.get(key, 0) or 0)
        for key in (
            "missing_node_path_count",
            "unindexed_path_count",
            "hash_mismatch_count",
            "read_error_count",
            "ineligible_indexed_node_count",
        )
    )
    issue_count += sum(
        int(fts_section.get(key, 0) or 0)
        for key in ("legacy_fts_orphan_count", "chunk_orphan_count", "chunk_fts_orphan_count")
    )
    issue_count += int(edge_section.get("orphan_count", 0) or 0)
    issue_count += int(edge_section.get("self_loop_count", 0) or 0)
    issue_count += int(outbox_section.get("orphan_node_count", 0) or 0)
    issue_count += int(correction_section.get("orphan_related_node_count", 0) or 0)
    issue_count += int(outbox_section.get("processing", 0) or 0)
    issue_count += int(outbox_section.get("failed", 0) or 0)
    # A backlog counts as exactly one issue, not one per job: the size is
    # already reported in `outbox.backlog`, and a five-figure backlog must not
    # drown the other diagnostics in the aggregate counter.
    if outbox_degraded:
        issue_count += 1
    if vector_expected:
        vector_issue_keys = ["orphan_count"]
        vector_issue_keys.append(
            "missing_chunk_count"
            if vector.get("kind") == "chunk"
            else "missing_embedded_node_count"
        )
        for key in vector_issue_keys:
            value = vector.get(key)
            if value is not None:
                issue_count += int(value or 0)
        if snapshot.get("vector_degraded"):
            issue_count += 1
    if sqlite_section.get("foreign_keys_enabled") is False:
        issue_count += 1
    if any(
        section.get("error_type") or section.get("error_types")
        for section in (
            sqlite_section,
            fts_section,
            edge_section,
            outbox_section,
            correction_section,
        )
    ):
        issue_count += 1
    if sqlite_section.get("available") is not True:
        snapshot["status"] = "unavailable"
    else:
        snapshot["status"] = "ok" if sqlite_ok and fk_ok and issue_count == 0 else "degraded"
    return snapshot


async def health_snapshot(
    db: sqlite3.Connection | None,
    workspace_path: str | Path,
    collection: Any = None,
    *,
    vector_expected: bool = True,
    index_worker_expected: bool = True,
    outbox_stall_seconds: float = OUTBOX_STALL_SECONDS,
) -> dict[str, Any]:
    """Return a JSON-serializable, read-only caller-owned health snapshot."""
    snapshot, id_sets = await _async_db_snapshot(db, workspace_path)
    return await _finish_health_snapshot(
        snapshot,
        id_sets,
        collection,
        vector_expected=vector_expected,
        index_worker_expected=index_worker_expected,
        outbox_stall_seconds=outbox_stall_seconds,
    )


async def health_snapshot_from_path(
    db_path: str | Path,
    workspace_path: str | Path,
    collection: Any = None,
    *,
    vector_expected: bool = True,
    index_worker_expected: bool = True,
    outbox_stall_seconds: float = OUTBOX_STALL_SECONDS,
) -> dict[str, Any]:
    """Collect health through an isolated read-only SQLite connection."""
    try:
        snapshot, id_sets = await run_db(
            _collect_read_only_db_snapshot,
            db_path,
            workspace_path,
        )
    except (OSError, ValueError, sqlite3.Error):
        snapshot, id_sets = await run_db(_collect_empty_db_snapshot, workspace_path)
    return await _finish_health_snapshot(
        snapshot,
        id_sets,
        collection,
        vector_expected=vector_expected,
        index_worker_expected=index_worker_expected,
        outbox_stall_seconds=outbox_stall_seconds,
    )


# Explicit aliases make the sync/async boundary discoverable to callers.
health_snapshot_sync = collect_health_snapshot
health_snapshot_async = health_snapshot


__all__ = [
    "SUPPORTED_WORKSPACE_SUFFIXES",
    "collect_health_snapshot",
    "health_snapshot",
    "health_snapshot_async",
    "health_snapshot_from_path",
    "health_snapshot_sync",
]
