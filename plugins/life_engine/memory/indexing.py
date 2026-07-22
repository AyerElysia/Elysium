"""SQLite document-index primitives for Life Engine memory.

This module contains only synchronous SQLite operations.  The service layer is
responsible for running them in ``asyncio.to_thread`` and serializing writes.
No vector database or network operation belongs here.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from .eligibility import assess_document_path
from .nodes import (
    NodeType,
    canonical_file_node_id,
    compute_content_hash,
    generate_file_node_id,
)
from .temporal import extract_document_date

INDEX_SCHEMA_NAME = "document_index"
INDEX_SCHEMA_VERSION = 4
ACTIVE_CHUNK_STATE_KEY = "active_chunk_collection"
DEFAULT_CHUNK_SIZE = 900
DEFAULT_CHUNK_OVERLAP = 120


class DocumentIdentityConflict(ValueError):
    """Raised when a file-node ID cannot safely map to one document path."""


@dataclass(frozen=True)
class DocumentChunk:
    """A deterministic piece of a document."""

    chunk_id: str
    node_id: str
    chunk_index: int
    content_hash: str
    content: str
    title: str = ""


@dataclass(frozen=True)
class DocumentIndexResult:
    """Result of a document write transaction."""

    node_id: str
    file_path: str
    content_hash: str | None
    chunks: tuple[DocumentChunk, ...]
    job_id: str | None
    source_mtime: float | None = None


@dataclass(frozen=True)
class IndexJob:
    """An outbox job waiting for a future embedding/index worker."""

    job_id: str
    node_id: str
    content_hash: str
    status: str
    created_at: float
    updated_at: float
    attempts: int = 0
    error: str = ""
    index_revision: int = 0


@dataclass(frozen=True)
class ChunkIndexState:
    """Persisted identity of the active chunk-vector collection."""

    collection_name: str
    model_name: str
    dimension: int
    version: int
    updated_at: float


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _configure_connection(db: sqlite3.Connection) -> None:
    """Use named rows for helpers regardless of the caller's connection setup."""
    if db.row_factory is None:
        db.row_factory = sqlite3.Row


def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_index_tables(db: sqlite3.Connection) -> None:
    """Make standalone row helpers safe on a minimally initialized connection."""
    _configure_connection(db)
    required = (
        "memory_nodes",
        "memory_schema",
        "memory_chunks",
        "memory_chunks_fts",
        "memory_index_jobs",
        "memory_index_state",
    )
    if any(not _table_exists(db, table) for table in required):
        create_memory_schema(db)

@contextmanager
def transaction(
    db: sqlite3.Connection,
    *,
    immediate: bool = False,
) -> Iterator[sqlite3.Cursor]:
    """Run work in a transaction, supporting callers already in a transaction.

    ``immediate=True`` acquires SQLite's reserved write lock at the root so a
    maintenance batch cannot interleave its planning and mutation phases with a
    second writer. Nested callers always use savepoints.
    """
    savepoint: str | None = None
    if db.in_transaction:
        savepoint = f"life_index_{id(db):x}_{time.monotonic_ns()}"
        db.execute(f"SAVEPOINT {savepoint}")
    else:
        db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    cursor = db.cursor()
    try:
        yield cursor
        if savepoint is not None:
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            db.commit()
    except Exception:
        if savepoint is not None:
            db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            db.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            db.rollback()
        raise


