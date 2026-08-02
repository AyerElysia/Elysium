"""SQLite runtime primitives for the Life Engine memory subsystem.

This module owns three things that the rest of the memory package must not
re-invent:

1. **Connection configuration.** Every handle onto ``memory.db`` — writer or
   reader — is opened through :func:`open_memory_connection`, so journal mode,
   durability and cache settings can never drift between call sites. SQLite
   applies most ``PRAGMA`` settings per connection, so configuring one handle
   configures nothing else.

2. **Thread isolation.** Memory database work used to ride on
   ``asyncio.to_thread``, which dispatches onto the interpreter's *default*
   executor. That pool is shared with media decoding, adapter file reads and
   vector queries, so a burst of memory writes could occupy every worker and
   stall unrelated subsystems. :func:`run_db` dispatches onto a bounded pool
   reserved for this database instead.

3. **Reader/writer roles.** In WAL mode SQLite supports one writer and any
   number of concurrent readers. Read-only work routed through
   :func:`run_read` gets a thread-local reader handle, so it neither contends
   on the writer connection's statement lock nor blocks a commit. Reader
   handles are opened ``query_only``: a write attempted on a read path raises
   instead of silently succeeding on the wrong connection.

The writer connection itself is still shared and still serialized by
``indexing._TRANSACTION_LOCK``. That is not a limitation to remove — SQLite
permits exactly one writer, so serializing writes is the correct model.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

from .eligibility import register_indexed_path_sql_function

T = TypeVar("T")

ConnectionRole = Literal["writer", "reader"]

# Durability and concurrency settings shared by every handle.
#
# ``journal_mode = WAL`` is persisted in the database header, so it survives
# reconnects; every other pragma here is connection-scoped and must be applied
# to each new handle.
#
# ``synchronous = NORMAL`` is the documented companion of WAL: commits stop
# issuing an fsync each, and the remaining durability gap is a loss of the most
# recent transactions on power failure, never a corrupt database.
_BASE_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "10000"),
    ("temp_store", "MEMORY"),
    # Negative values are a KiB budget rather than a page count: 64 MiB.
    ("cache_size", "-64000"),
    # Memory-map the database so warm reads skip the read(2) path entirely.
    ("mmap_size", "268435456"),
)

_WRITER_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("foreign_keys", "ON"),
)

_READER_PRAGMAS: tuple[tuple[str, str], ...] = (
    # Reader handles must not be able to write. A read path that reaches for a
    # write should fail loudly here rather than commit through a connection
    # whose transaction is invisible to the writer lock.
    ("query_only", "ON"),
)

# The pool is deliberately small. Statements on a single connection serialize
# regardless of caller count, so extra threads buy throughput only for the
# read paths that hold their own handle. Four keeps a slow read from
# head-of-line blocking a fast one without letting this subsystem monopolize
# CPU or file descriptors.
_DEFAULT_MAX_WORKERS = 4
_EXECUTOR_ENV_VAR = "ELYSIUM_MEMORY_DB_WORKERS"

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

_reader_local = threading.local()
_reader_registry: set[sqlite3.Connection] = set()
_reader_registry_lock = threading.Lock()
_reader_db_path: Path | None = None
# Bumped on every (re)bind. Executor threads cache their reader handle in a
# thread-local, so the path alone cannot tell a live handle from one that a
# previous ``bind_reader_pool`` already closed — a service that closes and
# reopens against the same file would otherwise reuse a dead connection.
_reader_generation = 0


def _resolve_max_workers() -> int:
    """Return the memory executor's worker count.

    Honours ``ELYSIUM_MEMORY_DB_WORKERS`` for operators who need to tune the
    pool against a specific disk, and rejects unusable values rather than
    quietly substituting the default for a deliberate misconfiguration.

    Returns:
        int: Worker count, at least 1.

    Raises:
        ValueError: If the environment variable is set but not a positive
            integer.
    """
    raw = os.environ.get(_EXECUTOR_ENV_VAR)
    if raw is None or not raw.strip():
        return _DEFAULT_MAX_WORKERS

    try:
        workers = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{_EXECUTOR_ENV_VAR} must be a positive integer, got {raw!r}"
        ) from exc

    if workers < 1:
        raise ValueError(
            f"{_EXECUTOR_ENV_VAR} must be a positive integer, got {workers}"
        )
    return workers


def get_db_executor() -> ThreadPoolExecutor:
    """Return the thread pool reserved for memory database work.

    The pool is created on first use and lives for the process. Keeping it
    independent of service lifecycle means free functions in this package can
    dispatch without knowing whether ``LifeMemoryService`` is running, which
    matters for both startup ordering and tests.

    Returns:
        ThreadPoolExecutor: The dedicated executor.
    """
    global _executor

    if _executor is not None:
        return _executor

    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=_resolve_max_workers(),
                thread_name_prefix="life-memory-db",
            )
        return _executor


def configure_connection(
    db: sqlite3.Connection,
    *,
    role: ConnectionRole = "writer",
) -> None:
    """Apply the memory subsystem's pragma set to one connection.

    Args:
        db: The connection to configure.
        role: ``"writer"`` enables foreign keys; ``"reader"`` marks the handle
            query-only.

    Raises:
        sqlite3.DatabaseError: If a pragma cannot be applied. Configuration is
            not optional — a handle running on default durability settings
            would silently reintroduce an fsync per commit.
    """
    pragmas = _BASE_PRAGMAS + (
        _WRITER_PRAGMAS if role == "writer" else _READER_PRAGMAS
    )
    for name, value in pragmas:
        db.execute(f"PRAGMA {name} = {value}")


def open_memory_connection(
    db_path: str | Path,
    *,
    role: ConnectionRole = "writer",
) -> sqlite3.Connection:
    """Open a fully configured handle onto the memory database.

    Args:
        db_path: Path to ``memory.db``. Parent directories are created for
            writer handles.
        role: Connection role, see :func:`configure_connection`.

    Returns:
        sqlite3.Connection: A handle with ``row_factory`` set to
        :class:`sqlite3.Row` and the strict stored-path UDF installed.
    """
    path = Path(db_path)
    if role == "writer":
        path.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(str(path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    configure_connection(db, role=role)
    register_indexed_path_sql_function(db)
    return db


def bind_reader_pool(db_path: str | Path | None) -> None:
    """Point the reader pool at a database, closing any previous handles.

    Called by the memory service when it opens or closes the writer
    connection, so reader handles can never outlive the database they were
    opened against.

    Handles are closed eagerly rather than left for their owning thread to
    reclaim: they pin the WAL, so a lazily-closed reader would keep the
    database from checkpointing after the service shuts down. Closing from
    another thread is safe here because rebinding only happens on open and
    close, when no read is in flight; a stale thread-local is caught by the
    generation check on next use.

    Args:
        db_path: Path to ``memory.db``, or ``None`` to tear the pool down.
    """
    global _reader_db_path, _reader_generation

    with _reader_registry_lock:
        previous = list(_reader_registry)
        _reader_registry.clear()
        _reader_db_path = Path(db_path) if db_path is not None else None
        _reader_generation += 1

    for connection in previous:
        try:
            connection.close()
        except sqlite3.Error:
            # A handle owned by an executor thread that is mid-statement will
            # refuse to close. It is unreachable either way: the thread-local
            # slot is re-checked against the live generation on next use.
            pass


def _reader_target() -> tuple[Path, int]:
    """Return the currently bound reader database and its generation.

    Returns:
        tuple[Path, int]: Database path and pool generation.

    Raises:
        RuntimeError: If no database is bound.
    """
    with _reader_registry_lock:
        path = _reader_db_path
        generation = _reader_generation

    if path is None:
        raise RuntimeError(
            "记忆读连接池尚未绑定数据库；请先初始化 LifeMemoryService"
        )
    return path, generation


def _thread_reader(expected_path: Path, generation: int) -> sqlite3.Connection:
    """Return this thread's reader handle, opening it on first use.

    Args:
        expected_path: The database the handle must point at.
        generation: Pool generation the handle must belong to. A cached handle
            from an earlier generation has already been closed by
            :func:`bind_reader_pool` and must be replaced.

    Returns:
        sqlite3.Connection: A live, query-only handle owned by this thread.
    """
    key = (expected_path, generation)
    cached: sqlite3.Connection | None = getattr(_reader_local, "connection", None)

    if cached is not None and getattr(_reader_local, "key", None) == key:
        return cached

    if cached is not None:
        _reader_local.connection = None
        _reader_local.key = None
        with _reader_registry_lock:
            _reader_registry.discard(cached)
        try:
            cached.close()
        except sqlite3.Error:
            pass

    connection = open_memory_connection(expected_path, role="reader")
    _reader_local.connection = connection
    _reader_local.key = key
    with _reader_registry_lock:
        _reader_registry.add(connection)
    return connection


async def run_db(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run synchronous SQLite work on the memory-dedicated executor.

    Drop-in replacement for ``asyncio.to_thread`` within this package. The
    behavioural difference is the target pool: memory work can no longer
    exhaust the default executor that the rest of the process depends on.

    Args:
        fn: Synchronous callable performing the database work.
        *args: Positional arguments for ``fn``.
        **kwargs: Keyword arguments for ``fn``.

    Returns:
        T: Whatever ``fn`` returns.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        get_db_executor(), partial(fn, *args, **kwargs)
    )


async def run_read(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run read-only SQLite work against a thread-local reader connection.

    ``fn`` receives the reader handle as its first positional argument. Under
    WAL these reads run concurrently with each other and with an in-flight
    commit on the writer connection.

    Args:
        fn: Callable taking ``(connection, *args)``.
        *args: Additional positional arguments for ``fn``.
        **kwargs: Keyword arguments for ``fn``.

    Returns:
        T: Whatever ``fn`` returns.

    Raises:
        RuntimeError: If the reader pool has no database bound, which means
            the memory service is not open.
    """
    path, generation = _reader_target()

    def _call() -> T:
        return fn(_thread_reader(path, generation), *args, **kwargs)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(get_db_executor(), _call)


def shutdown_db_runtime(*, wait: bool = True) -> None:
    """Close reader handles and shut the dedicated executor down.

    Args:
        wait: Whether to block until in-flight database work finishes.
    """
    global _executor

    bind_reader_pool(None)

    with _executor_lock:
        executor = _executor
        _executor = None

    if executor is not None:
        executor.shutdown(wait=wait)


atexit.register(shutdown_db_runtime, wait=False)
