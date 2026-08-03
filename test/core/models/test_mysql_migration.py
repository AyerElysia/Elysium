"""SQLite→MySQL 无损迁移与快照测试。"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.models.sql_alchemy import Base
from src.core.utils import mysql_migration
from src.core.utils.mysql_migration import (
    MIGRATION_TABLE_NAME,
    MigrationSafetyError,
    SqliteToMySQLMigrator,
    file_sha256,
    snapshot_sqlite_database,
)


async def test_snapshot_is_consistent_manifested_and_never_overwritten(
    tmp_path: Path,
) -> None:
    metadata = MetaData()
    records = Table(
        "records",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("value", String(32), nullable=False),
    )
    source = tmp_path / "source.db"
    engine = create_engine(f"sqlite:///{source}")
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(
            records.insert(),
            [{"id": 1, "value": "爱莉"}, {"id": 2, "value": "Elysia"}],
        )
    engine.dispose()

    destination = tmp_path / "snapshot.db"
    result = await snapshot_sqlite_database(source, destination, metadata)

    assert result.file_sha256
    assert result.data.tables[0].row_count == 2
    assert destination.is_file()
    assert destination.with_suffix(".db.manifest.json").is_file()
    with pytest.raises(MigrationSafetyError, match="已存在"):
        await snapshot_sqlite_database(source, destination, metadata)


async def test_snapshot_explicitly_maps_a_new_nullable_column_to_null(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-core.db"
    engine = create_engine(f"sqlite:///{source}")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        connection.execute(text("DROP INDEX ix_person_info_canonical_person_key"))
        connection.execute(
            text("ALTER TABLE person_info DROP COLUMN canonical_person_key")
        )
        connection.execute(
            Base.metadata.tables["person_info"].insert().values(
                id=1,
                person_id="legacy:1",
                platform="legacy",
                user_id="1",
                nickname="爱莉",
                cardname=None,
                impression=None,
                short_impression=None,
                points=None,
                info_list=None,
                first_interaction=None,
                last_interaction=None,
                interaction_count=1,
                attitude=50,
                created_at=1.0,
                updated_at=1.0,
            )
        )
    engine.dispose()

    result = await snapshot_sqlite_database(
        source,
        tmp_path / "legacy-snapshot.db",
        Base.metadata,
    )

    assert result.nullable_columns_filled_with_null == (
        "person_info.canonical_person_key",
    )
    assert {table.name: table.row_count for table in result.data.tables}[
        "person_info"
    ] == 1


@pytest.mark.integration
async def test_real_mysql_migration_is_verified_idempotent_and_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_url = os.environ.get("ELYSIUM_TEST_MYSQL_URL", "")
    if not target_url:
        pytest.skip("ELYSIUM_TEST_MYSQL_URL 未设置")
    parsed_url = make_url(target_url)
    database_name = parsed_url.database or ""
    if not database_name.startswith("elysium_test_"):
        pytest.fail("集成测试只允许使用 elysium_test_ 前缀的专用数据库")

    source = tmp_path / "core.db"
    source_engine = create_async_engine(f"sqlite+aiosqlite:///{source}")
    async with source_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            Base.metadata.tables["chat_streams"].insert().values(
                id=1,
                stream_id="stream-1",
                person_id="person-1",
                platform="test",
                group_id=None,
                group_name=None,
                chat_type="private",
                created_at=1.0,
                last_active_time=2.0,
                context_cleared_at=None,
            )
        )
        await connection.execute(
            Base.metadata.tables["messages"].insert().values(
                id=1,
                message_id="message-1",
                stream_id="stream-1",
                person_id="person-1",
                time=2.0,
                message_type="text",
                content="你好，爱莉",
                processed_plain_text="你好，爱莉",
                reply_to=None,
                platform="test",
            )
        )
    await source_engine.dispose()

    target_engine = create_async_engine(target_url)
    async with target_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text(f"DROP TABLE IF EXISTS {MIGRATION_TABLE_NAME}"))
    await target_engine.dispose()

    migrator = SqliteToMySQLMigrator(source, target_url, Base.metadata, batch_size=1)

    original_copy_table = mysql_migration._copy_table

    async def fail_after_first_nonempty_table(*args, **kwargs):
        copied = await original_copy_table(*args, **kwargs)
        table = args[2]
        if table.name == "chat_streams":
            raise RuntimeError("injected migration failure")
        return copied

    monkeypatch.setattr(mysql_migration, "_copy_table", fail_after_first_nonempty_table)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        await migrator.migrate()
    monkeypatch.setattr(mysql_migration, "_copy_table", original_copy_table)

    target_engine = create_async_engine(target_url)
    async with target_engine.connect() as connection:
        for table in Base.metadata.sorted_tables:
            count = await connection.scalar(select(func.count()).select_from(table))
            assert count == 0, f"{table.name} retained rows after rollback"
        status = await connection.scalar(
            text(
                f"SELECT status FROM {MIGRATION_TABLE_NAME} "
                "ORDER BY started_at DESC LIMIT 1"
            )
        )
        assert status == "failed"
    async with target_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text(f"DROP TABLE IF EXISTS {MIGRATION_TABLE_NAME}"))
    await target_engine.dispose()

    first = await migrator.migrate()
    second = await migrator.migrate()
    verified = await migrator.verify()

    physically_different_source = tmp_path / "core-checkpointed.db"
    shutil.copy2(source, physically_different_source)
    connection = sqlite3.connect(physically_different_source)
    try:
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()
    assert file_sha256(physically_different_source) != file_sha256(source)
    same_logical_data = await SqliteToMySQLMigrator(
        physically_different_source,
        target_url,
        Base.metadata,
    ).migrate()

    assert first.already_applied is False
    assert second.already_applied is True
    assert second.run_id == first.run_id
    assert same_logical_data.already_applied is True
    assert same_logical_data.run_id == first.run_id
    assert verified.source_data == verified.target_data
    assert {
        table.name: table.row_count for table in verified.target_data.tables
    }["messages"] == 1

    target_engine = create_async_engine(target_url)
    async with target_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.execute(text(f"DROP TABLE IF EXISTS {MIGRATION_TABLE_NAME}"))
    await target_engine.dispose()
