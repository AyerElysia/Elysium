"""Temporary-process checks for manual-only Spark control entry points."""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from scripts.start_elysium_service import check_not_running, start

ROOT = Path(__file__).resolve().parents[2]


def _fixture_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    (repository / "data/runtime").mkdir(parents=True)
    main = repository / "main.py"
    main.write_text(
        "import fcntl, signal, time\n"
        "signal.signal(signal.SIGINT, lambda *args: exit(0))\n"
        "lock = open('data/runtime/elysium.lock', 'a+b')\n"
        "fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "while True: time.sleep(0.1)\n",
        encoding="utf-8",
    )
    launcher = repository / "run_elysium.sh"
    launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" main.py\n', encoding="utf-8")
    launcher.chmod(0o700)
    return repository


def test_live_main_lock_refuses_start_without_changing_lock(tmp_path: Path) -> None:
    repository = _fixture_repository(tmp_path)
    lock = repository / "data/runtime/elysium.lock"
    lock.write_bytes(b"existing-owner-metadata\n")
    with lock.open("rb") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            check_not_running(repository)
    assert lock.read_bytes() == b"existing-owner-metadata\n"


@pytest.mark.parametrize("existing", ["tmux.sock", "main.pid"])
def test_existing_runtime_is_not_replaced(tmp_path: Path, existing: str) -> None:
    repository = _fixture_repository(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / existing).write_bytes(b"preserve")
    with pytest.raises(RuntimeError, match="already contains"):
        start(repository, runtime)
    assert (runtime / existing).read_bytes() == b"preserve"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required")
def test_actual_tmux_main_pid_and_no_automatic_restart(tmp_path: Path) -> None:
    repository = _fixture_repository(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket = runtime / "tmux.sock"
    try:
        pid = start(repository, runtime)
        assert (runtime / "main.pid").read_text().strip() == str(pid)
        assert (Path("/proc") / str(pid) / "cwd").resolve() == repository
        deadline = time.monotonic() + 3
        while True:
            try:
                check_not_running(repository)
            except RuntimeError:
                break
            if time.monotonic() >= deadline:
                pytest.fail("Fixture main did not acquire its lock")
            time.sleep(0.02)
        # This is our temporary fixture child, verified above, not the service.
        os.kill(pid, signal.SIGINT)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not Path(f"/proc/{pid}").exists():
                break
            time.sleep(0.02)
        process = Path(f"/proc/{pid}")
        assert not process.exists(), {
            "status": process.joinpath("status").read_text(),
            "pane": subprocess.check_output(
                ["tmux", "-S", str(socket), "list-panes", "-t", "=elysium",
                 "-F", "#{pane_dead}|#{pane_dead_status}|#{pane_dead_signal}"],
                text=True,
            ),
        }
        state = subprocess.check_output(
            ["tmux", "-S", str(socket), "list-panes", "-t", "=elysium",
             "-F", "#{pane_dead}|#{pane_dead_status}"], text=True,
        ).strip()
        assert state == "1|0"
        check_not_running(repository)
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket), "kill-session", "-t", "=elysium"],
            capture_output=True, check=False,
        )


def test_attach_refuses_ambiguous_instances_without_starting(tmp_path: Path) -> None:
    fake_tmux = tmp_path / "tmux"
    calls = tmp_path / "calls"
    fake_tmux.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit 0\n',
        encoding="utf-8",
    )
    fake_tmux.chmod(0o700)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/elysia_attach.sh")],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert "Multiple Elysium" in result.stderr
    assert "new-session" not in calls.read_text()
    assert "attach-session" not in calls.read_text()


def test_direct_service_helper_does_not_start_without_systemd() -> None:
    environment = dict(os.environ)
    environment.pop("INVOCATION_ID", None)
    environment.pop("RUNTIME_DIRECTORY", None)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/start_elysium_service.py")],
        env=environment, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "through elysium.service" in result.stderr


