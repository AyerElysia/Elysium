from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.streams.legacy_snapshot import (
    LegacySnapshotChangedError,
    LegacySnapshotDecodeError,
    LegacySnapshotFormatError,
    LegacySnapshotNotFoundError,
    LegacySnapshotSchemaError,
    read_legacy_streams_snapshot,
)


def _row(stream_id: str, **overrides):
    row = {
        "id": stream_id,
        "title": f"thread-{stream_id}",
        "created_at": "2026-08-06T00:00:00+00:00",
        "last_advanced_at": "2026-08-06T00:01:00+00:00",
        "advance_count": 1,
        "curiosity_score": 0.7,
        "last_thought": "subject text",
        "related_memories": [],
        "status": "active",
        "last_focused_at": "",
        "last_decay_at": "",
        "revision": 1,
    }
    row.update(overrides)
    return row


def _write_snapshot(path: Path, rows: list[dict], *, global_revision=10) -> bytes:
    raw = json.dumps(
        {
            "schema_version": 2,
            "global_revision": global_revision,
            "streams": rows,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    path.write_bytes(raw)
    return raw


def test_snapshot_preserves_raw_bytes_order_unknown_fields_and_hashes(tmp_path) -> None:
    path = tmp_path / "streams.json"
    rows = [
        _row("ts_a", extension={"items": [1, "two"]}),
        _row("ts_b", status="dormant", revision=2),
        _row("ts_c", status="completed", revision=3),
    ]
    raw = _write_snapshot(path, rows)

    snapshot = read_legacy_streams_snapshot(path)

    assert snapshot.source_path == path
    assert snapshot.raw_bytes == raw
    assert snapshot.byte_length == len(raw)
    assert snapshot.sha256 == hashlib.sha256(raw).hexdigest()
    assert snapshot.schema_version == 2
    assert snapshot.global_revision == 10
    assert [row.source_ordinal for row in snapshot.rows] == [0, 1, 2]
    assert [row.original_fields["id"] for row in snapshot.rows] == [
        "ts_a",
        "ts_b",
        "ts_c",
    ]
    assert snapshot.rows[0].original_fields["extension"] == {"items": [1, "two"]}
    canonical = json.dumps(
        rows[0], ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    assert snapshot.rows[0].row_sha256 == hashlib.sha256(canonical).hexdigest()
    assert dict(snapshot.status_counts) == {
        "active": 1,
        "completed": 1,
        "dormant": 1,
    }
    assert path.read_bytes() == raw


def test_snapshot_missing_and_non_utf8_fail_closed(tmp_path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(LegacySnapshotNotFoundError):
        read_legacy_streams_snapshot(missing)

    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"\xff\xfe")
    with pytest.raises(LegacySnapshotDecodeError) as raised:
        read_legacy_streams_snapshot(invalid)
    assert raised.value.__context__ is None


def test_snapshot_rejects_duplicate_json_keys(tmp_path) -> None:
    path = tmp_path / "streams.json"
    path.write_text(
        '{"schema_version":2,"schema_version":2,"streams":[]}',
        encoding="utf-8",
    )

    with pytest.raises(LegacySnapshotFormatError, match="duplicate key"):
        read_legacy_streams_snapshot(path)


def test_snapshot_rejects_non_finite_decoded_number(tmp_path) -> None:
    path = tmp_path / "streams.json"
    path.write_text(
        '{"schema_version":2,"streams":[],"extension":1e400}',
        encoding="utf-8",
    )

    with pytest.raises(LegacySnapshotFormatError, match="non-standard number"):
        read_legacy_streams_snapshot(path)


def test_snapshot_rejects_non_object_document(tmp_path) -> None:
    path = tmp_path / "streams.json"
    path.write_text("[1, 2]", encoding="utf-8")

    with pytest.raises(LegacySnapshotFormatError, match="top-level"):
        read_legacy_streams_snapshot(path)


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ({"schema_version": 1, "streams": []}, "not supported"),
        ({"schema_version": 2, "streams": {}}, "must be a list"),
        (
            {"schema_version": 2, "streams": [_row("ts_a", status="unknown")]},
            "status is not part",
        ),
        (
            {"schema_version": 2, "streams": [_row("ts_a", revision=True)]},
            "invalid legacy type",
        ),
        (
            {
                "schema_version": 2,
                "global_revision": 1,
                "streams": [_row("ts_a", revision=2)],
            },
            "exceeds global_revision",
        ),
        (
            {
                "schema_version": 2,
                "streams": [_row("ts_a"), _row("ts_a")],
            },
            "duplicated",
        ),
    ],
)
def test_snapshot_rejects_invalid_schema(tmp_path, document, reason) -> None:
    path = tmp_path / "streams.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(LegacySnapshotSchemaError, match=reason):
        read_legacy_streams_snapshot(path)


def test_schema_error_exposes_location_but_not_subject_text(tmp_path) -> None:
    path = tmp_path / "streams.json"
    _write_snapshot(
        path,
        [_row("ts_a", title="PRIVATE SUBJECT TEXT", advance_count="invalid")],
    )

    with pytest.raises(LegacySnapshotSchemaError) as raised:
        read_legacy_streams_snapshot(path)

    message = str(raised.value)
    assert str(path) in message
    assert "row=0" in message
    assert "field=advance_count" in message
    assert "PRIVATE SUBJECT TEXT" not in message


def test_snapshot_detects_source_change_during_read(tmp_path, monkeypatch) -> None:
    path = tmp_path / "streams.json"
    _write_snapshot(path, [_row("ts_a")])
    real_stat = path.stat()
    original_stat = Path.stat
    calls = 0

    def _changing_stat(self):
        nonlocal calls
        if self != path:
            return original_stat(self)
        calls += 1
        if calls == 1:
            return real_stat
        return SimpleNamespace(
            st_dev=real_stat.st_dev,
            st_ino=real_stat.st_ino,
            st_size=real_stat.st_size,
            st_mtime_ns=real_stat.st_mtime_ns + 1,
        )

    monkeypatch.setattr(Path, "stat", _changing_stat)

    with pytest.raises(LegacySnapshotChangedError):
        read_legacy_streams_snapshot(path)
