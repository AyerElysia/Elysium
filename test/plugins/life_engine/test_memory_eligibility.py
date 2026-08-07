"""Eligibility boundaries for Life Engine memory documents."""

from __future__ import annotations

import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.life_engine.memory.eligibility import (
    assess_document_path,
    assess_indexed_document_path,
    assess_workspace_document,
    read_workspace_document,
    register_indexed_path_sql_function,
    scan_workspace_documents,
)
from plugins.life_engine.memory.indexing import create_memory_schema, upsert_document_rows
from plugins.life_engine.memory.search import _node_filter_sql


@pytest.mark.parametrize(
    ("path", "eligible", "reason"),
    [
        ("MEMORY.md", True, ""),
        ("AyerElysia_preferences.txt", True, ""),
        ("diaries/2026-07-20.md", True, ""),
        ("dreams/2026-07-20.md", True, ""),
        ("notes/relationships/elysia.md", True, ""),
        ("narrative/autobiography.md", True, ""),
        ("runtime/life_chatter_rolling_context.json", False, "unsupported_suffix"),
        ("thoughts/streams.json", False, "unsupported_suffix"),
        (".life_trace/blobs/a.txt", False, "hidden_directory"),
        ("notes/.draft.md", False, "hidden_directory"),
        ("notes/idea.md.backup", False, "temporary_name"),
        ("life_events.jsonl", False, "unsupported_suffix"),
        ("todos.json", False, "unsupported_suffix"),
        ("misc/note.md", False, "unsupported_directory"),
        ("unlisted.md", False, "root_not_whitelisted"),
        ("../outside.md", False, "invalid_path"),
        ("/absolute.md", False, "absolute_path"),
    ],
)
def test_document_path_eligibility_matrix(path: str, eligible: bool, reason: str) -> None:
    decision = assess_document_path(path)

    assert decision.eligible is eligible
    assert decision.reason == reason


def test_workspace_scan_does_not_recurse_rejected_runtime_or_hidden_trees(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes").mkdir()
    runtime = tmp_path / "runtime"
    trace = tmp_path / ".life_trace"
    runtime.mkdir()
    trace.mkdir()
    (runtime / "nested").mkdir()
    (trace / "nested").mkdir()
    (tmp_path / "notes" / "kept.md").write_text("kept", encoding="utf-8")
    (runtime / "state.json").write_text("{}", encoding="utf-8")
    (runtime / "nested" / "ignored.md").write_text("ignored", encoding="utf-8")
    (trace / "trace.txt").write_text("trace", encoding="utf-8")
    (trace / "nested" / "ignored.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "large.md").write_text("not whitelisted", encoding="utf-8")

    scan = scan_workspace_documents(tmp_path)

    assert [item.path for item in scan.documents] == ["notes/kept.md"]
    assert scan.rejected_reason_counts == {
        "blocked_directory": 1,
        "hidden_directory": 1,
        "root_not_whitelisted": 1,
    }
    assert {item.path for item in scan.rejected} == {".life_trace", "large.md", "runtime"}


def test_workspace_document_read_uses_checked_file_on_current_platform(
    tmp_path: Path,
) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    document = notes / "entry.md"
    document.write_text("current body", encoding="utf-8")

    content, source_mtime, size = read_workspace_document(tmp_path, "notes/entry.md")

    assert content == "current body"
    assert source_mtime == document.stat().st_mtime
    assert size == len("current body")


def test_workspace_eligibility_rejects_symlink_and_oversized_file(tmp_path: Path) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    target = notes / "target.md"
    target.write_text("body", encoding="utf-8")
    link = notes / "link.md"
    link.symlink_to(target)
    diary_target = tmp_path / "diary_target"
    diary_target.mkdir()
    (diary_target / "entry.md").write_text("entry", encoding="utf-8")
    linked_diaries = tmp_path / "diaries"
    linked_diaries.symlink_to(diary_target, target_is_directory=True)
    oversized = notes / "oversized.md"
    oversized.write_text("12345", encoding="utf-8")

    assert assess_workspace_document(tmp_path, "notes/link.md").reason == "symlink"
    assert assess_workspace_document(tmp_path, "diaries/entry.md").reason == "symlink"
    assert assess_workspace_document(tmp_path, "notes/oversized.md", max_bytes=4).reason == "too_large"


def test_document_upsert_rejects_runtime_path_before_creating_rows(tmp_path: Path) -> None:
    db = sqlite3.connect(str(tmp_path / "memory.db"))
    db.row_factory = sqlite3.Row
    create_memory_schema(db)

    with pytest.raises(ValueError, match="不支持索引的记忆文档路径: unsupported_suffix"):
        upsert_document_rows(db, "runtime/life_chatter_rolling_context.json", "{}")

    assert db.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0] == 0


async def test_file_tool_does_not_sync_ineligible_runtime_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.tools.file_tools import _sync_memory_embedding_for_file

    upsert_document = AsyncMock()
    fake_service = SimpleNamespace(
        _memory_service=SimpleNamespace(upsert_document=upsert_document),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.LifeEngineService.get_instance",
        lambda: fake_service,
    )

    await _sync_memory_embedding_for_file(object(), "runtime/state.json", "{}")

    upsert_document.assert_not_awaited()


async def test_fetch_memory_tool_rejects_runtime_file(tmp_path: Path) -> None:
    from plugins.life_engine.core.config import LifeEngineConfig
    from plugins.life_engine.tools.file_tools import FetchLifeMemoryTool

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "state.json").write_text('{"private": true}', encoding="utf-8")
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)

    ok, payload = await FetchLifeMemoryTool(
        plugin=SimpleNamespace(config=config),
    ).execute(["runtime/state.json"])

    assert ok is True
    assert payload["successful"] == 0
    assert payload["failed"] == 1
    assert payload["files"] == [
        {
            "path": "runtime/state.json",
            "error": "不是可读取的记忆文档: unsupported_suffix",
        }
    ]


