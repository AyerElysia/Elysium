"""Byte-exact projection races: temporary directories only, no stores or network."""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import threading
from pathlib import Path

import pytest

from plugins.life_engine.storage import workspace_file_io as file_io


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    for name in ("connect", "connect_ex", "bind"):
        original = getattr(socket.socket, name)

        def guarded(self, address, *args, _original=original, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                pytest.fail("workspace I/O tests must not access real network/ports")
            return _original(self, address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)


def _quarantines(root: Path) -> list[Path]:
    return list(root.rglob(".elysium-quarantine-*"))


def test_create_read_verify_and_repeat_are_exact_and_idempotent(tmp_path):
    content = b"\xef\xbb\xbfsynthetic\r\n\x00bytes\n"
    path = "life_engine_workspace/notes/new.any"
    file_io.project_exact_bytes(tmp_path, path, content)
    target = tmp_path / path
    before = target.stat()
    assert (
        file_io.read_exact_bytes(tmp_path, path, expected_hash=_sha(content)) == content
    )
    file_io.project_exact_bytes(tmp_path, path, content)
    after = target.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert after.st_mode & 0o777 == 0o600
    assert not _quarantines(tmp_path)


@pytest.mark.parametrize("expected", [None, _sha(b"not current")])
def test_unknown_existing_bytes_never_overwritten(tmp_path, expected):
    target = tmp_path / "file"
    target.write_bytes(b"unknown original")
    before = target.stat().st_ino
    with pytest.raises(file_io.WorkspaceFileConflict):
        file_io.project_exact_bytes(
            tmp_path, "file", b"new", expected_parent_hash=expected
        )
    assert target.read_bytes() == b"unknown original"
    assert target.stat().st_ino == before
    assert not _quarantines(tmp_path)


@pytest.mark.parametrize("expected", [None, _sha(b"different predecessor")])
def test_cache_can_disable_equal_bytes_idempotency_without_claiming_unknown(
    tmp_path, expected
):
    target = tmp_path / "file"
    target.write_bytes(b"same bytes")
    inode = target.stat().st_ino
    with pytest.raises(file_io.WorkspaceFileConflict):
        file_io.project_exact_bytes(
            tmp_path,
            "file",
            b"same bytes",
            expected_parent_hash=expected,
            allow_equal_existing=False,
        )
    assert target.read_bytes() == b"same bytes"
    assert target.stat().st_ino == inode
    assert not _quarantines(tmp_path)


def test_replace_exact_predecessor_preserves_normal_permissions(tmp_path):
    target = tmp_path / "file"
    target.write_bytes(b"old")
    target.chmod(0o640)
    file_io.project_exact_bytes(
        tmp_path, "file", b"new", expected_parent_hash=_sha(b"old")
    )
    assert target.read_bytes() == b"new"
    assert target.stat().st_mode & 0o777 == 0o640
    assert not _quarantines(tmp_path)


def test_delete_only_exact_bytes_and_absence_is_idempotent(tmp_path):
    target = tmp_path / "file"
    target.write_bytes(b"old")
    with pytest.raises(file_io.WorkspaceFileConflict):
        file_io.remove_exact_bytes(tmp_path, "file", expected_hash=_sha(b"unknown"))
    assert target.read_bytes() == b"old"
    file_io.remove_exact_bytes(tmp_path, "file", expected_hash=_sha(b"old"))
    file_io.remove_exact_bytes(tmp_path, "file", expected_hash=_sha(b"old"))
    file_io.remove_exact_bytes(tmp_path, "absent/child", expected_hash=_sha(b"old"))
    assert not target.exists()
    assert not _quarantines(tmp_path)


@pytest.mark.parametrize(
    "path", ["../outside", "/absolute", "a/../b", "a\\b", "", "a//b", "a/./b"]
)
def test_invalid_paths_fail_without_changes(tmp_path, path):
    with pytest.raises(file_io.WorkspaceFileError):
        file_io.project_exact_bytes(tmp_path, path, b"content")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("operation", ["read", "project", "remove"])
def test_symlink_parent_never_traversed(tmp_path, operation):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "file").write_bytes(b"outside")
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        if operation == "read":
            file_io.read_exact_bytes(root, "link/file")
        elif operation == "project":
            file_io.project_exact_bytes(
                root, "link/file", b"new", expected_parent_hash=_sha(b"outside")
            )
        else:
            file_io.remove_exact_bytes(
                root, "link/file", expected_hash=_sha(b"outside")
            )
    assert (outside / "file").read_bytes() == b"outside"
    assert not _quarantines(outside)


