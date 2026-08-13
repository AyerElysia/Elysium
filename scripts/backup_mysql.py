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
from contextlib import contextmanager, suppress
from pathlib import Path

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


class BackupError(RuntimeError):
    """备份前置条件、导出或完整性校验失败。"""


def _is_windows() -> bool:
    return os.name == "nt"


def _restrict_windows_file_acl(path: Path) -> None:
    """Protect an empty private backup file before content is written."""

    if not _is_windows():
        return
    whoami = shutil.which("whoami")
    icacls = shutil.which("icacls")
    if whoami is None or icacls is None:
        raise BackupError("无法保护 MySQL 私有文件：缺少 Windows ACL 工具")
    try:
        identity_result = subprocess.run(
            [whoami],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackupError("无法识别当前 Windows 用户") from error
    identity = identity_result.stdout.strip()
    if identity_result.returncode != 0 or not identity:
        raise BackupError("无法识别当前 Windows 用户")
    try:
        for command in (
            [icacls, str(path), "/setowner", identity],
            [icacls, str(path), "/inheritance:r", "/grant:r", f"{identity}:(F)"],
        ):
            result = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            if result.returncode != 0:
                raise BackupError("无法将 MySQL 私有文件限制为当前用户")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackupError("无法将 MySQL 私有文件限制为当前用户") from error


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
    try:
        url = make_url(raw)
    except ArgumentError as error:
        raise BackupError("MySQL URL 无法解析（原值已隐藏）") from error
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
        _restrict_windows_file_acl(path)

        def option(value: object) -> str:
            text = str(value)
            if "\x00" in text or "\n" in text or "\r" in text:
                raise BackupError("MySQL 连接参数不能包含 NUL 或换行")
            return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'

        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            descriptor = -1
            file_handle.write("[client]\n")
            file_handle.write(f"host={option(url.host)}\n")
            file_handle.write(f"port={option(url.port or 3306)}\n")
            file_handle.write(f"user={option(url.username)}\n")
            file_handle.write(f"password={option(url.password or '')}\n")
            file_handle.write("default-character-set=utf8mb4\n")
        yield path
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)


def _prepare_output_directory(
    output_dir: Path,
    *,
    precreated_output: bool,
) -> Path:
    """Establish a non-symlink output directory contract before writing."""

    raw_output = output_dir.absolute()
    if os.path.lexists(raw_output):
        metadata = raw_output.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise BackupError("备份输出必须是普通目录，不能是符号链接")
        if precreated_output and any(raw_output.iterdir()):
            raise BackupError("预创建的备份输出目录必须为空")
    elif precreated_output:
        raise BackupError("部署入口尚未建立受保护的备份目录")
    else:
        raw_output.mkdir(mode=0o700, parents=True)
    return raw_output.resolve(strict=True)


def create_snapshot(
    url: URL,
    output_dir: Path,
    *,
    precreated_output: bool = False,
) -> dict[str, object]:
    """流式生成不可覆盖的 gzip SQL 快照及 SHA-256 manifest。"""
    output_dir = _prepare_output_directory(
        output_dir,
        precreated_output=precreated_output,
    )
    mysqldump = shutil.which("mysqldump")
    if mysqldump is None:
        raise BackupError("未找到 mysqldump")
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    final_path = output_dir / f"elysium-mysql-{timestamp}.sql.gz"
    partial_path = final_path.with_suffix(final_path.suffix + ".partial")
    manifest_path = final_path.with_suffix(final_path.suffix + ".manifest.json")
    if final_path.exists() or partial_path.exists() or manifest_path.exists():
        raise BackupError("目标快照或 manifest 已存在，拒绝覆盖")

    with _mysql_defaults_file(url) as defaults_file:
        command = [
            mysqldump,
            f"--defaults-file={defaults_file}",
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
            child_environment = {
                name: value
                for name, value in os.environ.items()
                if name
                in {
                    "LANG",
                    "LC_ALL",
                    "LD_LIBRARY_PATH",
                    "DYLD_LIBRARY_PATH",
                    "PATH",
                    "SYSTEMROOT",
                    "TEMP",
                    "TMP",
                    "TMPDIR",
                    "WINDIR",
                }
            }
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                env=child_environment,
            )
            assert process.stdout is not None
            descriptor: int | None = None
            try:
                try:
                    descriptor = os.open(
                        partial_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        stat.S_IRUSR | stat.S_IWUSR,
                    )
                    _restrict_windows_file_acl(partial_path)
                    raw_output = os.fdopen(descriptor, "wb")
                    descriptor = None
                    with (
                        raw_output,
                        gzip.GzipFile(
                            fileobj=raw_output,
                            mode="wb",
                            compresslevel=6,
                        ) as compressed,
                    ):
                        while chunk := process.stdout.read(1024 * 1024):
                            compressed.write(chunk)
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                    process.stdout.close()
                return_code = process.wait()
                if return_code != 0:
                    raise BackupError(
                        f"mysqldump 失败（退出码 {return_code}，详细输出已隐藏）"
                    )
            except BaseException:
                with suppress(OSError):
                    process.kill()
                with suppress(OSError):
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
    descriptor = os.open(
        manifest_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        _restrict_windows_file_acl(manifest_path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as manifest_file:
            descriptor = -1
            manifest_file.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        manifest_path.unlink(missing_ok=True)
        raise
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
    snapshot.add_argument(
        "--precreated-output",
        action="store_true",
        help=argparse.SUPPRESS,
    )
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
                precreated_output=args.precreated_output,
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
