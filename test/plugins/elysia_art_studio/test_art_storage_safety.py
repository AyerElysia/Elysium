"""Temporary synthetic bytes only; no generated media, private config or runtime."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.elysia_art_studio.config import ImageGeneratorConfig, VibeItemConfig
from plugins.elysia_art_studio.services import image_service as service_module
from plugins.elysia_art_studio.services import storage_paths as storage
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.storage import workspace_file_io as file_io
from src.app.plugin_system.api import plugin_api

DIRECTORIES = ("temp_images", "vibes", "command_images")
IMAGE = b"\x89PNG synthetic immutable artifact"
FIXED_UUID = "38cec42a-8810-4a19-9f93-a48e30e2a813"


@pytest.fixture(autouse=True)
def _deny_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("art storage tests cannot use real network or lifecycle")

    monkeypatch.setattr(service_module.aiohttp, "ClientSession", forbidden)
    monkeypatch.setattr(service_module, "get_task_manager", forbidden)
    monkeypatch.setattr(plugin_api, "get_plugin", lambda name: None)
    for name in ("connect", "connect_ex", "bind"):
        original = getattr(socket.socket, name)

        def guarded(self, address, *args, _original=original, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                forbidden()
            return _original(self, address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    root = tmp_path / "art-plugin"
    root.mkdir()
    workspace = tmp_path / "subject-workspace"
    monkeypatch.setattr(storage, "configured_workspace_path", lambda: workspace)
    return root, workspace


def _service(root):
    service = service_module.ImageGeneratorService(
        SimpleNamespace(config=ImageGeneratorConfig())
    )
    service.plugin_dir = root
    service._start_queue_worker = AsyncMock()
    return service


@pytest.mark.parametrize("from_command", [False, True])
async def test_defaults_keep_existing_art_and_publish_exact_new_bytes(
    layout, from_command
):
    root, workspace = layout
    old = root / "old.data"
    old.write_bytes(b"unrelated old artifact")
    service = _service(root)
    await service.initialize()
    assert service.storage_status["status"] == "ready"
    assert service.storage_status["source_origin"] == "external_generated_artwork"
    service._start_queue_worker.assert_awaited_once()
    success, _, path = await service._save_image_from_bytes(
        IMAGE, from_command=from_command
    )
    assert success and path is not None
    target = Path(path)
    assert target.parent == root / ("command_images" if from_command else "temp_images")
    assert target.read_bytes() == IMAGE
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert all(
        stat.S_IMODE((root / name).stat().st_mode) == 0o700 for name in DIRECTORIES
    )
    assert old.read_bytes() == b"unrelated old artifact"
    assert not workspace.exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        ".",
        "..",
        "../subject",
        "one/../two",
        "one/./two",
        "one//two",
        "one/",
        "/absolute",
        "C:/outside",
        "C:\\outside",
        "\\\\server\\share",
        "one\\two",
        "one\x00two",
        None,
        32,
    ],
)
def test_all_three_paths_validated_before_first_mkdir(layout, invalid):
    root, workspace = layout
    with pytest.raises(storage.ArtStorageError, match="RelativeDirectoryRequired"):
        storage.prepare_storage(root, ("valid-one", "valid-two", invalid))
    assert list(root.iterdir()) == []
    assert not workspace.exists()


@pytest.mark.parametrize(
    "component", ["root", "root_parent", "directory", "nested", "dangling"]
)
def test_every_existing_symlink_component_rejected(layout, tmp_path, component):
    root, workspace = layout
    outside = tmp_path / "unowned"
    outside.mkdir()
    dirs = DIRECTORIES
    if component == "root":
        alias = tmp_path / "plugin-alias"
        alias.symlink_to(root, target_is_directory=True)
        root = alias
    elif component == "root_parent":
        alias = tmp_path / "parent-alias"
        alias.symlink_to(tmp_path, target_is_directory=True)
        root = alias / root.name
    elif component == "directory":
        (root / "command_images").symlink_to(outside, target_is_directory=True)
    elif component == "nested":
        (root / "alias").symlink_to(outside, target_is_directory=True)
        dirs = ("temp_images", "vibes", "alias/new")
    else:
        (root / "command_images").symlink_to(
            outside / "absent", target_is_directory=True
        )
    with pytest.raises(storage.ArtStorageError, match="Symlink"):
        storage.prepare_storage(root, dirs)
    assert list(outside.iterdir()) == []
    assert not (root / "temp_images").exists()
    assert not workspace.exists()


def test_file_cannot_be_used_as_directory_and_is_never_changed(layout):
    root, _ = layout
    old = root / "command_images"
    old.write_bytes(b"unowned bytes")
    before = old.stat()
    with pytest.raises(storage.ArtStorageError, match="ParentNotDirectory"):
        storage.prepare_storage(root, DIRECTORIES)
    assert old.read_bytes() == b"unowned bytes"
    assert old.stat().st_ino == before.st_ino
    assert not (root / "temp_images").exists()


@pytest.mark.parametrize(
    "relationship", ["equal", "subject_below", "plugin_below", "subject_alias"]
)
def test_entire_subject_workspace_is_excluded_both_directions(
    layout, tmp_path, monkeypatch, relationship
):
    root, _ = layout
    if relationship == "equal":
        workspace = root
    elif relationship == "subject_below":
        workspace = root / "innocent-extension.bin"
    elif relationship == "plugin_below":
        workspace = tmp_path
    else:
        workspace = tmp_path / "subject-alias"
        workspace.symlink_to(root, target_is_directory=True)
    monkeypatch.setattr(storage, "configured_workspace_path", lambda: workspace)
    with pytest.raises(storage.ArtStorageError, match="WorkspaceOverlap"):
        storage.prepare_storage(root, DIRECTORIES)
    assert list(root.iterdir()) == []


def test_similar_prefix_is_not_workspace_overlap(layout, monkeypatch):
    root, _ = layout
    monkeypatch.setattr(
        storage,
        "configured_workspace_path",
        lambda: root.with_name(root.name + "-subject"),
    )
    paths = storage.prepare_storage(root, DIRECTORIES)
    assert paths.temp_dir == root / "temp_images"


@pytest.mark.parametrize(
    "config", [None, SimpleNamespace(settings=SimpleNamespace(workspace_path="/"))]
)
def test_missing_validated_configuration_never_guesses_default(
    tmp_path, monkeypatch, config
):
    monkeypatch.setattr(
        plugin_api, "get_plugin", lambda name: SimpleNamespace(config=config)
    )
    with pytest.raises(storage.ArtStorageError, match="BoundaryUnavailable"):
        storage.configured_workspace_path()
    assert list(tmp_path.iterdir()) == []


def test_configured_boundary_reads_only_supplied_validated_path(tmp_path, monkeypatch):
    config = LifeEngineConfig()
    expected = tmp_path / "configured-subject"
    config.settings.workspace_path = str(expected)
    monkeypatch.setattr(
        plugin_api, "get_plugin", lambda name: SimpleNamespace(config=config)
    )
    assert storage.configured_workspace_path() == expected
    assert not expected.exists()


@pytest.mark.parametrize("kind", ["different", "equal", "empty", "symlink", "hardlink"])
async def test_uuid_collision_never_reclaims_or_overwrites_existing_bytes(
    layout, tmp_path, monkeypatch, kind
):
    root, _ = layout
    storage.prepare_storage(root, DIRECTORIES)
    target = root / "temp_images" / f"{FIXED_UUID}.png"
    outside = tmp_path / "other-original"
    outside.write_bytes(b"original outside the artifact domain")
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    else:
        target.write_bytes({"different": b"old", "equal": IMAGE, "empty": b""}[kind])
    before = target.lstat()
    original = target.read_bytes()
    monkeypatch.setattr(service_module.uuid, "uuid4", lambda: FIXED_UUID)
    success, _, path = await _service(root)._save_image_from_bytes(IMAGE)
    assert not success and path is None
    assert target.read_bytes() == original
    assert target.lstat().st_ino == before.st_ino
    assert outside.read_bytes() == b"original outside the artifact domain"


async def test_unknown_boundary_defers_storage_without_blocking_initialization(
    layout, monkeypatch
):
    root, workspace = layout

    def unavailable():
        raise storage.ArtStorageError("ArtWorkspaceBoundaryUnavailable")

    monkeypatch.setattr(storage, "configured_workspace_path", unavailable)
    service = _service(root)
    await service.initialize()
    assert service.storage_status["detail"] == "ArtWorkspaceBoundaryUnavailable"
    assert (
        service.temp_dir
        is service.vibe_storage_dir
        is service.command_images_dir
        is None
    )
    service._start_queue_worker.assert_awaited_once()
    assert list(root.iterdir()) == []
    assert service.list_vibe_files()[0] is False
    assert (await service.load_vibe_from_file("synthetic-user", "anything"))[0] is False
    monkeypatch.setattr(storage, "configured_workspace_path", lambda: workspace)
    success, _, path = await service._save_image_from_bytes(IMAGE)
    assert success and Path(path).read_bytes() == IMAGE
    assert service.storage_status["status"] == "ready"


async def test_storage_failure_blocks_external_generation_before_queue_work(layout):
    root, _ = layout
    service = _service(root)
    service.plugin.config.advanced.command_images_dir = "../outside"
    result = await service._generate_image_internal(
        "synthetic prompt", "synthetic-user"
    )
    assert result[0] is False and result[2] is None
    assert "RelativeDirectoryRequired" in result[1]
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("change", ["configuration", "workspace", "symlink"])
async def test_each_save_rechecks_boundary_after_successful_initialization(
    layout, tmp_path, monkeypatch, change
):
    root, workspace = layout
    service = _service(root)
    await service.initialize()
    if change == "configuration":
        service.plugin.config.advanced.temp_dir = "../outside"
    elif change == "workspace":
        monkeypatch.setattr(
            storage, "configured_workspace_path", lambda: root / "managed"
        )
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "temp_images").rmdir()
        (root / "temp_images").symlink_to(outside, target_is_directory=True)
    success, _, path = await service._save_image_from_bytes(IMAGE)
    assert not success and path is None
    assert (
        service.temp_dir
        is service.vibe_storage_dir
        is service.command_images_dir
        is None
    )
    assert not workspace.exists()
    assert list(tmp_path.rglob("*.png")) == []


async def test_partial_directory_failure_keeps_unowned_data_and_no_writable_state(
    layout, monkeypatch
):
    root, _ = layout
    service = _service(root)
    real_mkdir = file_io.os.mkdir

    def denied(path, *args, **kwargs):
        if path == "command_images":
            raise PermissionError("private operating-system diagnostic")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(file_io.os, "mkdir", denied)
    await service.initialize()
    assert (
        service.temp_dir
        is service.vibe_storage_dir
        is service.command_images_dir
        is None
    )
    assert service.storage_status["detail"] == "PermissionError"
    assert (root / "temp_images").is_dir() and (root / "vibes").is_dir()
    success, message, path = await service._save_image_from_bytes(IMAGE)
    assert not success and path is None
    assert "private operating-system" not in message
    assert list(root.rglob("*.png")) == []


async def test_directory_swap_before_publication_cannot_escape_to_subject(
    layout, monkeypatch
):
    root, workspace = layout
    workspace.mkdir()
    sentinel = workspace / "registered.any"
    sentinel.write_bytes(b"subject original")
    real_new = file_io._new_inode

    def swapped(target, content):
        directory = root / "temp_images"
        directory.rename(root / "detached-art-directory")
        directory.symlink_to(workspace, target_is_directory=True)
        return real_new(target, content)

    monkeypatch.setattr(file_io, "_new_inode", swapped)
    success, _, path = await _service(root)._save_image_from_bytes(IMAGE)
    assert not success and path is None
    assert sentinel.read_bytes() == b"subject original"
    assert list(workspace.iterdir()) == [sentinel]
    assert list((root / "detached-art-directory").iterdir()) == []


async def test_partial_write_never_publishes_partial_or_named_temporary_file(
    layout, monkeypatch
):
    root, _ = layout
    real_write = file_io.os.write
    writes = 0

    def failing_write(fd, content):
        nonlocal writes
        writes += 1
        if writes == 1:
            return real_write(fd, content[:3])
        raise OSError("private write diagnostic")

    monkeypatch.setattr(file_io.os, "write", failing_write)
    success, message, path = await _service(root)._save_image_from_bytes(IMAGE)
    assert not success and path is None
    assert "private" not in message
    assert writes == 2
    assert list((root / "temp_images").iterdir()) == []


async def test_published_but_unconfirmed_durability_retains_bytes_and_reports_uncertainty(
    layout, monkeypatch
):
    root, _ = layout
    monkeypatch.setattr(service_module.uuid, "uuid4", lambda: FIXED_UUID)
    real_fsync = file_io.os.fsync

    def fail_after_publication(fd):
        try:
            os.stat(f"{FIXED_UUID}.png", dir_fd=fd, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return real_fsync(fd)
        raise OSError("private fsync diagnostic")

    monkeypatch.setattr(file_io.os, "fsync", fail_after_publication)
    service = _service(root)
    success, message, path = await service._save_image_from_bytes(IMAGE)
    target = root / "temp_images" / f"{FIXED_UUID}.png"
    assert not success and path is None
    assert target.read_bytes() == IMAGE
    assert (
        service.storage_status["detail"] == "ArtStoragePublicationDurabilityUnconfirmed"
    )
    assert service.storage_status["retained_path"] == str(target)
    assert "private" not in message


async def test_repeated_cancellation_joins_owned_writer_and_keeps_state_unavailable(
    layout, monkeypatch
):
    root, _ = layout
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    real_new = file_io._new_inode

    def blocked(target, content):
        entered.set()
        assert release.wait(5), "test writer was not released"
        try:
            return real_new(target, content)
        finally:
            stopped.set()

    monkeypatch.setattr(file_io, "_new_inode", blocked)
    service = _service(root)
    task = asyncio.create_task(service._save_image_from_bytes(IMAGE))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done()
            assert service._storage_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert stopped.is_set() and not service._storage_lock.locked()
        assert (
            service.temp_dir
            is service.vibe_storage_dir
            is service.command_images_dir
            is None
        )
        assert service.storage_status["status"] == "unavailable"
        assert (
            service.storage_status["detail"]
            == "ArtStoragePublicationOutcomeUnconfirmed"
        )
        assert Path(service.storage_status["candidate_path"]).read_bytes() == IMAGE
        assert (await service._save_image_from_bytes(IMAGE))[0] is False
        assert len(list((root / "temp_images").iterdir())) == 1
        assert [path.read_bytes() for path in (root / "temp_images").iterdir()] == [
            IMAGE
        ]
        assert not any(
            pending.get_name() == "subject-workspace-owned-io"
            for pending in asyncio.all_tasks()
            if not pending.done()
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_concurrent_same_uuid_does_not_overwrite_first_publication(
    layout, monkeypatch
):
    root, _ = layout
    monkeypatch.setattr(service_module.uuid, "uuid4", lambda: FIXED_UUID)
    service = _service(root)
    results = await asyncio.gather(
        service._save_image_from_bytes(IMAGE),
        service._save_image_from_bytes(b"second artifact"),
    )
    assert [result[0] for result in results] == [True, False]
    assert (root / "temp_images" / f"{FIXED_UUID}.png").read_bytes() == IMAGE


def test_boundary_change_mid_prepare_blocks_publication(layout, monkeypatch):
    root, workspace = layout
    calls = 0

    def boundary():
        nonlocal calls
        calls += 1
        return workspace if calls == 1 else workspace.with_name("changed-workspace")

    monkeypatch.setattr(storage, "configured_workspace_path", boundary)
    with pytest.raises(storage.ArtStorageError, match="BoundaryChanged"):
        storage.prepare_storage(root, DIRECTORIES, content=IMAGE, filename="new.png")
    assert list((root / "temp_images").iterdir()) == []


def test_byte_budget_is_checked_before_creating_directories(layout, monkeypatch):
    root, _ = layout
    monkeypatch.setattr(storage, "DEFAULT_MAX_FILE_BYTES", 4)
    with pytest.raises(storage.ArtStorageError, match="ByteLimitExceeded"):
        storage.prepare_storage(root, DIRECTORIES, content=b"12345", filename="new.png")
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("existing", [False, True])
async def test_deferred_vibe_list_recovers_without_generating_or_creating_directories(
    layout, monkeypatch, existing
):
    root, workspace = layout
    if existing:
        (root / "vibes").mkdir()
        (root / "vibes" / "synthetic.png").write_bytes(IMAGE)

    def unavailable():
        raise storage.ArtStorageError("ArtWorkspaceBoundaryUnavailable")

    monkeypatch.setattr(storage, "configured_workspace_path", unavailable)
    service = _service(root)
    await service.initialize()
    assert service.list_vibe_files()[0] is False
    monkeypatch.setattr(storage, "configured_workspace_path", lambda: workspace)
    success, message = service.list_vibe_files()
    assert success
    assert ("synthetic.png" in message) is existing
    assert not (root / "temp_images").exists()
    assert not (root / "command_images").exists()
    assert (
        service.temp_dir
        is service.vibe_storage_dir
        is service.command_images_dir
        is None
    )


async def test_deferred_vibe_load_retries_boundary_without_prior_image_save(
    layout, monkeypatch
):
    root, workspace = layout
    (root / "vibes").mkdir()
    (root / "vibes" / "synthetic.png").write_bytes(IMAGE)

    def unavailable():
        raise storage.ArtStorageError("ArtWorkspaceBoundaryUnavailable")

    monkeypatch.setattr(storage, "configured_workspace_path", unavailable)
    service = _service(root)
    service._read_image_b64_from_vibe_file = lambda path: "synthetic bytes"
    service._encode_vibe = AsyncMock(return_value="synthetic encoding")
    await service.initialize()
    assert (await service.load_vibe_from_file("synthetic-user", "synthetic.png"))[
        0
    ] is False
    service._encode_vibe.assert_not_awaited()
    monkeypatch.setattr(storage, "configured_workspace_path", lambda: workspace)
    assert (await service.load_vibe_from_file("synthetic-user", "synthetic.png"))[
        0
    ] is True
    service._encode_vibe.assert_awaited_once()
    assert service.user_vibes["synthetic-user"][0]["data"] == "synthetic encoding"
    assert list((root / "temp_images").iterdir()) == []


@pytest.mark.parametrize("change", ["directory", "plugin_root"])
async def test_uncertain_receipt_uses_frozen_target_and_blocks_republication(
    layout, tmp_path, monkeypatch, change
):
    root, _ = layout
    service = _service(root)
    monkeypatch.setattr(service_module.uuid, "uuid4", lambda: FIXED_UUID)
    real_fsync = file_io.os.fsync

    def change_config_after_publication(fd):
        try:
            os.stat(f"{FIXED_UUID}.png", dir_fd=fd, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return real_fsync(fd)
        if change == "directory":
            service.plugin.config.advanced.temp_dir = "changed-directory"
        else:
            service.plugin_dir = tmp_path / "changed-plugin-root"
        raise OSError("private durability diagnostic")

    monkeypatch.setattr(file_io.os, "fsync", change_config_after_publication)
    assert (await service._save_image_from_bytes(IMAGE))[0] is False
    target = root / "temp_images" / f"{FIXED_UUID}.png"
    assert service.storage_status["retained_path"] == str(target)
    assert target.read_bytes() == IMAGE
    monkeypatch.setattr(file_io.os, "fsync", real_fsync)
    monkeypatch.setattr(service_module.uuid, "uuid4", lambda: "a-new-publication")
    second = await service._save_image_from_bytes(IMAGE)
    assert second[0] is False and "未自动重新发布" in second[1]
    assert service.storage_status["retained_path"] == str(target)
    assert list(tmp_path.rglob("*.png")) == [target]
    assert (
        service.temp_dir
        is service.vibe_storage_dir
        is service.command_images_dir
        is None
    )


async def test_deferred_configured_vibes_are_not_silently_omitted_on_first_generation(
    layout, monkeypatch
):
    root, workspace = layout
    service = _service(root)
    config = service.plugin.config
    config.api.api_keys = ["synthetic openai credential"]
    config.api.novelai_api_keys = ["synthetic novelai credential"]
    config.plugin.engine = "openai"
    config.vibe.always = [VibeItemConfig(file="synthetic.png")]
    config.vibe.selectable = [VibeItemConfig(file="synthetic-other.png")]
    service._load_preset_vibes = AsyncMock()
    service._load_selectable_vibes = AsyncMock()
    service._call_openai_api = AsyncMock(
        return_value=(False, "synthetic completion", None)
    )

    def unavailable():
        raise storage.ArtStorageError("ArtWorkspaceBoundaryUnavailable")

    monkeypatch.setattr(storage, "configured_workspace_path", unavailable)
    await service.initialize()
    assert service._preset_vibes_deferred
    service._load_preset_vibes.assert_not_awaited()
    service._load_selectable_vibes.assert_not_awaited()
    monkeypatch.setattr(storage, "configured_workspace_path", lambda: workspace)
    result = await service._generate_image_internal(
        "synthetic prompt", "synthetic-user"
    )
    assert result == (False, "synthetic completion", None)
    service._load_preset_vibes.assert_awaited_once_with(config.vibe.always)
    service._load_selectable_vibes.assert_awaited_once_with(config.vibe.selectable)
    assert not service._preset_vibes_deferred