async def test_sync_embedding_skips_ineligible_runtime_file() -> None:
    from plugins.life_engine.memory.search import sync_embedding

    lookup = AsyncMock()
    collection = Mock()

    await sync_embedding(
        sqlite3.connect(":memory:"),
        collection,
        "runtime/state.json",
        "{}",
        lookup,
    )

    lookup.assert_not_awaited()
    collection.upsert.assert_not_called()


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("./notes/entry.md", "noncanonical_path"),
        ("notes//entry.md", "noncanonical_path"),
        ("notes\\entry.md", "noncanonical_path"),
    ],
)
def test_stored_document_paths_must_already_be_canonical(path: str, reason: str) -> None:
    decision = assess_indexed_document_path(path)

    assert decision.eligible is False
    assert decision.reason == reason


@pytest.mark.skipif(os.name == "nt", reason="literal backslash filename is POSIX-only")
async def test_file_tool_syncs_only_canonical_safe_workspace_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.core.config import LifeEngineConfig
    from plugins.life_engine.tools.file_tools import _sync_memory_embedding_for_file

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "entry.md").write_text("authoritative content", encoding="utf-8")
    # On POSIX this is a distinct literal filename; it must not index notes/entry.md.
    (tmp_path / "notes\\entry.md").write_text("wrong physical file", encoding="utf-8")
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    upsert_document = AsyncMock()
    fake_service = SimpleNamespace(
        _memory_service=SimpleNamespace(upsert_document=upsert_document),
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.LifeEngineService.get_instance",
        lambda: fake_service,
    )

    await _sync_memory_embedding_for_file(
        SimpleNamespace(config=config),
        "notes\\entry.md",
        "wrong physical file",
    )
    upsert_document.assert_not_awaited()

    await _sync_memory_embedding_for_file(
        SimpleNamespace(config=config),
        "notes/entry.md",
        "ignored caller content",
    )

    upsert_document.assert_awaited_once()
    args = upsert_document.await_args
    assert args.args[:2] == ("notes/entry.md", "authoritative content")
    assert args.kwargs["title"] == "entry"
    assert isinstance(args.kwargs["source_mtime"], float)


