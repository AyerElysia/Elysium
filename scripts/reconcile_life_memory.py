#!/usr/bin/env python3
"""Reconcile eligible workspace documents with the Life Engine SQLite index.

The default mode is a read-only dry run. ``--apply`` is the only mode that
writes indexed rows, and it backs up ``memory.db`` before doing so. This script
deliberately never opens the Chroma vector store.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from plugins.life_engine.memory.eligibility import (  # noqa: E402
    SUPPORTED_DOCUMENT_SUFFIXES,
    assess_indexed_document_path,
    assess_workspace_document,
    normalize_document_path,
    read_workspace_document,
    scan_workspace_documents,
    summarize_rejections,
)

# Compatibility aliases; the shared eligibility module remains authoritative.
SUPPORTED_SUFFIXES = SUPPORTED_DOCUMENT_SUFFIXES
normalize_file_path = normalize_document_path
MAX_REPORTED_PATHS = 1000


class MemoryDatabaseInUseError(RuntimeError):
    """Raised before a write when another process has the memory store open."""


def _database_holders(db_path: Path) -> tuple[int, ...]:
    """Return external PIDs with the SQLite database or its sidecars open.

    Linux exposes descriptor targets through ``/proc``.  Failure to inspect one
    process is deliberately ignored: SQLite's own locking remains the final
    guard, while a visible holder is enough to prevent an unsafe maintenance
    run.  PIDs are intentionally not emitted in the report to avoid exposing
    unrelated process details.
    """
    candidates = (db_path, db_path.with_name(f"{db_path.name}-wal"), db_path.with_name(f"{db_path.name}-shm"))
    targets = {str(candidate.resolve(strict=False)) for candidate in candidates}
    current_pid = os.getpid()
    holders: list[int] = []
    try:
        processes = tuple(Path("/proc").iterdir())
    except OSError:
        return ()

    for process in processes:
        if not process.name.isdigit():
            continue
        process_id = int(process.name)
        if process_id == current_pid:
            continue
        try:
            descriptors = tuple((process / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor).removesuffix(" (deleted)")
                resolved = str(Path(target).resolve(strict=False))
            except OSError:
                continue
            if resolved in targets:
                holders.append(process_id)
                break
    return tuple(sorted(set(holders)))


def compute_content_hash(content: str) -> str:
    """Match the memory node content hash without importing runtime modules."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _load_indexing_helpers() -> tuple[Any, Any, Any]:
    """Load SQLite indexing helpers only for apply mode."""
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        from plugins.life_engine.memory import indexing

    return (
        indexing.create_memory_schema,
        indexing.upsert_document_rows,
        indexing.delete_document_rows_by_id,
    )


def _default_workspace() -> Path:
    return _PROJECT_ROOT / "data" / "life_engine_workspace"


def _bounded_paths(values: list[str] | set[str]) -> list[str]:
    return sorted(str(value) for value in values)[:MAX_REPORTED_PATHS]


