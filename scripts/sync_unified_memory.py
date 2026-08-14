#!/usr/bin/env python3
"""Backfill, verify, and restore the owner-authorized MySQL memory archive."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.kernel.memory_archive.coordinator import MemoryArchiveCoordinator
from src.kernel.memory_archive.models import ArchiveRecord
from src.kernel.memory_archive.mysql_store import (
    MySQLArchiveConfig,
    RemoteMemoryArchive,
)
from src.kernel.memory_archive.restore import (
    ArchiveRestoreError,
    restore_sqlite_domain,
    restore_workspace,
)
from src.kernel.memory_archive.sources import (
    ArchiveSourceError,
    iter_data_root_records,
    iter_workspace_records,
    verify_backup_manifest,
)
from src.kernel.memory_archive.state import ArchiveState


class ArchiveCLIError(RuntimeError):
    """Invalid CLI scope or missing secret environment configuration."""


RESTORE_SQLITE_TARGETS = {
    "core": Path("Elysium.db"),
    "life_events": Path("life_engine_workspace/life_events.sqlite3"),
    "life_memory": Path("life_engine_workspace/.memory/memory.db"),
    "consciousness_presence": Path(
        "life_engine_workspace/runtime/consciousness_presence.sqlite3"
    ),
    "world_projection": Path("life_engine_workspace/runtime/world_projection.sqlite3"),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安全同步 Elysium 统一记忆到追加式 MySQL 归档",
    )
    parser.add_argument(
        "--mysql-url-env",
        default="ELYSIUM_MEMORY_ARCHIVE_MYSQL_URL",
        help="保存 mysql+asyncmy URL 的环境变量名",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="执行一次完整或增量同步")
    source = sync.add_mutually_exclusive_group(required=True)
    source.add_argument("--data-root", type=Path)
    source.add_argument(
        "--backup-root",
        type=Path,
        help="backup_life_data.py 生成的备份目录",
    )
    sync.add_argument("--state", type=Path, required=True)
    sync.add_argument("--full-snapshot", action="store_true")
    sync.add_argument("--publish-batch-size", type=int, default=250)
    sync.add_argument("--publish-concurrency", type=int, default=1)
    sync.add_argument("--scan-batch-size", type=int, default=500)
    sync.add_argument("--max-batch-mib", type=int, default=4)

    verify = subparsers.add_parser("verify-run", help="只读验证远端运行清单")
    verify.add_argument("--manifest-id", required=True)

    health = subparsers.add_parser("health", help="只读检查远端归档状态")
    health.add_argument("--state", type=Path)
    health.add_argument("--source-node-id", default="")

    restore = subparsers.add_parser("restore", help="恢复到全新的隔离目录并校验")
    restore.add_argument("--state", type=Path)
    restore.add_argument("--source-node-id", default="")
    restore.add_argument("--output", type=Path, required=True)
    return parser


def _remote_config(environment_name: str) -> MySQLArchiveConfig:
    raw = os.environ.get(environment_name, "").strip()
    if not raw:
        raise ArchiveCLIError(
            f"环境变量 {environment_name} 未设置；不会从命令行读取数据库密码"
        )
    url = make_url(raw)
    if url.get_backend_name() != "mysql":
        raise ArchiveCLIError("统一记忆归档目标必须是 MySQL")
    if not url.host or not url.database or not url.username or url.password is None:
        raise ArchiveCLIError("MySQL URL 缺少 host/database/user/password")
    query = dict(url.query)
    return MySQLArchiveConfig(
        host=str(url.host),
        port=int(url.port or 3306),
        database=str(url.database),
        user=str(url.username),
        password=str(url.password),
        ssl_mode=str(query.get("ssl_mode", "disabled")),
        ssl_ca=str(query.get("ssl_ca", "")),
        ssl_cert=str(query.get("ssl_cert", "")),
        ssl_key=str(query.get("ssl_key", "")),
        connect_timeout_seconds=int(query.get("connect_timeout", 5)),
    )


def _source_factory(
    args: argparse.Namespace,
    *,
    source_node_id: str,
) -> Callable[[], Iterator[ArchiveRecord]]:
    def records() -> Iterator[ArchiveRecord]:
        if args.data_root is not None:
            yield from iter_data_root_records(
                args.data_root,
                source_node_id=source_node_id,
                batch_size=args.scan_batch_size,
            )
            return
        backup_root = args.backup_root.resolve()
        sqlite_root = backup_root / "sqlite"
        workspace_root = backup_root / "workspace"
        verify_backup_manifest(backup_root)
        if not (backup_root / "manifest.json").is_file():
            raise ArchiveCLIError(f"备份目录缺少 manifest.json: {backup_root}")
        yield from iter_data_root_records(
            sqlite_root,
            source_node_id=source_node_id,
            batch_size=args.scan_batch_size,
        )
        yield from iter_workspace_records(
            workspace_root,
            source_node_id=source_node_id,
        )

    return records


async def _sync(args: argparse.Namespace, remote: RemoteMemoryArchive) -> dict:
    state = ArchiveState(args.state)
    source_node_id = await asyncio.to_thread(state.node_id)
    coordinator = MemoryArchiveCoordinator(
        state,
        remote,
        publish_batch_size=args.publish_batch_size,
        scan_batch_size=args.scan_batch_size,
        max_batch_bytes=max(1, args.max_batch_mib) * 1024 * 1024,
        publish_concurrency=args.publish_concurrency,
        progress_callback=lambda progress: print(
            json.dumps({"progress": progress}, ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        ),
    )
    summary = await coordinator.synchronize(
        _source_factory(args, source_node_id=source_node_id),
        full_snapshot=bool(args.full_snapshot),
    )
    verification = await remote.verify_run(summary.manifest_id)
    return {
        "summary": {
            "manifest_id": summary.manifest_id,
            "source_node_id": summary.source_node_id,
            "scanned": summary.scanned,
            "accepted": summary.accepted,
            "duplicates": summary.duplicates,
            "conflicts": summary.conflicts,
            "root_hash": summary.root_hash,
            "source_counts": summary.source_counts,
            "status": summary.status,
        },
        "verification": verification,
    }


async def _restore(args: argparse.Namespace, remote: RemoteMemoryArchive) -> dict:
    source_node_id = str(args.source_node_id or "").strip()
    if not source_node_id and args.state:
        source_node_id = await asyncio.to_thread(ArchiveState(args.state).node_id)
    if not source_node_id:
        raise ArchiveCLIError("restore 需要 --source-node-id 或 --state")
    output = args.output.resolve()
    if output.exists():
        raise ArchiveRestoreError(f"恢复目标已存在，拒绝覆盖: {output}")
    domains = await remote.source_domains(source_node_id)
    if "workspace" not in domains:
        raise ArchiveRestoreError("远端归档缺少 workspace 域")
    results: list[dict] = [
        await restore_workspace(
            remote,
            source_node_id=source_node_id,
            output_root=output,
        )
    ]
    for domain, relative in RESTORE_SQLITE_TARGETS.items():
        if domain not in domains:
            if domain == "world_projection":
                continue
            raise ArchiveRestoreError(f"远端归档缺少必需域: {domain}")
        results.append(
            await restore_sqlite_domain(
                remote,
                source_node_id=source_node_id,
                source_domain=domain,
                output=output / relative,
            )
        )
    return {
        "source_node_id": source_node_id,
        "output": str(output),
        "domains": results,
        "verified": True,
    }


async def _run(args: argparse.Namespace) -> dict:
    remote = RemoteMemoryArchive(_remote_config(args.mysql_url_env))
    try:
        if args.command == "sync":
            return await _sync(args, remote)
        await remote.initialize()
        if args.command == "verify-run":
            return await remote.verify_run(args.manifest_id)
        if args.command == "health":
            source_node_id = str(args.source_node_id or "").strip()
            if not source_node_id and args.state:
                source_node_id = await asyncio.to_thread(
                    ArchiveState(args.state).node_id
                )
            return await remote.health(source_node_id=source_node_id)
        if args.command == "restore":
            return await _restore(args, remote)
        raise ArchiveCLIError(f"unknown command: {args.command}")
    finally:
        await remote.close()


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except (
        ArchiveCLIError,
        ArchiveRestoreError,
        ArchiveSourceError,
        ValueError,
    ) as exc:
        print(f"统一记忆归档被安全检查拒绝: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("统一记忆归档已取消；本地权威数据未被修改。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - stable CLI failure contract
        print(
            f"统一记忆归档失败且不会切换数据源: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
