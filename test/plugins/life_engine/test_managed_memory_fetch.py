"""Isolated memory fetch contracts: fake metadata store and temporary disk only."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from plugins.life_engine.storage.subject_contracts import (
    SubjectDocumentHead,
    SubjectDocumentNotFound,
    SubjectDocumentPathBinding,
)
from plugins.life_engine.tools import managed_files
from plugins.life_engine.tools import managed_memory_fetch as fetch

_PREFIX = "life_engine_workspace/"


class _Store:
    def __init__(self):
        self.heads = {}
        self.documents = {}
        self.bindings = {}
        self.versions = {}
        self.version_reads = []

    def register(self, path, content, *, document="doc_a", version="ver_a"):
        logical = _PREFIX + path
        value = SimpleNamespace(
            document_id=document,
            version_id=version,
            logical_path=logical,
            content_bytes=content,
            content_hash=hashlib.sha256(content).hexdigest(),
            byte_length=len(content),
            recorded_at="2026-09-07T00:00:00Z",
            encoding="utf-8",
        )
        head = SubjectDocumentHead(document, logical, "elysia", version, 1, 1)
        self.heads[logical] = head
        self.documents[document] = head
        self.bindings[logical] = SubjectDocumentPathBinding(logical, document, 1)
        self.versions[version] = value
        return value

    def release(self, path):
        logical = _PREFIX + path
        self.heads.pop(logical)
        self.bindings[logical] = SubjectDocumentPathBinding(logical, None, 2)

    async def get_head(self, path):
        return self.heads.get(path)

    async def get_document_head(self, document):
        return self.documents.get(document)

    async def get_path_binding(self, path):
        return self.bindings.get(path)

    async def get_version(self, version):
        self.version_reads.append(version)
        if version not in self.versions:
            raise SubjectDocumentNotFound("synthetic version missing")
        return self.versions[version]

    async def get_version_descriptor(self, version):
        if version not in self.versions:
            raise SubjectDocumentNotFound("synthetic version missing")
        value = self.versions[version]
        return {
            key: getattr(value, key)
            for key in (
                "document_id",
                "version_id",
                "logical_path",
                "content_hash",
                "byte_length",
            )
        }


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(managed_files, "_get_workspace", lambda plugin: tmp_path)
    store = _Store()
    service = SimpleNamespace(
        _selectable_storage_enabled=True, _subject_document_store=store
    )
    tool = SimpleNamespace(plugin=SimpleNamespace(), _runtime_task_name="core")
    return tmp_path, store, service, tool


async def _fetch(fixture, paths, **kwargs):
    _, _, service, tool = fixture
    result = await fetch.fetch_managed_memories(tool, paths, service=service, **kwargs)
    assert result is not None
    ok, payload = result
    assert len(str(payload).encode("utf-8")) <= 8192
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) <= 8192
    return ok, payload


async def test_selected_current_and_legacy_mix_retains_exact_utf8(fixture):
    root, store, _, _ = fixture
    content = b"\xef\xbb\xbf# exact\r\n" + "爱莉".encode() + b"\r\n"
    version = store.register("notes/a.md", content)
    (root / "notes").mkdir()
    (root / "notes/a.md").write_bytes(b"STALE DISK")
    (root / "notes/legacy.md").write_bytes(b"legacy\r\n")
    ok, payload = await _fetch(
        fixture, ["notes/a.md", "notes/legacy.md"], include_metadata=False
    )
    assert ok and payload["successful"] == 2
    current, legacy = payload["files"]
    assert current["content"].encode() == content
    assert (
        current["file_ref"]
        == f"subject-file:{version.document_id}@{version.version_id}"
    )
    assert current["content_hash"] == version.content_hash
    assert "metadata" not in current
    assert legacy["content"] == "legacy\r\n"
    assert legacy["archived"] is False and "version_id" not in legacy
    assert legacy["legacy_sha256"] == hashlib.sha256(b"legacy\r\n").hexdigest()
    assert len(store.versions) == 1


async def test_released_path_does_not_read_stale_disk_or_lineage(fixture):
    root, store, service, _ = fixture
    store.register("notes/a.md", b"original")
    store.release("notes/a.md")
    (root / "notes").mkdir()
    (root / "notes/a.md").write_bytes(b"must not leak")

    async def forbidden(*args, **kwargs):
        pytest.fail("fetch must not consult old path lineage")

    service._memory_service = SimpleNamespace(resolve_canonical_path=forbidden)
    ok, payload = await _fetch(fixture, ["notes/a.md"])
    assert not ok and payload["failed"] == 1
    assert payload["files"][0]["error_type"] == "SubjectDocumentNotFound"
    assert "must not leak" not in str(payload)
    assert not store.version_reads


async def test_history_pin_survives_rename_deletion_and_path_reuse(fixture):
    _, store, _, _ = fixture
    original = store.register("notes/a.md", b"original")
    store.release("notes/a.md")
    store.documents[original.document_id] = SubjectDocumentHead(
        original.document_id,
        _PREFIX + "archive/renamed.md",
        "elysia",
        original.version_id,
        3,
        2,
        True,
    )
    newer = store.register(
        "notes/a.md", b"new occupant", document="doc_b", version="ver_b"
    )
    ok, historical = await _fetch(
        fixture, ["notes/a.md"], version_ids={"notes/a.md": original.version_id}
    )
    assert ok and historical["files"][0]["content"] == "original"
    assert historical["files"][0]["document_id"] == original.document_id
    ok, current = await _fetch(fixture, ["notes/a.md"])
    assert ok and current["files"][0]["document_id"] == newer.document_id
    assert current["files"][0]["content"] == "new occupant"


async def test_current_version_is_pinned_before_head_changes(fixture):
    _, store, _, _ = fixture
    original = store.register("notes/a.md", b"selected at start")
    descriptor = store.get_version_descriptor

    async def replace_head(version):
        result = await descriptor(version)
        store.register("notes/a.md", b"later head", document="doc_b", version="ver_b")
        return result

    store.get_version_descriptor = replace_head
    ok, payload = await _fetch(fixture, ["notes/a.md"])
    assert ok and payload["files"][0]["version_id"] == original.version_id
    assert payload["files"][0]["content"] == "selected at start"


async def test_large_registered_descriptor_is_referenced_without_blob_read(fixture):
    _, store, _, _ = fixture
    version = store.register("notes/large.md", b"not loaded")
    version.byte_length = fetch.MAX_FETCH_DOCUMENT_BYTES + 1
    ok, payload = await _fetch(fixture, ["notes/large.md"])
    assert not ok and not store.version_reads
    item = payload["files"][0]
    assert item["error_type"] == "ManagedMemoryFetchDocumentTooLarge"
    assert item["read_file"] == {
        "path": "notes/large.md",
        "version_id": version.version_id,
    }
    assert "content" not in item and item["truncated"] is False


async def test_output_escaping_is_counted_and_no_content_is_truncated(fixture):
    _, store, _, _ = fixture
    raw = b"\\\n\x00" * 2500
    store.register("notes/escaped.md", raw)
    ok, payload = await _fetch(fixture, ["notes/escaped.md"])
    assert not ok
    item = payload["files"][0]
    assert item["delivery"] == "reference_only"
    assert "content" not in item and item["truncated"] is False
    assert item["content_hash"] == hashlib.sha256(raw).hexdigest()


async def test_legacy_over_output_budget_remains_unarchived(fixture):
    root, _, _, _ = fixture
    (root / "notes").mkdir()
    (root / "notes/large.md").write_bytes(b"x" * 9000)
    ok, payload = await _fetch(fixture, ["notes/large.md"])
    assert not ok
    item = payload["files"][0]
    assert not item["archived"] and "version_id" not in item
    assert item["read_file"] == {"path": "notes/large.md"}
    assert "legacy_sha256" in item and "content" not in item


async def test_many_references_fail_explicitly_with_bounded_actionable_pins(fixture):
    _, store, _, _ = fixture
    paths = [f"notes/{index}.md" for index in range(64)]
    for index, path in enumerate(paths):
        version = store.register(
            path, b"", document=f"doc_{index}", version=f"ver_{index}"
        )
        version.byte_length = fetch.MAX_FETCH_DOCUMENT_BYTES + 1
    ok, payload = await _fetch(fixture, paths)
    assert not ok and payload["error_type"] == "ManagedMemoryFetchResultBudgetExceeded"
    assert 1 <= len(payload["read_references"]) <= 3
    assert all(item["read_file"]["version_id"] for item in payload["read_references"])
    assert not store.version_reads


async def test_non_utf8_does_not_replace_or_leak_bytes(fixture):
    _, store, _, _ = fixture
    store.register("notes/a.md", b"private\xffraw")
    ok, payload = await _fetch(fixture, ["notes/a.md"])
    assert not ok
    assert payload["files"][0]["error_type"] == "ManagedMemoryFetchRequiresUTF8"
    assert "private" not in str(payload) and "content" not in payload["files"][0]


@pytest.mark.parametrize(
    "paths,pins",
    [
        ([], None),
        (["notes/a.md"] * 65, None),
        (["notes/a.md", "notes/a.md"], None),
        ([None], None),
        (["x" * 1025], None),
        (["notes/a.md"], []),
        (["notes/a.md"], {"notes/other.md": "ver_a"}),
        (["notes/a.md"], {"notes/a.md": ""}),
        (["notes/a.md"], {"notes/a.md": 1}),
    ],
)
async def test_request_validation_precedes_storage_reads(fixture, paths, pins):
    _, store, _, _ = fixture
    ok, payload = await _fetch(fixture, paths, version_ids=pins)
    assert not ok and payload["error_type"] == "ManagedMemoryFetchRequestInvalid"
    assert not store.version_reads


@pytest.mark.parametrize(
    "path",
    ["../outside.md", "notes/../notes/a.md", "runtime/private.md", "notes/a.bin"],
)
async def test_existing_memory_eligibility_is_preserved(fixture, path):
    _, store, _, _ = fixture
    ok, payload = await _fetch(fixture, [path])
    assert not ok and payload["files"][0]["error_type"] == "MemoryDocumentIneligible"
    assert not store.version_reads


async def test_missing_selected_storage_never_falls_back_to_disk(fixture):
    _, _, service, _ = fixture
    service._subject_document_store = None
    ok, payload = await _fetch(fixture, ["MEMORY.md"])
    assert not ok and payload["error_type"] == "SelectedSubjectStorageNotStarted"


async def test_unselected_returns_only_legacy_handoff_and_rejects_history(fixture):
    _, _, service, tool = fixture
    service._selectable_storage_enabled = False
    assert (
        await fetch.fetch_managed_memories(tool, ["notes/a.md"], service=service)
        is None
    )
    ok, payload = await _fetch(
        fixture, ["notes/a.md"], version_ids={"notes/a.md": "ver_a"}
    )
    assert (
        not ok
        and payload["error_type"] == "HistoricalMemoryFetchRequiresSelectedStorage"
    )


async def test_failures_are_content_free_and_cancellation_propagates(fixture):
    _, store, _, _ = fixture

    async def fail(path):
        raise RuntimeError("private SQL statement with SECRET")

    store.get_head = fail
    ok, payload = await _fetch(fixture, ["notes/a.md"])
    assert not ok and payload["files"][0]["error_type"] == "RuntimeError"
    assert "SECRET" not in str(payload)

    async def cancel(path):
        raise asyncio.CancelledError()

    store.get_head = cancel
    with pytest.raises(asyncio.CancelledError):
        await _fetch(fixture, ["notes/a.md"])


async def test_aggregate_read_budget_stops_later_selected_blobs(fixture, monkeypatch):
    _, store, _, _ = fixture
    monkeypatch.setattr(fetch, "MAX_FETCH_READ_BYTES", 3)
    store.register("notes/a.md", b"abc")
    store.register("notes/b.md", b"d", document="doc_b", version="ver_b")
    ok, payload = await _fetch(fixture, ["notes/a.md", "notes/b.md"])
    assert ok and payload["successful"] == 1 and payload["failed"] == 1
    assert store.version_reads == ["ver_a"]
    assert payload["files"][1]["error_type"] == "ManagedMemoryFetchReadBudgetExceeded"


async def test_symlink_alias_cannot_change_requested_authority_path(fixture):
    root, store, _, _ = fixture
    (root / "notes").mkdir()
    (root / "notes/a.md").symlink_to(root / "notes/other.md")
    store.register("notes/other.md", b"other authority")
    ok, payload = await _fetch(fixture, ["notes/a.md"])
    assert not ok and payload["files"][0]["error_type"] == "ValueError"
    assert not store.version_reads and "other authority" not in str(payload)


async def test_current_head_cannot_select_another_documents_version(fixture):
    _, store, _, _ = fixture
    store.register("notes/a.md", b"a")
    store.register("notes/b.md", b"must not leak", document="doc_b", version="ver_b")
    store.heads[_PREFIX + "notes/a.md"] = SubjectDocumentHead(
        "doc_a",
        _PREFIX + "notes/a.md",
        "elysia",
        "ver_b",
        2,
        1,
    )
    ok, payload = await _fetch(fixture, ["notes/a.md"])
    assert not ok and not store.version_reads
    assert "must not leak" not in str(payload)


async def test_legacy_registration_race_is_not_delivered_as_current_disk(
    fixture, monkeypatch
):
    root, store, _, _ = fixture
    (root / "notes").mkdir()
    (root / "notes/a.md").write_bytes(b"stale legacy")
    original_read = managed_files.read_exact_bytes

    def register_during_read(*args, **kwargs):
        raw = original_read(*args, **kwargs)
        store.register("notes/a.md", b"new authority")
        return raw

    monkeypatch.setattr(managed_files, "read_exact_bytes", register_during_read)
    ok, payload = await _fetch(fixture, ["notes/a.md"])
    assert not ok and "stale legacy" not in str(payload)
    assert payload["files"][0]["error_type"] == "ValueError"


async def test_legacy_reads_receive_the_remaining_batch_hard_cap(fixture, monkeypatch):
    root, _, _, _ = fixture
    (root / "notes").mkdir()
    (root / "notes/a.md").write_bytes(b"abc")
    (root / "notes/b.md").write_bytes(b"def")
    monkeypatch.setattr(fetch, "MAX_FETCH_READ_BYTES", 5)
    caps = []
    original_read = managed_files.read_exact_bytes

    def capture_cap(*args, **kwargs):
        caps.append(kwargs["max_bytes"])
        return original_read(*args, **kwargs)

    monkeypatch.setattr(managed_files, "read_exact_bytes", capture_cap)
    ok, payload = await _fetch(fixture, ["notes/a.md", "notes/b.md"])
    assert ok and payload["successful"] == 1 and payload["failed"] == 1
    assert caps == [5, 2]
    assert payload["files"][0]["content"] == "abc"
    assert "content" not in payload["files"][1]
    assert "legacy_sha256" not in payload["files"][1]


async def test_legacy_single_file_cap_prevents_unbounded_capture(fixture, monkeypatch):
    root, _, _, _ = fixture
    (root / "notes").mkdir()
    (root / "notes/a.md").write_bytes(b"too large")
    monkeypatch.setattr(fetch, "MAX_FETCH_DOCUMENT_BYTES", 3)
    ok, payload = await _fetch(fixture, ["notes/a.md"])
    assert not ok and payload["failed"] == 1
    assert "content" not in payload["files"][0]
    assert "legacy_sha256" not in payload["files"][0]


async def test_empty_selected_document_is_readable_at_exact_batch_boundary(
    fixture, monkeypatch
):
    _, store, _, _ = fixture
    monkeypatch.setattr(fetch, "MAX_FETCH_READ_BYTES", 3)
    store.register("notes/a.md", b"abc")
    store.register("notes/b.md", b"", document="doc_b", version="ver_b")
    ok, payload = await _fetch(fixture, ["notes/a.md", "notes/b.md"])
    assert ok and payload["successful"] == 2
    assert payload["files"][1]["content"] == ""
