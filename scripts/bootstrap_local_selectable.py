#!/usr/bin/env python3
"""本地 selectable 存储的一站式引导：冻结快照 → schema → 五域导入 → 注册 generation。

用法（Elysium 必须已停止，elysium.lock 必须已释放）:

    .venv/bin/python scripts/bootstrap_local_selectable.py \
        --generation-id local-selectable-20260823-v1 \
        --output /absolute/new/evidence/dir \
        [--snapshot /absolute/existing/frozen-snapshot]

不传 --snapshot 时自动调用 scripts/backup_life_data.py --writer-frozen 生成
冻结快照（输出到 <output>/snapshot）。

该脚本：
1. 预检：主进程不在运行、实例锁空闲、目标 SQLite 新鲜；
2. 从冻结快照读取权威源（迁移永远是复制，源文件不移动不删除）；
3. 在 data/life_storage/local.sqlite3 上以 CANDIDATE_COPY 角色初始化全部
   selectable schema（Life Event、Subject、Presence/World、Learning、
   Attention、Runtime State；Memory 在本地模式下保留自有库，不迁移）；
4. 逐域导入并按各域合同校验（字节/hash/行数/根）；
5. 在 data/life_storage/authority.json 注册 VERIFIED 本地 generation。

不修改 config/core.toml、不启动 Elysium。切换配置并手动启动是独立的
人工步骤。复制运行的进度与冲突以 content-free JSONL 留存在 evidence 目录。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from sqlalchemy.ext.asyncio import async_sessionmaker

from plugins.life_engine.storage.attention_schema import (
    ensure_attention_thread_schema,
)
from plugins.life_engine.storage.authority import FileAuthorityRegistry
from plugins.life_engine.storage.contracts import (
    StorageBackendRuntime,
    StorageWriterRole,
)
from plugins.life_engine.storage.domain_factory import open_presence_world_stores
from plugins.life_engine.storage.event_factory import open_life_event_store
from plugins.life_engine.storage.learning_factory import open_learning_stores
from plugins.life_engine.storage.migration.copy_authority import CopyAuthorityToken
from plugins.life_engine.storage.migration.domain_copy import (
    copy_presence_world_from_snapshot,
)
from plugins.life_engine.storage.migration.event_copy import (
    copy_life_events_from_sqlite,
)
from plugins.life_engine.storage.migration.manifest import (
    build_backend_generation,
    load_snapshot_manifest,
)
from plugins.life_engine.storage.migration.subject_copy import (
    copy_subject_documents_from_snapshot,
)
from plugins.life_engine.storage.models import BackendKind
from plugins.life_engine.storage.proactive_migration import (
    copy_proactive_authority_from_snapshot,
    verify_proactive_authority_copy,
)
from plugins.life_engine.storage.runtime_schema import ensure_runtime_state_schema
from plugins.life_engine.storage.subject_factory import open_subject_document_store
from src.kernel.storage import SQLiteStorageConfig, create_sqlite_storage_engine

_EVENT_SOURCE_RELATIVE = PurePosixPath("life_engine_workspace/life_events.sqlite3")
_SCHEMA_VERSION = 3


class BootstrapError(RuntimeError):
    """本地 selectable 引导失败；保留现场，不清理、不覆盖。"""


@dataclass
class _LocalRunRecorder:
    """Duck-typed replacement for MySQLCopyAuthorityRegistry bookkeeping.

    复制函数只用 get_run/record_conflict/set_progress 三件事做簿记；本地
    引导把它们写成 evidence 目录里的 content-free JSONL。真正的写入安全
    来自：Elysium 已停止（预检）+ 目标库新鲜（预检）+ 各域导入自身的
    hash/parity 合同。
    """

    evidence_path: Path
    run_id: str
    writer_frozen: bool

    def _append(self, event: dict[str, Any]) -> None:
        payload = {"ts": datetime.now(UTC).isoformat(), "run_id": self.run_id, **event}
        with self.evidence_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    async def get_run(self, run_id: str) -> dict[str, Any]:
        if str(run_id) != self.run_id:
            raise BootstrapError(f"unknown local copy run: {run_id}")
        return {"run_id": self.run_id, "writer_frozen": self.writer_frozen}

    async def record_conflict(
        self,
        token: CopyAuthorityToken,
        *,
        domain_name: str,
        source_identity: str,
        expected_hash: str,
        actual_hash: str,
        detail: str = "",
    ) -> None:
        self._append(
            {
                "kind": "conflict",
                "domain": domain_name,
                "source_identity": source_identity,
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
            }
        )

    async def set_progress(
        self,
        token: CopyAuthorityToken,
        *,
        copied_records: int,
    ) -> None:
        self._append(
            {"kind": "progress", "copied_records": int(copied_records)}
        )


def _preflight(data_root: Path, database_path: Path, lock_path: Path) -> None:
    probe = subprocess.run(
        ["pgrep", "-f", r"\.venv/bin/python main\.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip():
        raise BootstrapError(
            f"Elysium main process is still running (pids={probe.stdout.strip()}); "
            "stop it before bootstrapping local selectable storage"
        )
    if lock_path.exists():
        try:
            import fcntl

            with lock_path.open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
        except OSError as exc:
            raise BootstrapError(
                f"instance lock {lock_path} is still held: {exc}"
            ) from exc
    if database_path.exists():
        raise BootstrapError(
            f"target database already exists: {database_path}; bootstrap requires "
            "a fresh database (move it aside or choose another data root)"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)


def _create_snapshot(data_root: Path, output_root: Path) -> Path:
    snapshot_dir = output_root / "snapshot"
    if snapshot_dir.exists():
        raise BootstrapError(f"snapshot directory already exists: {snapshot_dir}")
    command = [
        sys.executable,
        str(_REPOSITORY_ROOT / "scripts" / "backup_life_data.py"),
        "--data-root",
        str(data_root),
        "--output",
        str(snapshot_dir),
        "--writer-frozen",
    ]
    completed = subprocess.run(command, cwd=str(_REPOSITORY_ROOT), check=False)
    if completed.returncode != 0:
        raise BootstrapError(
            f"snapshot command failed with exit code {completed.returncode}"
        )
    return snapshot_dir


def _snapshot_event_source(snapshot: Path, manifest: dict[str, Any]) -> Path:
    rows = manifest.get("sqlite")
    if not isinstance(rows, list):
        raise BootstrapError("snapshot SQLite manifest is malformed")
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and PurePosixPath(str(row.get("source_relative", "")))
        == _EVENT_SOURCE_RELATIVE
    ]
    if len(matches) != 1:
        raise BootstrapError("snapshot must declare exactly one Life Event ledger")
    row = matches[0]
    backup_relative = PurePosixPath(str(row.get("backup_relative", "")))
    if backup_relative.is_absolute() or ".." in backup_relative.parts:
        raise BootstrapError("Life Event snapshot path escapes the snapshot root")
    source = (snapshot / backup_relative.as_posix()).resolve()
    try:
        source.relative_to(snapshot)
    except ValueError as exc:
        raise BootstrapError(
            "Life Event snapshot path escapes the snapshot root"
        ) from exc
    expected_hash = str(row.get("backup_sha256") or row.get("sha256") or "")
    if not source.is_file() or len(expected_hash) != 64:
        raise BootstrapError("Life Event snapshot evidence is incomplete")
    actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        raise BootstrapError("Life Event snapshot database hash mismatch")
    return source


def _open_local_copy_runtime(database_path: Path) -> StorageBackendRuntime:
    config = SQLiteStorageConfig(
        database_path=database_path,
        busy_timeout_seconds=10,
    )
    engine = create_sqlite_storage_engine(config)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return StorageBackendRuntime(
        enabled=True,
        backend=BackendKind.LOCAL,
        backend_identity=config.safe_identity,
        generation=None,
        authority_registry=None,
        authority_token=None,
        engine=engine,
        session_factory=session_factory,
        writer_role=StorageWriterRole.CANDIDATE_COPY,
        writer_epoch=1,
    )


def _verification_root(reports: dict[str, Any]) -> str:
    canonical = json.dumps(reports, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    data_root = args.data_root.resolve()
    output_root = args.output.resolve()
    if output_root.exists():
        raise BootstrapError(f"output directory already exists: {output_root}")
    database_path = (data_root / "life_storage" / "local.sqlite3").resolve()
    authority_path = (data_root / "life_storage" / "authority.json").resolve()
    lock_path = (data_root / "runtime" / "elysium.lock").resolve()

    _preflight(data_root, database_path, lock_path)
    output_root.mkdir(parents=True)

    if args.snapshot is not None:
        snapshot_dir = args.snapshot.resolve()
    else:
        snapshot_dir = _create_snapshot(data_root, output_root)
    manifest = load_snapshot_manifest(snapshot_dir / "manifest.json")
    if not bool(manifest.get("writer_frozen")):
        raise BootstrapError(
            "snapshot is not writer-frozen; a verified local generation requires "
            "a frozen snapshot taken while Elysium was stopped"
        )

    recorder = _LocalRunRecorder(
        evidence_path=output_root / "copy_events.jsonl",
        run_id=f"local-selectable-{manifest.get('manifest_sha256', '')[:16]}",
        writer_frozen=True,
    )
    token = CopyAuthorityToken(
        run_id=recorder.run_id,
        authority_epoch=1,
        owner_id="bootstrap-local-selectable",
        lease_until=datetime.now(UTC).isoformat(),
        fencing_token=os.urandom(32).hex(),
    )

    runtime = _open_local_copy_runtime(database_path)
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "generation_id": args.generation_id,
        "snapshot": str(snapshot_dir),
        "database": str(database_path),
        "domains": {},
    }
    try:
        # ── schema 初始化（业务启动固定 initialize_schema=false）──
        event_store = await open_life_event_store(
            runtime, initialize_schema=True, require_database_immutability=False
        )
        subject_store = await open_subject_document_store(
            runtime, initialize_schema=True, require_database_immutability=False
        )
        await open_presence_world_stores(
            runtime, initialize_schema=True, require_database_immutability=False
        )
        await open_learning_stores(
            runtime, initialize_schema=True, require_database_immutability=False
        )
        await ensure_runtime_state_schema(runtime)
        await ensure_attention_thread_schema(runtime)
        report["schema_initialized"] = True

        # ── Life Event ──
        event_source = _snapshot_event_source(snapshot_dir, manifest)
        event_report = await copy_life_events_from_sqlite(
            event_source,
            event_store,
            copy_registry=recorder,  # type: ignore[arg-type]
            token=token,
        )
        report["domains"]["life_event"] = event_report.to_dict()

        # ── Subject documents ──
        subject_report = await copy_subject_documents_from_snapshot(
            snapshot_dir,
            subject_store,
            copy_registry=recorder,  # type: ignore[arg-type]
            token=token,
        )
        report["domains"]["subject_document"] = subject_report.to_dict()

        # ── Presence / World ──
        presence_world_report = await copy_presence_world_from_snapshot(
            snapshot_dir,
            runtime,
            copy_registry=recorder,  # type: ignore[arg-type]
            token=token,
        )
        report["domains"]["presence_world"] = presence_world_report.to_dict()

        # ── Learning（legacy .life_learning → selectable 账本）──
        from plugins.life_engine.storage.learning_migration import (
            import_legacy_learning_snapshot,
            verify_legacy_learning_import,
        )

        snapshot_workspace = snapshot_dir / "life_engine_workspace"
        learning_report = await import_legacy_learning_snapshot(
            snapshot_workspace,
            (await open_learning_stores(runtime, initialize_schema=False)).store,
        )
        report["domains"]["learning"] = {
            "event_count": int(learning_report.event_count),
            "projection_revisions": list(learning_report.projection_revisions),
        }
        learning_verification = await verify_legacy_learning_import(
            snapshot_workspace,
            (await open_learning_stores(runtime, initialize_schema=False)).store,
        )
        if not bool(learning_verification.get("verified")):
            raise BootstrapError("learning legacy import verification failed")
        report["domains"]["learning"]["verified"] = True

        # ── Proactive（attention + initiative + 绑定证书）──
        proactive_report = await copy_proactive_authority_from_snapshot(
            snapshot_dir,
            runtime,
            migration_id=f"proactive-{args.generation_id}",
        )
        proactive_verification = await verify_proactive_authority_copy(
            snapshot_dir,
            runtime,
        )
        if not bool(proactive_verification.get("verified")):
            raise BootstrapError("proactive authority copy verification failed")
        report["domains"]["proactive"] = {
            "copied": proactive_report.to_dict(),
            "verified": True,
        }

        # ── 注册 VERIFIED 本地 generation ──
        verification = {
            "verified": True,
            "verified_at": datetime.now(UTC).isoformat(),
            "verification_root_sha256": _verification_root(report["domains"]),
        }
        generation = build_backend_generation(
            manifest,
            generation_id=args.generation_id,
            backend=BackendKind.LOCAL,
            backend_schema_version=_SCHEMA_VERSION,
            verification=verification,
        )
        registry = FileAuthorityRegistry(authority_path, registry_id="life-domain")
        await registry.register_generation(generation)
        report["generation_registered"] = {
            "generation_id": generation.generation_id,
            "status": generation.status.value,
            "authority_path": str(authority_path),
        }
        report["finished_at"] = datetime.now(UTC).isoformat()
        (output_root / "bootstrap_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return report
    finally:
        await runtime.close()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=_REPOSITORY_ROOT / "data")
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        report = asyncio.run(_run(args))
    except BootstrapError as exc:
        print(f"BOOTSTRAP FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        "BOOTSTRAP OK: generation="
        f"{report['generation_registered']['generation_id']} status="
        f"{report['generation_registered']['status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
