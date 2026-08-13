"""Contract tests for runtime/app_api_v1_env.local injection (env_local.py).

Covers: CRLF files, comment/blank skipping, never overwriting an existing
variable, and a missing file being a no-op.
"""

from __future__ import annotations

import os

from src.app.runtime.env_local import load_local_env


def test_loads_crlf_key_value_file(tmp_path, monkeypatch):
    env_file = tmp_path / "env.local"
    env_file.write_bytes(
        b"ELYSIUM_APP_API_V1_SIGNING_SECRET=0123456789abcdef0123456789abcdef\r\n"
        b"ELYSIUM_INSTALLATION_ID=test-instance\r\n"
    )
    monkeypatch.delenv("ELYSIUM_APP_API_V1_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("ELYSIUM_INSTALLATION_ID", raising=False)

    loaded = load_local_env(env_file)

    assert set(loaded) == {
        "ELYSIUM_APP_API_V1_SIGNING_SECRET",
        "ELYSIUM_INSTALLATION_ID",
    }
    secret = os.environ["ELYSIUM_APP_API_V1_SIGNING_SECRET"]
    assert len(secret.encode("utf-8")) >= 32
    assert "\r" not in secret
    assert os.environ["ELYSIUM_INSTALLATION_ID"] == "test-instance"


def test_skips_comments_and_blank_lines(tmp_path, monkeypatch):
    env_file = tmp_path / "env.local"
    env_file.write_text(
        "# comment line\n\nELYSIUM_INSTALLATION_ID=test\n# another\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ELYSIUM_INSTALLATION_ID", raising=False)

    loaded = load_local_env(env_file)

    assert loaded == ("ELYSIUM_INSTALLATION_ID",)
    assert os.environ["ELYSIUM_INSTALLATION_ID"] == "test"


def test_never_overwrites_existing_environment(tmp_path, monkeypatch):
    env_file = tmp_path / "env.local"
    env_file.write_text("ELYSIUM_INSTALLATION_ID=from-file\n", encoding="utf-8")
    monkeypatch.setenv("ELYSIUM_INSTALLATION_ID", "from-shell")

    loaded = load_local_env(env_file)

    assert loaded == ()
    assert os.environ["ELYSIUM_INSTALLATION_ID"] == "from-shell"


def test_missing_file_is_a_noop(tmp_path):
    assert load_local_env(tmp_path / "does_not_exist.local") == ()
