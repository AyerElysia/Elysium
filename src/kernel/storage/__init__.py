"""Storage 模块

提供简单的 JSON 本地持久化存储服务，用于非结构化数据的快速读写。

该模块在 data/json_storage 目录下创建 .json 文件来持久化数据。

典型使用示例:
    from src.kernel.storage import json_store

    # 保存数据
    await json_store.save("my_plugin_data", {"key": "value", "count": 42})

    # 读取数据
    data = await json_store.load("my_plugin_data")
    print(data)  # {"key": "value", "count": 42}

    # 检查数据是否存在
    if await json_store.exists("my_plugin_data"):
        print("数据已存在")

    # 列出所有数据
    all_data = await json_store.list_all()
    print(all_data)  # ["my_plugin_data", ...]

    # 删除数据
    await json_store.delete("my_plugin_data")
"""

from __future__ import annotations

from .core import JSONStore, json_store
from .engine import (
    MySQLStorageConfig,
    SQLiteStorageConfig,
    create_mysql_storage_engine,
    create_sqlite_storage_engine,
    mysql_storage_health,
    sqlite_storage_health,
)
from .migration_runner import (
    MigrationDriftError,
    MigrationLockError,
    MigrationResult,
    MySQLMigrationRunner,
    SchemaMigration,
)
from .outbox_primitives import (
    CursorConflict,
    ImmutableIdentity,
    StableIdentityConflict,
    canonical_json,
    canonical_json_sha256,
    compare_and_advance_cursor,
)
from .transaction import (
    AfterCommitError,
    AsyncUnitOfWork,
    UnitOfWorkState,
)

__all__ = [
    "AfterCommitError",
    "AsyncUnitOfWork",
    "CursorConflict",
    "ImmutableIdentity",
    "JSONStore",
    "MigrationDriftError",
    "MigrationLockError",
    "MigrationResult",
    "MySQLMigrationRunner",
    "MySQLStorageConfig",
    "SQLiteStorageConfig",
    "SchemaMigration",
    "StableIdentityConflict",
    "UnitOfWorkState",
    "canonical_json",
    "canonical_json_sha256",
    "compare_and_advance_cursor",
    "create_mysql_storage_engine",
    "create_sqlite_storage_engine",
    "json_store",
    "mysql_storage_health",
    "sqlite_storage_health",
]

# 版本信息
__version__ = "1.1.0-alpha"
