"""Run one attachable process for a manual Type=exec systemd service.

Remain in the foreground, forward stop signals to the exact owned child,
and return its real exit status. No retries, takeover or restart loop exists.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import select
import signal
import subprocess
import time
from pathlib import Path


def check_not_running(repository: Path) -> None:
    """Observe the existing main lock without truncating or replacing it."""
    lock = repository / "data/runtime/elysium.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "Elysium is already running outside this start operation. "
                "Use elysia-attach and Ctrl+C, wait for shutdown, then retry."
            ) from exc
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def start(
    repository: Path,
    runtime_directory: Path,
) -> int:
    """Fork tmux once, validate its main child, then publish that exact PID."""
    repository = repository.resolve(strict=True)
    runtime_directory = runtime_directory.resolve(strict=True)
    launcher = repository / "run_elysium.sh"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RuntimeError("The one-shot run_elysium.sh launcher is unavailable")
    if runtime_directory.stat().st_uid != os.getuid():
        raise RuntimeError("Runtime directory belongs to another user")
    socket = runtime_directory / "tmux.sock"
    pidfile = runtime_directory / "main.pid"
    config = runtime_directory / "tmux.conf"
    if socket.exists() or pidfile.exists() or config.exists():
        raise RuntimeError("Runtime directory already contains a socket or PID file")
    check_not_running(repository)
    # Retain a dead pane just long enough for this foreground owner to read
    # the original status, including processes which fail during imports.
    with config.open("x", encoding="utf-8") as handle:
        handle.write("set-window-option -g remain-on-exit on\n")
    command = ["tmux", "-f", str(config), "-S", str(socket)]
    environment = dict(os.environ)
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    created = False
    try:
        result = subprocess.run(
            [*command, "new-session", "-d", "-s", "elysium", "-c", str(repository),
             "-x", "200", "-y", "50", "-P", "-F", "#{pane_pid}", str(launcher)],
            check=True, capture_output=True, text=True, timeout=10, env=environment,
        )
        created = True
        pid = int(result.stdout.strip())
        process = Path("/proc") / str(pid)
        deadline = time.monotonic() + 5
        while True:
            args = process.joinpath("cmdline").read_bytes().split(b"\0")
            if len(args) == 3 and args[1:] == [b"main.py", b""]:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("The launcher did not exec the main Python process")
            time.sleep(0.02)
        if process.stat().st_uid != os.getuid():
            raise RuntimeError("Main process user changed")
        if process.joinpath("cwd").resolve() != repository:
            raise RuntimeError("Main process repository does not match")
        # Diagnostic main PID only: systemd tracks this foreground owner.
        descriptor = os.open(pidfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"{pid}\n")
            handle.flush()
            os.fsync(handle.fileno())
        return pid
    except BaseException:
        if created:
            # This is our fresh, dedicated session, never a pre-existing one.
            subprocess.run(
                [*command, "kill-session", "-t", "=elysium"],
                capture_output=True, timeout=5, check=False,
            )
        raise


def run(repository: Path, runtime_directory: Path) -> int:
    """Wait for this exact child, relaying SIGINT/TERM without ever relaunching."""
    descriptor: int | None = None
    stop_requested = False
    command = ["tmux", "-S", str(runtime_directory / "tmux.sock")]

    def forward_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        if descriptor is None:
            return
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGINT)
        except ProcessLookupError:
            pass

    previous = {
        signum: signal.signal(signum, forward_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        pid = start(repository, runtime_directory)
        descriptor = os.pidfd_open(pid)
        if stop_requested:
            forward_stop(signal.SIGINT, None)
        print(f"Elysium main PID {pid}", flush=True)
        while True:
            result = subprocess.run(
                [*command, "list-panes", "-t", "=elysium", "-F",
                 "#{pane_pid}|#{pane_dead}|#{pane_dead_status}|#{pane_dead_signal}"],
                check=True, capture_output=True, text=True, timeout=5,
            )
            rows = result.stdout.strip().splitlines()
            if len(rows) != 1:
                raise RuntimeError("Owned console no longer has exactly one pane")
            actual_pid, dead, status, signum = rows[0].split("|")
            if int(actual_pid) != pid:
                raise RuntimeError("Owned main pane identity changed")
            # PTY EOF may precede SIGCHLD/reaping. A dead pane alone is not
            # an exit receipt; never fabricate SIGHUP while status is pending.
            if dead == "1" and (status or signum):
                return int(status) if status else 128 + int(signum)
            time.sleep(0.25)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if descriptor is not None:
            # Do not close the terminal on a still-running main when the
            # console query fails. Give its normal shutdown time to finish.
            forward_stop(signal.SIGINT, None)
            exited, _, _ = select.select([descriptor], [], [], 90)
            os.close(descriptor)
            if exited:
                subprocess.run(
                    [*command, "kill-session", "-t", "=elysium"],
                    capture_output=True, timeout=5, check=False,
                )
            else:
                raise RuntimeError("Owned main did not stop; no force-kill was used")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Only check the main lock")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    if args.check:
        check_not_running(repository)
        print("No current main lock owner; no process was started.")
        return
    directory = os.environ.get("RUNTIME_DIRECTORY", "")
    if not directory or not os.environ.get("INVOCATION_ID"):
        parser.error("Start this helper through elysium.service, not directly")
    raise SystemExit(run(repository, Path(directory)))


if __name__ == "__main__":
    main()
