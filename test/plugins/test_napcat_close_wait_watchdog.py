"""Isolated watchdog contracts: no real socket, process control or storage."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.napcat_adapter.config import NapcatAdapterConfig
from plugins.napcat_adapter.plugin import NapcatAdapter

MODULE = "plugins.napcat_adapter.plugin"
SOCKET = ("192.0.2.1:40000 -> 198.18.0.1:443", 17)
IDENTITY = (1901, 1800, 1000, 42.0, "/owned/napcat")


@pytest.fixture
def adapter():
    instance = object.__new__(NapcatAdapter)
    instance._get_config = Mock(return_value=SimpleNamespace(
        napcat_server=SimpleNamespace(
            mode="reverse", port=8095, access_token="test-watchdog-token",
            close_wait_watchdog_enabled=True,
            close_wait_watchdog_process_dir="/owned/napcat",
        ),
    ))
    instance._ws = SimpleNamespace(
        local_address=("127.0.0.1", 8095),
        remote_address=("127.0.0.1", 50000), closed=False,
    )
    instance._watchdog_identity = None
    instance._close_wait_seen_at = {}
    instance._watchdog_cleanup_attempts = 0
    instance._watchdog_next_cleanup_at = 0.0
    instance._watchdog_unowned_reported = False
    instance._napcat_present = False
    instance._last_qq_message_time = 0.0
    instance._watchdog_task = None
    return instance


def test_new_config_is_disabled_without_process_directory():
    config = NapcatAdapterConfig.NapcatServerSection()
    assert config.close_wait_watchdog_enabled is False
    assert config.close_wait_watchdog_process_dir == ""


@pytest.mark.parametrize("flag", [False, None, "false", "true", 1])
async def test_disabled_or_non_boolean_flag_never_probes(adapter, flag):
    adapter._get_config().napcat_server.close_wait_watchdog_enabled = flag
    adapter._find_napcat_pid = AsyncMock()
    adapter._close_wait_seen_at[SOCKET] = 0.0
    await adapter._check_napcat_health()
    adapter._find_napcat_pid.assert_not_awaited()
    assert adapter._close_wait_seen_at == {}


def test_disabled_watchdog_never_starts_task(adapter, monkeypatch):
    adapter._get_config().napcat_server.close_wait_watchdog_enabled = False
    manager = Mock()
    monkeypatch.setattr(f"{MODULE}.get_task_manager", manager)
    adapter._start_watchdog()
    manager.assert_not_called()


def test_config_failure_disables_watchdog(adapter):
    adapter._get_config.side_effect = RuntimeError("unavailable")
    assert adapter._watchdog_config_enabled() is False


@pytest.mark.parametrize("uid_offset,wrong_directory", [(1, False), (0, True)])
def test_owner_requires_same_user_and_explicit_exact_directory(
    adapter, monkeypatch, tmp_path, uid_offset, wrong_directory,
):
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    adapter._get_config().napcat_server.close_wait_watchdog_process_dir = str(allowed)
    process = SimpleNamespace(
        uids=lambda: SimpleNamespace(real=os.getuid() + uid_offset),
        cwd=lambda: str(other if wrong_directory else allowed),
        ppid=lambda: 1800, create_time=lambda: 42.0,
    )
    monkeypatch.setattr(f"{MODULE}.psutil.Process", lambda _pid: process)
    assert adapter._read_watchdog_process_identity(1901) is None


def test_missing_target_directory_never_reads_process(adapter, monkeypatch):
    adapter._get_config().napcat_server.close_wait_watchdog_process_dir = ""
    lookup = Mock()
    monkeypatch.setattr(f"{MODULE}.psutil.Process", lookup)
    assert adapter._read_watchdog_process_identity(1901) is None
    lookup.assert_not_called()


async def test_pid_selection_matches_actual_authenticated_ws_peer(adapter):
    adapter._watchdog_command = AsyncMock(return_value=(
        b'0 0 127.0.0.1:49999 127.0.0.1:8095 users:(("qq",pid=9999,fd=4))\n'
        b'0 0 127.0.0.1:50000 127.0.0.1:8095 users:(("qq",pid=1901,fd=5))\n'
    ))
    adapter._read_watchdog_process_identity = Mock(return_value=IDENTITY)
    assert await adapter._find_napcat_pid(8095) == 1901
    adapter._read_watchdog_process_identity.assert_called_once_with(1901)


@pytest.mark.parametrize("peer,token", [
    (("172.18.0.2", 50000), "test-token"), (("127.0.0.1", 50000), ""),
])
async def test_remote_container_or_unauthenticated_peer_is_never_owned(
    adapter, peer, token,
):
    adapter._ws.remote_address = peer
    adapter._get_config().napcat_server.access_token = token
    adapter._watchdog_command = AsyncMock()
    assert await adapter._find_napcat_pid(8095) is None
    adapter._watchdog_command.assert_not_awaited()


async def test_multiple_process_owners_are_ambiguous(adapter):
    adapter._watchdog_command = AsyncMock(return_value=(
        b'0 0 127.0.0.1:50000 127.0.0.1:8095 users:(("qq",pid=1901,fd=5),("qq",pid=1902,fd=5))\n'
    ))
    adapter._read_watchdog_process_identity = Mock()
    assert await adapter._find_napcat_pid(8095) is None
    adapter._read_watchdog_process_identity.assert_not_called()


async def test_cleanup_requires_same_process_lifetime_and_current_socket(
    adapter, monkeypatch,
):
    adapter._watchdog_identity = IDENTITY
    adapter._find_napcat_pid = AsyncMock(return_value=1901)
    adapter._read_watchdog_process_identity = Mock(return_value=IDENTITY)
    adapter._get_close_wait_sockets = AsyncMock(return_value=[SOCKET])
    adapter._close_wait_seen_at[SOCKET] = 0.0
    monkeypatch.setattr(f"{MODULE}.time.monotonic", lambda: 100.0)
    assert await adapter._validated_watchdog_targets([SOCKET]) == [SOCKET]

    adapter._read_watchdog_process_identity.return_value = (
        *IDENTITY[:3], 99.0, IDENTITY[4],
    )
    assert await adapter._validated_watchdog_targets([SOCKET]) == []
    adapter._read_watchdog_process_identity.return_value = IDENTITY
    adapter._get_close_wait_sockets.return_value = []
    assert await adapter._validated_watchdog_targets([SOCKET]) == []


async def test_same_socket_must_be_observed_persistently_not_just_business_silence(
    adapter, monkeypatch,
):
    clock = [0.0]
    monkeypatch.setattr(f"{MODULE}.time.monotonic", lambda: clock[0])
    adapter._find_napcat_pid = AsyncMock(return_value=1901)
    adapter._read_watchdog_process_identity = Mock(return_value=IDENTITY)
    adapter._get_close_wait_sockets = AsyncMock(return_value=[SOCKET])
    adapter._kill_close_wait_sockets = AsyncMock(return_value=0)
    await adapter._check_napcat_health()
    clock[0] = 89.0
    await adapter._check_napcat_health()
    adapter._kill_close_wait_sockets.assert_not_awaited()
    clock[0] = 91.0
    await adapter._check_napcat_health()
    adapter._kill_close_wait_sockets.assert_awaited_once_with([SOCKET])


async def test_process_identity_change_restarts_observation_age(adapter, monkeypatch):
    adapter._watchdog_identity = IDENTITY
    adapter._close_wait_seen_at[SOCKET] = 0.0
    adapter._find_napcat_pid = AsyncMock(return_value=1901)
    adapter._read_watchdog_process_identity = Mock(return_value=(
        *IDENTITY[:3], 99.0, IDENTITY[4],
    ))
    adapter._get_close_wait_sockets = AsyncMock(return_value=[SOCKET])
    adapter._kill_close_wait_sockets = AsyncMock()
    monkeypatch.setattr(f"{MODULE}.time.monotonic", lambda: 500.0)
    await adapter._check_napcat_health()
    adapter._kill_close_wait_sockets.assert_not_awaited()
    assert adapter._close_wait_seen_at[SOCKET] == 500.0


async def test_cleanup_attempts_have_backoff_and_finite_budget(adapter, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(f"{MODULE}.time.monotonic", lambda: clock[0])
    adapter._find_napcat_pid = AsyncMock(return_value=1901)
    adapter._read_watchdog_process_identity = Mock(return_value=IDENTITY)
    adapter._get_close_wait_sockets = AsyncMock(return_value=[SOCKET])
    adapter._kill_close_wait_sockets = AsyncMock(return_value=0)
    for now in (0, 100, 101, 192, 401, 402, 493, 1002, 1003, 3000):
        clock[0] = float(now)
        await adapter._check_napcat_health()
    assert adapter._kill_close_wait_sockets.await_count == 3
    assert adapter._watchdog_cleanup_attempts == 3


async def test_native_cleanup_is_exact_and_state_filtered(adapter):
    adapter._watchdog_identity = IDENTITY
    adapter._validated_watchdog_targets = AsyncMock(return_value=[SOCKET])
    adapter._watchdog_command = AsyncMock(return_value=b"")
    assert await adapter._kill_close_wait_native([SOCKET]) == 1
    adapter._watchdog_command.assert_awaited_once_with(
        "ss", "-K", "state", "close-wait",
        "src", "192.0.2.1:40000", "dst", "198.18.0.1:443",
    )


async def test_wsl_never_invokes_legacy_pidfd_shutdown(adapter):
    adapter._validated_watchdog_targets = AsyncMock(return_value=[SOCKET])
    adapter._detect_wsl2 = AsyncMock(return_value=True)
    adapter._kill_close_wait_wsl2 = AsyncMock()
    adapter._kill_close_wait_native = AsyncMock()
    assert await adapter._kill_close_wait_sockets([SOCKET]) == 0
    adapter._kill_close_wait_wsl2.assert_not_awaited()
    adapter._kill_close_wait_native.assert_not_awaited()


@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_diagnostic_timeout_and_cancel_reap_owned_subprocess(
    adapter, monkeypatch, failure,
):
    process = SimpleNamespace(
        returncode=None, kill=Mock(),
        communicate=AsyncMock(side_effect=[failure(), (b"", b"")]),
    )
    monkeypatch.setattr(
        f"{MODULE}.asyncio.create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    with pytest.raises(failure):
        await adapter._watchdog_command("ss", "-tnp")
    process.kill.assert_called_once()
    assert process.communicate.await_count == 2


def test_matching_owner_records_pid_parent_uid_start_and_cwd(
    adapter, monkeypatch, tmp_path,
):
    adapter._get_config().napcat_server.close_wait_watchdog_process_dir = str(tmp_path)
    process = SimpleNamespace(
        uids=lambda: SimpleNamespace(real=os.getuid()),
        cwd=lambda: str(tmp_path), ppid=lambda: 1800,
        create_time=lambda: 42.0,
    )
    monkeypatch.setattr(f"{MODULE}.psutil.Process", lambda _pid: process)
    assert adapter._read_watchdog_process_identity(1901) == (
        1901, 1800, os.getuid(), 42.0, str(tmp_path),
    )


async def test_one_recovery_attempt_never_cleans_multiple_connections(
    adapter, monkeypatch,
):
    other = ("192.0.2.1:40001 -> 198.18.0.2:443", 18)
    adapter._watchdog_identity = IDENTITY
    adapter._close_wait_seen_at = {SOCKET: 0.0, other: 0.0}
    adapter._find_napcat_pid = AsyncMock(return_value=1901)
    adapter._read_watchdog_process_identity = Mock(return_value=IDENTITY)
    adapter._get_close_wait_sockets = AsyncMock(return_value=[SOCKET, other])
    adapter._kill_close_wait_sockets = AsyncMock(return_value=0)
    monkeypatch.setattr(f"{MODULE}.time.monotonic", lambda: 100.0)
    await adapter._check_napcat_health()
    adapter._kill_close_wait_sockets.assert_awaited_once_with([SOCKET])
