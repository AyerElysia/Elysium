"""Synthetic cache bytes only: no real screen, model, store, network or lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import socket
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.storage import workspace_file_io as file_io
from plugins.life_engine.tools import screen_tools as screen


@pytest.fixture(autouse=True)
def _deny_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("screen cache tests must not access real screen/model/network")

    for name in (
        "_capture_with_pil",
        "_capture_with_powershell",
        "_capture_with_grim",
        "_capture_with_ffmpeg",
        "_analyze_screenshot_with_model",
    ):
        monkeypatch.setattr(screen, name, forbidden)
    monkeypatch.setattr(screen.ImageGrab, "grab", forbidden)
    monkeypatch.setattr(screen, "create_llm_request", forbidden)
    for name in ("connect", "connect_ex", "bind"):
        original = getattr(socket.socket, name)

        def guarded(self, address, *args, _original=original, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                forbidden()
            return _original(self, address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)


def _plugin(tmp_path, relative="screenshots/latest_screen.png"):
    cfg = LifeEngineConfig()
    cfg.settings.workspace_path = str(tmp_path)
    cfg.screen.enabled = True
    cfg.screen.save_latest = True
    cfg.screen.latest_path = relative
    cfg.screen.capture_method = "pil"
    return SimpleNamespace(
        config=cfg,
        service=SimpleNamespace(
            _subject_document_store=None, _selectable_storage_enabled=False
        ),
    )


def _sha(content):
    return hashlib.sha256(content).hexdigest()


async def test_only_current_process_owned_exact_cache_can_be_replaced(tmp_path):
    plugin = _plugin(tmp_path)
    first = await screen._save_latest_cache(plugin, b"synthetic capture one")
    second = await screen._save_latest_cache(plugin, b"synthetic capture two")
    target = tmp_path / plugin.config.screen.latest_path
    assert target.read_bytes() == b"synthetic capture two"
    assert first["source_origin"] == second["source_origin"] == "external_screen_cache"
    assert second["sha256"] == _sha(b"synthetic capture two")
    assert second["ownership"] == "current_process_only"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.rglob(".elysium-quarantine-*"))
    with pytest.raises(screen.ScreenCacheSafetyError, match="Unowned"):
        await screen._save_latest_cache(_plugin(tmp_path), b"synthetic capture two")
    assert target.read_bytes() == b"synthetic capture two"


@pytest.mark.parametrize("existing", [b"unknown original", b"same incoming", b""])
async def test_unowned_existing_never_overwritten_even_if_equal(tmp_path, existing):
    target = tmp_path / "cache.bin"
    target.write_bytes(existing)
    before = target.stat()
    with pytest.raises(screen.ScreenCacheSafetyError, match="Unowned"):
        await screen._save_latest_cache(
            _plugin(tmp_path, "cache.bin"), b"same incoming"
        )
    assert target.read_bytes() == existing
    assert target.stat().st_ino == before.st_ino
    assert target.stat().st_mtime_ns == before.st_mtime_ns


async def test_successful_path_switch_keeps_only_one_bounded_cache_owner(tmp_path):
    plugin = _plugin(tmp_path, "one.bin")
    await screen._save_latest_cache(plugin, b"first cache")
    plugin.config.screen.latest_path = "two.bin"
    await screen._save_latest_cache(plugin, b"second cache")
    plugin.config.screen.latest_path = "one.bin"
    with pytest.raises(screen.ScreenCacheSafetyError, match="Unowned"):
        await screen._save_latest_cache(plugin, b"first cache")
    assert (tmp_path / "one.bin").read_bytes() == b"first cache"
    assert (tmp_path / "two.bin").read_bytes() == b"second cache"


async def test_owner_hash_cannot_overwrite_externally_changed_equal_desired(tmp_path):
    plugin = _plugin(tmp_path, "cache.bin")
    await screen._save_latest_cache(plugin, b"owned old")
    target = tmp_path / "cache.bin"
    target.write_bytes(b"unknown replacement")
    before = target.stat().st_ino
    with pytest.raises(file_io.WorkspaceFileConflict):
        await screen._save_latest_cache(plugin, b"unknown replacement")
    assert target.read_bytes() == b"unknown replacement"
    assert target.stat().st_ino == before
    assert plugin._life_latest_screen_cache_state.content_hash == _sha(b"owned old")


@pytest.mark.parametrize(
    "path",
    [
        "/outside",
        "../outside",
        "a/../outside",
        "a//b",
        "a/./b",
        "a\\b",
        "C:/outside",
        "a\x00b",
    ],
)
async def test_strict_relative_path_rejects_before_filesystem_change(tmp_path, path):
    with pytest.raises(screen.ScreenCacheSafetyError, match="RelativePath"):
        await screen._save_latest_cache(_plugin(tmp_path, path), b"image")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "path",
    [
        "SOUL.md",
        "USER.md/child",
        "MEMORY.md",
        "notes/one.txt",
        "notes",
        "diaries/one.bin",
        "thoughts/streams.json/child",
        "runtime/proactive/authority.json",
        "runtime/proactive/backend-binding.json",
    ],
)
async def test_declared_subject_and_reserved_ancestors_never_receive_cache(
    tmp_path, path
):
    with pytest.raises(screen.ScreenCacheSafetyError):
        await screen._save_latest_cache(_plugin(tmp_path, path), b"image")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "mode",
    [
        "active",
        "released",
        "ancestor",
        "missing_head",
        "missing_binding",
        "missing_fence",
        "rejected_fence",
        "query_error",
        "success",
        "released_ancestor",
    ],
)
async def test_selected_registry_and_namespace_fence_fail_closed(
    tmp_path, monkeypatch, mode
):
    plugin = _plugin(tmp_path, "cache/one.png")
    plugin.service._selectable_storage_enabled = True
    state = SimpleNamespace(inside=False, checks=[])

    async def get_head(path):
        assert state.inside
        state.checks.append(path)
        if mode == "query_error":
            raise OSError("private backend credential")
        return (
            object()
            if mode == "ancestor" and path == "life_engine_workspace/cache"
            else None
        )

    async def get_binding(path):
        assert state.inside
        return (
            object()
            if mode in {"active", "released"}
            or (mode == "released_ancestor" and path == "life_engine_workspace/cache")
            else None
        )

    @asynccontextmanager
    async def fence():
        if mode == "rejected_fence":
            raise RuntimeError("private backend credential")
        state.inside = True
        try:
            yield
        finally:
            state.inside = False

    store = SimpleNamespace(
        get_head=get_head, get_path_binding=get_binding, workspace_namespace_fence=fence
    )
    for name, missing in (
        ("get_head", "missing_head"),
        ("get_path_binding", "missing_binding"),
        ("workspace_namespace_fence", "missing_fence"),
    ):
        if mode == missing:
            delattr(store, name)
    plugin.service._subject_document_store = store
    original = screen.project_exact_bytes

    def checked_publish(*args, **kwargs):
        assert state.inside
        return original(*args, **kwargs)

    monkeypatch.setattr(screen, "project_exact_bytes", checked_publish)
    if mode in {"success", "released_ancestor"}:
        assert (await screen._save_latest_cache(plugin, b"image"))["status"] == "saved"
        assert (tmp_path / "cache/one.png").read_bytes() == b"image"
    else:
        with pytest.raises(screen.ScreenCacheSafetyError) as raised:
            await screen._save_latest_cache(plugin, b"image")
        assert "private" not in str(raised.value)
        assert not list(tmp_path.iterdir())
    assert not state.inside


@pytest.mark.parametrize("mode", ["parent", "target", "dangling"])
async def test_symlinks_never_followed(tmp_path, mode):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    sentinel = outside / "original"
    sentinel.write_bytes(b"external original")
    if mode == "parent":
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
        relative = "linked/original"
    else:
        relative = "cache.bin"
        (workspace / relative).symlink_to(
            sentinel if mode == "target" else outside / "missing"
        )
    with pytest.raises(screen.ScreenCacheSafetyError, match="Symlink"):
        await screen._save_latest_cache(_plugin(workspace, relative), b"image")
    assert sentinel.read_bytes() == b"external original"
    assert not (outside / "missing").exists()


async def test_first_publish_racing_equal_unknown_file_is_not_claimed(
    tmp_path, monkeypatch
):
    plugin = _plugin(tmp_path, "cache.bin")
    original = screen.project_exact_bytes
    target = tmp_path / "cache.bin"

    def raced(*args, **kwargs):
        target.write_bytes(b"image")
        return original(*args, **kwargs)

    monkeypatch.setattr(screen, "project_exact_bytes", raced)
    with pytest.raises(file_io.WorkspaceFileConflict):
        await screen._save_latest_cache(plugin, b"image")
    assert target.read_bytes() == b"image"
    assert plugin._life_latest_screen_cache_state.target is None


async def test_parent_swap_during_publish_writes_no_outside_bytes(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    original = file_io._new_inode

    def swapped(target, content):
        (workspace / "cache").rename(workspace / "moved")
        (workspace / "cache").symlink_to(outside, target_is_directory=True)
        return original(target, content)

    monkeypatch.setattr(file_io, "_new_inode", swapped)
    with pytest.raises(file_io.WorkspaceFileConflict):
        await screen._save_latest_cache(_plugin(workspace, "cache/one.png"), b"image")
    assert not list(outside.iterdir())
    assert not list((workspace / "moved").iterdir())


async def test_concurrent_same_process_cache_has_single_owner(tmp_path):
    plugin = _plugin(tmp_path, "cache.bin")
    receipts = await asyncio.gather(
        *(
            screen._save_latest_cache(plugin, content)
            for content in (b"first", b"second")
        )
    )
    assert all(item["status"] == "saved" for item in receipts)
    assert plugin._life_latest_screen_cache_state.content_hash == _sha(
        (tmp_path / "cache.bin").read_bytes()
    )
    assert not list(tmp_path.rglob(".elysium-quarantine-*"))


async def test_cancelled_cache_write_is_joined_before_lock_release(
    tmp_path, monkeypatch
):
    plugin = _plugin(tmp_path, "cache.bin")
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original = screen.project_exact_bytes

    def delayed(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(screen, "project_exact_bytes", delayed)
    task = asyncio.create_task(screen._save_latest_cache(plugin, b"image"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert plugin._life_latest_screen_cache_state.lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not plugin._life_latest_screen_cache_state.lock.locked()
    assert plugin._life_latest_screen_cache_state.target is None
    assert (tmp_path / "cache.bin").read_bytes() == b"image"


async def test_cache_durability_failure_keeps_bytes_without_claiming_owner(
    tmp_path, monkeypatch
):
    plugin = _plugin(tmp_path, "cache.bin")
    target = tmp_path / "cache.bin"
    original = os.fsync

    def failed(fd):
        if target.exists() and os.readlink(f"/proc/self/fd/{fd}") == str(tmp_path):
            raise OSError("private synthetic disk detail")
        return original(fd)

    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(file_io.WorkspaceFileError) as raised:
        await screen._save_latest_cache(plugin, b"image")
    receipt = screen._failed_cache_receipt(raised.value)
    assert "published_durability_unconfirmed" in receipt["detail"]
    assert "private" not in str(receipt)
    assert target.read_bytes() == b"image"
    assert plugin._life_latest_screen_cache_state.target is None


def test_recovery_receipt_retains_path_and_unknown_exception_hides_private_text(
    tmp_path,
):
    recovery = tmp_path / "retained-original"
    receipt = screen._failed_cache_receipt(
        file_io.WorkspaceFileRecoveryRequired(
            "cache", recovery, reason="restoration_conflict"
        )
    )
    assert receipt["status"] == "failed" and receipt["detail"] == "recovery_required"
    assert receipt["recovery_path"] == str(recovery)
    assert "private" not in str(
        screen._failed_cache_receipt(OSError("private content"))
    )


@pytest.mark.parametrize("mode", ["saved", "blocked", "disabled"])
async def test_optional_cache_failure_preserves_capture_and_tool_observation(
    tmp_path, monkeypatch, mode
):
    blocked = mode == "blocked"
    workspace, capture_temp = tmp_path / "workspace", tmp_path / "capture-temp"
    workspace.mkdir()
    capture_temp.mkdir()
    plugin = _plugin(workspace, "SOUL.md" if blocked else "cache.bin")
    if mode == "disabled":
        plugin.config.screen.save_latest = False
        plugin.service = None

        async def forbidden_cache(*args, **kwargs):
            pytest.fail("disabled save_latest must not access the cache or registry")

        monkeypatch.setattr(screen, "_save_latest_cache", forbidden_cache)
    original_media = tmp_path / "original-media.bin"
    original_media.write_bytes(b"unrelated original media")
    monkeypatch.setattr(screen.tempfile, "tempdir", str(capture_temp))

    async def fake_capture(path, cfg):
        path.write_bytes(b"synthetic image only")
        return True, ""

    async def fake_observation(plugin, captured, **kwargs):
        assert base64.b64decode(captured.base64_data) == b"synthetic image only"
        return "synthetic", "Synthetic observation remains available."

    monkeypatch.setattr(screen, "_capture_with_pil", fake_capture)
    monkeypatch.setattr(screen, "_is_blank_image", lambda path: False)
    monkeypatch.setattr(screen, "_resize_and_resave", lambda path, cfg: (2, 2, "png"))
    monkeypatch.setattr(screen, "_is_supported_image_data", lambda data: True)
    monkeypatch.setattr(screen, "_observe_screen", fake_observation)
    ok, result = await screen.LifeEngineViewScreenTool(plugin=plugin).execute()
    assert ok is True, result
    assert result["observation"] == "Synthetic observation remains available."
    assert result["screen_size"] == "2x2"
    if mode == "disabled":
        assert "cache" not in result
    else:
        assert result["cache"]["status"] == ("failed" if blocked else "saved")
    assert ("saved_path" in result) is (mode == "saved")
    assert not (workspace / "SOUL.md").exists()
    assert not list(capture_temp.iterdir())
    assert original_media.read_bytes() == b"unrelated original media"