def _ensure_memory_nodes(db: sqlite3.Connection) -> None:
    """Create or minimally upgrade the compatible node table."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_nodes (
            node_id TEXT PRIMARY KEY,
            node_type TEXT NOT NULL,
            file_path TEXT,
            content_hash TEXT,
            title TEXT,
            activation_strength REAL DEFAULT 1.0,
            access_count INTEGER DEFAULT 0,
            last_accessed_at REAL,
            emotional_valence REAL DEFAULT 0.0,
            emotional_arousal REAL DEFAULT 0.0,
            importance REAL DEFAULT 0.5,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            embedding_synced INTEGER DEFAULT 0,
            source_mtime REAL,
            event_date TEXT,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            fts_content_hash TEXT,
            embedding_content_hash TEXT,
            embedding_model TEXT,
            embedding_updated_at REAL,
            index_revision INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Existing installations may have an older, narrower memory_nodes table.
    # SQLite cannot add a required column without a default for old rows.
    legacy_columns = (
        ("node_type", "TEXT NOT NULL DEFAULT 'file'"),
        ("file_path", "TEXT"),
        ("content_hash", "TEXT"),
        ("title", "TEXT NOT NULL DEFAULT ''"),
        ("activation_strength", "REAL NOT NULL DEFAULT 1.0"),
        ("access_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_accessed_at", "REAL"),
        ("emotional_valence", "REAL NOT NULL DEFAULT 0.0"),
        ("emotional_arousal", "REAL NOT NULL DEFAULT 0.0"),
        ("importance", "REAL NOT NULL DEFAULT 0.5"),
        ("created_at", "REAL NOT NULL DEFAULT 0"),
        ("updated_at", "REAL NOT NULL DEFAULT 0"),
        ("embedding_synced", "INTEGER NOT NULL DEFAULT 0"),
        ("source_mtime", "REAL"),
        ("event_date", "TEXT"),
        ("is_deleted", "INTEGER NOT NULL DEFAULT 0"),
        ("fts_content_hash", "TEXT"),
        ("embedding_content_hash", "TEXT"),
        ("embedding_model", "TEXT"),
        ("embedding_updated_at", "REAL"),
        ("index_revision", "INTEGER NOT NULL DEFAULT 0"),
    )
    for column, definition in legacy_columns:
        _ensure_column(db, "memory_nodes", column, definition)


def _try_create_chunks_fts(db: sqlite3.Connection) -> str:
    """Create the chunk FTS table, preferring trigram where available."""
    if _table_exists(db, "memory_chunks_fts"):
        return ""
    definition = "chunk_id, node_id, content, title"
    try:
        db.execute(
            "CREATE VIRTUAL TABLE memory_chunks_fts USING fts5("
            f"{definition}, tokenize='trigram')"
        )
        return "trigram"
    except sqlite3.Error:
        # A failed virtual-table creation can leave a partially-created object
        # on some SQLite builds.  Only the new index object is removed here.
        db.execute("DROP TABLE IF EXISTS memory_chunks_fts")
        db.execute(
            "CREATE VIRTUAL TABLE memory_chunks_fts USING fts5("
            f"{definition}, tokenize='unicode61')"
        )
        return "unicode61"


def _existing_fts_tokenizer(db: sqlite3.Connection) -> str:
    row = db.execute(
        "SELECT tokenizer FROM memory_schema WHERE schema_name = ?",
        (INDEX_SCHEMA_NAME,),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    sql_row = db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'memory_chunks_fts'"
    ).fetchone()
    sql = str(sql_row[0] if sql_row else "").lower()
    return "trigram" if "trigram" in sql else "unicode61"


def create_memory_schema(db: sqlite3.Connection, *, now: float | None = None) -> str:
    """Create or upgrade the document-index tables on ``db``.

    Existing tables are preserved.  The returned tokenizer is either
    ``trigram`` or ``unicode61`` and is also recorded in ``memory_schema``.
    """
    timestamp = float(time.time() if now is None else now)
    if db.row_factory is None:
        db.row_factory = sqlite3.Row
    with transaction(db):
        _ensure_memory_nodes(db)
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_schema (
                schema_name TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                tokenizer TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        _ensure_column(db, "memory_schema", "version", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(db, "memory_schema", "tokenizer", "TEXT NOT NULL DEFAULT 'unicode61'")
        _ensure_column(db, "memory_schema", "updated_at", "REAL NOT NULL DEFAULT 0")
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_chunks (
                chunk_id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (node_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE
            )
            """
        )
        _ensure_column(db, "memory_chunks", "node_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "memory_chunks", "chunk_index", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "memory_chunks", "content_hash", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "memory_chunks", "content", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "memory_chunks", "title", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "memory_chunks", "created_at", "REAL NOT NULL DEFAULT 0")
        _ensure_column(db, "memory_chunks", "updated_at", "REAL NOT NULL DEFAULT 0")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_chunks_node_index "
            "ON memory_chunks(node_id, chunk_index)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_index_jobs (
                job_id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                index_revision INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (node_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE
            )
            """
        )
        _ensure_column(db, "memory_index_jobs", "job_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "memory_index_jobs", "node_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "memory_index_jobs", "content_hash", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "memory_index_jobs", "status", "TEXT NOT NULL DEFAULT 'pending'")
        _ensure_column(db, "memory_index_jobs", "created_at", "REAL NOT NULL DEFAULT 0")
        _ensure_column(db, "memory_index_jobs", "updated_at", "REAL NOT NULL DEFAULT 0")
        _ensure_column(db, "memory_index_jobs", "attempts", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "memory_index_jobs", "error", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "memory_index_jobs", "index_revision", "INTEGER NOT NULL DEFAULT 0")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_index_jobs_status "
            "ON memory_index_jobs(status, created_at)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_index_state (
                state_key TEXT PRIMARY KEY,
                collection_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                version INTEGER NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        _ensure_column(db, "memory_index_state", "collection_name", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "memory_index_state", "model_name", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "memory_index_state", "dimension", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "memory_index_state", "version", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "memory_index_state", "updated_at", "REAL NOT NULL DEFAULT 0")
        tokenizer = _try_create_chunks_fts(db)
        if not tokenizer:
            tokenizer = _existing_fts_tokenizer(db)
        existing_schema = db.execute(
            "SELECT 1 FROM memory_schema WHERE schema_name = ?",
            (INDEX_SCHEMA_NAME,),
        ).fetchone()
        if existing_schema:
            db.execute(
                "UPDATE memory_schema SET version = ?, tokenizer = ?, updated_at = ? "
                "WHERE schema_name = ?",
                (INDEX_SCHEMA_VERSION, tokenizer, timestamp, INDEX_SCHEMA_NAME),
            )
        else:
            db.execute(
                "INSERT INTO memory_schema(schema_name, version, tokenizer, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (INDEX_SCHEMA_NAME, INDEX_SCHEMA_VERSION, tokenizer, timestamp),
            )
    return tokenizer


# Descriptive aliases used by callers that prefer a verb.
ensure_memory_schema = create_memory_schema
create_index_schema = create_memory_schema


def read_active_chunk_index_state(db: sqlite3.Connection) -> ChunkIndexState | None:
    """Read the single active chunk collection marker, if one exists."""
    _ensure_index_tables(db)
    row = db.execute(
        "SELECT collection_name, model_name, dimension, version, updated_at "
        "FROM memory_index_state WHERE state_key = ?",
        (ACTIVE_CHUNK_STATE_KEY,),
    ).fetchone()
    if row is None:
        return None
    return ChunkIndexState(
        collection_name=str(row["collection_name"] or ""),
        model_name=str(row["model_name"] or ""),
        dimension=int(row["dimension"] or 0),
        version=int(row["version"] or 0),
        updated_at=float(row["updated_at"] or 0.0),
    )


def write_active_chunk_index_state(
    db: sqlite3.Connection,
    collection_name: str,
    model_name: str,
    dimension: int,
    version: int,
    *,
    now: float | None = None,
) -> ChunkIndexState:
    """Atomically replace the active chunk collection marker."""
    _ensure_index_tables(db)
    name = str(collection_name or "").strip()
    model = str(model_name or "").strip()
    vector_dimension = int(dimension)
    index_version = int(version)
    if not name or not model:
        raise ValueError("collection_name 和 model_name 不能为空")
    if vector_dimension <= 0 or index_version <= 0:
        raise ValueError("dimension 和 version 必须为正整数")
    timestamp = float(time.time() if now is None else now)
    with transaction(db):
        db.execute(
            """
            INSERT INTO memory_index_state
                (state_key, collection_name, model_name, dimension, version, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                collection_name = excluded.collection_name,
                model_name = excluded.model_name,
                dimension = excluded.dimension,
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (
                ACTIVE_CHUNK_STATE_KEY,
                name,
                model,
                vector_dimension,
                index_version,
                timestamp,
            ),
        )
    return ChunkIndexState(
        collection_name=name,
        model_name=model,
        dimension=vector_dimension,
        version=index_version,
        updated_at=timestamp,
    )


def chunk_document(
    node_id: str,
    content: str,
    title: str = "",
    *,
    max_chars: int = DEFAULT_CHUNK_SIZE,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP,
) -> list[DocumentChunk]:
    """Split content deterministically with a bounded overlap.

    Character slicing is intentional: it is independent of tokenizer versions,
    locale, and platform newline handling.  Empty chunks are never emitted.
    Invalid overlap values are clamped so the cursor always advances.
    """
    text = str(content or "")
    if not text:
        return []
    size = max(1, int(max_chars))
    overlap = max(0, min(int(overlap_chars), size - 1))
    step = size - overlap
    chunks: list[DocumentChunk] = []
    start = 0
    index = 0
    while start < len(text):
        chunk_content = text[start : start + size]
        if not chunk_content:
            break
        chunk_hash = compute_content_hash(chunk_content)
        chunk_id = f"{node_id}:{index}:{chunk_hash}"
        chunks.append(
            DocumentChunk(
                chunk_id=chunk_id,
                node_id=node_id,
                chunk_index=index,
                content_hash=chunk_hash,
                content=chunk_content,
                title=str(title or ""),
            )
        )
        index += 1
        if start + size >= len(text):
            break
        start += step
    return chunks


def _enqueue_index_job_in_transaction(
    db: sqlite3.Connection,
    node_id: str,
    content_hash: str,
    *,
    now: float,
    status: str = "pending",
    index_revision: int | None = None,
) -> str:
    job_id = f"{node_id}:{content_hash}"
    if index_revision is None:
        row = db.execute(
            "SELECT index_revision FROM memory_nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        index_revision = int(row[0] or 0) if row else 0
    existing = db.execute(
        "SELECT 1 FROM memory_index_jobs WHERE job_id = ? LIMIT 1",
        (job_id,),
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE memory_index_jobs SET node_id = ?, content_hash = ?, status = ?, "
            "index_revision = ?, updated_at = ?, error = '' WHERE job_id = ?",
            (node_id, content_hash, status, int(index_revision), now, job_id),
        )
    else:
        db.execute(
            """
            INSERT INTO memory_index_jobs
                (job_id, node_id, content_hash, status, created_at, updated_at, attempts, error,
                 index_revision)
            VALUES (?, ?, ?, ?, ?, ?, 0, '', ?)
            """,
            (job_id, node_id, content_hash, status, now, now, int(index_revision)),
        )
    return job_id


def enqueue_index_job(
    db: sqlite3.Connection,
    node_id: str,
    content_hash: str,
    *,
    now: float | None = None,
    status: str = "pending",
) -> str:
    """Insert or reset one deterministic pending outbox job."""
    if not node_id or not content_hash:
        raise ValueError("node_id 和 content_hash 不能为空")
    _ensure_index_tables(db)
    timestamp = float(time.time() if now is None else now)
    with transaction(db):
        return _enqueue_index_job_in_transaction(
            db, node_id, content_hash, now=timestamp, status=status
        )


def _replace_old_fts(
    db: sqlite3.Connection,
    node_id: str,
    title: str,
    content: str,
) -> None:
    """Keep the legacy node FTS table in sync when it exists."""
    if not _table_exists(db, "memory_fts"):
        return
    db.execute("DELETE FROM memory_fts WHERE node_id = ?", (node_id,))
    if not content:
        return
    db.execute(
        "INSERT INTO memory_fts(node_id, title, content) VALUES (?, ?, ?)",
        (node_id, title, content),
    )


def _replace_chunk_fts(
    db: sqlite3.Connection,
    node_id: str,
    chunks: Sequence[DocumentChunk],
) -> None:
    db.execute("DELETE FROM memory_chunks_fts WHERE node_id = ?", (node_id,))
    db.executemany(
        "INSERT INTO memory_chunks_fts(chunk_id, node_id, content, title) VALUES (?, ?, ?, ?)",
        [(chunk.chunk_id, node_id, chunk.content, chunk.title) for chunk in chunks],
    )


def upsert_document_rows(
    db: sqlite3.Connection,
    file_path: str,
    content: str,
    title: str = "",
    source_mtime: float | None = None,
    *,
    now: float | None = None,
    max_chars: int = DEFAULT_CHUNK_SIZE,
    overlap_chars: int = DEFAULT_CHUNK_OVERLAP,
) -> DocumentIndexResult:
    """Atomically upsert one eligible document, its FTS rows, chunks, and outbox job."""
    normalized_path, node_id = canonical_file_node_id(file_path)
    _ensure_index_tables(db)
    text = str(content or "")
    document_title = str(title or Path(normalized_path).stem)
    content_hash = compute_content_hash(text) if text else None
    event_date = extract_document_date(normalized_path, document_title, text)
    event_date_text = event_date.isoformat() if event_date is not None else None
    timestamp = float(time.time() if now is None else now)
    chunks = tuple(
        chunk_document(
            node_id,
            text,
            document_title,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
    )
    job_id: str | None = None

    with transaction(db):
        _ensure_memory_nodes(db)
        existing = db.execute(
            "SELECT node_type, file_path, created_at FROM memory_nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if existing is not None:
            existing_path = str(existing["file_path"] or "")
            existing_type = str(existing["node_type"] or "file").lower()
            if existing_type != NodeType.FILE.value or existing_path != normalized_path:
                raise DocumentIdentityConflict("node ID belongs to another document")
        created_at = (
            float(existing["created_at"])
            if existing is not None and existing["created_at"] is not None
            else timestamp
        )
        db.execute(
            """
            INSERT INTO memory_nodes
                (node_id, node_type, file_path, content_hash, title,
                 activation_strength, access_count, last_accessed_at,
                 emotional_valence, emotional_arousal, importance,
                 created_at, updated_at, embedding_synced, source_mtime,
                 event_date, is_deleted, fts_content_hash, index_revision)
            VALUES (?, ?, ?, ?, ?, 1.0, 0, NULL, 0.0, 0.0, 0.5, ?, ?, 0, ?, ?, 0, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                node_type = excluded.node_type,
                file_path = excluded.file_path,
                content_hash = excluded.content_hash,
                title = excluded.title,
                updated_at = excluded.updated_at,
                embedding_synced = 0,
                embedding_content_hash = NULL,
                embedding_model = NULL,
                embedding_updated_at = NULL,
                source_mtime = COALESCE(excluded.source_mtime, memory_nodes.source_mtime),
                event_date = excluded.event_date,
                is_deleted = 0,
                fts_content_hash = excluded.fts_content_hash,
                index_revision = memory_nodes.index_revision + 1
            """,
            (
                node_id,
                NodeType.FILE.value,
                normalized_path,
                content_hash,
                document_title,
                created_at,
                timestamp,
                source_mtime,
                event_date_text,
                content_hash,
                1,
            ),
        )
        _replace_old_fts(db, node_id, document_title, text)
        db.execute("DELETE FROM memory_chunks WHERE node_id = ?", (node_id,))
        if chunks:
            db.executemany(
                """
                INSERT INTO memory_chunks
                    (chunk_id, node_id, chunk_index, content_hash, content, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.chunk_id,
                        chunk.node_id,
                        chunk.chunk_index,
                        chunk.content_hash,
                        chunk.content,
                        chunk.title,
                        timestamp,
                        timestamp,
                    )
                    for chunk in chunks
                ],
            )
        _replace_chunk_fts(db, node_id, chunks)
        if content_hash:
            db.execute(
                "UPDATE memory_index_jobs SET status = 'stale', updated_at = ?, "
                "error = 'SupersededContent' WHERE node_id = ? AND content_hash <> ?",
                (timestamp, node_id, content_hash),
            )
            job_id = _enqueue_index_job_in_transaction(
                db,
                node_id,
                content_hash,
                now=timestamp,
            )
        else:
            db.execute("DELETE FROM memory_index_jobs WHERE node_id = ?", (node_id,))

    return DocumentIndexResult(
        node_id=node_id,
        file_path=normalized_path,
        content_hash=content_hash,
        chunks=chunks,
        job_id=job_id,
        source_mtime=source_mtime,
    )


def delete_document_rows_by_id(
    db: sqlite3.Connection,
    node_id: str,
) -> bool:
    """Atomically delete one stored file node and every SQLite reference."""
    identifier = str(node_id or "").strip()
    if not identifier:
        return False
    _ensure_index_tables(db)
    with transaction(db):
        row = db.execute(
            "SELECT 1 FROM memory_nodes WHERE node_id = ?",
            (identifier,),
        ).fetchone()
        if row is None:
            return False
        if _table_exists(db, "memory_chunks_fts"):
            db.execute("DELETE FROM memory_chunks_fts WHERE node_id = ?", (identifier,))
        if _table_exists(db, "memory_fts"):
            db.execute("DELETE FROM memory_fts WHERE node_id = ?", (identifier,))
        if _table_exists(db, "memory_chunks"):
            db.execute("DELETE FROM memory_chunks WHERE node_id = ?", (identifier,))
        if _table_exists(db, "memory_index_jobs"):
            db.execute("DELETE FROM memory_index_jobs WHERE node_id = ?", (identifier,))
        if _table_exists(db, "memory_edges"):
            db.execute(
                "DELETE FROM memory_edges WHERE source_id = ? OR target_id = ?",
                (identifier, identifier),
            )
        if _table_exists(db, "memory_corrections"):
            db.execute(
                "UPDATE memory_corrections SET related_node_id = NULL WHERE related_node_id = ?",
                (identifier,),
            )
        db.execute("DELETE FROM memory_nodes WHERE node_id = ?", (identifier,))
    return True


def _has_node_references(db: sqlite3.Connection, node_id: str) -> bool:
    """Return whether dangling rows already claim a future node identity."""
    checks = (
        ("memory_edges", "source_id = ? OR target_id = ?", (node_id, node_id)),
        ("memory_corrections", "related_node_id = ?", (node_id,)),
        ("memory_fts", "node_id = ?", (node_id,)),
        ("memory_chunks", "node_id = ?", (node_id,)),
        ("memory_chunks_fts", "node_id = ?", (node_id,)),
        ("memory_index_jobs", "node_id = ?", (node_id,)),
    )
    for table, predicate, params in checks:
        if _table_exists(db, table) and db.execute(
            f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1", params
        ).fetchone():
            return True
    return False


def rekey_document_rows_by_id(
    db: sqlite3.Connection,
    old_node_id: str,
    new_path: str,
    *,
    expected_old_path: str | None = None,
    now: float | None = None,
) -> bool:
    """Move one file node to a canonical identity without implicit merging.

    Every SQLite reference moves in one transaction.  Existing target rows or
    dangling target references are conflicts, never candidates for a lossy
    merge.  Chroma is deliberately not touched; a fresh outbox job makes the
    vector side eventually consistent.
    """
    target_path, target_node_id = canonical_file_node_id(new_path)
    expected_path = None
    if expected_old_path is not None:
        expected_path, _ = canonical_file_node_id(expected_old_path)
    source_id = str(old_node_id or "").strip()
    if not source_id:
        return False
    _ensure_index_tables(db)
    timestamp = float(time.time() if now is None else now)

    with transaction(db):
        old_row = db.execute(
            "SELECT * FROM memory_nodes WHERE node_id = ?",
            (source_id,),
        ).fetchone()
        if old_row is None:
            return False
        if str(old_row["node_type"] or "file").lower() != NodeType.FILE.value:
            raise DocumentIdentityConflict("source node is not a file node")
        old_decision = assess_document_path(str(old_row["file_path"] or ""))
        if not old_decision.eligible:
            raise DocumentIdentityConflict("source path is not an eligible document")
        if expected_path is not None and old_decision.path != expected_path:
            raise DocumentIdentityConflict("source path does not match expected identity")

        if source_id == target_node_id:
            if str(old_row["file_path"] or "") != target_path:
                db.execute(
                    "UPDATE memory_nodes SET file_path = ?, updated_at = ?, "
                    "embedding_synced = 0, embedding_content_hash = NULL, "
                    "embedding_model = NULL, embedding_updated_at = NULL, "
                    "index_revision = index_revision + 1 WHERE node_id = ?",
                    (target_path, timestamp, source_id),
                )
            return True

        target_row = db.execute(
            "SELECT 1 FROM memory_nodes WHERE node_id = ?",
            (target_node_id,),
        ).fetchone()
        if target_row is not None or _has_node_references(db, target_node_id):
            raise DocumentIdentityConflict("target identity is already occupied")

        source_mtime = old_row["source_mtime"]
        source_revision = int(old_row["index_revision"] or 0)
        target_revision = source_revision + 1
        db.execute(
            """
            INSERT INTO memory_nodes
                (node_id, node_type, file_path, content_hash, title,
                 activation_strength, access_count, last_accessed_at,
                 emotional_valence, emotional_arousal, importance,
                 created_at, updated_at, embedding_synced, source_mtime,
                 event_date, is_deleted, fts_content_hash, embedding_content_hash,
                 embedding_model, embedding_updated_at, index_revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (
                target_node_id,
                NodeType.FILE.value,
                target_path,
                old_row["content_hash"],
                old_row["title"],
                old_row["activation_strength"],
                old_row["access_count"],
                old_row["last_accessed_at"],
                old_row["emotional_valence"],
                old_row["emotional_arousal"],
                old_row["importance"],
                old_row["created_at"],
                timestamp,
                source_mtime,
                old_row["event_date"],
                old_row["is_deleted"],
                old_row["fts_content_hash"],
                target_revision,
            ),
        )

        if _table_exists(db, "memory_edges"):
            db.execute(
                "UPDATE memory_edges "
                "SET source_id = CASE WHEN source_id = ? THEN ? ELSE source_id END, "
                "target_id = CASE WHEN target_id = ? THEN ? ELSE target_id END "
                "WHERE source_id = ? OR target_id = ?",
                (source_id, target_node_id, source_id, target_node_id, source_id, source_id),
            )
        if _table_exists(db, "memory_corrections"):
            db.execute(
                "UPDATE memory_corrections SET related_node_id = ? WHERE related_node_id = ?",
                (target_node_id, source_id),
            )

        chunk_rows = []
        if _table_exists(db, "memory_chunks"):
            chunk_rows = db.execute(
                "SELECT chunk_index, content_hash, content, title, created_at "
                "FROM memory_chunks WHERE node_id = ? ORDER BY chunk_index",
                (source_id,),
            ).fetchall()
        if _table_exists(db, "memory_chunks_fts"):
            db.execute("DELETE FROM memory_chunks_fts WHERE node_id = ?", (source_id,))
        if _table_exists(db, "memory_chunks"):
            db.execute("DELETE FROM memory_chunks WHERE node_id = ?", (source_id,))
        chunks = [
            DocumentChunk(
                chunk_id=f"{target_node_id}:{int(row['chunk_index'])}:{row['content_hash']}",
                node_id=target_node_id,
                chunk_index=int(row["chunk_index"]),
                content_hash=str(row["content_hash"]),
                content=str(row["content"]),
                title=str(row["title"] or old_row["title"] or ""),
            )
            for row in chunk_rows
        ]
        if chunks:
            db.executemany(
                """
                INSERT INTO memory_chunks
                    (chunk_id, node_id, chunk_index, content_hash, content, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.chunk_id,
                        chunk.node_id,
                        chunk.chunk_index,
                        chunk.content_hash,
                        chunk.content,
                        chunk.title,
                        float(row["created_at"] or timestamp),
                        timestamp,
                    )
                    for chunk, row in zip(chunks, chunk_rows)
                ],
            )
            _replace_chunk_fts(db, target_node_id, chunks)

        if _table_exists(db, "memory_fts"):
            fts_rows = db.execute(
                "SELECT title, content FROM memory_fts WHERE node_id = ?",
                (source_id,),
            ).fetchall()
            db.execute("DELETE FROM memory_fts WHERE node_id = ?", (source_id,))
            if fts_rows:
                db.executemany(
                    "INSERT INTO memory_fts(node_id, title, content) VALUES (?, ?, ?)",
                    [(target_node_id, row["title"], row["content"]) for row in fts_rows],
                )

        if _table_exists(db, "memory_index_jobs"):
            db.execute("DELETE FROM memory_index_jobs WHERE node_id = ?", (source_id,))
            content_hash = str(old_row["content_hash"] or "")
            if content_hash and chunks:
                _enqueue_index_job_in_transaction(
                    db,
                    target_node_id,
                    content_hash,
                    now=timestamp,
                    status="pending",
                    index_revision=target_revision,
                )
        db.execute("DELETE FROM memory_nodes WHERE node_id = ?", (source_id,))
    return True


def delete_document_rows(
    db: sqlite3.Connection,
    file_path: str,
    *,
    now: float | None = None,
) -> bool:
    """Atomically remove a document node, chunks, FTS rows, and outbox jobs."""
    _ensure_index_tables(db)
    normalized_path = normalize_file_path(file_path)
    if not normalized_path:
        return False
    node_id = generate_file_node_id(normalized_path)
    with transaction(db):
        existed = db.execute(
            "SELECT 1 FROM memory_nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if _table_exists(db, "memory_chunks_fts"):
            db.execute("DELETE FROM memory_chunks_fts WHERE node_id = ?", (node_id,))
        if _table_exists(db, "memory_fts"):
            db.execute("DELETE FROM memory_fts WHERE node_id = ?", (node_id,))
        if _table_exists(db, "memory_chunks"):
            db.execute("DELETE FROM memory_chunks WHERE node_id = ?", (node_id,))
        if _table_exists(db, "memory_index_jobs"):
            db.execute("DELETE FROM memory_index_jobs WHERE node_id = ?", (node_id,))
        if _table_exists(db, "memory_edges"):
            db.execute(
                "DELETE FROM memory_edges WHERE source_id = ? OR target_id = ?",
                (node_id, node_id),
            )
        if _table_exists(db, "memory_corrections"):
            db.execute(
                "UPDATE memory_corrections SET related_node_id = NULL WHERE related_node_id = ?",
                (node_id,),
            )
        if existed:
            db.execute("DELETE FROM memory_nodes WHERE node_id = ?", (node_id,))
    return existed is not None


def move_document_rows(
    db: sqlite3.Connection,
    old_path: str,
    new_path: str,
    *,
    now: float | None = None,
) -> bool:
    """Move one document's identity without merging with an existing target."""
    old_norm = normalize_file_path(old_path)
    target_eligibility = assess_document_path(new_path)
    if not old_norm:
        raise ValueError("old_path 不能为空")
    if not target_eligibility.eligible:
        raise ValueError(f"不支持索引的记忆文档路径: {target_eligibility.reason}")
    new_norm = target_eligibility.path
    _ensure_index_tables(db)
    if old_norm == new_norm:
        return True
    old_id = generate_file_node_id(old_norm)
    new_id = generate_file_node_id(new_norm)
    timestamp = float(time.time() if now is None else now)

    with transaction(db):
        old_row = db.execute(
            "SELECT * FROM memory_nodes WHERE node_id = ?", (old_id,)
        ).fetchone()
        if old_row is None:
            raise FileNotFoundError(f"源文档不存在于记忆索引: {old_norm}")
        target_row = db.execute(
            "SELECT 1 FROM memory_nodes WHERE node_id = ?", (new_id,)
        ).fetchone()
        if target_row is not None:
            raise FileExistsError(f"目标文档已存在于记忆索引: {new_norm}")

        columns = _columns(db, "memory_nodes")
        source_mtime = old_row["source_mtime"] if "source_mtime" in columns else None
        db.execute(
            """
            INSERT INTO memory_nodes
                (node_id, node_type, file_path, content_hash, title,
                 activation_strength, access_count, last_accessed_at,
                 emotional_valence, emotional_arousal, importance,
                 created_at, updated_at, embedding_synced, source_mtime,
                 event_date, is_deleted, fts_content_hash, embedding_content_hash,
                 embedding_model, embedding_updated_at, index_revision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id,
                old_row["node_type"],
                new_norm,
                old_row["content_hash"],
                old_row["title"],
                old_row["activation_strength"],
                old_row["access_count"],
                old_row["last_accessed_at"],
                old_row["emotional_valence"],
                old_row["emotional_arousal"],
                old_row["importance"],
                old_row["created_at"],
                timestamp,
                0,
                source_mtime,
                old_row["event_date"] if "event_date" in columns else None,
                old_row["is_deleted"] if "is_deleted" in columns else 0,
                old_row["fts_content_hash"] if "fts_content_hash" in columns else None,
                None,
                None,
                None,
                (int(old_row["index_revision"] or 0) + 1) if "index_revision" in columns else 1,
            ),
        )

        if _table_exists(db, "memory_edges"):
            # The target node is guaranteed to be new, so updating the endpoint
            # preserves edge IDs and avoids primary-key collisions.  Keeping
            # this in the same transaction also prevents a half-moved graph.
            db.execute(
                """
                UPDATE memory_edges
                SET source_id = CASE WHEN source_id = ? THEN ? ELSE source_id END,
                    target_id = CASE WHEN target_id = ? THEN ? ELSE target_id END
                WHERE source_id = ? OR target_id = ?
                """,
                (old_id, new_id, old_id, new_id, old_id, old_id),
            )

        if _table_exists(db, "memory_corrections"):
            db.execute(
                "UPDATE memory_corrections SET related_node_id = ? WHERE related_node_id = ?",
                (new_id, old_id),
            )

        old_chunks = []
        if _table_exists(db, "memory_chunks"):
            old_chunks = db.execute(
                "SELECT chunk_index, content_hash, content, title, created_at, updated_at "
                "FROM memory_chunks WHERE node_id = ? ORDER BY chunk_index",
                (old_id,),
            ).fetchall()
            db.execute("DELETE FROM memory_chunks WHERE node_id = ?", (old_id,))
        if _table_exists(db, "memory_chunks_fts"):
            db.execute("DELETE FROM memory_chunks_fts WHERE node_id = ?", (old_id,))
        chunks = [
            DocumentChunk(
                chunk_id=f"{new_id}:{int(row['chunk_index'])}:{row['content_hash']}",
                node_id=new_id,
                chunk_index=int(row["chunk_index"]),
                content_hash=str(row["content_hash"]),
                content=str(row["content"]),
                title=str(row["title"] or old_row["title"] or ""),
            )
            for row in old_chunks
        ]
        if chunks:
            db.executemany(
                """
                INSERT INTO memory_chunks
                    (chunk_id, node_id, chunk_index, content_hash, content, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chunk.chunk_id,
                        new_id,
                        chunk.chunk_index,
                        chunk.content_hash,
                        chunk.content,
                        chunk.title,
                        next(
                            (
                                float(row["created_at"])
                                for row in old_chunks
                                if int(row["chunk_index"]) == chunk.chunk_index
                            ),
                            timestamp,
                        ),
                        timestamp,
                    )
                    for chunk in chunks
                ],
            )
            db.executemany(
                "INSERT INTO memory_chunks_fts(chunk_id, node_id, content, title) VALUES (?, ?, ?, ?)",
                [(chunk.chunk_id, new_id, chunk.content, chunk.title) for chunk in chunks],
            )

        if _table_exists(db, "memory_fts"):
            old_fts = db.execute(
                "SELECT title, content FROM memory_fts WHERE node_id = ? LIMIT 1", (old_id,)
            ).fetchone()
            db.execute("DELETE FROM memory_fts WHERE node_id = ?", (old_id,))
            if old_fts:
                db.execute(
                    "INSERT INTO memory_fts(node_id, title, content) VALUES (?, ?, ?)",
                    (new_id, old_fts["title"], old_fts["content"]),
                )

        if _table_exists(db, "memory_index_jobs"):
            db.execute("DELETE FROM memory_index_jobs WHERE node_id = ?", (old_id,))
            current_content_hash = str(old_row["content_hash"] or "")
            if current_content_hash and chunks:
                revision_row = db.execute(
                    "SELECT index_revision FROM memory_nodes WHERE node_id = ?",
                    (new_id,),
                ).fetchone()
                _enqueue_index_job_in_transaction(
                    db,
                    new_id,
                    current_content_hash,
                    now=timestamp,
                    status="pending",
                    index_revision=int(revision_row[0] or 0) if revision_row else 0,
                )

        db.execute("DELETE FROM memory_nodes WHERE node_id = ?", (old_id,))
    return True


def list_index_jobs(
    db: sqlite3.Connection,
    *,
    status: str = "pending",
    limit: int = 100,
) -> list[IndexJob]:
    """Read outbox jobs without embedding or network work."""
    _ensure_index_tables(db)
    rows = db.execute(
        """
        SELECT job_id, node_id, content_hash, status, created_at, updated_at, attempts, error,
               index_revision
        FROM memory_index_jobs
        WHERE status = ?
        ORDER BY created_at, job_id
        LIMIT ?
        """,
        (status, max(0, int(limit))),
    ).fetchall()
    return [
        IndexJob(
            job_id=str(row["job_id"]),
            node_id=str(row["node_id"]),
            content_hash=str(row["content_hash"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            attempts=int(row["attempts"] or 0),
            error=str(row["error"] or ""),
            index_revision=int(row["index_revision"] or 0),
        )
        for row in rows
    ]


def claim_index_jobs(
    db: sqlite3.Connection,
    *,
    limit: int = 10,
    now: float | None = None,
    reclaim_after: float | None = None,
    retry_failed: bool = False,
) -> list[IndexJob]:
    """Claim eligible jobs, optionally retrying failed or abandoned work."""
    _ensure_index_tables(db)
    timestamp = float(time.time() if now is None else now)
    clauses = ["status = 'pending'"]
    params: list[object] = []
    if retry_failed:
        clauses.append("status = 'failed'")
    if reclaim_after is not None:
        clauses.append("(status = 'processing' AND updated_at <= ?)")
        params.append(timestamp - max(0.0, float(reclaim_after)))

    with transaction(db):
        rows = db.execute(
            "SELECT job_id FROM memory_index_jobs WHERE ("
            + " OR ".join(clauses)
            + ") ORDER BY created_at, job_id LIMIT ?",
            [*params, max(0, int(limit))],
        ).fetchall()
        ids = [str(row[0]) for row in rows]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        db.execute(
            f"UPDATE memory_index_jobs SET status = 'processing', updated_at = ?, "
            f"attempts = attempts + 1, error = '' WHERE job_id IN ({placeholders})",
            [timestamp, *ids],
        )
        rows = db.execute(
            """
            SELECT job_id, node_id, content_hash, status, created_at, updated_at, attempts, error,
                   index_revision
            FROM memory_index_jobs
            WHERE status = 'processing' AND job_id IN (""" + placeholders + ") ORDER BY created_at, job_id",
            ids,
        ).fetchall()
        return [
            IndexJob(
                job_id=str(row["job_id"]),
                node_id=str(row["node_id"]),
                content_hash=str(row["content_hash"]),
                status=str(row["status"]),
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]),
                attempts=int(row["attempts"] or 0),
                error=str(row["error"] or ""),
                index_revision=int(row["index_revision"] or 0),
            )
            for row in rows
        ]


def set_index_job_status(
    db: sqlite3.Connection,
    job_id: str,
    status: str,
    *,
    error: str = "",
    now: float | None = None,
) -> bool:
    """Update one outbox state for an external worker."""
    _ensure_index_tables(db)
    timestamp = float(time.time() if now is None else now)
    with transaction(db):
        cursor = db.execute(
            "UPDATE memory_index_jobs SET status = ?, updated_at = ?, error = ? WHERE job_id = ?",
            (str(status), timestamp, str(error or ""), job_id),
        )
    return cursor.rowcount > 0


__all__ = [
    "ACTIVE_CHUNK_STATE_KEY",
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "ChunkIndexState",
    "DocumentChunk",
    "DocumentIndexResult",
    "INDEX_SCHEMA_NAME",
    "INDEX_SCHEMA_VERSION",
    "IndexJob",
    "claim_index_jobs",
    "chunk_document",
    "create_index_schema",
    "create_memory_schema",
    "delete_document_rows",
    "enqueue_index_job",
    "ensure_memory_schema",
    "list_index_jobs",
    "move_document_rows",
    "read_active_chunk_index_state",
    "set_index_job_status",
    "transaction",
    "upsert_document_rows",
    "write_active_chunk_index_state",
]