@pytest.mark.skipif(os.name != "nt", reason="Windows path normalization contract")
def test_windows_backslash_path_is_rejected_as_noncanonical() -> None:
    decision = assess_indexed_document_path("notes\\entry.md")

    assert decision.eligible is False
    assert decision.path == "notes/entry.md"
    assert decision.reason == "noncanonical_path"


async def test_fetch_memory_rejects_noncanonical_paths_without_creating_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.core.config import LifeEngineConfig
    from plugins.life_engine.tools.file_tools import FetchLifeMemoryTool

    workspace = tmp_path / "missing-workspace"
    config = LifeEngineConfig()
    config.settings.workspace_path = str(workspace)
    monkeypatch.setattr(
        "plugins.life_engine.tools.file_tools._get_life_engine_service",
        lambda _plugin: None,
    )

    ok, payload = await FetchLifeMemoryTool(
        plugin=SimpleNamespace(config=config),
    ).execute(["./notes/entry.md"])

    assert ok is True
    assert payload["successful"] == 0
    assert payload["files"] == [
        {
            "path": "./notes/entry.md",
            "error": "不是可读取的记忆文档: noncanonical_path",
        }
    ]
    assert workspace.exists() is False


async def test_fetch_memory_rejects_noncanonical_lineage_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.life_engine.core.config import LifeEngineConfig
    from plugins.life_engine.tools.file_tools import FetchLifeMemoryTool

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "current.md").write_text("current", encoding="utf-8")
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)

    async def _resolve(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"resolved": True, "resolved_path": "./notes/current.md"}

    memory_service = SimpleNamespace(resolve_canonical_path=_resolve)
    monkeypatch.setattr(
        "plugins.life_engine.tools.file_tools._get_life_engine_service",
        lambda _plugin: SimpleNamespace(_memory_service=memory_service),
    )

    ok, payload = await FetchLifeMemoryTool(
        plugin=SimpleNamespace(config=config),
    ).execute(["notes/entry.md"])

    assert ok is True
    assert payload["successful"] == 0
    assert payload["files"] == [
        {
            "path": "notes/entry.md",
            "error": "不是可读取的记忆文档: noncanonical_path",
        }
    ]


class _CountingConnection(sqlite3.Connection):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.create_function_calls = 0

    def create_function(self, *args: object, **kwargs: object) -> None:
        self.create_function_calls += 1
        super().create_function(*args, **kwargs)


