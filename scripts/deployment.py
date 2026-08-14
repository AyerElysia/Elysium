#!/usr/bin/env python3
"""Safe, repeatable deployment entry point for Elysium.

This module intentionally stops at infrastructure preparation.  It never starts
Elysium during ``bootstrap`` or ``doctor`` and never installs an automatic
startup mechanism.  The ``run`` command must be invoked by an operator and
replaces itself with the foreground Elysium process.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

MINIMUM_PYTHON = (3, 11)
REQUIRED_MODEL_TASKS = frozenset(
    {
        "core",
        "learning",
        "expression",
        "witness",
        "agent",
        "utility",
        "vision",
        "voice",
        "embedding",
        "router",
        "router_context_projection",
        "live",
    }
)
SUBJECT_FILES = ("SOUL.md", "USER.md", "MEMORY.md")
SECRET_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
PLACEHOLDER_MARKERS = (
    "replace-with",
    "change-me",
    "changeme",
    "your-token",
    "your-api-key",
)
RUNTIME_DIRECTORIES = (
    "config/plugins",
    "data/runtime",
    "logs",
)
PLUGIN_CONFIGS: Mapping[str, str] = {
    "config/mcp.toml": (
        "# MCP servers are opt-in. Add explicit, least-privilege entries here.\n"
        "[mcp]\n"
        "enabled = false\n"
    ),
    "config/plugins/ayla_adapter/config.toml": "[plugin]\nenabled = false\n",
    "config/plugins/commands_plugin/config.toml": "[plugin]\nenabled = false\n",
    "config/plugins/feishu_adapter/config.toml": "[plugin]\nenabled = false\n",
    "config/plugins/emoji/config.toml": "[plugin]\nenabled = false\n",
    "config/plugins/kook_adapter/config.toml": "[plugin]\nenabled = false\n",
    "config/plugins/Livestream/config.toml": (
        "[plugin]\nenabled = false\nauto_start = false\n"
    ),
    "config/plugins/napcat_adapter/config.toml": (
        '[plugin]\nenabled = false\n\n[bot]\nqq_id = ""\nqq_nickname = ""\n'
    ),
    "config/plugins/neko_surface/config.toml": "[plugin]\nenabled = false\n",
    "config/plugins/skill_manager/config.toml": (
        "[plugin]\nenabled = false\n\n[manager]\nallow_script_execution = false\n"
    ),
    "config/plugins/tts_voice_plugin/config.toml": ("[plugin]\nenable = false\n"),
    "config/plugins/Voice-Live/config.toml": "[plugin]\nenabled = false\n",
    "config/plugins/werewolf_game/config.toml": ("[plugin]\nenabled = false\n"),
    "config/plugins/life_engine/config.toml": (
        "# Subject authority is restored separately; deployment never creates it.\n"
        "[settings]\n"
        "enabled = true\n"
        "\n[chatter]\n"
        "enabled = false\n"
    ),
}


class DeploymentError(RuntimeError):
    """A deployment precondition failed without changing runtime state."""


CheckStatus = Literal["passed", "warning", "failed", "disabled"]


@dataclass(frozen=True, slots=True)
class Check:
    """One non-secret deployment diagnostic."""

    name: str
    status: CheckStatus
    message: str


@dataclass(frozen=True, slots=True)
class ProcessOwner:
    """Minimal process identity safe to expose in diagnostics."""

    pid: int
    ppid: int | None
    name: str
    cwd: str | None


@dataclass(slots=True)
class DoctorReport:
    """Read-only preflight result."""

    checks: list[Check]

    @property
    def ready(self) -> bool:
        return not any(check.status == "failed" for check in self.checks)

    def add(self, name: str, status: CheckStatus, message: str) -> None:
        self.checks.append(Check(name=name, status=status, message=message))

    def as_dict(self) -> dict[str, Any]:
        counts = {
            status: sum(check.status == status for check in self.checks)
            for status in ("passed", "warning", "failed", "disabled")
        }
        return {
            "ready": self.ready,
            "state": "manual_start_required" if self.ready else "failed",
            "counts": counts,
            "checks": [asdict(check) for check in self.checks],
        }


def repository_root() -> Path:
    """Return the repository root independently of the caller's cwd."""

    return Path(__file__).resolve().parents[1]


def _assert_repository(root: Path) -> None:
    required = ("AGENTS.md", "main.py", "pyproject.toml", "uv.lock")
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise DeploymentError(
            "不是有效的 Elysium 仓库，缺少: " + ", ".join(sorted(missing))
        )


def _assert_python_version() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        required = ".".join(str(part) for part in MINIMUM_PYTHON)
        raise DeploymentError(f"需要 Python >= {required}")


def _path_exists(path: Path) -> bool:
    """Return true for ordinary paths and dangling symlinks."""

    return os.path.lexists(path)


def _ensure_safe_directory(root: Path, relative: str) -> Path:
    """Create a repository-local directory without following symlinks."""

    root = root.resolve(strict=True)
    current = root
    for part in Path(relative).parts:
        if part in {"", ".", ".."}:
            raise DeploymentError(f"非法目录路径: {relative}")
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                metadata = current.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise DeploymentError(f"目录路径不安全: {current}") from None
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise DeploymentError(f"目录路径不安全: {current}")
    return current


def _assert_safe_existing_parents(root: Path, relative: str) -> None:
    """Reject symlink/non-directory ancestors before dependency installation."""

    current = root.resolve(strict=True)
    for part in Path(relative).parent.parts:
        if part in {"", ".", ".."}:
            raise DeploymentError(f"非法配置路径: {relative}")
        current = current / part
        if not _path_exists(current):
            return
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise DeploymentError(f"配置父路径不安全: {current}")


