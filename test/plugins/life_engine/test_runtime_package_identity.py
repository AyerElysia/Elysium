"""Regression tests for the package identity used by the plugin loader."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_world_adapter_uses_runtime_perception_conflict_identity() -> None:
    """The folder loader imports the plugin as ``life_engine``, not ``plugins``."""
    repository_root = Path(__file__).resolve().parents[3]
    environment = dict(os.environ)
    existing_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (
            str(repository_root / "plugins"),
            existing_pythonpath,
        )
        if part
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from life_engine.service.world_projection import "
                "PerceptionCursorConflict as service_conflict; "
                "from life_engine.storage.world_adapters import "
                "PerceptionCursorConflict as adapter_conflict; "
                "assert service_conflict is adapter_conflict"
            ),
        ],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
