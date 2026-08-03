#!/usr/bin/env python3
"""核心 SQLite 业务库到 MySQL 8 的安全迁移入口。

凭据只从环境变量读取，不接受命令行明文 URL，避免进入 shell history。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.core.models.sql_alchemy import Base
from src.core.utils.mysql_migration import (
    MigrationSafetyError,
    SqliteToMySQLMigrator,
    analyze_source_schema,
    analyze_source_string_lengths,
    assert_sqlite_integrity,
    create_sqlite_readonly_engine,
    database_digest,
    file_sha256,
    snapshot_sqlite_database,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="无损迁移 Elysium 核心 SQLite 数据到 MySQL 8",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot = subparsers.add_parser(
        "snapshot", help="在线创建 SQLite 一致性快照和内容清单"
    )
    snapshot.add_argument("--source", type=Path, required=True)
    snapshot.add_argument("--output", type=Path, required=True)

    audit = subparsers.add_parser(
        "audit", help="只读审计 SQLite 与当前 MySQL schema 的兼容性"
    )
    audit.add_argument("--source", type=Path, required=True)

    for command, help_text in (
        ("migrate", "迁移到空 MySQL 库并在提交前校验"),
        ("verify", "只读复核已迁移的 MySQL 库"),
    ):
        subcommand = subparsers.add_parser(command, help=help_text)
        subcommand.add_argument("--source", type=Path, required=True)
        subcommand.add_argument(
            "--target-url-env",
            default="ELYSIUM_MYSQL_URL",
            help="保存 mysql+asyncmy URL 的环境变量名",
        )
        subcommand.add_argument("--batch-size", type=int, default=1000)

    return parser


def _target_url(environment_name: str) -> str:
    target_url = os.environ.get(environment_name, "").strip()
    if not target_url:
        raise MigrationSafetyError(
            f"环境变量 {environment_name} 未设置；不会从命令行读取数据库密码"
        )
    return target_url


async def _run(args: argparse.Namespace) -> dict:
    if args.command == "audit":
        source = args.source.resolve()
        engine = create_sqlite_readonly_engine(source)
        try:
            async with engine.connect() as connection:
                await assert_sqlite_integrity(connection)
                nullable_fills = await analyze_source_schema(
                    connection, Base.metadata
                )
                string_lengths = await analyze_source_string_lengths(
                    connection,
                    Base.metadata,
                    nullable_columns_filled_with_null=nullable_fills,
                )
                data = await database_digest(
                    connection,
                    Base.metadata,
                    nullable_columns_filled_with_null=nullable_fills,
                )
        finally:
            await engine.dispose()
        return {
            "source": str(source),
            "file_sha256": file_sha256(source),
            "data": data.to_dict(),
            "nullable_columns_filled_with_null": list(nullable_fills),
            "string_lengths": [
                {
                    "name": item.name,
                    "declared_length": item.declared_length,
                    "actual_max_length": item.actual_max_length,
                    "fits": item.actual_max_length <= item.declared_length,
                }
                for item in string_lengths
            ],
        }

    if args.command == "snapshot":
        result = await snapshot_sqlite_database(
            args.source,
            args.output,
            Base.metadata,
        )
        return result.to_dict()

    migrator = SqliteToMySQLMigrator(
        args.source,
        _target_url(args.target_url_env),
        Base.metadata,
        batch_size=args.batch_size,
    )
    if args.command == "migrate":
        return (await migrator.migrate()).to_dict()
    return (await migrator.verify()).to_dict()


def main() -> int:
    """执行命令并输出可归档的 JSON 结果。"""
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except (MigrationSafetyError, ValueError) as error:
        print(f"迁移被安全检查拒绝: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("迁移已取消；源数据库未被修改。", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - CLI 边界必须返回稳定退出码
        print(
            f"迁移失败且不会切换数据源: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
