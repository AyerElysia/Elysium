"""Headless bot body process owner for the mineflayer Minecraft body.

The Elysium session owns this launcher, and this launcher owns exactly one
``node`` child process.  Lifecycle rules follow the project contract: start
and stop are idempotent, token bootstrap never overwrites an existing token,
and every failure returns an exact diagnosable reason instead of guessing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from collections import deque
from pathlib import Path

logger = logging.getLogger("life_engine.minecraft.bot_launcher")

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BOT_DIRECTORY = REPOSITORY_ROOT / "integrations" / "minecraft_bot"
ENTRYPOINT = BOT_DIRECTORY / "src" / "index.js"
TERMINATE_TIMEOUT_SECONDS = 5.0
MINIMUM_NODE_VERSION = (20, 10, 0)


class MinecraftBotLauncher:
    """Own the headless bot child process with explicit start/stop semantics."""

    def __init__(self, bot_dir: Path | None = None) -> None:
        """Bind the launcher to the exact integration directory."""

        self._directory = bot_dir or BOT_DIRECTORY
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=32)

    @property
    def directory(self) -> Path:
        """Return the exact integration directory used by this launcher."""

        return self._directory

    @property
    def pid(self) -> int | None:
        """Return the owned child process PID when it is alive."""

        process = self._process
        if process is None or process.returncode is not None:
            return None
        return process.pid

    @staticmethod
    def resolve_server_host(configured: str) -> str:
        """Resolve the WSL default gateway when configured as ``auto``."""

        if configured != "auto":
            return configured
        try:
            with open("/proc/net/route", encoding="ascii") as route_file:
                for line in route_file.read().splitlines()[1:]:
                    fields = line.split()
                    if len(fields) >= 3 and fields[1] == "00000000":
                        gateway = fields[2]
                        if gateway == "00000000":
                            break
                        return ".".join(
                            str(int(gateway[i : i + 2], 16))
                            for i in (6, 4, 2, 0)
                        )
        except OSError:
            pass
        return "127.0.0.1"

    async def check_node(self) -> dict[str, object]:
        """Inspect node availability without launching anything."""

        try:
            probe = await asyncio.create_subprocess_exec(
                "node",
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(probe.communicate(), timeout=10)
        except FileNotFoundError:
            return {
                "available": False,
                "version": "",
                "error": "node not found in PATH",
            }
        except TimeoutError:
            return {
                "available": False,
                "version": "",
                "error": "node --version timed out",
            }
        if probe.returncode != 0:
            return {
                "available": False,
                "version": "",
                "error": f"node exited with code {probe.returncode}",
            }
        version = stdout.decode().strip()
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", version)
        if match is None:
            return {
                "available": False,
                "version": version,
                "error": f"could not parse node version: {version or 'empty'}",
            }
        parsed = tuple(int(part) for part in match.groups())
        if parsed < MINIMUM_NODE_VERSION:
            required = ".".join(str(part) for part in MINIMUM_NODE_VERSION)
            return {
                "available": False,
                "version": version,
                "error": f"node {version} is older than required v{required}",
            }
        return {"available": True, "version": version, "error": None}

    def check_dependencies(self) -> dict[str, object]:
        """Verify the pinned integration layout and installed dependencies."""

        entrypoint = self._directory / "src" / "index.js"
        required_modules = (
            "mineflayer",
            "mineflayer-collectblock",
            "mineflayer-pathfinder",
            "vec3",
            "ws",
        )
        missing_modules = tuple(
            name
            for name in required_modules
            if not (self._directory / "node_modules" / name).exists()
        )
        result: dict[str, object] = {
            "directory": str(self._directory),
            "entrypoint_exists": entrypoint.exists(),
            "lockfile_exists": (self._directory / "package-lock.json").is_file(),
            "dependencies_installed": not missing_modules,
            "missing_modules": missing_modules,
        }
        return result

    @classmethod
    async def check_server(
        cls,
        configured_host: str,
        port: int,
        *,
        timeout_seconds: float = 1.5,
    ) -> dict[str, object]:
        """Prove the configured Minecraft endpoint accepts a TCP connection."""

        host = cls.resolve_server_host(configured_host)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=timeout_seconds,
            )
        except (OSError, TimeoutError) as exception:
            return {
                "available": False,
                "host": host,
                "port": port,
                "error": f"{type(exception).__name__}: {exception}",
            }
        del reader
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return {"available": True, "host": host, "port": port, "error": None}

    @staticmethod
    def ensure_token(token_file: Path) -> str:
        """Create one generated token file or reuse the exact existing token.

        The token is never logged.  An existing file is authoritative; a
        missing or malformed file is an explicit failure with its reason.
        """

        def read_existing() -> str:
            payload = json.loads(token_file.read_text(encoding="utf-8"))
            token = str(payload.get("authentication_token") or "")
            if not token:
                raise ValueError(f"authentication_token is absent in {token_file}")
            return token

        if token_file.exists():
            return read_existing()
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        temporary_file = token_file.with_name(
            f".{token_file.name}.{secrets.token_hex(8)}.tmp"
        )
        descriptor = os.open(
            temporary_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"authentication_token": token}, indent=2) + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_file, token_file)
            except FileExistsError:
                return read_existing()
        finally:
            temporary_file.unlink(missing_ok=True)
        logger.info("generated fresh bot bridge token file")
        return token

    def diagnostics(self) -> dict[str, object]:
        """Return bounded non-secret process facts for startup errors."""

        process = self._process
        return {
            "pid": process.pid if process is not None else None,
            "returncode": process.returncode if process is not None else None,
            "stderr_tail": tuple(self._stderr_tail),
        }

    async def start(
        self,
        *,
        bridge_uri: str,
        token: str,
        server_host: str,
        server_port: int,
        username: str,
        minecraft_version: str,
        instance_id: str,
        observation_interval_ms: int = 1000,
        entity_radius_blocks: int = 32,
    ) -> dict[str, object]:
        """Launch the owned bot process; never start a second instance."""

        if self.pid is not None:
            return {
                "success": True,
                "reused_existing": True,
                "pid": self.pid,
                "error": None,
            }
        stale = self._process
        if (
            stale is not None and stale.returncode is None
        ):  # pragma: no cover - defensive
            return {
                "success": True,
                "reused_existing": True,
                "pid": stale.pid,
                "error": None,
            }
        if stale is not None:
            await self._finish_stderr_task()
            self._process = None
        if server_host == "auto":
            server_host = self.resolve_server_host(server_host)
        node_check = await self.check_node()
        if not node_check["available"]:
            return {
                "success": False,
                "reused_existing": False,
                "pid": None,
                "error": f"node runtime is unavailable: {node_check['error']}",
            }
        dependency_check = self.check_dependencies()
        if not dependency_check["entrypoint_exists"]:
            return {
                "success": False,
                "reused_existing": False,
                "pid": None,
                "error": f"bot entrypoint is missing: {self._directory / 'src' / 'index.js'}",
            }
        if not dependency_check["dependencies_installed"]:
            return {
                "success": False,
                "reused_existing": False,
                "pid": None,
                "error": (
                    "bot dependencies are not installed; run "
                    f"`npm install` in {self._directory}"
                ),
            }
        environment = {
            "ELYSIUM_BOT_BRIDGE_URI": bridge_uri,
            "ELYSIUM_BOT_TOKEN": token,
            "ELYSIUM_BOT_SERVER_HOST": server_host,
            "ELYSIUM_BOT_SERVER_PORT": str(server_port),
            "ELYSIUM_BOT_USERNAME": username,
            "ELYSIUM_BOT_MC_VERSION": minecraft_version,
            "ELYSIUM_BOT_INSTANCE_ID": instance_id,
            "ELYSIUM_BOT_OBSERVATION_INTERVAL_MS": str(observation_interval_ms),
            "ELYSIUM_BOT_ENTITY_RADIUS_BLOCKS": str(entity_radius_blocks),
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": __import__("os").environ.get("HOME", ""),
        }
        try:
            process = await asyncio.create_subprocess_exec(
                "node",
                str(self._directory / "src" / "index.js"),
                cwd=str(self._directory),
                env=environment,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exception:  # noqa: BLE001 - launch boundary reports exact cause
            return {
                "success": False,
                "reused_existing": False,
                "pid": None,
                "error": f"bot process launch failed: {exception}",
            }
        self._process = process
        self._stderr_tail.clear()
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(process),
            name=f"minecraft_bot_stderr:{process.pid}",
        )
        logger.info("started headless bot body process pid=%s", process.pid)
        return {
            "success": True,
            "reused_existing": False,
            "pid": process.pid,
            "error": None,
        }

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        """Forward bounded bot diagnostics into the service log."""

        stream = process.stderr
        if stream is None:
            return
        try:
            async for raw_line in stream:
                line = raw_line.decode(errors="replace").rstrip()
                if line:
                    self._stderr_tail.append(line[:2048])
                    logger.debug("bot: %s", line)
        except Exception:  # noqa: BLE001 - diagnostics must never break shutdown
            logger.debug("bot stderr drain ended")

    async def _finish_stderr_task(self) -> None:
        """Join the owned diagnostics task after process termination."""

        task = self._stderr_task
        self._stderr_task = None
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def stop(self) -> dict[str, object]:
        """Stop the owned process idempotently with bounded escalation."""

        process = self._process
        if process is None:
            return {"success": True, "already_stopped": True, "pid": None}
        if process.returncode is not None:
            self._process = None
            await self._finish_stderr_task()
            return {"success": True, "already_stopped": True, "pid": process.pid}
        try:
            process.terminate()
        except ProcessLookupError:
            self._process = None
            await self._finish_stderr_task()
            return {"success": True, "already_stopped": True, "pid": process.pid}
        try:
            await asyncio.wait_for(process.wait(), timeout=TERMINATE_TIMEOUT_SECONDS)
            self._process = None
            await self._finish_stderr_task()
            return {"success": True, "already_stopped": False, "pid": process.pid}
        except TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=TERMINATE_TIMEOUT_SECONDS
                )
            except TimeoutError:
                return {
                    "success": False,
                    "already_stopped": False,
                    "pid": process.pid,
                    "error": "bot process did not exit after kill",
                }
            self._process = None
            await self._finish_stderr_task()
            return {"success": True, "already_stopped": False, "pid": process.pid}
