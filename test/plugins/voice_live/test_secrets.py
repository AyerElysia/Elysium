from __future__ import annotations

import os
from pathlib import Path

import pytest

from plugins.voice_live.secrets import SecretConfigurationError, resolve_secret


def _write_owner_only(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def test_secret_file_is_a_durable_environment_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "provider.key"
    _write_owner_only(path, "file-secret\n")
    monkeypatch.delenv("VOICE_SECRET_TEST", raising=False)

    assert (
        resolve_secret("VOICE_SECRET_TEST", str(path), label="test provider")
        == "file-secret"
    )


def test_environment_secret_has_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "provider.key"
    _write_owner_only(path, "file-secret")
    monkeypatch.setenv("VOICE_SECRET_TEST", "environment-secret")

    assert (
        resolve_secret("VOICE_SECRET_TEST", str(path), label="test provider")
        == "environment-secret"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_secret_file_rejects_group_or_other_access(tmp_path: Path) -> None:
    path = tmp_path / "provider.key"
    path.write_text("unsafe", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(SecretConfigurationError, match="owner-only"):
        resolve_secret("", str(path), label="test provider")


def test_secret_file_rejects_multiple_values(tmp_path: Path) -> None:
    path = tmp_path / "provider.key"
    _write_owner_only(path, "first\nsecond\n")

    with pytest.raises(SecretConfigurationError, match="exactly one"):
        resolve_secret("", str(path), label="test provider")
