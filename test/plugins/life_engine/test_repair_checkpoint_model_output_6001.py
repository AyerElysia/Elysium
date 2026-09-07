"""Isolated SQLite tests for the fixed #6001 projection repair.

No model, network, service startup, production data, or writer claims are used.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import stat

import pytest

from scripts import repair_checkpoint_model_output_6001 as repair
from src.kernel.storage import canonical_json, canonical_json_sha256


RUNTIME_SCHEMA = """
CREATE TABLE runtime_states (
 namespace TEXT NOT NULL, state_key TEXT NOT NULL, revision INTEGER NOT NULL,
 schema_version INTEGER NOT NULL, payload_json TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(namespace,state_key)
);
CREATE TABLE raw_life_events (
 ingest_position INTEGER PRIMARY KEY AUTOINCREMENT,
 occurrence_id TEXT NOT NULL UNIQUE, source_event_id TEXT NOT NULL,
 source_sequence INTEGER NOT NULL DEFAULT 0, occurred_at TEXT NOT NULL,
 recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL
);
"""


def _envelope(value):
    return (
        "<subject_self_continuity_checkpoint>\n"
        + canonical_json(value)
        + "\n</subject_self_continuity_checkpoint>"
    )


def _text(value):
    return {"type": "text", "text": value}


@pytest.fixture
def lab(tmp_path, monkeypatch):
    database = tmp_path / "local.sqlite3"
    monkeypatch.setattr(repair, "DATABASE_PATH", database)
    monkeypatch.setattr(repair, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(repair, "assert_service_stopped", lambda *_args: None)
    connection = sqlite3.connect(database)
    connection.executescript(RUNTIME_SCHEMA)
    payloads = [
        {"role": "user", "content": [_text(f"synthetic context {index}")]}
        for index in range(35)
    ]
    archive_refs = []
    for index, revision in ((1, 2), (4, 5), (30, 23)):
        record = {
            "schema": "elysium.context_group.v1",
            "payloads": [{"role": "user", "content": [_text(f"synthetic archive {revision}")]}],
        }
        digest = canonical_json_sha256(record)
        ref = "ctxg_" + digest
        archive_refs.append(ref)
        archive = {
            "schema": "elysium.context_group_archive.v1", "group_ref": ref,
            "group_sha256": digest, "utf8_bytes": len(canonical_json(record).encode()),
            "record": record,
        }
        serialized = canonical_json(archive)
        connection.execute(
            "INSERT INTO runtime_states VALUES(?,?,?,?,?,?,?)",
            (repair.HEARTBEAT_ARCHIVE_NAMESPACE, ref, 1, 1, serialized,
             repair._sha(serialized), "2026-09-06T00:00:00+00:00"),
        )
        continuity = f"synthetic subject continuity {revision}"
        checkpoint = {
            "schema": "elysium.subject_self_continuity_checkpoint.v1",
            "revision": revision, "checkpoint_id": repair.TRUE_CHECKPOINT_IDS[revision],
            "actor_consciousness_instance_id": "chat_global",
            "released_group_refs": [ref],
            "exact_archive": {
                "namespace": repair.HEARTBEAT_ARCHIVE_NAMESPACE, "state_keys": [ref],
            },
            "continuity_text": continuity,
            "continuity_text_sha256": repair._sha(continuity),
        }
        payloads[index] = {"role": "assistant", "content": [_text(_envelope(checkpoint))]}

    bad = {
        "schema": "elysium.subject_self_continuity_checkpoint.v1",
        "revision": 29, "checkpoint_id": "synthetic-model-copy",
        "actor_consciousness_instance_id": "chat_global",
        "released_group_refs": ["ctxg_" + "0" * 64],
        "exact_archive": {
            "namespace": repair.HEARTBEAT_ARCHIVE_NAMESPACE,
            "state_keys": ["ctxg_" + "0" * 64],
        },
        "continuity_text": "synthetic uncommitted model text",
        "continuity_text_sha256": "1" * 64,
    }
    model_text = _envelope(bad)
    payloads[34] = {
        "role": "assistant", "content": [
            {"type": "reasoning_text", "text": "synthetic reasoning",
             "signature": "", "redacted_data": ""},
            _text(model_text),
        ],
    }
    snapshot = {
        "version": 1, "runtime_key": repair.NAMESPACE, "payloads": payloads,
        "payload_digest": canonical_json_sha256(payloads),
    }
    serialized = canonical_json(snapshot)
    monkeypatch.setattr(repair, "MODEL_TEXT_SHA", repair._sha(model_text))
    monkeypatch.setattr(repair, "OLD_OUTER_SHA", repair._sha(serialized))
    monkeypatch.setattr(repair, "OLD_INNER_SHA", snapshot["payload_digest"])
    connection.execute(
        "INSERT INTO runtime_states VALUES(?,?,?,?,?,?,?)",
        (repair.NAMESPACE, repair.STATE_KEY, 1568, 1, serialized,
         repair.OLD_OUTER_SHA, "2026-09-06T13:43:22.969000+00:00"),
    )
    event = {
        "event_type": "conscious_activity_model_turn",
        "metadata": {"heartbeat_run_id": repair.RAW_RUN},
        "content": canonical_json({
            "phase": "generated", "tool_call_ids": [],
            "assistant_message": model_text,
        }),
    }
    raw = canonical_json(event)
    connection.execute(
        "INSERT INTO raw_life_events VALUES(?,?,?,?,?,?,?,?)",
        (repair.RAW_POSITION, repair.RAW_OCCURRENCE, repair.RAW_OCCURRENCE,
         0, "synthetic occurred", "synthetic recorded", raw, repair._sha(raw)),
    )
    connection.execute(
        "INSERT INTO runtime_states VALUES(?,?,?,?,?,?,?)",
        ("unrelated", "keep", 1, 1, "{}", repair._sha("{}"), "synthetic time"),
    )
    connection.commit()
    connection.close()
    return database, snapshot, archive_refs


def _rows(database):
    with sqlite3.connect(database) as connection:
        return (
            connection.execute("SELECT * FROM runtime_states ORDER BY namespace,state_key").fetchall(),
            connection.execute("SELECT * FROM raw_life_events").fetchall(),
        )


def test_dry_run_is_content_free_and_does_not_write(lab):
    database, _, _ = lab
    before = _rows(database)
    result = repair.repair()
    assert result["status"] == "dry_run"
    assert result["verified_archive_count"] == 3
    assert result["backup_path"] is None
    assert _rows(database) == before
    assert not (database.parent / "data").exists()
    assert "synthetic uncommitted model text" not in json.dumps(result)


def test_apply_changes_only_target_and_synced_private_backup_is_exact(lab, monkeypatch):
    database, original, _ = lab
    before = _rows(database)
    fsync_calls = []
    real_fsync = repair.os.fsync

    def synced(descriptor):
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(repair.os, "fsync", synced)
    result = repair.repair(apply=True)
    assert result["status"] == "repaired"
    assert len(fsync_calls) >= 5
    after = _rows(database)
    assert after[1] == before[1]
    changed = [(left, right) for left, right in zip(before[0], after[0]) if left != right]
    assert len(changed) == 1
    old, new = changed[0]
    assert old[0:2] == (repair.NAMESPACE, repair.STATE_KEY)
    assert new[2] == 1569
    assert repair._sha(new[4]) == new[5]
    restored = json.loads(new[4])
    restored["payloads"][34]["content"][1]["text"] = original["payloads"][34]["content"][1]["text"]
    restored["payload_digest"] = original["payload_digest"]
    assert restored == original
    backup = Path(result["backup_path"])
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    saved = json.loads(backup.read_text(encoding="utf-8"))
    assert tuple(saved["row"][key] for key in repair.ROW_COLUMNS) == old
    assert repair.repair(apply=True)["status"] == "already_repaired"
    assert _rows(database) == after


@pytest.mark.parametrize("mutation", ["revision", "outer_digest", "inner_digest"])
def test_changed_target_is_refused(lab, mutation):
    database, original, _ = lab
    with sqlite3.connect(database) as connection:
        if mutation == "revision":
            connection.execute("UPDATE runtime_states SET revision=2000 WHERE namespace=?", (repair.NAMESPACE,))
        elif mutation == "outer_digest":
            connection.execute("UPDATE runtime_states SET payload_sha256=? WHERE namespace=?",
                               ("bad", repair.NAMESPACE))
        else:
            corrupted = copy.deepcopy(original)
            corrupted["payload_digest"] = "bad"
            serialized = canonical_json(corrupted)
            connection.execute(
                "UPDATE runtime_states SET payload_json=?,payload_sha256=? WHERE namespace=?",
                (serialized, repair._sha(serialized), repair.NAMESPACE),
            )
    before = _rows(database)
    with pytest.raises(Exception):
        repair.repair(apply=True)
    assert _rows(database) == before
    assert not (database.parent / "data").exists()


@pytest.mark.parametrize("mutation", ["message", "tools"])
def test_raw_evidence_must_match_without_tools(lab, mutation):
    database, _, _ = lab
    with sqlite3.connect(database) as connection:
        raw = json.loads(connection.execute("SELECT payload_json FROM raw_life_events").fetchone()[0])
        activity = json.loads(raw["content"])
        if mutation == "message":
            activity["assistant_message"] += " changed"
        else:
            activity["tool_call_ids"] = ["synthetic-real-tool"]
        raw["content"] = canonical_json(activity)
        serialized = canonical_json(raw)
        connection.execute("UPDATE raw_life_events SET payload_json=?,payload_hash=?",
                           (serialized, repair._sha(serialized)))
    before = _rows(database)
    with pytest.raises(repair.RepairRefused):
        repair.repair(apply=True)
    assert _rows(database) == before


@pytest.mark.parametrize("mutation", ["missing", "digest", "metadata"])
def test_all_genuine_archives_are_strictly_verified(lab, mutation):
    database, _, refs = lab
    with sqlite3.connect(database) as connection:
        if mutation == "missing":
            connection.execute("DELETE FROM runtime_states WHERE state_key=?", (refs[0],))
        elif mutation == "digest":
            connection.execute("UPDATE runtime_states SET payload_sha256=? WHERE state_key=?",
                               ("bad", refs[0]))
        else:
            archive = json.loads(connection.execute(
                "SELECT payload_json FROM runtime_states WHERE state_key=?", (refs[0],),
            ).fetchone()[0])
            archive["utf8_bytes"] += 1
            serialized = canonical_json(archive)
            connection.execute(
                "UPDATE runtime_states SET payload_json=?,payload_sha256=? WHERE state_key=?",
                (serialized, repair._sha(serialized), refs[0]),
            )
    before = _rows(database)
    with pytest.raises(Exception):
        repair.repair(apply=True)
    assert _rows(database) == before


def test_running_service_and_failed_backup_prevent_mutation(lab, monkeypatch):
    database, _, _ = lab
    before = _rows(database)

    def refuse(*_args):
        raise repair.RepairRefused("RepositoryMainStillRunning:synthetic")

    monkeypatch.setattr(repair, "assert_service_stopped", refuse)
    with pytest.raises(repair.RepairRefused, match="StillRunning"):
        repair.repair(apply=True)
    assert _rows(database) == before
    monkeypatch.setattr(repair, "assert_service_stopped", lambda *_args: None)

    def failed_backup(_plan):
        raise OSError("synthetic backup failure")

    monkeypatch.setattr(repair, "_write_backup", failed_backup)
    with pytest.raises(OSError, match="backup"):
        repair.repair(apply=True)
    assert _rows(database) == before


def test_exact_cas_rejects_even_timestamp_only_change(lab):
    database, _, _ = lab
    connection = repair._open_database(writable=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        plan = repair.prepare_plan(connection)
        connection.execute("UPDATE runtime_states SET updated_at=? WHERE namespace=?",
                           ("concurrent writer", repair.NAMESPACE))
        with pytest.raises(repair.RepairRefused, match="CAS"):
            repair._apply_cas(connection, plan)
        connection.rollback()
    finally:
        connection.close()


def test_process_guard_matches_exact_checkout_main(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    main = repository / "main.py"
    main.write_text("", encoding="utf-8")
    proc = tmp_path / "proc"
    entry = proc / "123"
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"python\0main.py\0")
    (entry / "cwd").symlink_to(repository, target_is_directory=True)
    with pytest.raises(repair.RepairRefused, match="pid=123"):
        repair.assert_service_stopped(repository, proc_root=proc)
    (entry / "cmdline").write_bytes(b"python\0another.py\0")
    repair.assert_service_stopped(repository, proc_root=proc)


@pytest.mark.parametrize("mutation", ["symlink", "public_mode"])
def test_backup_ancestors_and_private_leaf_are_checked(lab, mutation):
    database, _, _ = lab
    before = _rows(database)
    data = database.parent / "data"
    data.mkdir()
    if mutation == "symlink":
        elsewhere = database.parent / "elsewhere"
        elsewhere.mkdir()
        (data / "repair_backups").symlink_to(elsewhere, target_is_directory=True)
    else:
        root = data / "repair_backups/heartbeat_checkpoint_6001"
        root.mkdir(parents=True)
        root.chmod(0o755)
    with pytest.raises(repair.RepairRefused):
        repair.repair(apply=True)
    assert _rows(database) == before
