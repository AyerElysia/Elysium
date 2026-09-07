"""Offline synthetic SQLite contracts for the fixed #6062 redelivery utility.

No production database, subject writer, model, service, network, or tool execution.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import stat

import pytest

from scripts import replay_subconscious_history_6062 as replay
from src.kernel.storage import canonical_json

SCHEMA = """
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
CREATE TABLE raw_event_consumer_offsets (
 consumer_id TEXT PRIMARY KEY, ingest_position INTEGER NOT NULL,
 revision INTEGER NOT NULL, updated_at TEXT NOT NULL, metadata_json TEXT NOT NULL
);
CREATE TABLE subject_documents (path TEXT PRIMARY KEY, body TEXT NOT NULL);
"""
SECRET = "synthetic-private-content-DO-NOT-PRINT-\u7231\u8389"


def _existing(identity, sequence):
    return {
        "event_id": identity, "occurrence_id": "occ-" + identity,
        "event_type": "message", "sequence": sequence,
        "timestamp": "2026-09-06T00:00:00+00:00", "source": "synthetic",
        "content": SECRET + identity, "raw_content": SECRET + identity,
        "heartbeat_context_consumed": True, "unknown_future_field": {"preserve": [1, 2]},
    }


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "REPO_ROOT", tmp_path)
    database = tmp_path / "data/life_storage/local.sqlite3"
    database.parent.mkdir(parents=True)
    monkeypatch.setattr(replay, "DATABASE_PATH", database)
    monkeypatch.setattr(replay, "assert_service_stopped", lambda *_args: None)
    (tmp_path / "config/plugins/life_engine").mkdir(parents=True)
    (tmp_path / "config/elysium.toml").write_text(
        '[storage]\nbackend="local"\nlocal_selectable_enabled=true\n'
        'multi_writer_enabled=false\nschema_version=3\n'
        'backend_generation="local-selectable-20260824-v3"\n', encoding="utf-8",
    )
    (tmp_path / "config/plugins/life_engine/config.toml").write_text(
        '[storage_local]\ndatabase_path="data/life_storage/local.sqlite3"\n'
        'authority_state_path="data/life_storage/authority.json"\n', encoding="utf-8",
    )
    (tmp_path / "data/runtime").mkdir()
    (tmp_path / "data/runtime/elysium.lock").write_text("old diagnostic PID", encoding="utf-8")
    connection = sqlite3.connect(database)
    connection.executescript(SCHEMA)
    all_manifest, work_manifest = [], []
    for index, position in enumerate(range(replay.RAW_LOWER, replay.RAW_UPPER + 1)):
        skipped = index in (3, 33)
        source_sequence = index % 7  # Deliberately reset and collide, including zero.
        event_id = f"source-event-{index}"
        timestamp = "2026-09-06T09:00:00+08:00"
        metadata = {} if skipped else {
            "legacy_event_type": "message", "legacy_event_id": event_id,
            "legacy_source": "original-source", "source_detail": "original-detail",
            "heartbeat_context_consumed": True, "content_type": "text",
            "parent_event_id": "unresolved-original-source",
        }
        if index in (1, 2):
            metadata.update(
                legacy_event_type="tool_call" if index == 1 else "tool_result",
                call_id="recorded-call-only", heartbeat_run_id="recorded-run",
                tool_name="must-never-execute", tool_args={"value": SECRET},
                tool_success=index == 2,
            )
        value = {
            "event_id": event_id, "occurrence_id": f"occ-source-{index}",
            "sequence": source_sequence, "source_sequence": source_sequence,
            "timestamp": timestamp, "recorded_at": "", "source": "original-source",
            "channel": "life", "event_type": "chat.message.send_requested" if skipped else "legacy",
            "content": SECRET + str(index), "stream_id": "original-stream",
            "source_instance_id": "original-instance", "causation_id": "original-cause",
            "correlation_id": "original-correlation", "content_ref": "original-ref",
            "metadata": metadata,
        }
        text = canonical_json(value)
        digest = replay._sha(text)
        connection.execute(
            "INSERT INTO raw_life_events VALUES(?,?,?,?,?,?,?,?)",
            (position, value["occurrence_id"], event_id, source_sequence,
             "2026-09-06T01:00:00+00:00", "2026-09-06T02:00:00+00:00", text, digest),
        )
        entry = {"ingest_position": position, "occurrence_id": value["occurrence_id"],
                 "payload_hash": digest}
        all_manifest.append(entry)
        if not skipped:
            work_manifest.append(entry)
    monkeypatch.setattr(replay, "RAW_MANIFEST_SHA256", replay._manifest(all_manifest))
    monkeypatch.setattr(replay, "WORKSET_MANIFEST_SHA256", replay._manifest(work_manifest))
    payload = {
        "version": 2, "event_history": [_existing("history", 100)],
        "pending_events": [_existing("pending", 150)],
        "state": {
            "event_sequence": 140, "heartbeat_context_cursor": 200,
            "heartbeat_count": 6062, "subconscious_summary": {"text": SECRET},
            "chatter_context_cursors": {"original-stream": 91},
            "unknown_state": {"keep": True},
        },
        "unknown_top_level": {"keep": SECRET},
    }
    for index, (namespace, key) in enumerate(replay.PRESERVED_TARGETS):
        value = payload if index == 0 else {"version": 1, "private_rolling": SECRET + str(index)}
        text = canonical_json(value)
        connection.execute(
            "INSERT INTO runtime_states VALUES(?,?,?,?,?,?,?)",
            (namespace, key, 30000 + index, 2 if index == 0 else 1,
             text, replay._sha(text), "2026-09-07T00:00:00+00:00"),
        )
    for consumer in ("life_engine_subconscious_ingest:v1", "memory_experience_ingest:v1"):
        connection.execute(
            "INSERT INTO raw_event_consumer_offsets VALUES(?,?,?,?,?)",
            (consumer, replay.RAW_UPPER + 77, 19, "original-time", '{"original":true}'),
        )
    connection.execute("INSERT INTO subject_documents VALUES(?,?)", ("MEMORY.md", SECRET))
    connection.commit()
    connection.close()
    return database


def _rows(database, table):
    connection = sqlite3.connect(database)
    result = connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
    connection.close()
    return result


def _global(database):
    connection = sqlite3.connect(database)
    row = connection.execute(
        "SELECT revision,payload_json,payload_sha256 FROM runtime_states "
        "WHERE namespace=? AND state_key=?", (replay.GLOBAL_NAMESPACE, replay.GLOBAL_KEY),
    ).fetchone()
    connection.close()
    return row[0], json.loads(row[1]), row[2]


def _change_global(database, mutate):
    connection = sqlite3.connect(database)
    revision, payload, _ = _global(database)
    mutate(payload)
    text = canonical_json(payload)
    connection.execute(
        "UPDATE runtime_states SET payload_json=?,payload_sha256=? "
        "WHERE namespace=? AND state_key=?",
        (text, replay._sha(text), replay.GLOBAL_NAMESPACE, replay.GLOBAL_KEY),
    )
    connection.commit()
    connection.close()
    return revision, replay._sha(text)


def _apply(database):
    revision, _, digest = _global(database)
    return replay.replay(apply=True, expected_revision=revision, expected_sha256=digest)


def _snapshot(database):
    return {table: _rows(database, table) for table in (
        "runtime_states", "raw_life_events", "raw_event_consumer_offsets", "subject_documents",
    )}


def _plan():
    connection = replay._open_database(writable=False)
    try:
        connection.execute("BEGIN")
        return replay.prepare_plan(connection)
    finally:
        connection.close()


def test_dry_run_is_read_only_and_does_not_create_backups(lab):
    before = _snapshot(lab)
    result = replay.replay()
    assert result["status"] == "dry_run"
    assert result["added_count"] == 112
    assert result["new_event_sequence"] == 312
    assert result["exact_old_runtime_restoration"] is False
    assert _snapshot(lab) == before
    assert not (lab.parents[1] / "repair_backups").exists()


def test_apply_preserves_every_original_item_state_and_raw_row(lab):
    before = _snapshot(lab)
    revision, payload, _ = _global(lab)
    result = _apply(lab)
    after_revision, after, _ = _global(lab)
    assert result["status"] == "applied"
    assert after_revision == revision + 1
    assert after["event_history"] == payload["event_history"]
    assert after["pending_events"][:1] == payload["pending_events"]
    assert after["state"]["event_sequence"] == 312
    assert after["state"]["pending_event_count"] == 113
    new = after["pending_events"][1:]
    assert [item["sequence"] for item in new] == list(range(201, 313))
    assert all(item["heartbeat_context_consumed"] is False for item in new)
    assert all(item["redelivery_operation_id"] == replay.OPERATION_ID for item in new)
    assert new[0]["timestamp"] == "2026-09-06T09:00:00+08:00"
    assert new[0]["source"] == "original-source"
    assert new[0]["source_detail"] == "original-detail"
    assert new[0]["content"] == new[0]["raw_content"] == SECRET + "0"
    assert new[0]["parent_event_id"] == "unresolved-original-source"
    restored = copy.deepcopy(after)
    restored["pending_events"] = payload["pending_events"]
    restored["state"]["event_sequence"] = payload["state"]["event_sequence"]
    restored["state"].pop("pending_event_count")
    assert restored == payload
    for table in ("raw_life_events", "raw_event_consumer_offsets", "subject_documents"):
        assert _rows(lab, table) == before[table]
    old_private = [row for row in before["runtime_states"] if row[0] != replay.GLOBAL_NAMESPACE]
    current = _rows(lab, "runtime_states")
    assert all(row in current for row in old_private)
    assert len(current) == len(before["runtime_states"]) + 1


def test_backup_is_private_complete_and_readable_before_commit(lab):
    before = _snapshot(lab)
    result = _apply(lab)
    path = Path(result["backup_path"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    backup = json.loads(path.read_text())
    assert len(backup["runtime_rows"]) == 3
    assert len(backup["raw_source_rows"]) == 114
    assert len(backup["raw_consumer_rows"]) == 2
    old_global = next(row for row in before["runtime_states"] if row[0] == replay.GLOBAL_NAMESPACE)
    assert backup["runtime_rows"][0]["payload_json"] == old_global[4]
    assert backup["raw_source_rows"][0]["source_sequence"] == 0
    assert backup["raw_source_rows"][0]["recorded_at"] == "2026-09-06T02:00:00+00:00"
    assert SECRET in backup["runtime_rows"][0]["payload_json"]
    journal_row = next(row for row in _rows(lab, "runtime_states") if row[0] == replay.JOURNAL_NAMESPACE)
    journal = json.loads(journal_row[4])
    assert journal["backup_sha256"] == replay._sha(path.read_text())
    assert "unresolved-original-source" in journal["outside_candidate_references"]
    assert journal["reference_check_scope"] == "candidate_manifest_only"
    assert journal["reference_status"] == "preserved_as_recorded_not_repaired"


def test_journal_prevents_redelivery_after_later_compaction(lab):
    _apply(lab)
    _change_global(lab, lambda value: value.update(event_history=[], pending_events=[]))
    before = _snapshot(lab)
    result = replay.replay()
    assert result["status"] == "already_applied"
    assert result["added_count"] == 112
    assert _snapshot(lab) == before


def test_apply_already_applied_does_not_require_old_global_revision(lab):
    _apply(lab)
    _change_global(lab, lambda value: value["state"].update(heartbeat_count=7000))
    before = _snapshot(lab)
    result = replay.replay(apply=True, expected_revision=1, expected_sha256="0" * 64)
    assert result["status"] == "already_applied"
    assert _snapshot(lab) == before


def test_existing_source_is_verified_but_keeps_its_flags_presentation_and_sequence(lab):
    plan = _plan()
    item = copy.deepcopy(plan["sources"]["templates"]["occ-source-0"])
    item.update(sequence=500, heartbeat_context_consumed=True, content="bounded old projection")
    _change_global(lab, lambda value: value["event_history"].append(item))
    _, before, _ = _global(lab)
    result = _apply(lab)
    _, after, _ = _global(lab)
    assert result["added_count"] == 111
    assert result["already_present_count"] == 1
    assert after["event_history"] == before["event_history"]
    assert after["pending_events"][1]["sequence"] == 501
    assert after["event_history"][-1]["heartbeat_context_consumed"] is True


@pytest.mark.parametrize("field,value", [
    ("raw_content", "changed original"),
    ("source", "different source"),
    ("timestamp", "different time"),
    ("parent_event_id", "different parent"),
    ("source_instance_id", "different instance"),
    ("tool_args", {"different": True}),
])
def test_existing_same_occurrence_source_conflict_is_refused(lab, field, value):
    item = copy.deepcopy(_plan()["sources"]["templates"]["occ-source-0"])
    item["sequence"] = 500
    item[field] = value
    _change_global(lab, lambda payload: payload["event_history"].append(item))
    before = _snapshot(lab)
    with pytest.raises(replay.ReplayRefused, match="ExistingOccurrence"):
        _apply(lab)
    assert _snapshot(lab) == before


def test_event_id_collision_with_different_occurrence_is_refused(lab):
    _change_global(lab, lambda value: value["event_history"][0].update(event_id="source-event-0"))
    before = _snapshot(lab)
    with pytest.raises(replay.ReplayRefused, match="RuntimeSourceEventIdConflict"):
        _apply(lab)
    assert _snapshot(lab) == before


def test_duplicate_current_identity_is_refused_without_cleaning_current_data(lab):
    _change_global(lab, lambda value: value["pending_events"].append(copy.deepcopy(value["event_history"][0])))
    before = _snapshot(lab)
    with pytest.raises(replay.ReplayRefused, match="RuntimeEventIdentityConflict"):
        _apply(lab)
    assert _snapshot(lab) == before


@pytest.mark.parametrize("revision,digest", [(24175, "0" * 64), (None, None), (30000, "0" * 64)])
def test_apply_requires_exact_current_revision_and_digest(lab, revision, digest):
    before = _snapshot(lab)
    with pytest.raises(replay.ReplayRefused):
        replay.replay(apply=True, expected_revision=revision, expected_sha256=digest)
    assert _snapshot(lab) == before


@pytest.mark.parametrize("mutation,expected", [
    ("UPDATE raw_life_events SET payload_hash='bad' WHERE ingest_position=262863", "SourcePayloadDigestMismatch"),
    ("DELETE FROM raw_life_events WHERE ingest_position=262864", "SourceRangeCountChanged"),
    ("UPDATE raw_life_events SET source_sequence=999 WHERE ingest_position=262863", "SourceColumnIdentityMismatch"),
    ("UPDATE raw_life_events SET occurred_at='2026-09-06T02:00:00+00:00' WHERE ingest_position=262863", "SourceColumnIdentityMismatch"),
    ("UPDATE raw_life_events SET recorded_at='changed' WHERE ingest_position=262863", None),
])
def test_source_preconditions(lab, mutation, expected):
    connection = sqlite3.connect(lab)
    connection.execute(mutation)
    connection.commit()
    connection.close()
    if expected is None:
        # recorded_at is not part of canonical payload hash, but is copied into the private backup.
        result = replay.replay()
        assert result["added_count"] == 112
    else:
        with pytest.raises(replay.ReplayRefused, match=expected):
            replay.replay()


def test_fixed_manifest_is_checked_even_with_rehashed_changed_source(lab):
    connection = sqlite3.connect(lab)
    text = connection.execute("SELECT payload_json FROM raw_life_events WHERE ingest_position=262863").fetchone()[0]
    value = json.loads(text)
    value["content"] = "different immutable original"
    changed = canonical_json(value)
    connection.execute("UPDATE raw_life_events SET payload_json=?,payload_hash=? WHERE ingest_position=262863",
                       (changed, replay._sha(changed)))
    connection.commit()
    connection.close()
    with pytest.raises(replay.ReplayRefused, match="FixedRawManifestMismatch"):
        replay.replay()


def test_existing_pending_count_is_updated_mechanically_only(lab):
    _change_global(lab, lambda value: value["state"].update(pending_event_count=999))
    _, before, _ = _global(lab)
    _apply(lab)
    _, after, _ = _global(lab)
    assert after["state"]["pending_event_count"] == len(after["pending_events"]) == 113
    after["state"]["pending_event_count"] = 999
    after["state"]["event_sequence"] = before["state"]["event_sequence"]
    after["pending_events"] = before["pending_events"]
    assert after == before


def test_schema_change_is_refused(lab):
    connection = sqlite3.connect(lab)
    connection.execute("ALTER TABLE runtime_states ADD COLUMN unexpected TEXT")
    connection.commit()
    connection.close()
    with pytest.raises(replay.ReplayRefused, match="RuntimeTableSchemaChanged"):
        replay.replay()


def test_nonlocal_backend_is_refused(lab):
    path = replay.REPO_ROOT / "config/elysium.toml"
    path.write_text(path.read_text().replace('backend="local"', 'backend="mysql"'))
    with pytest.raises(replay.ReplayRefused, match="SelectedStorageConfigurationChanged"):
        replay.replay()


def test_live_process_check_refuses_before_database_mutation(lab, monkeypatch):
    before = _snapshot(lab)
    def running(*_args):
        raise replay.ReplayRefused("RepositoryMainStillRunning")
    monkeypatch.setattr(replay, "assert_service_stopped", running)
    with pytest.raises(replay.ReplayRefused, match="RepositoryMainStillRunning"):
        _apply(lab)
    assert _snapshot(lab) == before
    assert replay.replay()["status"] == "dry_run"


@pytest.mark.parametrize("phase", [3, 4, 5])
def test_stopped_guard_is_rechecked_before_commit(lab, monkeypatch, phase):
    before = _snapshot(lab)
    count = 0
    def guard(*_args):
        nonlocal count
        count += 1
        if count == phase:
            raise replay.ReplayRefused("RepositoryMainStillRunning")
    monkeypatch.setattr(replay, "assert_service_stopped", guard)
    with pytest.raises(replay.ReplayRefused, match="RepositoryMainStillRunning"):
        _apply(lab)
    assert _snapshot(lab) == before


def test_existing_instance_lock_is_not_rewritten_and_busy_lock_refuses(lab):
    import fcntl
    path = replay.REPO_ROOT / "data/runtime/elysium.lock"
    original = path.read_bytes()
    with path.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(replay.ReplayRefused, match="InstanceLockBusy"):
            _apply(lab)
    assert path.read_bytes() == original
    _apply(lab)
    assert path.read_bytes() == original


def test_instance_lock_replacement_before_write_is_refused(lab, monkeypatch):
    before = _snapshot(lab)
    original = replay._write_backup
    def replace_lock(plan):
        result = original(plan)
        path = replay.REPO_ROOT / "data/runtime/elysium.lock"
        path.unlink()
        path.write_text("different inode")
        return result
    monkeypatch.setattr(replay, "_write_backup", replace_lock)
    with pytest.raises(replay.ReplayRefused, match="InstanceLockReplaced"):
        _apply(lab)
    assert _snapshot(lab) == before


def test_backup_failure_rolls_back_both_rows(lab, monkeypatch):
    before = _snapshot(lab)
    def fail(*_args):
        raise OSError("synthetic-private-backup-failure")
    monkeypatch.setattr(replay, "_write_backup", fail)
    with pytest.raises(OSError):
        _apply(lab)
    assert _snapshot(lab) == before


def test_backup_fsync_failure_rolls_back_both_rows(lab, monkeypatch):
    before = _snapshot(lab)
    def fail(_descriptor):
        raise OSError("synthetic fsync failure")
    monkeypatch.setattr(replay.os, "fsync", fail)
    with pytest.raises(OSError):
        _apply(lab)
    assert _snapshot(lab) == before


def test_nonprivate_backup_directory_is_refused(lab):
    root = replay.REPO_ROOT / "data/repair_backups/heartbeat_history_6062"
    root.mkdir(parents=True)
    root.chmod(0o755)
    before = _snapshot(lab)
    with pytest.raises(replay.ReplayRefused, match="BackupDirectoryNotPrivate"):
        _apply(lab)
    assert _snapshot(lab) == before


def test_symlink_backup_ancestor_is_refused(lab, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (replay.REPO_ROOT / "data/repair_backups").symlink_to(outside, target_is_directory=True)
    before = _snapshot(lab)
    with pytest.raises(replay.ReplayRefused, match="BackupAncestorInvalid"):
        _apply(lab)
    assert _snapshot(lab) == before
    assert list(outside.iterdir()) == []


def test_cas_failure_rolls_back(lab, monkeypatch):
    before = _snapshot(lab)
    original = replay._apply_cas
    def changed(connection, plan, journal):
        plan = copy.deepcopy(plan)
        plan["row"]["revision"] += 1
        original(connection, plan, journal)
    monkeypatch.setattr(replay, "_apply_cas", changed)
    with pytest.raises(replay.ReplayRefused, match="ExactGlobalCASFailed"):
        _apply(lab)
    assert _snapshot(lab) == before


def test_journal_insert_failure_rolls_back_global_update(lab):
    connection = sqlite3.connect(lab)
    connection.executescript(
        "CREATE TRIGGER fail_journal BEFORE INSERT ON runtime_states "
        "WHEN NEW.namespace='life_engine.recovery' "
        "BEGIN SELECT RAISE(ABORT,'synthetic journal failure'); END;"
    )
    connection.close()
    before = _snapshot(lab)
    with pytest.raises(sqlite3.IntegrityError):
        _apply(lab)
    assert _snapshot(lab) == before


def test_unexpected_trigger_write_is_rolled_back(lab):
    connection = sqlite3.connect(lab)
    connection.executescript(
        "CREATE TRIGGER side_write AFTER UPDATE ON runtime_states "
        "WHEN NEW.namespace='life_engine.runtime_context' "
        "BEGIN UPDATE subject_documents SET body='forbidden write'; END;"
    )
    connection.close()
    before = _snapshot(lab)
    with pytest.raises(replay.ReplayRefused, match="UnexpectedRowsChanged"):
        _apply(lab)
    assert _snapshot(lab) == before


@pytest.mark.parametrize("after_commit", [False, True])
def test_commit_error_reports_unknown_without_automatic_retry(lab, monkeypatch, after_commit):
    before = _snapshot(lab)
    def uncertain(connection):
        if after_commit:
            connection.commit()
        raise OSError("synthetic commit outcome unavailable")
    monkeypatch.setattr(replay, "_commit", uncertain)
    with pytest.raises(replay.ReplayCommitUnknown, match="InspectFixedRecoveryJournal"):
        _apply(lab)
    if after_commit:
        assert replay.replay()["status"] == "already_applied"
    else:
        assert _snapshot(lab) == before


@pytest.mark.parametrize("mutation", ["scope", "added_digest", "coverage", "row_digest"])
def test_conflicting_journal_is_not_accepted_as_idempotence(lab, mutation):
    _apply(lab)
    connection = sqlite3.connect(lab)
    text = connection.execute(
        "SELECT payload_json FROM runtime_states WHERE namespace=?", (replay.JOURNAL_NAMESPACE,),
    ).fetchone()[0]
    value = json.loads(text)
    if mutation == "scope":
        value["source_lower"] -= 1
    elif mutation == "added_digest":
        value["added"][0]["runtime_event_sha256"] = "0" * 64
    elif mutation == "coverage":
        value["added"].pop()
    changed = canonical_json(value)
    connection.execute(
        "UPDATE runtime_states SET payload_json=?,payload_sha256=? WHERE namespace=?",
        (changed, "0" * 64 if mutation == "row_digest" else replay._sha(changed), replay.JOURNAL_NAMESPACE),
    )
    connection.commit()
    connection.close()
    before = _snapshot(lab)
    with pytest.raises(replay.ReplayRefused):
        replay.replay()
    assert _snapshot(lab) == before


def test_cli_errors_do_not_echo_private_exception_text(lab, monkeypatch, capsys):
    def private_failure(**_kwargs):
        raise ValueError(SECRET)
    monkeypatch.setattr(replay, "replay", private_failure)
    monkeypatch.setattr(replay.sys, "argv", ["replay_subconscious_history_6062.py"])
    assert replay.main() == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.err)["reason"] == "ValidationOrPersistenceFailed"


def test_explicit_process_guard_detects_exact_checkout_only(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    proc = tmp_path / "proc"
    entry = proc / "1234"
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"python\0main.py\0")
    (entry / "cwd").symlink_to(repository, target_is_directory=True)
    with pytest.raises(replay.ReplayRefused, match="RepositoryMainStillRunning"):
        replay.assert_service_stopped(repository, proc_root=proc)
    replay.assert_service_stopped(tmp_path / "different-repo", proc_root=proc)
