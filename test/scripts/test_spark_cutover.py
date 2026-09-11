"""Content preservation and conflict checks for the explicit Spark cutover."""

import sqlite3

import pytest

from scripts.audit_spark_cutover import compare_database
from scripts.merge_spark_core_history import CoreMergeConflict, merge_core


def database():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, message_id TEXT UNIQUE, content TEXT)"
    )
    return connection


def test_local_id_collision_keeps_both_messages_and_replays():
    a, b = database(), database()
    a.execute("INSERT INTO messages VALUES (1, 'a', 'source exact')")
    b.execute("INSERT INTO messages VALUES (1, 'b', 'preserved exact')")
    a.commit()
    report = merge_core(a, b)
    assert report["inserted_messages"][0]["candidate_local_id"] == 2
    assert a.execute("SELECT * FROM messages ORDER BY id").fetchall() == [
        (1, "a", "source exact"), (2, "b", "preserved exact")
    ]
    assert merge_core(a, b)["inserted_messages"] == []


def test_stable_id_conflict_rolls_back_earlier_insert():
    a, b = database(), database()
    a.execute("INSERT INTO messages VALUES (1, 'a', 'original')")
    b.executemany("INSERT INTO messages VALUES (?, ?, ?)", [
        (2, "b", "new"), (3, "a", "conflict")
    ])
    a.commit()
    with pytest.raises(CoreMergeConflict, match="stable identity conflict"):
        merge_core(a, b)
    assert a.execute("SELECT * FROM messages").fetchall() == [(1, "a", "original")]


def test_equal_payload_different_local_id_does_not_duplicate():
    a, b = database(), database()
    a.execute("INSERT INTO messages VALUES (1, 'a', 'exact')")
    b.execute("INSERT INTO messages VALUES (8, 'a', 'exact')")
    a.commit()
    assert merge_core(a, b)["already_present"] == 1
    assert a.execute("SELECT COUNT(*) FROM messages").fetchone() == (1,)


def test_metadata_clocks_only_and_no_subject_changes():
    a, b = database(), database()
    for connection in (a, b):
        connection.execute("CREATE TABLE person_info (id INTEGER PRIMARY KEY, impression TEXT, updated_at TEXT)")
    a.execute("INSERT INTO person_info VALUES (1, 'exact', '2026-09-08 01:00:00')")
    b.execute("INSERT INTO person_info VALUES (1, 'exact', '2026-09-09 01:00:00')")
    a.commit()
    assert merge_core(a, b)["clock_updates"] == {"person_info": 1}
    b.execute("UPDATE person_info SET impression='not authorized'")
    with pytest.raises(CoreMergeConflict):
        merge_core(a, b)
    assert a.execute("SELECT impression FROM person_info").fetchone() == ("exact",)


def test_schema_change_fails_closed():
    a, b = database(), database()
    b.execute("ALTER TABLE messages ADD COLUMN unknown TEXT")
    with pytest.raises(CoreMergeConflict, match="schemas differ"):
        merge_core(a, b)


def test_audit_reports_changed_columns_without_payloads(tmp_path):
    paths = [tmp_path / "a.sqlite3", tmp_path / "b.sqlite3"]
    for path, secret in zip(paths, ("private A", "private B")):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, body TEXT)")
            connection.execute("INSERT INTO records VALUES (1, ?)", (secret,))
    report = compare_database(*paths)
    assert report["records"]["pk_conflicts"] == 1
    assert report["records"]["changed_columns"] == {"body": 1}
    assert "private" not in str(report)


@pytest.mark.asyncio
async def test_migrated_old_message_is_not_promoted_to_newest_context(monkeypatch):
    from unittest.mock import AsyncMock

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.core.managers.stream_manager import StreamManager
    from src.core.models.sql_alchemy import Messages
    from src.kernel.db import QueryBuilder

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Messages.__table__.create)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            session.add_all([
                Messages(id=row_id, message_id=identity, stream_id="stream-test",
                         person_id="person-test", time=timestamp, message_type="text",
                         content="synthetic", platform="test")
                for row_id, identity, timestamp in (
                    (2, "recent-a", 200.0), (3, "recent-b", 200.0),
                    (101, "migrated-old", 100.0),
                )
            ])
            await session.commit()

            def builder(model):
                query = QueryBuilder(model)

                async def all_rows():
                    return (await session.scalars(query._stmt)).all()

                query.all = all_rows
                return query

            monkeypatch.setattr("src.core.managers.stream_manager.QueryBuilder", builder)
            manager = StreamManager.__new__(StreamManager)
            manager._Messages = Messages
            manager._db_message_to_runtime = AsyncMock(side_effect=lambda row, **_kwargs: row)
            rows = await manager.get_stream_messages("stream-test", limit=2, defer_content=False)
            assert [row.message_id for row in rows] == ["recent-a", "recent-b"]
    finally:
        await engine.dispose()