def test_final_create_race_preserves_competitor(tmp_path, monkeypatch):
    original = file_io._publish

    def race(target, fd):
        target.path.write_bytes(b"competitor")
        return original(target, fd)

    monkeypatch.setattr(file_io, "_publish", race)
    with pytest.raises(FileExistsError):
        file_io.project_exact_bytes(tmp_path, "file", b"ours")
    assert (tmp_path / "file").read_bytes() == b"competitor"
    assert not _quarantines(tmp_path)


@pytest.mark.parametrize("operation", ["project", "remove"])
def test_actual_moved_unknown_inode_is_verified_and_restored(
    tmp_path, monkeypatch, operation
):
    target = tmp_path / "file"
    target.write_bytes(b"old")
    original = file_io._Quarantine.take

    def race(self):
        self.target.path.write_bytes(b"unknown replacement")
        original(self)

    monkeypatch.setattr(file_io._Quarantine, "take", race)
    with pytest.raises(file_io.WorkspaceFileConflict):
        if operation == "project":
            file_io.project_exact_bytes(
                tmp_path, "file", b"new", expected_parent_hash=_sha(b"old")
            )
        else:
            file_io.remove_exact_bytes(tmp_path, "file", expected_hash=_sha(b"old"))
    assert target.read_bytes() == b"unknown replacement"
    assert not _quarantines(tmp_path)


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_unexpected_entry_type_is_restored_without_following_or_deleting(
    tmp_path, monkeypatch, replacement
):
    target = tmp_path / "file"
    target.write_bytes(b"old")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    original = file_io._Quarantine.take

    def race(self):
        self.target.path.rename(tmp_path / "old-kept")
        if replacement == "symlink":
            self.target.path.symlink_to(outside)
        else:
            self.target.path.mkdir()
            (self.target.path / "unknown").write_bytes(b"unknown")
        original(self)

    monkeypatch.setattr(file_io._Quarantine, "take", race)
    with pytest.raises((OSError, file_io.WorkspaceFileConflict)):
        file_io.remove_exact_bytes(tmp_path, "file", expected_hash=_sha(b"old"))
    if replacement == "symlink":
        assert target.is_symlink()
    else:
        assert (target / "unknown").read_bytes() == b"unknown"
    assert outside.read_bytes() == b"outside"
    assert not _quarantines(tmp_path)


@pytest.mark.parametrize("operation", ["project", "remove"])
def test_restore_conflict_preserves_both_files_and_explicit_recovery_path(
    tmp_path, monkeypatch, operation
):
    target = tmp_path / "file"
    target.write_bytes(b"old")
    original = file_io._Quarantine.take

    def race(self):
        self.target.path.write_bytes(b"unknown moved bytes")
        original(self)
        self.target.path.write_bytes(b"new contender")

    monkeypatch.setattr(file_io._Quarantine, "take", race)
    with pytest.raises(file_io.WorkspaceFileRecoveryRequired) as raised:
        if operation == "project":
            file_io.project_exact_bytes(
                tmp_path, "file", b"ours", expected_parent_hash=_sha(b"old")
            )
        else:
            file_io.remove_exact_bytes(tmp_path, "file", expected_hash=_sha(b"old"))
    recovery = Path(raised.value.recovery_path)
    assert recovery.read_bytes() == b"unknown moved bytes"
    assert target.read_bytes() == b"new contender"
    assert recovery.parent.stat().st_mode & 0o777 == 0o700
    assert "unknown moved bytes" not in str(raised.value)
    assert "recovery_required" in str(raised.value)


