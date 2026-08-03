#!/usr/bin/env python3
"""为 Elysium MySQL 创建和校验本地事务一致性逻辑快照。"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.engine import URL, make_url


class BackupError(RuntimeError):
    """备份前置条件、导出或完整性校验失败。"""


def _file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        while chunk := file_handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _target_url(environment_name: str) -> URL:
    raw = os.environ.get(environment_name, "").strip()
    if not raw:
        raise BackupError(f"环境变量 {environment_name} 未设置")
    url = make_url(raw)
    if url.get_backend_name() != "mysql":
        raise BackupError("备份源必须是 MySQL URL")
    if not url.host or not url.username or not url.database:
        raise BackupError("MySQL URL 必须包含主机、用户名和数据库名")
    return url


@contextmanager
def _mysql_defaults_file(url: URL) -> Iterator[Path]:
    """使用仅当前用户可读的临时文件向原生客户端传递凭据。"""
    descriptor, raw_path = tempfile.mkstemp(prefix="elysium-mysql-", suffix=".cnf")
    path = Path(raw_path)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            file_handle.write("[client]\n")
            file_handle.write(f"host={url.host}\n")
            file_handle.write(f"port={url.port or 3306}\n")
            file_handle.write(f"user={url.username}\n")
            file_handle.write(f"password={url.password or ''}\n")
            file_handle.write("default-character-set=utf8mb4\n")
        yield path
    finally:
        path.unlink(missing_ok=True)


def create_snapshot(url: URL, output_dir: Path) -> dict[str, object]:
    """流式生成不可覆盖的 gzip SQL 快照及 SHA-256 manifest。"""
    mysqldump = shutil.which("mysqldump")
    if mysqldump is None:
        raise BackupError("未找到 mysqldump")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    final_path = output_dir / f"elysium-mysql-{timestamp}.sql.gz"
    partial_path = final_path.with_suffix(final_path.suffix + ".partial")
    manifest_path = final_path.with_suffix(final_path.suffix + ".manifest.json")
    if final_path.exists() or partial_path.exists() or manifest_path.exists():
        raise BackupError("目标快照或 manifest 已存在，拒绝覆盖")

    with _mysql_defaults_file(url) as defaults_file:
        command = [
            mysqldump,
            f"--defaults-extra-file={defaults_file}",
            "--single-transaction",
            "--quick",
            "--routines",
            "--events",
            "--triggers",
            "--hex-blob",
            "--no-tablespaces",
            "--set-gtid-purged=OFF",
            "--default-character-set=utf8mb4",
            str(url.database),
        ]
        with tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
            )
            assert process.stdout is not None
            try:
                with gzip.open(partial_path, "xb", compresslevel=6) as compressed:
                    while chunk := process.stdout.read(1024 * 1024):
                        compressed.write(chunk)
                return_code = process.wait()
                if return_code != 0:
                    stderr_file.seek(0)
                    message = stderr_file.read().decode("utf-8", errors="replace")
                    raise BackupError(
                        f"mysqldump 失败（退出码 {return_code}）: {message.strip()}"
                    )
            except BaseException:
                process.kill()
                process.wait()
                partial_path.unlink(missing_ok=True)
                raise

    # 完整读取一次，触发 gzip CRC 校验；随后原子改名，避免消费者看到半份备份。
    with gzip.open(partial_path, "rb") as compressed:
        while compressed.read(1024 * 1024):
            pass
    partial_path.replace(final_path)
    result: dict[str, object] = {
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(),
        "database": str(url.database),
        "source": f"{url.host}:{url.port or 3306}",
        "snapshot": str(final_path),
        "compressed_bytes": final_path.stat().st_size,
        "sha256": _file_sha256(final_path),
        "consistency": "mysqldump --single-transaction",
    }
    manifest_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result["manifest"] = str(manifest_path)
    return result


def verify_snapshot(snapshot: Path) -> dict[str, object]:
    """校验 manifest、压缩流 CRC 与文件 SHA-256。"""
    snapshot = snapshot.resolve()
    manifest_path = snapshot.with_suffix(snapshot.suffix + ".manifest.json")
    if not snapshot.is_file() or not manifest_path.is_file():
        raise BackupError("快照或 manifest 不存在")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_sha256 = _file_sha256(snapshot)
    if actual_sha256 != manifest.get("sha256"):
        raise BackupError("快照 SHA-256 与 manifest 不一致")
    uncompressed_bytes = 0
    with gzip.open(snapshot, "rb") as compressed:
        while chunk := compressed.read(1024 * 1024):
            uncompressed_bytes += len(chunk)
    return {
        "snapshot": str(snapshot),
        "sha256": actual_sha256,
        "compressed_bytes": snapshot.stat().st_size,
        "uncompressed_bytes": uncompressed_bytes,
        "verified": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Elysium MySQL 本地一致性备份")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot", help="创建一个不可覆盖的逻辑快照")
    snapshot.add_argument("--output-dir", type=Path, required=True)
    snapshot.add_argument("--source-url-env", default="ELYSIUM_MYSQL_URL")
    verify = subparsers.add_parser("verify", help="校验一个已有快照")
    verify.add_argument("--snapshot", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "snapshot":
            result = create_snapshot(
                _target_url(args.source_url_env),
                args.output_dir,
            )
        else:
            result = verify_snapshot(args.snapshot)
    except (BackupError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"备份失败: {error}", file=os.sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