def _read_documents(
    workspace: Path,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read bodies only after the shared scanner has accepted their metadata."""
    scan = scan_workspace_documents(workspace, limit=limit)
    documents: list[dict[str, Any]] = []
    for document in scan.documents:
        try:
            content, source_mtime, size_bytes = read_workspace_document(
                workspace,
                document.path,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            documents.append(
                {
                    "path": document.path,
                    "status": "read_error",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        documents.append(
            {
                "path": document.path,
                "content": content,
                "content_hash": compute_content_hash(content) if content else None,
                "source_mtime": source_mtime,
                "size_bytes": size_bytes,
            }
        )

    rejected_paths = [decision.path for decision in scan.rejected if decision.path]
    reason_counts = summarize_rejections(scan.rejected)
    return documents, {
        "eligible_document_count": len(scan.documents),
        "rejected_count": len(scan.rejected),
        "rejected_reason_counts": reason_counts,
        "reason_counts": reason_counts,
        "rejected_paths": _bounded_paths(rejected_paths),
    }


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') "
            "AND name = ? LIMIT 1",
            (table,),
        ).fetchone()
        is not None
    )


def _is_deleted(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return bool(int(value or 0))
    except (TypeError, ValueError):
        return bool(value)


def _indexed_nodes(
    db: sqlite3.Connection,
    workspace: Path,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Return active eligible nodes and active nodes outside shared eligibility.

    An eligible path whose source is now absent remains historical lineage
    evidence. A present symlink, oversized file, special file, or escaped path
    is active pollution and is therefore returned in the second collection.
    """
    if not _table_exists(db, "memory_nodes"):
        return {}, []
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(memory_nodes)").fetchall()}
    fields = ["file_path", "content_hash", "node_id", "is_deleted", "node_type"]
    select = [column if column in columns else f"NULL AS {column}" for column in fields]
    rows = db.execute(f"SELECT {', '.join(select)} FROM memory_nodes").fetchall()
    eligible: dict[str, dict[str, Any]] = {}
    ineligible: list[dict[str, Any]] = []
    for row in rows:
        values = dict(zip(fields, row))
        if _is_deleted(values.get("is_deleted")):
            continue
        if str(values.get("node_type") or "file").lower() != "file":
            continue
        stored_path = values.get("file_path")
        raw_path = "" if stored_path is None else str(stored_path)
        decision = assess_indexed_document_path(stored_path)
        if not decision.eligible:
            values["path"] = raw_path
            values["eligibility_reason"] = decision.reason
            ineligible.append(values)
            continue

        workspace_decision = assess_workspace_document(workspace, decision.path)
        is_missing_history = (
            workspace_decision.reason == "stat_error"
            and not (workspace / decision.path).exists()
        )
        if not workspace_decision.eligible and not is_missing_history:
            values["path"] = workspace_decision.path or decision.path
            values["eligibility_reason"] = (
                workspace_decision.reason or "workspace_ineligible"
            )
            ineligible.append(values)
            continue

        values["path"] = decision.path
        eligible.setdefault(decision.path, values)
    return eligible, ineligible


def _ineligible_index_summary(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    reason_counts: dict[str, int] = {}
    paths: list[str] = []
    for node in nodes:
        path = str(node.get("path") or "")
        if path:
            paths.append(path)
        reason = str(node.get("eligibility_reason") or "")
        if reason:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "ineligible_indexed_node_count": len(nodes),
        "ineligible_indexed_node_paths": _bounded_paths(paths),
        "ineligible_indexed_reason_counts": dict(sorted(reason_counts.items())),
    }


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(db, table):
        return set()
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _orphan_repair_counts(db: sqlite3.Connection) -> dict[str, int]:
    """Count relational/index rows that no longer have a valid owner.

    The counts intentionally contain no paths or document bodies.  They can be
    safely emitted by dry runs and health reports without disclosing memory
    content.
    """
    result = {
        "edge_orphan_count": 0,
        "correction_orphan_count": 0,
        "legacy_fts_orphan_count": 0,
        "chunk_orphan_count": 0,
        "chunk_fts_orphan_count": 0,
        "index_job_orphan_count": 0,
    }
    has_nodes = _table_exists(db, "memory_nodes")

    def _count(sql: str) -> int:
        return int(db.execute(sql).fetchone()[0] or 0)

    edge_columns = _table_columns(db, "memory_edges")
    if {"source_id", "target_id"}.issubset(edge_columns):
        if has_nodes:
            result["edge_orphan_count"] = _count(
                "SELECT COUNT(*) FROM memory_edges e "
                "LEFT JOIN memory_nodes s ON s.node_id = e.source_id "
                "LEFT JOIN memory_nodes t ON t.node_id = e.target_id "
                "WHERE e.source_id IS NULL OR e.target_id IS NULL "
                "OR s.node_id IS NULL OR t.node_id IS NULL"
            )
        else:
            result["edge_orphan_count"] = _count("SELECT COUNT(*) FROM memory_edges")

    correction_columns = _table_columns(db, "memory_corrections")
    if "related_node_id" in correction_columns:
        if has_nodes:
            result["correction_orphan_count"] = _count(
                "SELECT COUNT(*) FROM memory_corrections c "
                "LEFT JOIN memory_nodes n ON n.node_id = c.related_node_id "
                "WHERE c.related_node_id IS NOT NULL AND n.node_id IS NULL"
            )
        else:
            result["correction_orphan_count"] = _count(
                "SELECT COUNT(*) FROM memory_corrections WHERE related_node_id IS NOT NULL"
            )

    def _node_reference_count(table: str, column: str, key: str) -> None:
        if column not in _table_columns(db, table):
            return
        if has_nodes:
            result[key] = _count(
                f"SELECT COUNT(*) FROM {table} r "
                f"LEFT JOIN memory_nodes n ON n.node_id = r.{column} "
                f"WHERE r.{column} IS NULL OR n.node_id IS NULL"
            )
        else:
            result[key] = _count(f"SELECT COUNT(*) FROM {table}")

    _node_reference_count("memory_fts", "node_id", "legacy_fts_orphan_count")
    _node_reference_count("memory_chunks", "node_id", "chunk_orphan_count")
    _node_reference_count("memory_index_jobs", "node_id", "index_job_orphan_count")

    chunk_fts_columns = _table_columns(db, "memory_chunks_fts")
    if {"chunk_id", "node_id"}.issubset(chunk_fts_columns):
        if not _table_exists(db, "memory_chunks") or not has_nodes:
            result["chunk_fts_orphan_count"] = _count(
                "SELECT COUNT(*) FROM memory_chunks_fts"
            )
        else:
            result["chunk_fts_orphan_count"] = _count(
                "SELECT COUNT(*) FROM memory_chunks_fts f "
                "LEFT JOIN memory_chunks c "
                "ON c.chunk_id = f.chunk_id AND c.node_id = f.node_id "
                "LEFT JOIN memory_nodes n ON n.node_id = c.node_id "
                "WHERE f.chunk_id IS NULL OR f.node_id IS NULL "
                "OR c.chunk_id IS NULL OR n.node_id IS NULL"
            )

    result["total_count"] = sum(result.values())
    return result


def _repair_orphan_rows(db: sqlite3.Connection) -> dict[str, int]:
    """Remove only SQLite rows whose relational owner is already absent."""
    before = _orphan_repair_counts(db)
    has_nodes = _table_exists(db, "memory_nodes")
    savepoint: str | None = None
    if db.in_transaction:
        savepoint = "reconcile_orphan_repair"
        db.execute(f"SAVEPOINT {savepoint}")
    else:
        db.execute("BEGIN")

    def _delete_node_references(table: str, column: str) -> None:
        if column not in _table_columns(db, table):
            return
        if has_nodes:
            db.execute(
                f"DELETE FROM {table} WHERE {column} IS NULL OR NOT EXISTS ("
                f"SELECT 1 FROM memory_nodes n WHERE n.node_id = {table}.{column})"
            )
        else:
            db.execute(f"DELETE FROM {table}")

    try:
        _delete_node_references("memory_chunks", "node_id")

        chunk_fts_columns = _table_columns(db, "memory_chunks_fts")
        if {"chunk_id", "node_id"}.issubset(chunk_fts_columns):
            if _table_exists(db, "memory_chunks"):
                db.execute(
                    "DELETE FROM memory_chunks_fts WHERE chunk_id IS NULL OR node_id IS NULL "
                    "OR NOT EXISTS (SELECT 1 FROM memory_chunks c "
                    "WHERE c.chunk_id = memory_chunks_fts.chunk_id "
                    "AND c.node_id = memory_chunks_fts.node_id)"
                )
            else:
                db.execute("DELETE FROM memory_chunks_fts")

        _delete_node_references("memory_fts", "node_id")
        _delete_node_references("memory_index_jobs", "node_id")

        correction_columns = _table_columns(db, "memory_corrections")
        if "related_node_id" in correction_columns:
            if has_nodes:
                db.execute(
                    "UPDATE memory_corrections SET related_node_id = NULL "
                    "WHERE related_node_id IS NOT NULL AND NOT EXISTS ("
                    "SELECT 1 FROM memory_nodes n "
                    "WHERE n.node_id = memory_corrections.related_node_id)"
                )
            else:
                db.execute(
                    "UPDATE memory_corrections SET related_node_id = NULL "
                    "WHERE related_node_id IS NOT NULL"
                )

        edge_columns = _table_columns(db, "memory_edges")
        if {"source_id", "target_id"}.issubset(edge_columns):
            if has_nodes:
                db.execute(
                    "DELETE FROM memory_edges WHERE source_id IS NULL OR target_id IS NULL "
                    "OR NOT EXISTS (SELECT 1 FROM memory_nodes s "
                    "WHERE s.node_id = memory_edges.source_id) "
                    "OR NOT EXISTS (SELECT 1 FROM memory_nodes t "
                    "WHERE t.node_id = memory_edges.target_id)"
                )
            else:
                db.execute("DELETE FROM memory_edges")

        if savepoint is not None:
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            db.commit()
    except sqlite3.Error:
        if savepoint is not None:
            db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            db.rollback()
        raise

    after = _orphan_repair_counts(db)
    repaired = {
        key: max(0, int(before[key]) - int(after[key]))
        for key in before
        if key != "total_count"
    }
    repaired["total_count"] = sum(repaired.values())
    return repaired


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    # The pragma is connection-local and safe in read-only mode.  It makes the
    # diagnostic reflect enforcement capability rather than SQLite's default.
    db.execute("PRAGMA foreign_keys = ON")
    return db


def _open_apply(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def _backup_database(db_path: Path) -> Path | None:
    if not db_path.exists():
        return None
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / f"memory-{timestamp}.db"
    source = sqlite3.connect(str(db_path), check_same_thread=False)
    destination = sqlite3.connect(str(backup_path), check_same_thread=False)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()
    return backup_path


def reconcile(
    workspace: Path,
    *,
    apply: bool = False,
    limit: int | None = None,
    rebuild: bool = False,
    prune_ineligible: bool = False,
    repair_orphans: bool = False,
) -> dict[str, Any]:
    """Report or apply SQLite-only document-index reconciliation work."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")
    if rebuild and not apply:
        raise ValueError("--rebuild requires --apply")
    if prune_ineligible and not apply:
        raise ValueError("--prune-ineligible requires --apply")
    if repair_orphans and not apply:
        raise ValueError("--repair-orphans requires --apply")

    workspace = workspace.expanduser().resolve()
    db_path = workspace / ".memory" / "memory.db"
    if apply and _database_holders(db_path):
        raise MemoryDatabaseInUseError("memory.db is held by an active process")
    documents, eligibility = _read_documents(workspace, limit)

    create_memory_schema: Any = None
    upsert_document_rows: Any = None
    delete_document_rows_by_id: Any = None
    db: sqlite3.Connection | None = None
    if apply:
        (
            create_memory_schema,
            upsert_document_rows,
            delete_document_rows_by_id,
        ) = _load_indexing_helpers()
        # This occurs before schema upgrades, upserts, and ineligible-node
        # pruning so every SQLite mutation has a recoverable predecessor.
        backup_path = _backup_database(db_path)
        db = _open_apply(db_path)
    elif db_path.exists():
        backup_path = None
        db = _open_readonly(db_path)
    else:
        backup_path = None

    try:
        if apply and db is not None:
            # Keep schema migration, planning, SQLite mutations, and outbox
            # enqueueing in one reserved-lock transaction. Nested indexing
            # helpers use savepoints while this root transaction is active.
            db.execute("BEGIN IMMEDIATE")
            create_memory_schema(db)
        indexed, ineligible_indexed_nodes = (
            _indexed_nodes(db, workspace) if db is not None else ({}, [])
        )
        ineligible_index = _ineligible_index_summary(ineligible_indexed_nodes)
        orphan_repair_candidates = (
            _orphan_repair_counts(db)
            if db is not None
            else {
                "edge_orphan_count": 0,
                "correction_orphan_count": 0,
                "legacy_fts_orphan_count": 0,
                "chunk_orphan_count": 0,
                "chunk_fts_orphan_count": 0,
                "index_job_orphan_count": 0,
                "total_count": 0,
            }
        )
        new_paths: list[str] = []
        changed_paths: list[str] = []
        unchanged_paths: list[str] = []
        read_error_paths: list[str] = []
        selected_documents: list[dict[str, Any]] = []
        for document in documents:
            path = str(document["path"])
            if document.get("status") == "read_error":
                read_error_paths.append(path)
                continue
            selected_documents.append(document)
            row = indexed.get(path)
            if row is None:
                new_paths.append(path)
            elif row.get("content_hash") != document.get("content_hash"):
                changed_paths.append(path)
            else:
                unchanged_paths.append(path)

        workspace_paths = {
            str(document["path"])
            for document in documents
            if document.get("status") != "read_error"
        }
        # With --limit, the scan is intentionally partial; do not call
        # unvisited indexed nodes deletions or missing paths.
        missing_paths = [] if limit is not None else sorted(set(indexed) - workspace_paths)

        rebuild_candidate_paths = (
            [str(document["path"]) for document in selected_documents] if rebuild else []
        )
        prune_candidate_count = len(ineligible_indexed_nodes) if prune_ineligible else 0
        prune_candidate_paths = (
            [
                str(node.get("path") or "")
                for node in ineligible_indexed_nodes
                if str(node.get("path") or "")
            ]
            if prune_ineligible
            else []
        )
        pruned_paths: list[str] = []
        pruned_node_count = 0
        if apply and prune_ineligible and db is not None:
            for node in ineligible_indexed_nodes:
                path = str(node.get("path") or "")
                node_id = str(node.get("node_id") or "")
                # Persisted paths are never normalized for deletion. The stored
                # ID identifies the exact legacy or malformed row to remove.
                deleted = bool(delete_document_rows_by_id(db, node_id))
                if deleted:
                    pruned_node_count += 1
                    if path:
                        pruned_paths.append(path)

        applied_paths: list[str] = []
        rebuilt_paths: list[str] = []
        paths_to_upsert = (
            {str(document["path"]) for document in selected_documents}
            if rebuild
            else set(new_paths) | set(changed_paths)
        )
        if apply and db is not None and upsert_document_rows is not None:
            for document in selected_documents:
                path = str(document["path"])
                if path not in paths_to_upsert:
                    continue
                upsert_document_rows(
                    db,
                    path,
                    str(document["content"]),
                    Path(path).stem,
                    float(document["source_mtime"]),
                )
                applied_paths.append(path)
                if rebuild:
                    rebuilt_paths.append(path)

        orphan_repair_applied = {
            "edge_orphan_count": 0,
            "correction_orphan_count": 0,
            "legacy_fts_orphan_count": 0,
            "chunk_orphan_count": 0,
            "chunk_fts_orphan_count": 0,
            "index_job_orphan_count": 0,
            "total_count": 0,
        }
        if apply and repair_orphans and db is not None:
            orphan_repair_applied = _repair_orphan_rows(db)

        summary = {
            "mode": "apply" if apply else "dry-run",
            "workspace": str(workspace),
            "database": str(db_path),
            "database_exists": db_path.exists(),
            "backup": str(backup_path) if backup_path is not None else None,
            "limit": limit,
            # Retain this old report key, but source it from shared eligibility.
            "supported_suffixes": sorted(SUPPORTED_DOCUMENT_SUFFIXES),
            "eligibility": eligibility,
            "scanned_count": len(documents),
            "new_count": len(new_paths),
            "new_paths": sorted(new_paths),
            "changed_count": len(changed_paths),
            "changed_paths": sorted(changed_paths),
            "unchanged_count": len(unchanged_paths),
            "unchanged_paths": sorted(unchanged_paths),
            "missing_node_count": len(missing_paths),
            "missing_node_paths": missing_paths,
            "read_error_count": len(read_error_paths),
            "read_error_paths": sorted(read_error_paths),
            "applied_count": len(applied_paths),
            "applied_paths": sorted(applied_paths),
            **ineligible_index,
            "index": ineligible_index,
            "rebuild_requested": rebuild,
            "rebuild_candidate_count": len(rebuild_candidate_paths),
            "rebuild_candidate_paths": _bounded_paths(rebuild_candidate_paths),
            "rebuild_count": len(rebuilt_paths),
            "rebuild_paths": _bounded_paths(rebuilt_paths),
            "rebuilt_count": len(rebuilt_paths),
            "rebuilt_paths": _bounded_paths(rebuilt_paths),
            "prune_ineligible_requested": prune_ineligible,
            "prune_candidate_count": prune_candidate_count,
            "prune_candidate_paths": _bounded_paths(prune_candidate_paths),
            "prune_ineligible_count": pruned_node_count,
            "prune_ineligible_paths": _bounded_paths(pruned_paths),
            "prune_count": pruned_node_count,
            "prune_paths": _bounded_paths(pruned_paths),
            "pruned_count": pruned_node_count,
            "pruned_paths": _bounded_paths(pruned_paths),
            "repair_orphans_requested": repair_orphans,
            "repair_orphan_candidates": orphan_repair_candidates,
            "repair_orphan_candidate_count": orphan_repair_candidates["total_count"],
            "repair_orphans": orphan_repair_applied,
            "repair_orphan_count": orphan_repair_applied["total_count"],
            "deletions": pruned_node_count + orphan_repair_applied["total_count"],
        }
        if apply and db is not None:
            db.commit()
        return summary
    except BaseException:
        if apply and db is not None and db.in_transaction:
            db.rollback()
        raise
    finally:
        if db is not None:
            db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=_default_workspace(),
        help="Life Engine workspace root (default: data/life_engine_workspace)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Back up memory.db and apply SQLite reconciliation changes",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="With --apply, re-upsert every readable eligible document",
    )
    parser.add_argument(
        "--prune-ineligible",
        action="store_true",
        help="With --apply, remove active indexed file nodes outside eligibility",
    )
    parser.add_argument(
        "--repair-orphans",
        action="store_true",
        help="With --apply, repair orphaned SQLite relations and index rows",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of eligible documents to scan",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0")
    if args.rebuild and not args.apply:
        parser.error("--rebuild requires --apply")
    if args.prune_ineligible and not args.apply:
        parser.error("--prune-ineligible requires --apply")
    if args.repair_orphans and not args.apply:
        parser.error("--repair-orphans requires --apply")
    try:
        summary = reconcile(
            args.workspace,
            apply=bool(args.apply),
            limit=args.limit,
            rebuild=bool(args.rebuild),
            prune_ineligible=bool(args.prune_ineligible),
            repair_orphans=bool(args.repair_orphans),
        )
    except (OSError, sqlite3.Error, ValueError, MemoryDatabaseInUseError) as exc:
        parser.exit(1, f"reconcile failed: {type(exc).__name__}\n")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