def _inspect_config_target(root: Path, relative: str) -> Literal["missing", "present"]:
    _assert_safe_existing_parents(root, relative)
    target = root / relative
    if not _path_exists(target):
        return "missing"
    metadata = target.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise DeploymentError(f"配置目标不是普通文件，拒绝操作: {relative}")
    return "present"


def _restrict_windows_acl(path: Path, *, recursive: bool = False) -> None:
    """Remove inherited ACLs and grant only the current Windows identity."""

    if os.name != "nt":
        return
    whoami = shutil.which("whoami")
    icacls = shutil.which("icacls")
    if whoami is None or icacls is None:
        raise DeploymentError("无法收紧 Windows 配置 ACL：缺少 whoami 或 icacls")
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
        raise DeploymentError("无法识别当前 Windows 用户，ACL 未建立") from error
    identity = identity_result.stdout.strip()
    if identity_result.returncode != 0 or not identity:
        raise DeploymentError("无法识别当前 Windows 用户，配置 ACL 未建立")
    owner_command = [icacls, str(path), "/setowner", identity]
    if recursive:
        owner_command.append("/T")
    try:
        owner_result = subprocess.run(
            owner_command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError("无法将 Windows 路径 owner 设为当前用户") from error
    if owner_result.returncode != 0:
        raise DeploymentError("无法将 Windows 路径 owner 设为当前用户")
    grant = f"{identity}:(OI)(CI)(F)" if path.is_dir() else f"{identity}:(F)"
    command = [icacls, str(path), "/inheritance:r", "/grant:r", grant]
    if recursive:
        command.append("/T")
    try:
        acl_result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeploymentError("无法将 Windows 路径限制为当前用户") from error
    if acl_result.returncode != 0:
        raise DeploymentError("无法将新配置限制为当前 Windows 用户")


def _windows_acl_payload_is_private(payload: Any, identity: str) -> bool:
    """Validate a structured Get-Acl result without relying on localized text."""

    if not isinstance(payload, dict):
        return False
    normalized_identity = identity.strip().casefold()
    if not normalized_identity:
        return False
    if str(payload.get("Owner", "")).casefold() != normalized_identity:
        return False
    if payload.get("Protected") is not True:
        return False
    raw_rules = payload.get("Rules", [])
    rules = raw_rules if isinstance(raw_rules, list) else [raw_rules]
    current_user_allowed = False
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("Inherited") is not False:
            return False
        if str(rule.get("Type", "")).casefold() != "allow":
            continue
        if str(rule.get("Identity", "")).casefold() != normalized_identity:
            return False
        current_user_allowed = True
    return current_user_allowed


def _windows_acl_is_private(path: Path) -> bool:
    """Return whether a Windows path is owned and accessible only by this user."""

    if os.name != "nt":
        return True
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        return False
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$acl = Get-Acl -LiteralPath $args[0]; "
        "$rules = @($acl.Access | ForEach-Object { "
        "[PSCustomObject]@{ Identity = $_.IdentityReference.Value; "
        "Type = $_.AccessControlType.ToString(); Inherited = $_.IsInherited } }); "
        "[PSCustomObject]@{ Current = "
        "[System.Security.Principal.WindowsIdentity]::GetCurrent().Name; "
        "Owner = $acl.Owner; Protected = "
        "$acl.AreAccessRulesProtected; Rules = $rules } | "
        "ConvertTo-Json -Compress -Depth 5"
    )
    try:
        result = subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                str(path),
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False
    identity = str(payload.get("Current", "")) if isinstance(payload, dict) else ""
    return _windows_acl_payload_is_private(payload, identity)


def _atomic_create_private_file(root: Path, relative: str, content: str) -> bool:
    """Atomically create an owner-only file; never replace an existing path."""

    if _inspect_config_target(root, relative) == "present":
        return False
    target = root / relative
    parent_relative = str(target.parent.relative_to(root))
    parent = _ensure_safe_directory(root, parent_relative)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        except OSError as error:
            if error.errno in {
                errno.EPERM,
                errno.EOPNOTSUPP,
                getattr(errno, "ENOTSUP", errno.EOPNOTSUPP),
            }:
                raise DeploymentError(
                    f"文件系统不支持原子且不覆盖地创建配置: {relative}"
                ) from error
            raise
        try:
            _restrict_windows_acl(target)
        except DeploymentError:
            target.unlink(missing_ok=True)
            raise
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _configuration_sources(root: Path) -> dict[str, str]:
    sources = {
        "config/core.toml": root / "config/core.toml.example",
        "config/models.toml": root / "config/models.toml.example",
    }
    result: dict[str, str] = dict(PLUGIN_CONFIGS)
    for destination, source in sources.items():
        if not source.is_file():
            raise DeploymentError(f"缺少配置模板: {source.relative_to(root)}")
        result[destination] = source.read_text(encoding="utf-8")
    return result


def initialize_configuration(root: Path) -> tuple[list[str], list[str]]:
    """Create missing engineering configuration without overwriting files."""

    sources = _configuration_sources(root)
    for relative in sources:
        _inspect_config_target(root, relative)
    for relative in RUNTIME_DIRECTORIES:
        _ensure_safe_directory(root, relative)

    created: list[str] = []
    preserved: list[str] = []
    for relative, content in sources.items():
        if _atomic_create_private_file(root, relative, content):
            created.append(relative)
        else:
            preserved.append(relative)
    return sorted(created), sorted(preserved)


