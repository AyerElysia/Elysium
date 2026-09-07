"""Synthetic HTTP and temporary files only; never open production stores/network."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import socket
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools import download_tools as downloads


@pytest.fixture(autouse=True)
def _deny_network(monkeypatch):
    for name in ("connect", "connect_ex", "bind"):
        original = getattr(socket.socket, name)

        def guarded(self, address, *args, _original=original, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                pytest.fail("download safety tests must not access real network/ports")
            return _original(self, address, *args, **kwargs)

        monkeypatch.setattr(socket.socket, name, guarded)


def _plugin(tmp_path):
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = SimpleNamespace(
        _selectable_storage_enabled=False, _subject_document_store=None
    )
    return SimpleNamespace(config=config, service=service)


class _Response:
    def __init__(
        self,
        chunks=(),
        *,
        status=200,
        headers=None,
        url="https://example.test/file.bin",
    ):
        self.chunks = chunks
        self.status = status
        self.headers = headers or {}
        self.url = url
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        assert size == 65536
        for item in self.chunks:
            if isinstance(item, BaseException):
                raise item
            if callable(item):
                result = item()
                if inspect.isawaitable(result):
                    await result
            else:
                yield item


@pytest.fixture
def fake_http(monkeypatch):
    state = SimpleNamespace(response=_Response([b"external bytes"]), requests=[])

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            state.requests.append((url, kwargs))
            return state.response

    monkeypatch.setattr(aiohttp, "ClientSession", Session)
    return state


def _parts(tmp_path):
    return list(tmp_path.rglob(".nucleus-download-*.part"))


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://user:secret@example.test:8443/file?key=secret#private",
            "https://example.test:8443/file",
        ),
        ("http://user:secret@[::1]:18000/file?key=secret", "http://[::1]:18000/file"),
        ("https://example.test/file?key=secret", "https://example.test/file"),
    ],
)
def test_safe_url_keeps_endpoint_without_credentials(url, expected):
    assert downloads._safe_url(url) == expected


@asynccontextmanager
async def _fake_fence():
    yield


@pytest.mark.parametrize("save_path", ["", "assets/", "explicit.bin", "absolute"])
async def test_download_preserves_path_api_and_external_provenance(
    tmp_path, fake_http, save_path
):
    if save_path == "absolute":
        save_path = str(tmp_path / "absolute.bin")
    payload = "合成外部字节\n".encode()
    fake_http.response = _Response(
        [payload[:5], payload[5:]], headers={"Content-Length": str(len(payload))}
    )
    ok, result = await downloads.LifeEngineDownloadTool(
        plugin=_plugin(tmp_path)
    ).execute(
        "https://user:secret@example.test/file.bin?token=secret#private", save_path
    )
    assert ok is True, result
    target = Path(result["saved_to"])
    assert target.read_bytes() == payload
    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert result["source_origin"] == "external_download"
    assert "semantic_actor_id" not in result
    assert "secret" not in str(result) and "private" not in str(result)
    assert result["size_bytes"] == len(payload)
    assert target.stat().st_mode & 0o777 == 0o600
    assert not _parts(tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "SOUL.md",
        "SOUL.md/child.bin",
        "USER.md",
        "MEMORY.md",
        "notes/new.bin",
        "diaries/new.dat",
        "notes",
        "diaries",
        "thoughts/streams.json",
        "runtime/proactive/proactive.sqlite3",
        "runtime/proactive/proactive.sqlite3-wal",
        "runtime/proactive/authority.json",
        "runtime/proactive/authority.json/child.bin",
        "runtime/proactive/backend-binding.json",
        "../escape.bin",
    ],
)
async def test_protected_targets_rejected_before_request(tmp_path, fake_http, path):
    ok, result = await downloads.LifeEngineDownloadTool(
        plugin=_plugin(tmp_path)
    ).execute("https://example.test/file.bin", path)
    assert ok is False, result
    assert not fake_http.requests
    assert not list(tmp_path.iterdir())


async def test_existing_target_never_truncated(tmp_path, fake_http):
    target = tmp_path / "existing.txt"
    target.write_bytes(b"original")
    before = target.stat().st_mtime_ns
    ok, _ = await downloads.LifeEngineDownloadTool(plugin=_plugin(tmp_path)).execute(
        "https://example.test/file.bin", target.name
    )
    assert ok is False
    assert target.read_bytes() == b"original"
    assert target.stat().st_mtime_ns == before
    assert not fake_http.requests


async def test_registered_missing_ancestor_cannot_be_created_as_a_directory(
    tmp_path, fake_http
):
    plugin = _plugin(tmp_path)
    plugin.service._selectable_storage_enabled = True

    async def get_head(path):
        return object() if path == "life_engine_workspace/registered.any" else None

    plugin.service._subject_document_store = SimpleNamespace(get_head=get_head)
    ok, result = await downloads.LifeEngineDownloadTool(plugin=plugin).execute(
        "https://example.test/file.bin", "registered.any/child.bin"
    )
    assert ok is False and "RegisteredDocument" in str(result)
    assert not fake_http.requests
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "mode", ["registered", "missing_store", "missing_getter", "query_error"]
)
async def test_dynamic_registry_is_fail_closed_without_projection(
    tmp_path, fake_http, mode
):
    plugin = _plugin(tmp_path)
    plugin.service._selectable_storage_enabled = True
    if mode == "registered":
        getter = AsyncMock(return_value=object())
        plugin.service._subject_document_store = SimpleNamespace(get_head=getter)
    elif mode == "missing_getter":
        plugin.service._subject_document_store = SimpleNamespace()
    elif mode == "query_error":
        plugin.service._subject_document_store = SimpleNamespace(
            get_head=AsyncMock(side_effect=OSError("secret"))
        )
    ok, result = await downloads.LifeEngineDownloadTool(plugin=plugin).execute(
        "https://example.test/file.bin", "registered.any-extension"
    )
    assert ok is False
    assert not fake_http.requests
    assert "secret" not in str(result)
    assert not list(tmp_path.iterdir())
    if mode == "registered":
        getter.assert_awaited_once_with(
            "life_engine_workspace/registered.any-extension"
        )


async def test_registration_during_stream_blocks_publication(tmp_path, fake_http):
    plugin = _plugin(tmp_path)
    plugin.service._selectable_storage_enabled = True
    plugin.service._subject_document_store = SimpleNamespace(
        get_head=AsyncMock(side_effect=[None, None, object()]),
        get_path_binding=AsyncMock(return_value=None),
        workspace_namespace_fence=_fake_fence,
    )
    ok, result = await downloads.LifeEngineDownloadTool(plugin=plugin).execute(
        "https://example.test/file.bin", "reference.bin"
    )
    assert ok is False and "RegisteredDocument" in str(result)
    assert not (tmp_path / "reference.bin").exists()
    assert not _parts(tmp_path)


@pytest.mark.parametrize(
    "mode", ["missing", "rejected", "success", "registered_on_acquire"]
)
async def test_selected_publication_requires_shared_registry_fence(
    tmp_path, fake_http, monkeypatch, mode
):
    plugin = _plugin(tmp_path)
    plugin.service._selectable_storage_enabled = True
    state = SimpleNamespace(inside=False, registered=False, read_states=[])

    async def get_head(path):
        state.read_states.append(state.inside)
        return object() if state.registered else None

    @asynccontextmanager
    async def fence():
        if mode == "rejected":
            raise RuntimeError("private backend configuration")
        state.inside = True
        if mode == "registered_on_acquire":
            state.registered = True
        try:
            yield
        finally:
            state.inside = False

    plugin.service._subject_document_store = SimpleNamespace(
        get_head=get_head, get_path_binding=AsyncMock(return_value=None)
    )
    if mode != "missing":
        plugin.service._subject_document_store.workspace_namespace_fence = fence
    original = downloads._DownloadStaging.publish

    def checked_publish(self):
        assert state.inside
        return original(self)

    monkeypatch.setattr(downloads._DownloadStaging, "publish", checked_publish)
    ok, result = await downloads.LifeEngineDownloadTool(plugin=plugin).execute(
        "https://example.test/file.bin", "new.bin"
    )
    assert ok is (mode == "success"), result
    assert not state.inside
    assert "private backend configuration" not in str(result)
    if mode == "success":
        assert state.read_states == [False, False, True]
        assert (tmp_path / "new.bin").read_bytes() == b"external bytes"
    else:
        assert not (tmp_path / "new.bin").exists()
        assert (
            "RegisteredDocument" in str(result)
            if mode == "registered_on_acquire"
            else "Fence" in str(result)
        )
    assert not _parts(tmp_path)


@pytest.mark.parametrize(
    "mode", ["released", "active", "missing_binding", "released_ancestor"]
)
async def test_historical_managed_target_cannot_be_external_download(
    tmp_path, fake_http, mode
):
    plugin = _plugin(tmp_path)
    plugin.service._selectable_storage_enabled = True
    store = SimpleNamespace(
        get_head=AsyncMock(return_value=None), workspace_namespace_fence=_fake_fence
    )
    if mode != "missing_binding":

        async def binding(path):
            if mode == "released_ancestor":
                return object() if path == "life_engine_workspace/old" else None
            return SimpleNamespace(document_id="old-doc", released=mode == "released")

        store.get_path_binding = binding
    plugin.service._subject_document_store = store
    target = "old/child.bin" if mode == "released_ancestor" else "new.bin"
    ok, result = await downloads.LifeEngineDownloadTool(plugin=plugin).execute(
        "https://example.test/file.bin", target
    )
    assert ok is (mode == "released_ancestor"), result
    if ok:
        assert (tmp_path / target).read_bytes() == b"external bytes"
    else:
        assert not fake_http.requests
        assert not list(tmp_path.iterdir())
        assert "BindingRegistry" in str(result) or "HistoricalManagedPath" in str(
            result
        )


async def test_redirect_filename_rechecks_runtime_protection(tmp_path, fake_http):
    fake_http.response = _Response([b"bad"], url="https://example.test/streams.json")
    ok, _ = await downloads.LifeEngineDownloadTool(plugin=_plugin(tmp_path)).execute(
        "https://example.test/ordinary.bin", "thoughts/"
    )
    assert ok is False
    assert len(fake_http.requests) == 1
    assert not (tmp_path / "thoughts").exists()


@pytest.mark.parametrize("mode", ["parent", "target", "dangling"])
async def test_symlink_components_rejected_without_network(tmp_path, fake_http, mode):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if mode == "parent":
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
        save_path = "linked/new.bin"
    else:
        (workspace / "linked").symlink_to(
            outside / ("absent" if mode == "dangling" else "file.bin")
        )
        if mode == "target":
            (outside / "file.bin").write_bytes(b"outside")
        save_path = "linked"
    ok, _ = await downloads.LifeEngineDownloadTool(plugin=_plugin(workspace)).execute(
        "https://example.test/file.bin", save_path
    )
    assert ok is False
    assert not fake_http.requests
    assert not (outside / "new.bin").exists()


@pytest.mark.parametrize(
    "mode",
    [
        "size_header",
        "size_stream",
        "length",
        "invalid_length",
        "network",
        "timeout",
        "cancel",
        "partial",
    ],
)
async def test_failures_only_clean_owned_staging(tmp_path, fake_http, mode):
    keeper = tmp_path / ".nucleus-download-keep.part"
    keeper.write_bytes(b"someone else")
    chunks = [b"partial"]
    headers = {}
    status = 200
    if mode == "size_header":
        headers["Content-Length"] = str(1024 * 1024 + 1)
    elif mode == "size_stream":
        chunks = [b"x" * 65536] * 17
    elif mode == "length":
        headers["Content-Length"] = "99"
    elif mode == "invalid_length":
        headers["Content-Length"] = "-1"
    elif mode == "network":
        chunks += [OSError("https://user:secret@example.test/?token=secret")]
    elif mode == "timeout":
        chunks += [TimeoutError()]
    elif mode == "cancel":
        chunks += [asyncio.CancelledError()]
    elif mode == "partial":
        status = 206
    fake_http.response = _Response(chunks, headers=headers, status=status)
    operation = downloads.LifeEngineDownloadTool(plugin=_plugin(tmp_path)).execute(
        "https://user:secret@example.test/file.bin?token=secret",
        "new.bin",
        max_size_mb=1,
    )
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        ok, result = await operation
        assert ok is False
        assert "secret" not in str(result)
    assert not (tmp_path / "new.bin").exists()
    assert _parts(tmp_path) == [keeper]
    assert keeper.read_bytes() == b"someone else"


@pytest.mark.parametrize("symlink", [False, True])
async def test_final_atomic_publish_never_clobbers_racing_target(
    tmp_path, fake_http, monkeypatch, symlink
):
    original = downloads._DownloadStaging.publish
    outsider = tmp_path / "untouched.bin"
    outsider.write_bytes(b"other data")

    def racing_publish(self):
        if symlink:
            self.target.symlink_to(outsider)
        else:
            self.target.write_bytes(b"winner")
        return original(self)

    monkeypatch.setattr(downloads._DownloadStaging, "publish", racing_publish)
    ok, result = await downloads.LifeEngineDownloadTool(
        plugin=_plugin(tmp_path)
    ).execute("https://example.test/file.bin", "race.bin")
    assert ok is False and "AlreadyExists" in str(result)
    target = tmp_path / "race.bin"
    assert target.is_symlink() if symlink else target.read_bytes() == b"winner"
    assert outsider.read_bytes() == b"other data"
    assert not _parts(tmp_path)


async def test_parent_swap_cannot_write_through_new_symlink(tmp_path, fake_http):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parent = workspace / "assets"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap():
        parent.rename(workspace / "moved")
        parent.symlink_to(outside, target_is_directory=True)

    fake_http.response = _Response([b"before", swap, b"after"])
    ok, _ = await downloads.LifeEngineDownloadTool(plugin=_plugin(workspace)).execute(
        "https://example.test/file.bin", "assets/new.bin"
    )
    assert ok is False
    assert not list(outside.iterdir())
    assert not (workspace / "moved/new.bin").exists()
    assert not _parts(workspace / "moved")


async def test_unnamed_staging_neither_publishes_nor_deletes_foreign_part_file(
    tmp_path, fake_http
):
    replaced = []

    def replace():
        assert not _parts(tmp_path)
        stage = tmp_path / ".nucleus-download-intruder.part"
        stage.write_bytes(b"intruder")
        replaced.append(stage)

    fake_http.response = _Response([b"first", replace, b"second"])
    ok, result = await downloads.LifeEngineDownloadTool(
        plugin=_plugin(tmp_path)
    ).execute("https://example.test/file.bin", "new.bin")
    assert ok is True, result
    assert (tmp_path / "new.bin").read_bytes() == b"firstsecond"
    assert replaced[0].read_bytes() == b"intruder"


async def test_two_concurrent_downloads_have_exactly_one_winner(tmp_path, fake_http):
    tool = downloads.LifeEngineDownloadTool(plugin=_plugin(tmp_path))
    results = await asyncio.gather(
        *[tool.execute("https://example.test/file.bin", "race.bin") for _ in range(2)]
    )
    assert sum(ok for ok, _ in results) == 1
    assert (tmp_path / "race.bin").read_bytes() == b"external bytes"
    assert not _parts(tmp_path)


async def test_cancelled_disk_worker_is_drained_before_resource_cleanup():
    started = asyncio.Event()
    release = threading.Event()
    completed = threading.Event()
    loop = asyncio.get_running_loop()

    def operation():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        completed.set()

    task = asyncio.create_task(downloads._owned_io(operation))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()


async def test_empty_download_and_existing_directory_api(tmp_path, fake_http):
    (tmp_path / "existing-dir").mkdir()
    fake_http.response = _Response([], headers={"Content-Length": "0"})
    ok, result = await downloads.LifeEngineDownloadTool(
        plugin=_plugin(tmp_path)
    ).execute("https://example.test/file.bin", "existing-dir")
    assert ok is True, result
    assert (tmp_path / "existing-dir/file.bin").read_bytes() == b""
    assert not _parts(tmp_path)
