"""Metadata-only inventory tests; all files and bindings are synthetic."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.tools import managed_file_inventory as inventory

_PREFIX = "life_engine_workspace/"
_TIME = "2026-09-07T09:00:00.123456+00:00"


def _binding(path: str, *, released: bool = False, size: int = 123) -> dict[str, Any]:
    digest = hashlib.sha256(path.encode()).hexdigest()
    return {
        "logical_path": _PREFIX + path,
        "document_id": None if released else "doc-" + digest,
        "binding_revision": 2 if released else 1,
        "current_version_id": None if released else "version-" + digest,
        "document_revision": 0 if released else 1,
        "byte_length": None if released else size,
        "content_hash": None if released else digest,
        "recorded_at": None if released else _TIME,
        "encoding": None if released else "utf-8",
    }


class _Store:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = sorted(rows, key=lambda row: row["logical_path"])
        self.calls: list[dict[str, Any]] = []

    async def list_file_bindings(self, **request: Any) -> list[dict[str, Any]]:
        self.calls.append(request)
        return [
            dict(row)
            for row in self.rows
            if row["logical_path"].startswith(request["logical_path_prefix"])
            and row["logical_path"] > request["after_logical_path"]
        ][: request["limit"]]

    async def get_version(self, *_args: Any) -> Any:
        raise AssertionError("inventory must not read content blobs")

    async def get_head(self, logical_path: str) -> Any:
        for row in self.rows:
            if row["logical_path"] == logical_path and row["document_id"] is not None:
                return SimpleNamespace(document_id=row["document_id"])
        return None

    async def append_version(self, *_args: Any) -> Any:
        raise AssertionError("inventory must never import disk bytes")


def _session(workspace: Path, rows: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(workspace=workspace, store=_Store(rows))


async def test_inventory_masks_deleted_and_stale_disk_and_adds_virtual_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "deleted.md").write_bytes(b"stale-deleted-synthetic")
    (tmp_path / "renamed-old.md").write_bytes(b"stale-renamed-synthetic")
    (tmp_path / "current.md").write_bytes(b"stale")
    (tmp_path / "ordinary.txt").write_bytes(b"ordinary")
    session = _session(
        tmp_path,
        [
            _binding("deleted.md", released=True),
            _binding("renamed-old.md", released=True),
            _binding("current.md", size=999),
            _binding("virtual/nested/new.md", size=7),
        ],
    )
    items = await inventory.load_managed_inventory(session, root=tmp_path)
    by_path = {item["path"]: item for item in items}
    assert set(by_path) == {
        "current.md",
        "ordinary.txt",
        "virtual",
        "virtual/nested",
        "virtual/nested/new.md",
    }
    current = by_path["current.md"]
    assert current["size"] == 999 and current["size_human"] == "999B"
    assert current["source_authority"] == "subject_document_store"
    assert (
        current["file_ref"]
        == f"subject-file:{current['document_id']}@{current['subject_version_id']}"
    )
    assert by_path["ordinary.txt"]["source_authority"] == "unregistered_workspace_file"
    assert "subject_version_id" not in by_path["ordinary.txt"]
    assert by_path["virtual"]["type"] == "directory"
    assert by_path["virtual/nested/new.md"]["size"] == 7
    assert (tmp_path / "deleted.md").read_bytes() == b"stale-deleted-synthetic"
    assert not (tmp_path / "virtual").exists()


async def test_inventory_virtual_root_depth_and_literal_prefix(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            _binding("virtual_dir/a.md"),
            _binding("virtual_dir/sub/b.md"),
            _binding("virtual_dir/sub/deep/c.md"),
            _binding("virtualXdir/no.md"),
        ],
    )
    items = await inventory.load_managed_inventory(
        session, root=tmp_path / "virtual_dir", max_depth=1
    )
    assert {item["path"] for item in items} == {"virtual_dir/a.md", "virtual_dir/sub"}
    assert session.store.calls[0]["logical_path_prefix"] == _PREFIX + "virtual_dir/"
    items = await inventory.load_managed_inventory(
        session, root=tmp_path / "virtual_dir", max_depth=2
    )
    assert {item["path"] for item in items} == {
        "virtual_dir/a.md",
        "virtual_dir/sub",
        "virtual_dir/sub/b.md",
        "virtual_dir/sub/deep",
    }


async def test_hidden_rules_apply_to_disk_and_authority(tmp_path: Path) -> None:
    for name in (".git", ".memory", ".svn", "node_modules", "__pycache__", "visible"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "disk.md").write_bytes(b"synthetic")
    (tmp_path / ".hidden.md").write_bytes(b"synthetic")
    session = _session(
        tmp_path,
        [
            _binding(name + "/db.md")
            for name in (
                ".git",
                ".memory",
                ".svn",
                "node_modules",
                "__pycache__",
                "visible",
            )
        ],
    )
    hidden = await inventory.load_managed_inventory(
        session, root=tmp_path, include_hidden=False
    )
    assert {item["path"] for item in hidden} == {
        "visible",
        "visible/disk.md",
        "visible/db.md",
    }
    shown = await inventory.load_managed_inventory(
        session, root=tmp_path, include_hidden=True
    )
    assert ".hidden.md" in {item["path"] for item in shown}
    assert "node_modules/db.md" in {item["path"] for item in shown}


async def test_symlinks_are_not_followed_and_scope_is_guarded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.md").write_bytes(b"synthetic-outside")
    (workspace / "linked-dir").symlink_to(outside, target_is_directory=True)
    (workspace / "linked-file").symlink_to(outside / "private.md")
    session = _session(workspace, [])
    assert await inventory.load_managed_inventory(session, root=workspace) == []
    with pytest.raises(inventory.ManagedFileInventoryError, match="symlink_root"):
        await inventory.load_managed_inventory(session, root=workspace / "linked-dir")
    with pytest.raises(inventory.ManagedFileInventoryError, match="outside_workspace"):
        await inventory.load_managed_inventory(session, root=outside)


async def test_stale_disk_file_does_not_hide_authority_only_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "virtual").write_bytes(b"stale ordinary file")
    session = _session(tmp_path, [_binding("virtual/sub/file.md")])
    items = await inventory.load_managed_inventory(session, root=tmp_path / "virtual")
    assert {item["path"] for item in items} == {"virtual/sub", "virtual/sub/file.md"}
    with pytest.raises(inventory.ManagedFileInventoryError, match="not_directory"):
        await inventory.load_managed_inventory(
            _session(tmp_path, []), root=tmp_path / "virtual"
        )
    with pytest.raises(inventory.ManagedFileInventoryError, match="not_directory"):
        await inventory.load_managed_inventory(
            _session(tmp_path, [_binding("virtual")]),
            root=tmp_path / "virtual",
        )


async def test_registered_path_masks_stale_directory_descendants(
    tmp_path: Path,
) -> None:
    (tmp_path / "retired").mkdir()
    (tmp_path / "retired" / "stale.md").write_bytes(b"synthetic")
    (tmp_path / "file-now").mkdir()
    (tmp_path / "file-now" / "stale.md").write_bytes(b"synthetic")
    session = _session(
        tmp_path, [_binding("retired", released=True), _binding("file-now")]
    )
    items = await inventory.load_managed_inventory(session, root=tmp_path)
    assert (
        len(items) == 1
        and items[0]["path"] == "file-now"
        and items[0]["type"] == "file"
    )


async def test_binding_pagination_and_no_fallback_on_bad_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inventory, "_PAGE_SIZE", 2)
    session = _session(tmp_path, [_binding(f"file-{number}.md") for number in range(5)])
    items = await inventory.load_managed_inventory(session, root=tmp_path)
    assert len(items) == 5 and len(session.store.calls) == 4

    async def repeated(**_request: Any) -> list[dict[str, Any]]:
        return [_binding("same.md")]

    session.store.list_file_bindings = repeated
    with pytest.raises(inventory.ManagedFileInventoryError, match="nonmonotonic"):
        await inventory.load_managed_inventory(session, root=tmp_path)


async def test_combined_budget_fails_instead_of_returning_partial_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inventory, "_MAX_INVENTORY_ENTRIES", 3)
    (tmp_path / "disk-a").write_bytes(b"synthetic")
    (tmp_path / "disk-b").write_bytes(b"synthetic")
    session = _session(tmp_path, [_binding("db-a"), _binding("db-b")])
    with pytest.raises(inventory.ManagedFileInventoryError, match="budget_exceeded"):
        await inventory.load_managed_inventory(session, root=tmp_path)


@pytest.mark.parametrize(
    "damage", ["missing", "released", "time", "cross_scope", "negative_size"]
)
async def test_invalid_binding_metadata_is_explicit_failure(
    tmp_path: Path, damage: str
) -> None:
    row = _binding("file.md")
    if damage == "missing":
        row.pop("document_id")
    elif damage == "released":
        row["document_id"] = None
    elif damage == "time":
        row["recorded_at"] = None
    elif damage == "negative_size":
        row["byte_length"] = -1
    else:
        row["logical_path"] = "another_workspace/private.md"
    session = _session(tmp_path, [])

    async def malformed(**_request: Any) -> list[dict[str, Any]]:
        return [row]

    session.store.list_file_bindings = malformed
    with pytest.raises(inventory.ManagedFileInventoryError):
        await inventory.load_managed_inventory(session, root=tmp_path)


async def test_disk_stat_and_scan_run_off_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller_thread = threading.get_ident()
    original = inventory._scan_disk
    worker_threads: list[int] = []

    def checked(*args: Any, **kwargs: Any) -> Any:
        worker_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(inventory, "_scan_disk", checked)
    (tmp_path / "ordinary").write_bytes(b"synthetic")
    result = await inventory.load_managed_inventory(
        _session(tmp_path, []), root=tmp_path
    )
    assert len(result) == 1 and worker_threads[0] != caller_thread
