"""全局存储模式与 MySQL 连接参数契约测试。"""

import pytest
from pydantic import ValidationError

from src.core.config.core_config import CoreConfig


def test_mysql_connection_parameters_remain_in_database_section() -> None:
    """全局选择后，数据库节仍只负责 MySQL 连接参数。"""
    config = CoreConfig(
        storage=CoreConfig.StorageSection(backend="mysql"),
        database=CoreConfig.DatabaseSection(
            mysql_host="db.internal",
            mysql_port=3307,
            mysql_database="elysium_prod",
            mysql_user="elysia",
            mysql_password="secret",
            mysql_ssl_mode="verify-full",
            mysql_ssl_ca="/certs/ca.pem",
        ),
    )

    assert config.storage.backend == "mysql"
    assert config.database.mysql_charset == "utf8mb4"
    assert config.database.mysql_host == "db.internal"
    assert config.database.mysql_port == 3307
    assert config.database.mysql_database == "elysium_prod"
    assert config.database.mysql_user == "elysia"
    assert config.database.mysql_password == "secret"
    assert config.database.mysql_ssl_mode == "verify-full"
    assert config.database.mysql_ssl_ca == "/certs/ca.pem"


def test_mysql_charset_cannot_silently_downgrade_unicode() -> None:
    """不允许配置会截断四字节 Unicode 的旧 utf8 字符集。"""
    with pytest.raises(ValidationError):
        CoreConfig.DatabaseSection(
            mysql_charset="utf8",  # type: ignore[arg-type]
        )


def test_storage_backend_is_a_closed_global_contract() -> None:
    """全局后端拼写错误应在配置加载期失败。"""
    with pytest.raises(ValidationError):
        CoreConfig.StorageSection(backend="myslq")  # type: ignore[arg-type]


def test_database_section_rejects_removed_backend_selector() -> None:
    """模块数据库节不能重新取得独立选择后端的权力。"""
    with pytest.raises(ValidationError):
        CoreConfig.DatabaseSection(database_type="mysql")  # type: ignore[call-arg]
