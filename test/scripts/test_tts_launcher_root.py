"""Lightweight launcher contracts: no model imports, ports, or live storage."""

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).parents[2] / "scripts/tts/start_gpt_sovits_hiely.sh"


def make_runtime(root: Path) -> tuple[Path, dict[str, str]]:
    root.mkdir()
    (root / "api_v2.py").touch()
    config_dir = root / "GPT_SoVITS/configs"
    config_dir.mkdir(parents=True)
    config = config_dir / "tts_infer_hiely.yaml"
    config.write_text(
        "custom:\n  t2s_weights_path: old-gpt\n  vits_weights_path: old-vits\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("GPT_SOVITS_")}
    for kind in ("GPT", "SOVITS"):
        path = root / f"{kind}.bin"
        content = kind.encode()
        path.write_bytes(content)
        env[f"GPT_SOVITS_{kind}_CHECKPOINT"] = str(path)
        env[f"GPT_SOVITS_{kind}_SHA256"] = hashlib.sha256(content).hexdigest()
    return config, env


@pytest.mark.parametrize("explicit_root", [False, True])
def test_launcher_resolves_runtime_and_preserves_approved_weights(tmp_path, explicit_root):
    root = tmp_path / "runtime"
    config, env = make_runtime(root)
    if explicit_root:
        env["GPT_SOVITS_ROOT"] = str(root)
    result = subprocess.run(
        ["bash", str(LAUNCHER), "--dry-config"],
        cwd=tmp_path if explicit_root else root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "not starting service" in result.stdout
    assert str(root / "latest/hiely-gpt.ckpt") in config.read_text()
    assert (root / "latest/hiely-gpt.ckpt").resolve() == root / "GPT.bin"
    assert (root / "latest/hiely-sovits.pth").resolve() == root / "SOVITS.bin"


def test_launcher_rejects_wrong_hash_without_changing_config(tmp_path):
    root = tmp_path / "runtime"
    config, env = make_runtime(root)
    previous = config.read_bytes()
    env["GPT_SOVITS_GPT_SHA256"] = "0" * 64
    result = subprocess.run(
        ["bash", str(LAUNCHER), "--dry-config"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert "SHA-256 mismatch" in result.stderr
    assert config.read_bytes() == previous
    assert not (root / "latest/hiely-gpt.ckpt").exists()
