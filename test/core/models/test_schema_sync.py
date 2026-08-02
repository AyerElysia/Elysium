from __future__ import annotations

import pytest
from sqlalchemy import Integer, Text, inspect, text
from sqlalchemy.orm import Mapped, declarative_base, mapped_column

from src.core.utils.schema_sync import enforce_database_schema_consistency
from src.kernel.db import configure_engine, get_engine
from src.kernel.db.core.engine import _build_sqlite_config


TestBase = declarative_base()
TestBase.__test__ = False


@pytest.fixture(scope="module", autouse=True)
def _configure_kernel_db_for_schema_sync_tests(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """为当前测试模块配置 kernel/db 引擎（支持单文件运行）。"""
    db_path = tmp_path_factory.mktemp("schema_sync") / "schema_sync.db"
    url, engine_kwargs = _build_sqlite_config(str(db_path))

    try:
        configure_engine(
            url,
            engine_kwargs=engine_kwargs,
            db_type="sqlite",
            apply_optimizations=False,
        )
    except RuntimeError:
        # 其他测试模块可能已完成配置，复用即可
        pass


class SyncTarget(TestBase):
    """用于验证 schema 同步行为的测试表。"""

    __tablename__ = "schema_sync_target"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class TypeMismatchTarget(TestBase):
    """用于验证类型不一致时的硬失败行为。"""

    __tablename__ = "schema_sync_type_mismatch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)


async def test_schema_sync_adds_missing_and_preserves_undefined_columns() -> None:
    """启动同步应补齐缺失字段，但不得删除历史字段。"""
    engine = await get_engine()

    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS schema_sync_target"))
        await conn.execute(
            text(
                """
                CREATE TABLE schema_sync_target (
                    id INTEGER PRIMARY KEY,
                    legacy_col TEXT
                )
                """
            )
        )

    try:
        stats = await enforce_database_schema_consistency(TestBase.metadata)
        assert stats.tables_checked >= 1
        assert stats.columns_added >= 1
        assert stats.columns_removed == 0
        assert stats.columns_preserved >= 1
        assert stats.requires_migration

        async with engine.begin() as conn:
            columns = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns("schema_sync_target")
            )

        names = {column["name"] for column in columns}
        assert "id" in names
        assert "name" in names
        assert "legacy_col" in names
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS schema_sync_target"))


async def test_explicit_schema_migration_can_remove_undefined_columns() -> None:
    """只有显式迁移模式才允许删除历史字段。"""
    engine = await get_engine()

    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS schema_sync_target"))
        await conn.execute(
            text(
                """
                CREATE TABLE schema_sync_target (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    legacy_col TEXT
                )
                """
            )
        )

    try:
        stats = await enforce_database_schema_consistency(
            TestBase.metadata,
            allow_destructive=True,
        )
        assert stats.columns_removed >= 1

        async with engine.begin() as conn:
            columns = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns("schema_sync_target")
            )
        assert "legacy_col" not in {column["name"] for column in columns}
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS schema_sync_target"))


async def test_schema_sync_reports_sqlite_type_mismatch_without_mutating() -> None:
    """SQLite 类型漂移应被报告，并留给显式迁移处理。"""
    engine = await get_engine()

    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS schema_sync_type_mismatch"))
        await conn.execute(
            text(
                """
                CREATE TABLE schema_sync_type_mismatch (
                    id INTEGER PRIMARY KEY,
                    score TEXT NOT NULL
                )
                """
            )
        )

    try:
        stats = await enforce_database_schema_consistency(TestBase.metadata)
        assert stats.type_mismatches >= 1
        assert stats.requires_migration

        async with engine.begin() as conn:
            columns = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns(
                    "schema_sync_type_mismatch"
                )
            )
        score = next(column for column in columns if column["name"] == "score")
        assert isinstance(score["type"], Text)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS schema_sync_type_mismatch"))
