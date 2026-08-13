"""显式的本地 SQLite → MySQL 增量同步工具。

本工具只按已登记的自然唯一键补充 MySQL 中缺少的行，不会更新或删除
已有行。它不得被 cron 或 Elysium 启动入口隐式调用。

调用者必须显式选择 ``--dry-run`` 或 ``--apply``，并通过
``--confirm-database`` 重复目标数据库名。所有连接信息均由环境提供：

- ``ELYSIUM_SYNC_SQLITE_PATH``
- ``ELYSIUM_MYSQL_HOST`` / ``ELYSIUM_MYSQL_PORT``
- ``ELYSIUM_MYSQL_USER`` / ``ELYSIUM_MYSQL_PASSWORD``
- ``ELYSIUM_MYSQL_DATABASE``
- ``ELYSIUM_MYSQL_SSL_MODE``（disabled/required/verify-ca/verify-full）
- ``ELYSIUM_MYSQL_SSL_CA``（verify-ca/verify-full 时必须）
- ``ELYSIUM_MYSQL_SSL_CERT`` / ``ELYSIUM_MYSQL_SSL_KEY``（可选，必须成对）

密码只写入权限 ``0600`` 的短命 MySQL option file，不会进入子进程命令行、
子进程环境或日志。
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TABLES: tuple[tuple[str, str], ...] = (
    ("messages", "message_id"),
    ("chat_streams", "stream_id"),
    ("person_info", "person_id"),
)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MYSQL_SSL_MODES = {
    "disabled": "DISABLED",
    "required": "REQUIRED",
    "verify-ca": "VERIFY_CA",
    "verify-full": "VERIFY_IDENTITY",
}
_SECRET_ENVIRONMENT_NAMES = frozenset(
    {
        "ELYSIUM_MYSQL_PASSWORD",
        "MYSQL_PWD",
    }
)


class SyncError(RuntimeError):
    """同步前置条件或外部客户端执行失败。"""


@dataclass(frozen=True, slots=True)
class SyncConfig:
    """已校验且不带隐式远端默认值的同步配置。"""

    sqlite_path: Path
    mysql_client: Path
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str
    ssl_mode: str
    ssl_ca: Path | None
    ssl_cert: Path | None
    ssl_key: Path | None
    command_timeout_seconds: int


def _required_environment(
    environment: Mapping[str, str],
    name: str,
    *,
    preserve_whitespace: bool = False,
) -> str:
    value = environment.get(name)
    if value is None or value == "":
        raise SyncError(f"环境变量 {name} 未设置")
    if preserve_whitespace:
        return value
    value = value.strip()
    if not value:
        raise SyncError(f"环境变量 {name} 不能为空")
    return value


def _configured_file(
    environment: Mapping[str, str],
    name: str,
    *,
    required: bool,
) -> Path | None:
    raw = environment.get(name, "").strip()
    if not raw:
        if required:
            raise SyncError(f"环境变量 {name} 未设置")
        return None
    try:
        path = Path(raw).expanduser().resolve(strict=True)
    except OSError as error:
        raise SyncError(f"{name} 指向的文件不可用") from error
    if not path.is_file():
        raise SyncError(f"{name} 必须指向普通文件")
    return path


def load_config(environment: Mapping[str, str] | None = None) -> SyncConfig:
    """从环境构造配置；任一远端身份缺失时 fail closed。"""

    values = os.environ if environment is None else environment
    sqlite_path = _configured_file(
        values,
        "ELYSIUM_SYNC_SQLITE_PATH",
        required=True,
    )
    assert sqlite_path is not None

    client_name = values.get("ELYSIUM_MYSQL_CLIENT", "mysql").strip()
    if not client_name:
        raise SyncError("ELYSIUM_MYSQL_CLIENT 不能为空")
    client_path = shutil.which(client_name)
    if client_path is None:
        raise SyncError("未找到 MySQL 命令行客户端")

    port_text = _required_environment(values, "ELYSIUM_MYSQL_PORT")
    try:
        port = int(port_text)
    except ValueError as error:
        raise SyncError("ELYSIUM_MYSQL_PORT 必须是整数") from error
    if not 1 <= port <= 65535:
        raise SyncError("ELYSIUM_MYSQL_PORT 必须在 1..65535 之间")

    ssl_mode = _required_environment(values, "ELYSIUM_MYSQL_SSL_MODE").lower()
    if ssl_mode not in _MYSQL_SSL_MODES:
        raise SyncError(
            "ELYSIUM_MYSQL_SSL_MODE 只允许 disabled/required/verify-ca/verify-full"
        )
    ssl_ca = _configured_file(
        values,
        "ELYSIUM_MYSQL_SSL_CA",
        required=ssl_mode in {"verify-ca", "verify-full"},
    )
    ssl_cert = _configured_file(
        values,
        "ELYSIUM_MYSQL_SSL_CERT",
        required=False,
    )
    ssl_key = _configured_file(
        values,
        "ELYSIUM_MYSQL_SSL_KEY",
        required=False,
    )
    if (ssl_cert is None) != (ssl_key is None):
        raise SyncError("ELYSIUM_MYSQL_SSL_CERT 与 ELYSIUM_MYSQL_SSL_KEY 必须成对设置")

    timeout_text = values.get("ELYSIUM_SYNC_COMMAND_TIMEOUT_SECONDS", "300").strip()
    try:
        command_timeout_seconds = int(timeout_text)
    except ValueError as error:
        raise SyncError("ELYSIUM_SYNC_COMMAND_TIMEOUT_SECONDS 必须是整数") from error
    if not 1 <= command_timeout_seconds <= 3600:
        raise SyncError("ELYSIUM_SYNC_COMMAND_TIMEOUT_SECONDS 必须在 1..3600 之间")

    return SyncConfig(
        sqlite_path=sqlite_path,
        mysql_client=Path(client_path).resolve(),
        host=_required_environment(values, "ELYSIUM_MYSQL_HOST"),
        port=port,
        user=_required_environment(values, "ELYSIUM_MYSQL_USER"),
        password=_required_environment(
            values,
            "ELYSIUM_MYSQL_PASSWORD",
            preserve_whitespace=True,
        ),
        database=_required_environment(values, "ELYSIUM_MYSQL_DATABASE"),
        ssl_mode=ssl_mode,
        ssl_ca=ssl_ca,
        ssl_cert=ssl_cert,
        ssl_key=ssl_key,
        command_timeout_seconds=command_timeout_seconds,
    )


def _mysql_option_value(value: str) -> str:
    """将值安全编码为 MySQL option-file 的双引号值。"""

    if "\x00" in value or "\n" in value or "\r" in value:
        raise SyncError("MySQL 连接参数不能包含 NUL 或换行")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _restrict_windows_file_acl(path: Path) -> None:
    """Protect an empty option file before writing connection credentials."""

    if os.name != "nt":
        return
    whoami = shutil.which("whoami")
    icacls = shutil.which("icacls")
    if whoami is None or icacls is None:
        raise SyncError("无法保护 MySQL 临时凭据：缺少 Windows ACL 工具")
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
        raise SyncError("无法识别当前 Windows 用户") from error
    identity = identity_result.stdout.strip()
    if identity_result.returncode != 0 or not identity:
        raise SyncError("无法识别当前 Windows 用户")
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
                raise SyncError("无法将 MySQL 临时凭据限制为当前用户")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SyncError("无法将 MySQL 临时凭据限制为当前用户") from error


@contextmanager
def mysql_defaults_file(config: SyncConfig) -> Iterator[Path]:
    """通过权限 0600 的短命文件传递 MySQL 凭据。"""

    descriptor, raw_path = tempfile.mkstemp(
        prefix="elysium-sync-mysql-",
        suffix=".cnf",
    )
    path = Path(raw_path)
    descriptor_open = True
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        _restrict_windows_file_acl(path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file_handle:
            descriptor_open = False
            options: list[tuple[str, str]] = [
                ("host", config.host),
                ("port", str(config.port)),
                ("user", config.user),
                ("password", config.password),
                ("database", config.database),
                ("protocol", "tcp"),
                ("default-character-set", "utf8mb4"),
                ("ssl-mode", _MYSQL_SSL_MODES[config.ssl_mode]),
            ]
            if config.ssl_ca is not None:
                options.append(("ssl-ca", str(config.ssl_ca)))
            if config.ssl_cert is not None:
                options.append(("ssl-cert", str(config.ssl_cert)))
            if config.ssl_key is not None:
                options.append(("ssl-key", str(config.ssl_key)))
            file_handle.write("[client]\n")
            for name, value in options:
                file_handle.write(f"{name}={_mysql_option_value(value)}\n")
        yield path
    finally:
        try:
            if descriptor_open:
                os.close(descriptor)
        finally:
            path.unlink(missing_ok=True)


def _mysql_subprocess_environment() -> dict[str, str]:
    allowed_names = {
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
    return {
        name: value
        for name, value in os.environ.items()
        if name in allowed_names and name not in _SECRET_ENVIRONMENT_NAMES
    }


def run_mysql(config: SyncConfig, defaults_file: Path, sql: str) -> str:
    """通过 stdin 执行 SQL，避免将 SQL 或凭据放进 argv。"""

    command = [
        str(config.mysql_client),
        f"--defaults-file={defaults_file}",
        "--batch",
        "--raw",
        "--skip-column-names",
    ]
    try:
        result = subprocess.run(
            command,
            input=sql,
            capture_output=True,
            text=True,
            check=False,
            env=_mysql_subprocess_environment(),
            timeout=config.command_timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise SyncError("MySQL 客户端执行超时，未记录其输出") from error
    except OSError as error:
        raise SyncError("MySQL 客户端无法执行") from error
    if result.returncode != 0:
        raise SyncError(
            f"MySQL 客户端失败（退出码 {result.returncode}），详细输出已抑制"
        )
    return result.stdout


def _quoted_identifier(identifier: str) -> str:
    if not _IDENTIFIER_PATTERN.fullmatch(identifier):
        raise SyncError("遇到未授权的 SQLite 标识符")
    return f"`{identifier}`"


def mysql_literal(value: Any) -> str:
    """使用数字或十六进制字面量表示 SQLite 值，不拼接原始文本。"""

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SyncError("SQLite 数据包含无法安全同步的非有限浮点数")
        return repr(value)
    if isinstance(value, bytes):
        return f"X'{value.hex()}'"
    if isinstance(value, str):
        encoded = value.encode("utf-8").hex()
        return f"CONVERT(X'{encoded}' USING utf8mb4)"
    raise SyncError(f"SQLite 数据包含不支持的值类型: {type(value).__name__}")


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    table_identifier = _quoted_identifier(table)
    cursor = connection.execute(f"PRAGMA table_info({table_identifier})")
    columns = [str(row[1]) for row in cursor.fetchall() if str(row[1]) != "id"]
    if not columns:
        raise SyncError(f"SQLite 表 {table} 不存在或没有可同步列")
    for column in columns:
        _quoted_identifier(column)
    return columns


def _remote_existing_keys(
    config: SyncConfig,
    defaults_file: Path,
    table: str,
    key_column: str,
) -> set[str]:
    sql = f"SELECT {_quoted_identifier(key_column)} FROM {_quoted_identifier(table)};\n"
    output = run_mysql(config, defaults_file, sql)
    return {line for line in output.splitlines() if line}


def _local_key_text(value: Any) -> str:
    if value is None:
        raise SyncError("自然唯一键不能为 NULL")
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SyncError("自然唯一键必须是 UTF-8 文本或数字") from error
    return str(value)


def sync(config: SyncConfig, *, apply: bool) -> int:
    """执行一次显式对比，返回计划新增的总行数。"""

    sqlite_uri = f"{config.sqlite_path.as_uri()}?mode=ro"
    total_planned = 0
    with (
        sqlite3.connect(sqlite_uri, uri=True) as connection,
        mysql_defaults_file(config) as defaults_file,
    ):
        for table, key_column in TABLES:
            columns = _sqlite_columns(connection, table)
            if key_column not in columns:
                raise SyncError(f"SQLite 表 {table} 缺少自然唯一键 {key_column}")
            existing = _remote_existing_keys(
                config,
                defaults_file,
                table,
                key_column,
            )
            key_index = columns.index(key_column)
            select_columns = ", ".join(_quoted_identifier(item) for item in columns)
            rows_to_insert = [
                row
                for row in connection.execute(
                    f"SELECT {select_columns} FROM {_quoted_identifier(table)}"
                )
                if _local_key_text(row[key_index]) not in existing
            ]
            print(f"[{table}] 远端键数={len(existing)} 计划新增={len(rows_to_insert)}")
            total_planned += len(rows_to_insert)
            if not apply or not rows_to_insert:
                continue

            column_list = ", ".join(_quoted_identifier(item) for item in columns)
            statements = [
                f"INSERT INTO {_quoted_identifier(table)} ({column_list}) VALUES "
                f"({', '.join(mysql_literal(value) for value in row)});"
                for row in rows_to_insert
            ]
            run_mysql(config, defaults_file, "\n".join(statements) + "\n")
            print(f"[{table}] 已新增={len(rows_to_insert)}")
    return total_planned


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="显式的 SQLite → MySQL 增量同步（不更新、不删除）"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只对比，不写入")
    mode.add_argument("--apply", action="store_true", help="写入目标中缺少的行")
    parser.add_argument(
        "--confirm-database",
        required=True,
        metavar="NAME",
        help="必须与 ELYSIUM_MYSQL_DATABASE 完全一致",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config()
        if args.confirm_database != config.database:
            raise SyncError(
                "--confirm-database 与 ELYSIUM_MYSQL_DATABASE 不一致，拒绝连接"
            )
        total_planned = sync(config, apply=args.apply)
    except (OSError, sqlite3.Error, SyncError) as error:
        print(f"同步失败: {error}", file=sys.stderr)
        return 2
    mode = "apply" if args.apply else "dry-run"
    print(f"{mode} 完成：计划新增 {total_planned} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