def test_indexed_path_udf_is_installed_once_under_concurrent_reads(tmp_path: Path) -> None:
    db = sqlite3.connect(
        str(tmp_path / "udf.db"),
        factory=_CountingConnection,
        check_same_thread=False,
    )
    db.row_factory = sqlite3.Row
    create_memory_schema(db)
    assert db.create_function_calls == 1

    errors: list[BaseException] = []

    def read_filter() -> None:
        try:
            register_indexed_path_sql_function(db)
            _node_filter_sql(db, alias="n", event_date=None, file_types=None)
        except BaseException as exc:  # pragma: no cover - assertion aid for threads
            errors.append(exc)

    threads = [threading.Thread(target=read_filter) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert db.create_function_calls == 1


class _LegacyCountingConnection(_CountingConnection):
    """Connection double that emulates SQLite without pragma_function_list."""

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        if "pragma_function_list" in sql.lower():
            raise sqlite3.OperationalError("no such table: pragma_function_list")
        return super().execute(sql, parameters)


def test_indexed_path_udf_old_sqlite_fallback_installs_once_concurrently(
    tmp_path: Path,
) -> None:
    db = sqlite3.connect(
        str(tmp_path / "legacy-udf.db"),
        factory=_LegacyCountingConnection,
        check_same_thread=False,
    )
    errors: list[BaseException] = []

    def register() -> None:
        try:
            register_indexed_path_sql_function(db)
        except BaseException as exc:  # pragma: no cover - assertion aid for threads
            errors.append(exc)

    threads = [threading.Thread(target=register) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert db.create_function_calls == 1
    assert db.execute("SELECT life_memory_indexed_path_ok(?)", ("notes/valid.md",)).fetchone() == (
        1,
    )


def test_search_and_decay_udf_callsites_do_not_deadlock_under_concurrency(
    tmp_path: Path,
) -> None:
    """Regression for real search.py/decay.py call sites, not the UDF in isolation.

    Before ``eligibility.register_indexed_path_sql_function`` gained its own
    ``_SQL_FUNCTION_LOCK`` and per-connection probe, two threads racing to
    (re)install the UDF on the same ``check_same_thread=False`` connection
    could hit ``sqlite3.OperationalError: Error creating function`` or contend
    indefinitely. This drives every synchronous call site in ``search.py`` and
    ``decay.py`` that calls ``register_indexed_path_sql_function`` unconditionally
    on each invocation through a bounded ``ThreadPoolExecutor``.
    ``as_completed(..., timeout=...)`` makes a real deadlock regression fail the
    test instead of hanging the suite forever.

    Only the synchronous DB worker paths are driven here: the async wrappers in
    ``decay.py`` (``list_dream_candidate_nodes``, etc.) ultimately delegate to
    these same sync closures via ``asyncio.to_thread``, so covering the sync
    path is sufficient and avoids nested thread-pool contention.

    Each worker receives its own dedicated ``sqlite3.Connection`` to the same
    on-disk file so that CPython's per-connection sqlite3 internal lock cannot
    serialize or deadlock concurrent operations.
    """
    from plugins.life_engine.memory.search import (
        _current_chunk_map,
        _node_filter_sql,
        _read_traversable_node_ids,
    )
    from plugins.life_engine.memory.eligibility import (
        eligible_document_path_sql,
        register_indexed_path_sql_function,
    )
    from plugins.life_engine.memory.nodes import NodeType

    db_path = str(tmp_path / "udf-callsites.db")
    # Create schema once on a setup connection to a named file.
    setup_db = sqlite3.connect(db_path, check_same_thread=False)
    setup_db.row_factory = sqlite3.Row
    setup_db.execute(
        """
        CREATE TABLE memory_edges (
            edge_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL DEFAULT 0.5,
            base_strength REAL DEFAULT 0.5,
            reinforcement REAL DEFAULT 0.0,
            activation_count INTEGER DEFAULT 0,
            last_activated_at REAL,
            reason TEXT,
            created_at REAL NOT NULL,
            bidirectional INTEGER DEFAULT 1,
            UNIQUE(source_id, target_id, edge_type)
        )
        """
    )
    create_memory_schema(setup_db, now=1.0)
    upsert_document_rows(setup_db, "notes/one.md", "alpha beta gamma " * 5, now=2.0)
    upsert_document_rows(setup_db, "notes/two.md", "delta epsilon zeta " * 5, now=3.0)

    chunk_row = setup_db.execute("SELECT chunk_id FROM memory_chunks LIMIT 1").fetchone()
    assert chunk_row is not None
    chunk_id = str(chunk_row["chunk_id"])
    node_row = setup_db.execute("SELECT node_id FROM memory_nodes LIMIT 1").fetchone()
    assert node_row is not None
    node_id = str(node_row["node_id"])
    setup_db.close()

    # --- Sync worker functions matching real call sites in search.py/decay.py ---
    # Each accepts its own dedicated connection; the caller closes it when done.

    def _search_node_filter(conn: sqlite3.Connection) -> None:
        # Mirrors search.py:_node_filter_sql (line ~795)
        for _ in range(20):
            register_indexed_path_sql_function(conn)
            _node_filter_sql(conn, alias="n", event_date=None, file_types=None)

    def _search_traversable_ids(conn: sqlite3.Connection) -> None:
        # Mirrors search.py:_read_traversable_node_ids (line ~1329)
        for _ in range(20):
            _read_traversable_node_ids(conn)

    def _search_chunk_map(conn: sqlite3.Connection) -> None:
        # Mirrors search.py:_current_chunk_map (line ~391)
        for _ in range(20):
            _current_chunk_map(conn, [chunk_id])

    def _search_filter_scores(conn: sqlite3.Connection) -> None:
        # Mirrors the _do_db_work closure in search.py:filter_existing_scores (~1496)
        for _ in range(20):
            register_indexed_path_sql_function(conn)
            eligibility_sql, eligibility_params = eligible_document_path_sql("file_path")
            conn.execute(
                f"SELECT node_id FROM memory_nodes WHERE node_id = ? AND {eligibility_sql}",
                [node_id, *eligibility_params],
            ).fetchall()

    def _decay_dream_candidates(conn: sqlite3.Connection) -> None:
        # Mirrors the _do_db_work closure in decay.py:list_dream_candidate_nodes (~458)
        for _ in range(20):
            register_indexed_path_sql_function(conn)
            eligibility_sql, eligibility_params = eligible_document_path_sql("file_path")
            conn.execute(
                "SELECT node_id FROM memory_nodes WHERE node_type = ? "
                f"AND file_path IS NOT NULL AND {eligibility_sql} LIMIT 5",
                [NodeType.FILE.value, *eligibility_params],
            ).fetchall()

    def _decay_random_nodes(conn: sqlite3.Connection) -> None:
        # Mirrors the _do_db_work closure in decay.py:list_random_file_nodes (~520)
        for _ in range(20):
            register_indexed_path_sql_function(conn)
            eligibility_sql, eligibility_params = eligible_document_path_sql("file_path")
            conn.execute(
                "SELECT node_id FROM memory_nodes WHERE node_type = ? "
                f"AND file_path IS NOT NULL AND {eligibility_sql} ORDER BY RANDOM() LIMIT 5",
                [NodeType.FILE.value, *eligibility_params],
            ).fetchall()

    def _decay_dream_walk_load(conn: sqlite3.Connection) -> None:
        # Mirrors the _load_nodes closure in decay.py:dream_walk (~299)
        for _ in range(20):
            register_indexed_path_sql_function(conn)
            eligibility_sql, eligibility_params = eligible_document_path_sql("file_path")
            conn.execute(
                "SELECT node_id, activation_strength FROM memory_nodes "
                "WHERE node_type = ? AND file_path IS NOT NULL "
                f"AND {eligibility_sql} ORDER BY activation_strength DESC",
                [NodeType.FILE.value, *eligibility_params],
            ).fetchall()

    worker_fns = [
        _search_node_filter,
        _search_traversable_ids,
        _search_chunk_map,
        _search_filter_scores,
        _decay_dream_candidates,
        _decay_random_nodes,
        _decay_dream_walk_load,
    ] * 3  # 7 worker functions × 3 = 21

    # Pre-create one dedicated connection per worker BEFORE the pool starts.
    worker_connections = [
        sqlite3.connect(db_path, check_same_thread=False)
        for _ in range(len(worker_fns))
    ]
    for conn in worker_connections:
        conn.row_factory = sqlite3.Row

    def _run_worker(fn, conn: sqlite3.Connection) -> None:
        try:
            fn(conn)
        finally:
            conn.close()

    worker_pairs = list(zip(worker_fns, worker_connections))

    with ThreadPoolExecutor(max_workers=len(worker_pairs)) as pool:
        futures = {pool.submit(_run_worker, fn, conn): fn for fn, conn in worker_pairs}
        pending = set(futures)
        try:
            for future in as_completed(futures, timeout=10.0):
                pending.discard(future)
                future.result()
        except TimeoutError:  # pragma: no cover - indicates a real deadlock regression
            names = sorted({futures[f].__name__ for f in pending})
            pytest.fail(
                f"register_indexed_path_sql_function call sites deadlocked: "
                f"{len(pending)} worker(s) still pending after 10s ({names})"
            )
