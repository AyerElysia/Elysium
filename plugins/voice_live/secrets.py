"""Secure runtime secret resolution for Voice Live.

Environment variables remain the highest-priority source.  A local file may
be configured as a durable fallback, but it must be a regular, owner-only
file.  Secret values are never included in errors or diagnostics.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class SecretConfigurationError(RuntimeError):
    """A configured secret source is present but unsafe or malformed."""


def resolve_secret(
    environment_name: str,
    file_path: str,
    *,
    label: str,
) -> str:
    """Resolve one secret without logging or exposing its value.

    An explicitly exported environment variable wins over the file fallback.
    Missing sources return an empty string so the owning subsystem can report
    its own readiness state.  Unsafe or malformed files fail explicitly.
    """

    name = str(environment_name or "").strip()
    if name:
        value = os.environ.get(name, "").strip()
        if value:
            return value

    configured_path = str(file_path or "").strip()
    if not configured_path:
        return ""
    path = Path(configured_path).expanduser()
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ""
    if stat.S_ISLNK(metadata.st_mode):
        raise SecretConfigurationError(f"{label} secret file must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise SecretConfigurationError(f"{label} secret file must be a regular file")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise SecretConfigurationError(
            f"{label} secret file must be owned by the current user"
        )
    if os.name != "nt" and metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SecretConfigurationError(
            f"{label} secret file permissions must be owner-only"
        )

    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise SecretConfigurationError(f"{label} secret file is empty")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise SecretConfigurationError(
            f"{label} secret file must contain exactly one text value"
        )
    return value


def secret_readiness(
    environment_name: str,
    file_path: str,
    *,
    label: str,
) -> tuple[bool, str]:
    """Return redacted readiness for health endpoints."""

    try:
        value = resolve_secret(environment_name, file_path, label=label)
    except SecretConfigurationError as exc:
        return False, str(exc)
    if value:
        return True, ""
    return False, f"{label} credential is not configured"


__all__ = [
    "SecretConfigurationError",
    "resolve_secret",
    "secret_readiness",
]
