"""Life Engine lifecycle adapter for the unified memory archive."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.kernel.memory_archive.coordinator import MemoryArchiveCoordinator
from src.kernel.memory_archive.mysql_store import (
    MySQLArchiveConfig,
    RemoteMemoryArchive,
)
from src.kernel.memory_archive.sources import iter_data_root_records
from src.kernel.memory_archive.state import ArchiveState


class MemoryArchiveSyncBridge:
    """Continuously project exact local memory records to owner-authorized MySQL."""

    def __init__(self, section: Any, workspace_path: str | Path) -> None:
        host = str(getattr(section, "remote_host", "") or "").strip()
        user = str(getattr(section, "remote_user", "") or "").strip()
        database = str(
            getattr(section, "remote_database", "elysium") or "elysium"
        ).strip()
        password_env = str(
            getattr(
                section,
                "remote_password_env",
                "ELYSIUM_MEMORY_ARCHIVE_MYSQL_PASSWORD",
            )
            or "ELYSIUM_MEMORY_ARCHIVE_MYSQL_PASSWORD"
        ).strip()
        password = os.environ.get(password_env, "")
        if not host or not user or not database:
            raise ValueError(
                "memory_archive_sync remote_host/remote_user/remote_database are required"
            )
        if not password:
            raise ValueError(
                "memory_archive_sync password environment variable is not set: "
                f"{password_env}"
            )

        workspace = Path(workspace_path).expanduser().resolve()
        data_root = workspace.parent
        state_path = Path(
            str(
                getattr(
                    section,
                    "local_state_path",
                    ".memory/archive_sync_state.sqlite3",
                )
                or ".memory/archive_sync_state.sqlite3"
            )
        ).expanduser()
        if not state_path.is_absolute():
            state_path = workspace / state_path

        self._state = ArchiveState(state_path)
        self._source_node_id = self._state.node_id()
        self._data_root = data_root
        self._scan_batch_size = int(getattr(section, "scan_batch_size", 500) or 500)
        self._remote = RemoteMemoryArchive(
            MySQLArchiveConfig(
                host=host,
                port=int(getattr(section, "remote_port", 3306) or 3306),
                database=database,
                user=user,
                password=password,
                ssl_mode=str(
                    getattr(section, "mysql_ssl_mode", "disabled") or "disabled"
                ),
                ssl_ca=str(getattr(section, "mysql_ssl_ca", "") or ""),
                ssl_cert=str(getattr(section, "mysql_ssl_cert", "") or ""),
                ssl_key=str(getattr(section, "mysql_ssl_key", "") or ""),
                connect_timeout_seconds=int(
                    getattr(section, "connect_timeout_seconds", 5) or 5
                ),
            )
        )
        self._coordinator = MemoryArchiveCoordinator(
            self._state,
            self._remote,
            publish_batch_size=int(getattr(section, "publish_batch_size", 250) or 250),
            scan_batch_size=self._scan_batch_size,
            max_batch_bytes=(
                int(getattr(section, "max_batch_mib", 4) or 4) * 1024 * 1024
            ),
            publish_concurrency=int(getattr(section, "publish_concurrency", 2) or 2),
        )
        self._interval_seconds = float(
            getattr(section, "interval_seconds", 300.0) or 300.0
        )
        self._retry_max_seconds = float(
            getattr(section, "retry_max_seconds", 900.0) or 900.0
        )

    def _records(self):
        return iter_data_root_records(
            self._data_root,
            source_node_id=self._source_node_id,
            batch_size=self._scan_batch_size,
        )

    async def run(self, stop_event: Any) -> None:
        await self._coordinator.run_forever(
            stop_event,
            self._records,
            interval_seconds=self._interval_seconds,
            retry_max_seconds=self._retry_max_seconds,
        )

    async def close(self) -> None:
        await self._remote.close()

    def health_snapshot(self) -> dict[str, Any]:
        snapshot = self._coordinator.health_snapshot()
        snapshot["source_node_id"] = self._source_node_id
        snapshot["data_root"] = str(self._data_root)
        return snapshot
