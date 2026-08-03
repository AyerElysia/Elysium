"""Life Engine adapter for the generic offline-first synchronization kernel."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Any

from src.kernel.sync import (
    LocalSyncStore,
    MySQLLedgerConfig,
    RemoteMySQLLedger,
    SyncCoordinator,
    SyncEnvelope,
)

from .event_bus import RawEventStore, life_event_from_dict


class SharedSyncBridge:
    """Connect shared remote events to the authoritative raw life-event ledger."""

    def __init__(self, section: Any, raw_store: RawEventStore) -> None:
        host = str(getattr(section, "remote_host", "") or "").strip()
        user = str(getattr(section, "remote_user", "") or "").strip()
        database = str(
            getattr(section, "remote_database", "elysium") or "elysium"
        ).strip()
        password_env = str(
            getattr(section, "remote_password_env", "ELYSIUM_SYNC_MYSQL_PASSWORD")
            or "ELYSIUM_SYNC_MYSQL_PASSWORD"
        ).strip()
        password = os.environ.get(password_env, "")
        if not host or not user or not database:
            raise ValueError(
                "shared_sync remote_host/remote_user/remote_database are required"
            )
        if not password:
            raise ValueError(
                f"shared_sync password environment variable is not set: {password_env}"
            )
        local = LocalSyncStore(raw_store.database_path)
        remote = RemoteMySQLLedger(
            MySQLLedgerConfig(
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
        allowed = {
            str(item).strip().lower()
            for item in list(getattr(section, "allowed_visibilities", ["shared"]) or [])
            if str(item).strip()
        }
        self._raw_store = raw_store
        self._coordinator = SyncCoordinator(
            local,
            remote,
            consumer_id=str(
                getattr(section, "consumer_id", "life_engine.shared_sync")
                or "life_engine.shared_sync"
            ),
            allowed_visibilities=allowed,
            batch_size=int(getattr(section, "batch_size", 100) or 100),
            lease_seconds=float(getattr(section, "lease_seconds", 60.0) or 60.0),
            base_backoff_seconds=float(
                getattr(section, "base_backoff_seconds", 1.0) or 0.0
            ),
            max_backoff_seconds=float(
                getattr(section, "max_backoff_seconds", 300.0) or 300.0
            ),
            apply_callback=self._apply_remote_event,
        )
        self._poll_interval_seconds = float(
            getattr(section, "poll_interval_seconds", 1.0) or 1.0
        )
        self._push_enabled = bool(getattr(section, "push_enabled", True))
        self._pull_enabled = bool(getattr(section, "pull_enabled", False))

    async def _apply_remote_event(self, envelope: SyncEnvelope) -> None:
        payload = json.loads(envelope.payload_json)
        if not isinstance(payload, dict):
            raise TypeError("shared life event payload must be a JSON object")
        event = life_event_from_dict(payload)
        metadata = dict(event.metadata)
        metadata["sync_export"] = False
        metadata["sync_import_origin_node_id"] = envelope.origin_node_id
        metadata["sync_import_origin_sequence"] = envelope.origin_sequence
        await self._raw_store.append(replace(event, metadata=metadata))

    async def run(self, stop_event: Any) -> None:
        await self._coordinator.run_forever(
            stop_event,
            poll_interval_seconds=self._poll_interval_seconds,
            push=self._push_enabled,
            pull=self._pull_enabled,
        )

    async def close(self) -> None:
        await self._coordinator.close()

    def health_snapshot(self) -> dict[str, Any]:
        return self._coordinator.health_snapshot()