def test_unit_is_manual_only_and_tracks_actual_pid() -> None:
    unit = (ROOT / "services/elysium/elysium.service").read_text()
    assert "Type=exec" in unit
    assert "PIDFile=" not in unit
    assert "Restart=no" in unit
    assert "KillSignal=SIGINT" in unit
    assert "SendSIGKILL=no" in unit
    assert "\n[Install]" not in unit
    assert "RemainAfterExit=yes" not in unit


@pytest.mark.skipif(
    os.environ.get("ELYSIUM_TEST_USER_SYSTEMD") != "1",
    reason="Opt-in isolated user-systemd fixture, never the formal service",
)
@pytest.mark.parametrize("action", ["exit", "restart"])
def test_native_systemd_tracks_main_exit_and_does_not_restart(
    tmp_path: Path, action: str,
) -> None:
    repository = _fixture_repository(tmp_path)
    # A nonzero, spontaneous fixture exit must remain visible to systemd.
    main = repository / "main.py"
    if action == "exit":
        main.write_text(main.read_text().replace("exit(0)", "exit(42)"))
    (repository / "scripts").mkdir()
    helper = repository / "scripts/start_elysium_service.py"
    shutil.copyfile(ROOT / "scripts/start_elysium_service.py", helper)
    name = f"elysium-control-test-{uuid.uuid4().hex}"
    unit = f"{name}.service"
    runtime = Path(f"/run/user/{os.getuid()}") / name
    try:
        result = subprocess.run(
            ["systemd-run", "--user", "--unit", unit,
             "--property=Type=exec", "--property=Restart=no",
             "--property=KillSignal=SIGINT", "--property=KillMode=mixed",
             "--property=TimeoutStopSec=5", "--property=SendSIGKILL=no",
             f"--property=RuntimeDirectory={name}",
             "/usr/bin/python3", str(helper)],
            capture_output=True, text=True, check=False, timeout=15,
        )
        assert result.returncode == 0, result.stderr
        deadline = time.monotonic() + 5
        while not (runtime / "main.pid").exists():
            if time.monotonic() >= deadline:
                pytest.fail("Service did not publish its owned main PID")
            time.sleep(0.02)
        pid = int((runtime / "main.pid").read_text())
        status = subprocess.check_output(
            ["systemctl", "--user", "show", unit, "--property=MainPID", "--value"],
            text=True,
        ).strip()
        assert int(status) > 0
        assert int(status) != pid  # systemd owns the foreground signal relay
        deadline = time.monotonic() + 3
        while True:
            try:
                check_not_running(repository)
            except RuntimeError:
                break
            if time.monotonic() >= deadline:
                pytest.fail("Fixture did not acquire its own main lock")
            time.sleep(0.02)
        if action == "restart":
            subprocess.run(
                ["systemctl", "--user", "restart", unit],
                check=True, capture_output=True, timeout=10,
            )
            assert not Path(f"/proc/{pid}").exists()
            deadline = time.monotonic() + 5
            while not (runtime / "main.pid").exists():
                if time.monotonic() >= deadline:
                    pytest.fail("Manual restart did not publish a new owned main PID")
                time.sleep(0.02)
            restarted_pid = int((runtime / "main.pid").read_text())
            assert restarted_pid != pid
            subprocess.run(
                ["systemctl", "--user", "stop", unit],
                check=True, capture_output=True, timeout=10,
            )
            assert not Path(f"/proc/{restarted_pid}").exists()
            assert not runtime.exists()
            return
        os.kill(pid, signal.SIGINT)
        deadline = time.monotonic() + 5
        while True:
            status = subprocess.check_output(
                ["systemctl", "--user", "show", unit,
                 "--property=ActiveState,ExecMainStatus,NRestarts,MainPID"],
                text=True,
            )
            if "ActiveState=failed" in status:
                break
            if time.monotonic() >= deadline:
                pytest.fail(f"Native service did not observe main exit: {status}")
            time.sleep(0.05)
        assert "ExecMainStatus=42" in status
        assert "NRestarts=0" in status
        assert "MainPID=0" in status
        assert not Path(f"/proc/{pid}").exists()
    finally:
        for cleanup_action in ("stop", "reset-failed"):
            subprocess.run(
                ["systemctl", "--user", cleanup_action, unit],
                capture_output=True, check=False, timeout=10,
            )