def _run_quiet(
    command: Sequence[str],
    *,
    root: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=root,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def sync_dependencies(root: Path, *, with_dev: bool) -> None:
    """Install exactly the locked dependency set without exposing resolver output."""

    uv = shutil.which("uv")
    if uv is None:
        raise DeploymentError("未找到 uv，请先安装 uv 并确保它位于 PATH")
    sync_command = [uv, "sync", "--locked"]
    sync_command.extend(["--group", "dev"] if with_dev else ["--no-dev"])
    try:
        sync_result = _run_quiet(sync_command, root=root, timeout=1800)
    except subprocess.TimeoutExpired as error:
        raise DeploymentError("uv sync 超时，未启动任何服务") from error
    if sync_result.returncode != 0:
        raise DeploymentError(
            f"锁定依赖同步失败（退出码 {sync_result.returncode}）；"
            "输出已隐藏以避免泄露私有索引凭据"
        )
    check_result = _run_quiet([uv, "pip", "check"], root=root, timeout=300)
    if check_result.returncode != 0:
        raise DeploymentError(
            f"依赖一致性检查失败（退出码 {check_result.returncode}）；"
            "输出已隐藏以避免泄露凭据"
        )


def bootstrap(
    root: Path, *, with_dev: bool, config_only: bool
) -> tuple[list[str], list[str]]:
    """Prepare dependencies and create-only configuration."""

    _assert_python_version()
    _assert_repository(root)
    sources = _configuration_sources(root)
    for relative in sources:
        _inspect_config_target(root, relative)
    if not config_only:
        sync_dependencies(root, with_dev=with_dev)
    return initialize_configuration(root)


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        parsed = tomllib.load(handle)
    if not isinstance(parsed, dict):
        raise TypeError("TOML 顶层必须是 table")
    return parsed


def _private_file_check(
    report: DoctorReport,
    root: Path,
    relative: str,
) -> bool:
    try:
        _assert_safe_existing_parents(root, relative)
    except DeploymentError:
        report.add(relative, "failed", "父路径包含符号链接或非目录组件")
        return False
    path = root / relative
    if not _path_exists(path):
        report.add(relative, "failed", "文件不存在")
        return False
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        report.add(relative, "failed", "必须是仓库内的普通文件，不能是符号链接")
        return False
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        report.add(relative, "failed", "文件解析到仓库边界之外")
        return False
    if os.name == "nt":
        if not _windows_acl_is_private(path):
            report.add(
                relative,
                "failed",
                "Windows ACL 未证明仅当前用户可访问",
            )
            return False
        report.add(relative, "passed", "Windows owner 与 ACL 边界符合要求")
        return True
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        report.add(relative, "failed", "文件 owner 不是当前用户")
        return False
    if metadata.st_mode & 0o077:
        report.add(relative, "failed", "权限过宽；应仅允许当前用户读写（0600）")
        return False
    report.add(relative, "passed", "存在且访问边界符合要求")
    return True


def _resolved_secret_state(
    value: Any, environment: Mapping[str, str]
) -> tuple[bool, str]:
    if not isinstance(value, str) or not value.strip():
        return False, "未配置"
    lowered = value.casefold()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return False, "仍是示例占位符"
    references = SECRET_REFERENCE.findall(value)
    missing = [name for name in references if not environment.get(name, "").strip()]
    if missing:
        return False, "缺少环境变量: " + ", ".join(sorted(set(missing)))
    if SECRET_REFERENCE.search(value):
        return True, "所需环境变量已设置"
    return True, "已配置（值未显示）"


def _check_models(
    report: DoctorReport,
    root: Path,
    environment: Mapping[str, str],
) -> None:
    relative = "config/models.toml"
    path = root / relative
    if not _private_file_check(report, root, relative):
        return
    try:
        data = _load_toml(path)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        report.add("models.schema", "failed", "TOML 无法解析")
        return
    providers = data.get("providers")
    models = data.get("models")
    tasks = data.get("tasks")
    if not isinstance(providers, dict) or not providers:
        report.add("models.providers", "failed", "至少需要一个 provider")
        return
    if not isinstance(models, dict) or not models:
        report.add("models.models", "failed", "至少需要一个 model")
        return
    if not isinstance(tasks, dict):
        report.add("models.tasks", "failed", "tasks table 缺失")
        return

    missing_tasks = sorted(REQUIRED_MODEL_TASKS.difference(tasks))
    if missing_tasks:
        report.add(
            "models.tasks",
            "failed",
            "缺少正式任务路由: " + ", ".join(missing_tasks),
        )
    else:
        report.add("models.tasks", "passed", "12 个正式任务路由均已声明")

    reference_errors: list[str] = []
    for model_name, raw_model in models.items():
        if not isinstance(raw_model, dict):
            reference_errors.append(str(model_name))
            continue
        provider = raw_model.get("provider")
        if not isinstance(provider, str) or provider not in providers:
            reference_errors.append(str(model_name))
    for task_name, raw_task in tasks.items():
        route = raw_task.get("models") if isinstance(raw_task, dict) else None
        if not isinstance(route, list) or not route:
            reference_errors.append(f"tasks.{task_name}")
            continue
        if any(not isinstance(name, str) or name not in models for name in route):
            reference_errors.append(f"tasks.{task_name}")
    if reference_errors:
        report.add(
            "models.references",
            "failed",
            "存在无效引用: " + ", ".join(sorted(reference_errors)),
        )
    else:
        report.add("models.references", "passed", "provider/model/task 引用闭合")

    invalid_providers: list[str] = []
    for provider_name, raw_provider in providers.items():
        value = raw_provider.get("api_key") if isinstance(raw_provider, dict) else None
        valid, _ = _resolved_secret_state(value, environment)
        if (
            not valid
            or not isinstance(value, str)
            or SECRET_REFERENCE.fullmatch(value.strip()) is None
        ):
            invalid_providers.append(str(provider_name))
    if invalid_providers:
        report.add(
            "models.secrets",
            "failed",
            "以下 provider 必须使用已解析的 ${ENV_VAR} 密钥引用: "
            + ", ".join(sorted(invalid_providers)),
        )
    else:
        report.add("models.secrets", "passed", "provider 密钥引用均已解析（值未显示）")


def _check_core(
    report: DoctorReport,
    root: Path,
    environment: Mapping[str, str],
) -> dict[str, Any] | None:
    relative = "config/core.toml"
    path = root / relative
    if not _private_file_check(report, root, relative):
        return None
    try:
        data = _load_toml(path)
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        report.add("core.schema", "failed", "TOML 无法解析")
        return None

    storage = data.get("storage", {})
    backend = storage.get("backend", "local") if isinstance(storage, dict) else None
    if backend not in {"local", "mysql"}:
        report.add("core.storage", "failed", "storage.backend 必须为 local 或 mysql")
    else:
        report.add("core.storage", "passed", f"已选择 {backend} 后端")
    if backend == "mysql":
        database = data.get("database", {})
        password = (
            database.get("mysql_password") if isinstance(database, dict) else None
        )
        valid, reason = _resolved_secret_state(password, environment)
        if (
            valid
            and isinstance(password, str)
            and SECRET_REFERENCE.fullmatch(password.strip()) is None
        ):
            valid = False
            reason = "mysql_password 必须使用 ${ENV_VAR} 引用，禁止明文"
        report.add(
            "core.mysql_secret",
            "passed" if valid else "failed",
            reason,
        )
        generation = storage.get("backend_generation", "")
        if not isinstance(generation, str) or not generation.strip():
            report.add(
                "core.mysql_generation",
                "failed",
                "MySQL 模式必须绑定已验证的 backend_generation",
            )
        else:
            report.add(
                "core.mysql_generation", "passed", "已声明存储 generation（值未显示）"
            )
        recycle = (
            database.get("mysql_pool_recycle_seconds", 120)
            if isinstance(database, dict)
            else None
        )
        idle_timeout = (
            database.get("mysql_idle_session_timeout_seconds", 180)
            if isinstance(database, dict)
            else None
        )
        valid_timeouts = (
            isinstance(recycle, int)
            and not isinstance(recycle, bool)
            and isinstance(idle_timeout, int)
            and not isinstance(idle_timeout, bool)
            and recycle < idle_timeout
        )
        report.add(
            "core.mysql_pool_lifetime",
            "passed" if valid_timeouts else "failed",
            (
                "连接池会在服务端空闲超时前回收连接"
                if valid_timeouts
                else "mysql_pool_recycle_seconds 必须小于 "
                "mysql_idle_session_timeout_seconds"
            ),
        )
        report.add(
            "core.mysql_connectivity",
            "warning",
            "离线 doctor 不连接数据库；启动时仍会执行 generation 与 schema 验证",
        )

    http = data.get("http_router", {})
    if isinstance(http, dict) and http.get("enable_app_api_v1", False):
        missing = [
            name
            for name in (
                "ELYSIUM_APP_API_V1_SIGNING_SECRET",
                "ELYSIUM_INSTALLATION_ID",
            )
            if not environment.get(name, "").strip()
        ]
        report.add(
            "core.app_api_secrets",
            "failed" if missing else "passed",
            ("缺少环境变量: " + ", ".join(missing))
            if missing
            else "App API 所需环境变量已设置（值未显示）",
        )
    else:
        report.add("core.app_api", "disabled", "App API v1 未启用")
    return data


def _check_subject_authority(
    report: DoctorReport,
    root: Path,
    core: Mapping[str, Any] | None,
) -> None:
    backend = "local"
    if core is not None and isinstance(core.get("storage"), dict):
        backend = str(core["storage"].get("backend", "local"))

    life_config_path = root / "config/plugins/life_engine/config.toml"
    enabled = True
    try:
        _assert_safe_existing_parents(
            root,
            "config/plugins/life_engine/config.toml",
        )
    except DeploymentError:
        report.add("life_engine.config", "failed", "配置父路径不安全")
        return
    if _path_exists(life_config_path):
        metadata = life_config_path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            report.add("life_engine.config", "failed", "配置必须是普通文件")
            return
        try:
            life_config = _load_toml(life_config_path)
            settings = life_config.get("settings", {})
            if isinstance(settings, dict):
                enabled = bool(settings.get("enabled", True))
        except (OSError, ValueError, tomllib.TOMLDecodeError):
            report.add("life_engine.config", "failed", "TOML 无法解析")
            return
    else:
        report.add("life_engine.config", "failed", "配置不存在")
        return
    if not enabled:
        report.add(
            "subject.authority",
            "disabled",
            "Life Engine 已显式禁用；当前只具备基础设施能力",
        )
        return
    if backend == "mysql":
        report.add(
            "subject.authority",
            "warning",
            "主体权威位于 MySQL；离线 doctor 不读取或改写远端内容",
        )
        return

    workspace = root / "data/life_engine_workspace"
    try:
        _assert_safe_existing_parents(
            root,
            "data/life_engine_workspace/.subject-authority",
        )
    except DeploymentError:
        for name in SUBJECT_FILES:
            report.add(
                f"subject.{name}",
                "failed",
                "主体工作区父路径包含符号链接或非目录组件",
            )
        return
    for name in SUBJECT_FILES:
        path = workspace / name
        check_name = f"subject.{name}"
        if not _path_exists(path):
            report.add(
                check_name,
                "failed",
                "缺失；只能从可信版本历史或备份逐字节恢复",
            )
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            report.add(check_name, "failed", "必须是普通文件，不能是符号链接")
            continue
        if os.name == "nt":
            if not _windows_acl_is_private(path):
                report.add(
                    check_name,
                    "failed",
                    "Windows ACL 未证明仅当前用户可访问",
                )
                continue
            permission_status: CheckStatus = "passed"
            permission_message = "内容可无损读取，Windows ACL 边界已验证"
        elif hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
            report.add(check_name, "failed", "主体文件 owner 不是当前用户")
            continue
        elif metadata.st_mode & 0o077:
            report.add(check_name, "failed", "权限过宽；主体文件应为 0600")
            continue
        else:
            permission_status = "passed"
            permission_message = "存在且可无损读取；内容未显示、未改写"
        try:
            contents = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            report.add(check_name, "failed", "不可读或不是有效 UTF-8")
            continue
        if name == "SOUL.md" and not contents.strip():
            report.add(check_name, "failed", "SOUL.md 不能为空")
            continue
        report.add(check_name, permission_status, permission_message)


def _safe_process_name(value: str | None) -> str:
    if not value:
        return "unknown"
    return Path(value).name[:80]


def _processes_with_psutil(root: Path) -> list[ProcessOwner] | None:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return None
    owners: list[ProcessOwner] = []
    root_text = str(root.resolve())
    try:
        processes = psutil.process_iter(["pid", "ppid", "name", "cwd", "cmdline"])
        for process in processes:
            try:
                info = process.info
                cwd = info.get("cwd")
                command = info.get("cmdline") or []
                if (
                    cwd
                    and str(Path(cwd).resolve()) == root_text
                    and any(
                        Path(str(argument)).name == "main.py" for argument in command
                    )
                ):
                    owners.append(
                        ProcessOwner(
                            pid=int(info["pid"]),
                            ppid=(
                                int(info["ppid"])
                                if info.get("ppid") is not None
                                else None
                            ),
                            name=_safe_process_name(info.get("name")),
                            cwd=cwd,
                        )
                    )
            except (OSError, psutil.Error):
                continue
    except (OSError, psutil.Error) as error:
        raise DeploymentError("无法完成运行实例遍历") from error
    return owners


def _processes_from_proc(root: Path) -> list[ProcessOwner] | None:
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    owners: list[ProcessOwner] = []
    root_resolved = root.resolve()
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cwd = (entry / "cwd").resolve(strict=True)
            if cwd != root_resolved:
                continue
            command = (entry / "cmdline").read_bytes().split(b"\0")
            arguments = [
                part.decode("utf-8", errors="replace") for part in command if part
            ]
            if not any(Path(argument).name == "main.py" for argument in arguments):
                continue
            status = (entry / "status").read_text(encoding="utf-8", errors="replace")
            parent_match = re.search(r"^PPid:\s+(\d+)$", status, re.MULTILINE)
            name_match = re.search(r"^Name:\s+(.+)$", status, re.MULTILINE)
            owners.append(
                ProcessOwner(
                    pid=int(entry.name),
                    ppid=int(parent_match.group(1)) if parent_match else None,
                    name=_safe_process_name(
                        name_match.group(1) if name_match else None
                    ),
                    cwd=str(cwd),
                )
            )
        except (FileNotFoundError, OSError, PermissionError):
            continue
    return owners


def find_elysium_processes(root: Path) -> list[ProcessOwner]:
    """Find exact-root Elysium processes without returning command arguments."""

    detected = _processes_with_psutil(root)
    if detected is not None:
        return detected
    detected = _processes_from_proc(root)
    if detected is not None:
        return detected
    raise DeploymentError(
        "当前解释器缺少 psutil 且平台无 /proc，无法证明未运行重复实例"
    )


def _port_owners(port: int) -> list[ProcessOwner]:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return []
    pids: set[int] = set()
    try:
        for connection in psutil.net_connections(kind="inet"):
            if (
                connection.status == psutil.CONN_LISTEN
                and connection.laddr.port == port
                and connection.pid is not None
            ):
                pids.add(connection.pid)
    except (OSError, psutil.Error):
        return []
    owners: list[ProcessOwner] = []
    for pid in sorted(pids):
        try:
            process = psutil.Process(pid)
            owners.append(
                ProcessOwner(
                    pid=pid,
                    ppid=process.ppid(),
                    name=_safe_process_name(process.name()),
                    cwd=process.cwd(),
                )
            )
        except (OSError, psutil.Error):
            owners.append(ProcessOwner(pid=pid, ppid=None, name="unknown", cwd=None))
    return owners


def _format_owners(owners: Sequence[ProcessOwner]) -> str:
    if not owners:
        return "owner 无法读取"
    return "; ".join(
        f"pid={owner.pid}, ppid={owner.ppid}, name={owner.name}, cwd={owner.cwd or 'unknown'}"
        for owner in owners
    )


def _check_process_and_port(
    report: DoctorReport,
    root: Path,
    core: Mapping[str, Any] | None,
) -> None:
    try:
        processes = find_elysium_processes(root)
    except DeploymentError as error:
        report.add("runtime.instance", "failed", str(error))
    else:
        if processes:
            report.add(
                "runtime.instance",
                "failed",
                "检测到同仓库 Elysium 实例: " + _format_owners(processes),
            )
        else:
            report.add("runtime.instance", "passed", "未检测到同仓库运行实例")

    http = core.get("http_router", {}) if core is not None else {}
    if not isinstance(http, dict) or not bool(http.get("enable_http_router", True)):
        report.add("runtime.http_port", "disabled", "Core HTTP Router 未启用")
        return
    host = str(http.get("http_router_host", "127.0.0.1"))
    port = http.get("http_router_port", 8000)
    if not isinstance(port, int) or not (1 <= port <= 65535):
        report.add("runtime.http_port", "failed", "HTTP 端口必须在 1..65535")
        return
    try:
        addresses = socket.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            flags=socket.AI_PASSIVE,
        )
    except socket.gaierror:
        report.add("runtime.http_port", "failed", "HTTP 监听地址无法解析")
        return
    last_error: OSError | None = None
    for family, socket_type, protocol, _, address in addresses:
        probe = socket.socket(family, socket_type, protocol)
        try:
            probe.bind(address)
        except OSError as error:
            last_error = error
        finally:
            probe.close()
        if last_error is not None:
            break
    if last_error is None:
        report.add("runtime.http_port", "passed", f"{host}:{port} 可绑定")
        return
    owners = _port_owners(port)
    detail = _format_owners(owners)
    if last_error is not None and last_error.errno == errno.EACCES:
        report.add("runtime.http_port", "failed", f"无权绑定 {host}:{port}; {detail}")
    else:
        report.add("runtime.http_port", "failed", f"{host}:{port} 已占用; {detail}")


