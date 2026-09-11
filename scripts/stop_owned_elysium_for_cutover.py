"""One-shot graceful cutover stop, fenced to a caller-specified Linux process."""

import argparse
import json
import os
from pathlib import Path
import signal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--start-ticks", required=True)
    parser.add_argument("--repository", type=Path, required=True)
    args = parser.parse_args()
    root = args.repository.resolve(strict=True)
    process = Path("/proc") / str(args.pid)
    # A pidfd prevents signaling a newly reused PID between inspection and send.
    descriptor = os.pidfd_open(args.pid)
    try:
        ticks = process.joinpath("stat").read_text().rpartition(")")[2].split()[19]
        command = process.joinpath("cmdline").read_bytes().split(b"\0")
        if ticks != args.start_ticks or process.joinpath("cwd").resolve() != root:
            raise RuntimeError("process ownership identity changed")
        if command != [b".venv/bin/python", b"main.py", b""]:
            raise RuntimeError("process command identity changed")
        signal.pidfd_send_signal(descriptor, signal.SIGINT)
        print(json.dumps({"pid": args.pid, "start_ticks": ticks, "signal": "SIGINT", "requested": True}))
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
