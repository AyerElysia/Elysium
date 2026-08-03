"""MySQL 核心配置契约测试。"""

import pytest
from pydantic import ValidationError

from src.core.config.core_config import CoreConfig


def test_mysql_config_exposes_values_to_legacy_runtime_bridge() -> None:
    """旧启动器参数槽位应收到用户填写的 MySQL 值。"""
    config = CoreConfig.DatabaseSection(
        database_type="mysql",
        mysql_host="db.internal",
        mysql_port=3307,
        mysql_database="elysium_prod",
        mysql_user="elysia",
        mysql_password="secret",
        mysql_ssl_mode="verify-full",
        mysql_ssl_ca="/certs/ca.pem",
    )

    assert config.mysql_charset == "utf8mb4"
    assert config.postgresql_host == "db.internal"
    assert config.postgresql_port == 3307
    assert config.postgresql_database == "elysium_prod"
    assert config.postgresql_user == "elysia"
    assert config.postgresql_password == "secret"
    assert config.postgresql_ssl_mode == "verify-full"
    assert config.postgresql_ssl_ca == "/certs/ca.pem"


def test_mysql_charset_cannot_silently_downgrade_unicode() -> None:
    """不允许配置会截断四字节 Unicode 的旧 utf8 字符集。"""
    with pytest.raises(ValidationError):
        CoreConfig.DatabaseSection(
            database_type="mysql",
            mysql_charset="utf8",  # type: ignore[arg-type]
        )


def test_database_type_is_a_closed_transport_contract() -> None:
    """技术协议类型拼写错误应在配置加载期失败。"""
    with pytest.raises(ValidationError):
        CoreConfig.DatabaseSection(database_type="myslq")  # type: ignore[arg-type]