def _check_dependencies(report: DoctorReport, root: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        report.add("dependencies.uv", "failed", "未找到 uv")
        return
    report.add("dependencies.uv", "passed", "uv 已安装")
    try:
        result = _run_quiet(
            [
                uv,
                "sync",
                "--check",
                "--locked",
                "--offline",
                "--no-dev",
                "--inexact",
            ],
            root=root,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        report.add("dependencies.lock", "failed", "锁定环境检查超时")
        return
    report.add(
        "dependencies.lock",
        "passed" if result.returncode == 0 else "failed",
        "环境与 uv.lock 一致"
        if result.returncode == 0
        else "环境与 uv.lock 不一致；请重新运行 bootstrap",
    )


def _check_runtime_schemas(report: DoctorReport, root: Path) -> None:
    try:
        python = _venv_python(root)
    except DeploymentError:
        report.add("config.schema", "failed", "项目虚拟环境不可用")
        return
    try:
        result = _run_quiet(
            [str(python), "scripts/deployment_schema_check.py"],
            root=root,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        report.add("config.schema", "failed", "当前 schema 只读校验超时")
        return
    report.add(
        "config.schema",
        "passed" if result.returncode == 0 else "failed",
        "Core、MCP、模型与插件配置通过当前代码 schema"
        if result.returncode == 0
        else "至少一份配置不符合当前代码 schema；详细值已隐藏",
    )


def _check_local_config_permissions(report: DoctorReport, root: Path) -> bool:
    config_root = root / "config"
    if not config_root.is_dir() or config_root.is_symlink():
        report.add("config.boundary", "failed", "config 必须是仓库内普通目录")
        return False
    insecure: list[str] = []
    unsafe: list[str] = []
    try:
        for current_root, directory_names, file_names in os.walk(
            config_root,
            followlinks=False,
        ):
            current = Path(current_root)
            for directory_name in list(directory_names):
                path = current / directory_name
                relative = str(path.relative_to(root))
                try:
                    metadata = path.lstat()
                except OSError:
                    unsafe.append(relative)
                    directory_names.remove(directory_name)
                    continue
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    unsafe.append(relative)
                    directory_names.remove(directory_name)
            for file_name in file_names:
                path = current / file_name
                relative = str(path.relative_to(root))
                try:
                    metadata = path.lstat()
                except OSError:
                    unsafe.append(relative)
                    continue
                if stat.S_ISLNK(metadata.st_mode) or (
                    path.suffix == ".toml" and not stat.S_ISREG(metadata.st_mode)
                ):
                    unsafe.append(relative)
                elif (
                    path.suffix == ".toml"
                    and os.name != "nt"
                    and metadata.st_mode & 0o077
                ):
                    insecure.append(relative)
    except OSError:
        report.add("config.boundary", "failed", "配置树无法安全遍历")
        return False
    if unsafe:
        report.add(
            "config.boundary",
            "failed",
            "配置中存在非普通文件: " + ", ".join(sorted(unsafe)),
        )
    else:
        report.add("config.boundary", "passed", "配置树未包含符号链接或异常文件")
    if os.name == "nt":
        unproven = [
            str(path.relative_to(root))
            for path in config_root.rglob("*.toml")
            if path.is_file()
            and not path.is_symlink()
            and not _windows_acl_is_private(path)
        ]
        if unproven:
            report.add(
                "config.permissions",
                "failed",
                "以下本地配置 ACL 未证明为 owner-only: " + ", ".join(sorted(unproven)),
            )
        else:
            report.add(
                "config.permissions",
                "passed",
                "本地 TOML 的 Windows owner 与 ACL 边界已验证",
            )
    elif insecure:
        report.add(
            "config.permissions",
            "failed",
            "以下本地配置权限应改为 0600: " + ", ".join(sorted(insecure)),
        )
    else:
        report.add("config.permissions", "passed", "本地 TOML 均为 owner-only")
    return not unsafe and not insecure and not (os.name == "nt" and bool(unproven))


def doctor_repository(
    root: Path,
    *,
    environment: Mapping[str, str] | None = None,
    check_dependencies: bool = True,
    check_processes: bool = True,
) -> DoctorReport:
    """Perform a read-only readiness check without importing runtime config."""

    environment = os.environ if environment is None else environment
    report = DoctorReport(checks=[])
    try:
        _assert_repository(root)
    except DeploymentError as error:
        report.add("repository", "failed", str(error))
        return report
    report.add("repository", "passed", "仓库锚点完整")
    if sys.version_info < MINIMUM_PYTHON:
        report.add("python", "failed", "需要 Python >= 3.11")
    else:
        report.add("python", "passed", "Python 版本满足要求")
    if check_dependencies:
        _check_dependencies(report, root)
    core = _check_core(report, root, environment)
    _check_models(report, root, environment)
    config_boundary_safe = _check_local_config_permissions(report, root)
    if check_dependencies and config_boundary_safe:
        _check_runtime_schemas(report, root)
    _check_subject_authority(report, root, core)
    if check_processes:
        _check_process_and_port(report, root, core)
    return report


def _print_report(report: DoctorReport, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        return
    labels = {
        "passed": "PASS",
        "warning": "WARN",
        "failed": "FAIL",
        "disabled": "OFF ",
    }
    for check in report.checks:
        print(f"[{labels[check.status]}] {check.name}: {check.message}")
    state = "可由用户手工前台启动" if report.ready else "未就绪"
    print(f"\n结果: {state}")


def _venv_python(root: Path) -> Path:
    candidates = (
        root / ".venv/Scripts/python.exe",
        root / ".venv/bin/python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise DeploymentError("未找到项目虚拟环境，请先运行 bootstrap")


def _reexec_with_project_python(root: Path, argv: Sequence[str]) -> None:
    """Use the locked environment for checks that require runtime dependencies."""

    expected_prefix = (root / ".venv").resolve()
    try:
        current_prefix = Path(sys.prefix).resolve()
    except OSError:
        current_prefix = Path(sys.prefix)
    if current_prefix == expected_prefix:
        return
    python = _venv_python(root)
    command = [str(python), str(Path(__file__).resolve()), *argv]
    try:
        os.execve(str(python), command, os.environ.copy())
    except OSError as error:
        raise DeploymentError("无法使用项目虚拟环境执行部署检查") from error


def run_foreground(root: Path) -> int:
    """Fail closed on preflight, then replace this process with Elysium."""

    report = doctor_repository(root)
    _print_report(report, as_json=False)
    if not report.ready:
        return 2
    uv = shutil.which("uv")
    if uv is None:
        raise DeploymentError("未找到 uv")
    command = [uv, "run", "--frozen", "--no-sync", "python", "main.py"]
    print("\n即将以前台方式启动；停止请使用一次 Ctrl-C。", flush=True)
    os.chdir(root)
    os.execvpe(uv, command, os.environ.copy())
    return 127


def _assert_private_backup_parent(parent: Path) -> None:
    """Require the operator-owned boundary promised by the backup contract."""

    try:
        metadata = parent.lstat()
    except OSError as error:
        raise DeploymentError("备份目标的父目录不存在或不可检查") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise DeploymentError("备份目标的父路径必须是普通目录")
    if os.name == "nt":
        if not _windows_acl_is_private(parent):
            raise DeploymentError("备份父目录的 Windows ACL 未证明为 owner-only")
        return
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise DeploymentError("备份父目录 owner 不是当前用户")
    if metadata.st_mode & 0o077:
        raise DeploymentError("备份父目录必须是 owner-only（POSIX 0700）")


def create_backup(
    root: Path,
    *,
    output: Path,
    writer_frozen: bool,
    mysql_url_env: str,
) -> int:
    """Delegate to the repository's non-overwriting backup implementations."""

    _assert_repository(root)
    requested_output = output.absolute()
    if _path_exists(requested_output):
        raise DeploymentError("备份目标已存在，拒绝覆盖或复用")
    _assert_private_backup_parent(requested_output.parent)
    output = requested_output.resolve()
    if _path_exists(output):
        raise DeploymentError("备份目标解析后已存在，拒绝覆盖或复用")
    if writer_frozen:
        owners = find_elysium_processes(root)
        if owners:
            raise DeploymentError(
                "不能声明 writer 已冻结；仍检测到 Elysium: " + _format_owners(owners)
            )
    core_path = root / "config/core.toml"
    boundary_report = DoctorReport(checks=[])
    if not _private_file_check(boundary_report, root, "config/core.toml"):
        raise DeploymentError("缺少安全的 config/core.toml")
    try:
        core = _load_toml(core_path)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
        raise DeploymentError("config/core.toml 无法解析") from error
    storage = core.get("storage", {})
    backend = storage.get("backend", "local") if isinstance(storage, dict) else None
    python = _venv_python(root)
    if backend == "local":
        database = core.get("database", {})
        sqlite_value = (
            database.get("sqlite_path", "data/Elysium.db")
            if isinstance(database, dict)
            else None
        )
        if not isinstance(sqlite_value, str) or not sqlite_value.strip():
            raise DeploymentError("database.sqlite_path 必须是 data 下的非空路径")
        sqlite_path = Path(sqlite_value)
        if sqlite_path.is_absolute():
            raise DeploymentError("database.sqlite_path 必须位于仓库 data 目录")
        sqlite_target = root / sqlite_path
        try:
            _assert_safe_existing_parents(root, sqlite_path.as_posix())
            target_metadata = sqlite_target.lstat()
            if stat.S_ISLNK(target_metadata.st_mode) or not stat.S_ISREG(
                target_metadata.st_mode
            ):
                raise DeploymentError("Core SQLite 源必须是普通文件")
            core_sqlite_relative = sqlite_target.resolve(strict=True).relative_to(
                (root / "data").resolve(strict=True)
            )
        except (DeploymentError, OSError, ValueError) as error:
            raise DeploymentError(
                "database.sqlite_path 必须指向 data 内已存在的普通 SQLite "
                "文件，且路径不含符号链接"
            ) from error
        command = [
            str(python),
            "scripts/backup_life_data.py",
            "--data-root",
            "data",
            "--output",
            str(output),
            "--core-sqlite-relative",
            core_sqlite_relative.as_posix(),
            "--precreated-output",
        ]
        if writer_frozen:
            command.append("--writer-frozen")
    elif backend == "mysql":
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", mysql_url_env):
            raise DeploymentError("MySQL URL 环境变量名无效")
        if not os.environ.get(mysql_url_env, "").strip():
            raise DeploymentError(f"环境变量 {mysql_url_env} 未设置")
        command = [
            str(python),
            "scripts/backup_mysql.py",
            "snapshot",
            "--output-dir",
            str(output),
            "--source-url-env",
            mysql_url_env,
            "--precreated-output",
        ]
    else:
        raise DeploymentError("未知 storage.backend，拒绝备份")
    previous_umask: int | None = None
    if os.name != "nt":
        previous_umask = os.umask(0o077)
    try:
        try:
            output.mkdir(mode=0o700, parents=False)
        except FileExistsError as error:
            raise DeploymentError("备份目标已存在，拒绝覆盖或复用") from error
        except OSError as error:
            raise DeploymentError(
                "无法创建备份目标；请先建立 owner-only 的父目录"
            ) from error
        try:
            _restrict_windows_acl(output)
        except DeploymentError:
            try:
                output.rmdir()
            except OSError:
                pass
            raise
        output_metadata = output.stat()
        output_identity = (int(output_metadata.st_dev), int(output_metadata.st_ino))
        try:
            result = subprocess.run(command, cwd=root, check=False)
        except OSError as error:
            raise DeploymentError("无法执行受控备份工具") from error
    finally:
        if previous_umask is not None:
            os.umask(previous_umask)
    try:
        final_metadata = output.lstat()
    except OSError as error:
        raise DeploymentError("备份子进程返回后目标目录不可用") from error
    if stat.S_ISLNK(final_metadata.st_mode) or not stat.S_ISDIR(final_metadata.st_mode):
        raise DeploymentError("备份子进程返回后目标边界已改变")
    final_identity = (int(final_metadata.st_dev), int(final_metadata.st_ino))
    if final_identity != output_identity:
        raise DeploymentError("备份子进程返回后目标目录身份已改变")
    if result.returncode == 0 and os.name == "nt":
        _restrict_windows_acl(output, recursive=True)
    return result.returncode


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Elysium 安全部署入口：只准备基础设施，Elysium 始终由用户手工前台启动"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="同步锁定依赖并原子创建缺失的工程配置"
    )
    bootstrap_parser.add_argument(
        "--with-dev", action="store_true", help="同时安装 dev 依赖组"
    )
    bootstrap_parser.add_argument(
        "--config-only",
        action="store_true",
        help="只创建缺失配置，不同步依赖（恢复/测试场景）",
    )

    doctor_parser = subparsers.add_parser("doctor", help="执行完全只读的启动前检查")
    doctor_parser.add_argument("--json", action="store_true", help="输出 JSON")

    subparsers.add_parser("run", help="检查通过后以前台方式手工启动")

    backup_parser = subparsers.add_parser(
        "backup", help="创建不覆盖源数据的本地或 MySQL 快照"
    )
    backup_parser.add_argument("--output", type=Path, required=True)
    backup_parser.add_argument(
        "--writer-frozen",
        action="store_true",
        help="仅在所有 writer 已由操作者确认停止后声明一致性候选",
    )
    backup_parser.add_argument(
        "--mysql-url-env",
        default="ELYSIUM_MYSQL_URL",
        help="保存 MySQL URL 的环境变量名（仅 MySQL 模式）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _assert_python_version()
        raw_argv = list(sys.argv[1:] if argv is None else argv)
        args = _parser().parse_args(raw_argv)
        root = repository_root()
        if args.command in {"doctor", "run", "backup"}:
            _reexec_with_project_python(root, raw_argv)
        if args.command == "bootstrap":
            created, preserved = bootstrap(
                root,
                with_dev=args.with_dev,
                config_only=args.config_only,
            )
            for relative in created:
                print(f"[CREATE] {relative}")
            for relative in preserved:
                print(f"[KEEP]   {relative}")
            print(
                "基础设施准备完成；尚未启动 Elysium。恢复主体权威并通过 doctor 后，"
                "由用户执行 run。"
            )
            return 0
        if args.command == "doctor":
            report = doctor_repository(root)
            _print_report(report, as_json=args.json)
            return 0 if report.ready else 2
        if args.command == "run":
            return run_foreground(root)
        if args.command == "backup":
            return create_backup(
                root,
                output=args.output,
                writer_frozen=args.writer_frozen,
                mysql_url_env=args.mysql_url_env,
            )
        raise DeploymentError("未知命令")
    except DeploymentError as error:
        print(f"部署失败: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("操作已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
