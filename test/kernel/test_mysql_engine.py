"""MySQL 异步引擎配置契约测试。"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from src.kernel.db.core.engine import (
    _build_mysql_config,
    _build_mysql_ssl_context,
    _infer_db_type_from_url,
    _install_mysql_optimizations,
)


def test_build_mysql_config_uses_asyncmy_utf8mb4_and_safe_pool() -> None:
    url, kwargs = _build_mysql_config(
        host="localhost",
        port=3306,
        user="elysia@example",
        password="p@ss:word",
        database="elysium",
        pool_size=7,
        connection_timeout=12,
    )

    assert url.startswith("mysql+asyncmy://elysia%40example:p%40ss%3Aword@127.0.0.1")
    assert url.endswith("/elysium?charset=utf8mb4")
    assert kwargs["pool_size"] == 7
    assert kwargs["max_overflow"] == 14
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == 900
    assert kwargs["connect_args"]["charset"] == "utf8mb4"
    assert kwargs["connect_args"]["connect_timeout"] == 12


def test_build_mysql_config_rejects_lossy_charset() -> None:
    with pytest.raises(ValueError, match="utf8mb4"):
        _build_mysql_config(
            "localhost",
            3306,
            "elysium",
            "",
            "elysium",
            charset="utf8",
        )


def test_mysql_ssl_modes_are_explicit() -> None:
    assert _build_mysql_ssl_context("disabled") is None
    with pytest.raises(ValueError, match="TLS"):
        _build_mysql_ssl_context("prefer")
    with pytest.raises(ValueError, match="必须同时配置"):
        _build_mysql_ssl_context("required", ssl_cert="client.pem")


def test_mysql_url_is_inferred_as_mysql() -> None:
    assert (
        _infer_db_type_from_url(
            "mysql+asyncmy://elysium:secret@127.0.0.1:3306/elysium"
        )
        == "mysql"
    )


@pytest.mark.integration
async def test_real_mysql_session_contract_is_applied() -> None:
    target_url = os.environ.get("ELYSIUM_TEST_MYSQL_URL", "")
    if not target_url:
        pytest.skip("ELYSIUM_TEST_MYSQL_URL 未设置")
    parsed = make_url(target_url)
    if not (parsed.database or "").startswith("elysium_test_"):
        pytest.fail("集成测试只允许使用 elysium_test_ 前缀数据库")

    url, kwargs = _build_mysql_config(
        host=parsed.host or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "",
        password=parsed.password or "",
        database=parsed.database or "",
    )
    engine = create_async_engine(url, **kwargs)
    _install_mysql_optimizations(engine)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT @@transaction_isolation AS isolation_level, "
                        "@@time_zone AS time_zone, "
                        "@@innodb_lock_wait_timeout AS lock_wait"
                    )
                )
            ).mappings().one()
        assert row["isolation_level"] == "READ-COMMITTED"
        assert row["time_zone"] == "+00:00"
        assert int(row["lock_wait"]) == 10
    finally:
        await engine.dispose()