def test_delete_recreated_target_is_preserved_not_unlinked(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_bytes(b"old")
    original = file_io._Quarantine.take

    def race(self):
        original(self)
        self.target.path.write_bytes(b"new contender")

    monkeypatch.setattr(file_io._Quarantine, "take", race)
    with pytest.raises(file_io.WorkspaceFileRecoveryRequired) as raised:
        file_io.remove_exact_bytes(tmp_path, "file", expected_hash=_sha(b"old"))
    assert target.read_bytes() == b"new contender"
    assert Path(raised.value.recovery_path).read_bytes() == b"old"


def test_publish_failure_restores_old_exact_bytes(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_bytes(b"old")

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(file_io, "_publish", fail)
    with pytest.raises(OSError):
        file_io.project_exact_bytes(
            tmp_path, "file", b"new", expected_parent_hash=_sha(b"old")
        )
    assert target.read_bytes() == b"old"
    assert not _quarantines(tmp_path)


def test_failure_after_publication_keeps_old_recoverable_and_new_untouched(
    tmp_path, monkeypatch
):
    target = tmp_path / "file"
    target.write_bytes(b"old")
    original = file_io._publish

    def fail(target, fd):
        original(target, fd)
        raise OSError("synthetic durability failure")

    monkeypatch.setattr(file_io, "_publish", fail)
    with pytest.raises(file_io.WorkspaceFileRecoveryRequired) as raised:
        file_io.project_exact_bytes(
            tmp_path, "file", b"new", expected_parent_hash=_sha(b"old")
        )
    assert target.read_bytes() == b"new"
    assert Path(raised.value.recovery_path).read_bytes() == b"old"


def test_parent_move_during_replace_preserves_quarantine_in_actual_location(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    (root / "parent").mkdir(parents=True)
    (root / "parent/file").write_bytes(b"old")
    outside = tmp_path / "outside"
    outside.mkdir()
    original = file_io._Quarantine.take

    def race(self):
        original(self)
        (root / "parent").rename(root / "moved")
        (root / "parent").symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(file_io._Quarantine, "take", race)
    with pytest.raises(file_io.WorkspaceFileRecoveryRequired) as raised:
        file_io.project_exact_bytes(
            root, "parent/file", b"new", expected_parent_hash=_sha(b"old")
        )
    assert not list(outside.iterdir())
    recovery = Path(raised.value.recovery_path)
    assert recovery.read_bytes() == b"old"
    assert recovery.is_relative_to(root / "moved")


def test_rename_composition_publishes_new_before_removing_old(tmp_path):
    (tmp_path / "old").write_bytes(b"exact")
    (tmp_path / "new").write_bytes(b"conflict")
    with pytest.raises(file_io.WorkspaceFileConflict):
        file_io.project_exact_bytes(tmp_path, "new", b"exact")
    assert (tmp_path / "old").read_bytes() == b"exact"
    file_io.project_exact_bytes(tmp_path, "new-free", b"exact")
    file_io.remove_exact_bytes(tmp_path, "old", expected_hash=_sha(b"exact"))
    assert (tmp_path / "new-free").read_bytes() == b"exact"
    assert not (tmp_path / "old").exists()


def test_hash_and_byte_limits_fail_without_truncation(tmp_path):
    target = tmp_path / "file"
    target.write_bytes(b"12345")
    with pytest.raises(file_io.WorkspaceFileConflict):
        file_io.read_exact_bytes(tmp_path, "file", expected_hash=_sha(b"other"))
    with pytest.raises(file_io.WorkspaceFileError):
        file_io.read_exact_bytes(tmp_path, "file", max_bytes=4)
    with pytest.raises(file_io.WorkspaceFileError):
        file_io.project_exact_bytes(
            tmp_path, "file", b"new", expected_parent_hash=_sha(b"12345"), max_bytes=4
        )
    assert target.read_bytes() == b"12345"
    assert not _quarantines(tmp_path)


def test_unsupported_unnamed_temp_fails_without_overwriting(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_bytes(b"old")
    original = os.open

    def unsupported(path, flags, *args, **kwargs):
        if flags & os.O_TMPFILE == os.O_TMPFILE:
            raise OSError("synthetic unsupported filesystem")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", unsupported)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {unsupported})
    with pytest.raises(OSError):
        file_io.project_exact_bytes(
            tmp_path, "file", b"new", expected_parent_hash=_sha(b"old")
        )
    assert target.read_bytes() == b"old"
    assert not _quarantines(tmp_path)


async def test_async_cancellation_joins_owned_worker_before_returning():
    started = asyncio.Event()
    release = threading.Event()
    done = threading.Event()
    loop = asyncio.get_running_loop()

    def work():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        done.set()

    task = asyncio.create_task(file_io.run_workspace_file_io(work))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert done.is_set()


def test_quarantine_initialization_failure_closes_owned_fds(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_bytes(b"old")
    count_before = len(os.listdir("/proc/self/fd"))
    original = os.fsync
    triggered = False

    def fail_once(fd):
        nonlocal triggered
        if (
            not triggered
            and _quarantines(tmp_path)
            and os.readlink(f"/proc/self/fd/{fd}") == str(tmp_path)
        ):
            triggered = True
            raise OSError("synthetic init durability failure")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fail_once)
    with pytest.raises(OSError):
        file_io.project_exact_bytes(
            tmp_path, "file", b"new", expected_parent_hash=_sha(b"old")
        )
    assert triggered
    assert target.read_bytes() == b"old"
    assert not _quarantines(tmp_path)
    assert len(os.listdir("/proc/self/fd")) == count_before


def test_restored_inode_durability_error_never_points_to_absent_quarantine_entry(
    tmp_path, monkeypatch
):
    target_path = tmp_path / "file"
    target_path.write_bytes(b"old")
    with file_io._AnchoredTarget(tmp_path, "file", create=False) as target:
        quarantine = file_io._Quarantine(target)
        quarantine.take()
        original = os.fsync
        triggered = False

        def fail_once(fd):
            nonlocal triggered
            if not triggered and fd == target.parent_fd and target_path.exists():
                triggered = True
                raise OSError("synthetic restored durability failure")
            return original(fd)

        monkeypatch.setattr(os, "fsync", fail_once)
        try:
            with pytest.raises(file_io.WorkspaceFileError) as raised:
                quarantine.restore("synthetic failure")
            assert not isinstance(raised.value, file_io.WorkspaceFileRecoveryRequired)
            assert "restored_durability_unconfirmed" in str(raised.value)
            assert not quarantine.moved
            assert target_path.read_bytes() == b"old"
        finally:
            quarantine.close()
    assert not _quarantines(tmp_path)


def test_create_post_publish_durability_error_keeps_published_exact_bytes(
    tmp_path, monkeypatch
):
    target = tmp_path / "file"
    original = os.fsync

    def fail_after_publish(fd):
        if target.exists() and os.readlink(f"/proc/self/fd/{fd}") == str(tmp_path):
            raise OSError("synthetic publish durability failure")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fail_after_publish)
    with pytest.raises(
        file_io.WorkspaceFileError, match="published_durability_unconfirmed"
    ):
        file_io.project_exact_bytes(tmp_path, "file", b"exact")
    assert target.read_bytes() == b"exact"
    assert not _quarantines(tmp_path)


async def test_cancellation_retains_recovery_failure_as_cause(tmp_path):
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    recovery = tmp_path / "retained"

    def work():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        raise file_io.WorkspaceFileRecoveryRequired(
            "file", recovery, reason="synthetic conflict"
        )

    task = asyncio.create_task(file_io.run_workspace_file_io(work))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert isinstance(raised.value.__cause__, file_io.WorkspaceFileRecoveryRequired)
    assert raised.value.__cause__.recovery_path == str(recovery)
