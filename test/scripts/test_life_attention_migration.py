"""AttentionThread migration CLI uses exact snapshot evidence only."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.migrate_life_attention_threads import (
    _archive_source,
    _manifest_attention_source,
)


def _snapshot(tmp_path: Path) -> tuple[Path, Path, bytes, dict[str, object]]:
    snapshot = tmp_path / "snapshot"
    source = snapshot / "workspace/life_engine_workspace/thoughts/streams.json"
    source.parent.mkdir(parents=True)
    raw = (
        json.dumps(
            {
                "schema_version": 2,
                "global_revision": 1,
                "streams": [
                    {
                        "id": "thread:legacy",
                        "title": "保留原样",
                        "created_at": "2026-08-06T00:00:00+00:00",
                        "last_advanced_at": "2026-08-06T00:01:00+00:00",
                        "status": "dormant",
                        "revision": 1,
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode()
    source.write_bytes(raw)
    manifest: dict[str, object] = {
        "exact_files": [
            {
                "source_relative": "life_engine_workspace/thoughts/streams.json",
                "backup_relative": source.relative_to(snapshot).as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            }
        ]
    }
    return snapshot, source, raw, manifest


def test_manifest_source_and_reused_archive_remain_exact(tmp_path: Path) -> None:
    snapshot, source, raw, manifest = _snapshot(tmp_path)

    selected = _manifest_attention_source(snapshot, manifest)
    first = _archive_source(selected, tmp_path / "archive")
    replay = _archive_source(selected, tmp_path / "archive")

    assert selected == source.resolve()
    assert first.snapshot_sha256 == hashlib.sha256(raw).hexdigest()
    assert replay == first
    assert source.read_bytes() == raw


def test_manifest_source_rejects_changed_or_duplicate_evidence(tmp_path: Path) -> None:
    snapshot, source, _, manifest = _snapshot(tmp_path)
    source.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="differs"):
        _manifest_attention_source(snapshot, manifest)

    _, _, _, clean_manifest = _snapshot(tmp_path / "second")
    rows = clean_manifest["exact_files"]
    assert isinstance(rows, list)
    rows.append(dict(rows[0]))
    with pytest.raises(RuntimeError, match="exactly one"):
        _manifest_attention_source(tmp_path / "second/snapshot", clean_manifest)
