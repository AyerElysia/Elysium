"""Transactional SQL authority for subject-authored initiative records."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncContextManager, TypeVar

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from ..initiative.contracts import (
    INITIATIVE_OUTREACH_OUTCOMES,
    InitiativeActorInactive,
    InitiativeConflict,
    InitiativeOutreachClaimReceipt,
    InitiativeOutreachCommand,
    InitiativeOutreachDeliveryReceipt,
    InitiativeOutreachOutcome,
    InitiativeOutreachReceipt,
    InitiativeOutreachResolutionReceipt,
    InitiativePendingExpression,
    InitiativePendingOutreach,
    InitiativePlatformDeliveryProofReceipt,
    InitiativeReencounterReceipt,
    InitiativeSeedCommand,
    InitiativeSeedCommit,
    InitiativeSeedView,
    InitiativeTransitionError,
)
from ..initiative.reducer import (
    apply_seed_event,
    outreach_claim_occurrence,
    outreach_command_from_payload,
    outreach_command_payload,
    outreach_delivery_proof_occurrence,
    outreach_inbox_occurrence,
    outreach_resolution_occurrence,
    reencounter_occurrence,
    seed_command_from_payload,
    seed_command_payload,
)
from .contracts import StorageBackendRuntime
from .models import BackendKind
from .proactive_decision_guard import (
    ProactiveDecisionGuardConflict,
    claim_proactive_decision,
)
from .runtime_contracts import RuntimeEventRecord, RuntimeStateCorrupt

_T = TypeVar("_T")
_SEED_EVENTS = "life_initiative.seed_decisions"
_SEED_HEADS = "life_initiative.seed_heads"
_OUTREACH_EVENTS = "life_initiative.outreach_decisions"
# Old releases wrote this after mutating an in-memory unread list.  Keep the
# immutable evidence readable, but do not treat it as a durable inbox receipt.
_OUTREACH_DELIVERIES = "life_initiative.outreach_deliveries"
_OUTREACH_INBOX_RECEIPTS = "life_initiative.outreach_inbox_receipts"
_OUTREACH_CLAIMS = "life_initiative.outreach_expression_claims"
_OUTREACH_DELIVERY_PROOFS = "life_initiative.outreach_delivery_proofs"
_OUTREACH_RESOLUTIONS = "life_initiative.outreach_resolutions"
_REENCOUNTER_DELIVERIES = "life_initiative.reencounter_deliveries"
_DOMAIN_LOCK_NAMESPACE = "life_initiative.metadata"
_DOMAIN_LOCK_KEY = "authority_projection_v1"
_SCHEMA_VERSION = 1
_MAX_WRITE_ATTEMPTS = 3
_BACKLOG_DEGRADED_SECONDS = 900.0


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _payload_json(payload: dict[str, Any]) -> tuple[str, str]:
    encoded = canonical_json(payload)
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validated_delivery_receipt(
    value: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Validate the exact content-free receipt emitted by MessageSender."""

    required = {
        "schema_version",
        "receipt_kind",
        "message_id",
        "platform",
        "adapter_signature",
        "provider_receipt",
    }
    if set(value) != required or int(value.get("schema_version") or 0) != 1:
        raise InitiativeTransitionError("delivery receipt schema is invalid")
    receipt_kind = str(value.get("receipt_kind") or "").strip()
    message_id = str(value.get("message_id") or "").strip()
    platform = str(value.get("platform") or "").strip()
    adapter_signature = str(value.get("adapter_signature") or "").strip()
    provider_receipt = value.get("provider_receipt")
    if (
        receipt_kind not in {"adapter_ack", "virtual_history_commit"}
        or not message_id
        or not platform
        or not adapter_signature
        or not isinstance(provider_receipt, dict)
    ):
        raise InitiativeTransitionError("delivery receipt identity is invalid")
    if (
        receipt_kind == "virtual_history_commit"
        and adapter_signature != "core:adapter:virtual_send"
    ):
        raise InitiativeTransitionError("virtual delivery receipt is inconsistent")
    if (
        receipt_kind == "adapter_ack"
        and adapter_signature == "core:adapter:virtual_send"
    ):
        raise InitiativeTransitionError("adapter delivery receipt is inconsistent")
    normalized = {
        "schema_version": 1,
        "receipt_kind": receipt_kind,
        "message_id": message_id,
        "platform": platform,
        "adapter_signature": adapter_signature,
        "provider_receipt": dict(provider_receipt),
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return normalized, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decode_payload(raw_value: Any, expected: Any, *, identity: str) -> dict[str, Any]:
    if isinstance(raw_value, bytes):
        raw = raw_value.decode("utf-8")
    else:
        raw = str(raw_value)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if digest != str(expected or ""):
        raise RuntimeStateCorrupt(f"InitiativePayloadCorrupt:{identity}")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeStateCorrupt(f"InitiativePayloadNotObject:{identity}")
    return value


class SQLInitiativeRecordStore:
    """One low-volume domain lock with transactional actor gate, event, and CAS."""

    def __init__(
        self,
        runtime: StorageBackendRuntime,
        *,
        validate_active_actor: Callable[[str], Awaitable[bool]] | None = None,
        actor_decision_guard: (
            Callable[[str], AsyncContextManager[None]] | None
        ) = None,
    ) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("initiative adapter requires enabled storage")
        if runtime.backend == BackendKind.MYSQL and validate_active_actor is not None:
            raise ValueError(
                "MySQL initiative actor validation must use transactional Presence"
            )
        if (
            runtime.backend == BackendKind.LOCAL
            and validate_active_actor is not None
            and actor_decision_guard is None
        ):
            raise ValueError(
                "local initiative actor validation requires a commit gate"
            )
        self.runtime = runtime
        self.backend = runtime.backend
        self._validate_active_actor = validate_active_actor
        self._actor_decision_guard = actor_decision_guard

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    def _bind_time(self, value: Any) -> datetime | str:
        parsed = _parse_time(value)
        if self.backend == BackendKind.MYSQL:
            return parsed.replace(tzinfo=None)
        return parsed.isoformat()

    async def _database_now(self, session: AsyncSession) -> datetime:
        if self.backend == BackendKind.MYSQL:
            value = await session.scalar(text("SELECT CURRENT_TIMESTAMP(6)"))
        else:
            value = await session.scalar(
                text("SELECT STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')")
            )
        return _parse_time(value)

    @staticmethod
    def _retryable(exc: DBAPIError) -> bool:
        message = str(exc.orig).lower()
        codes = {str(value) for value in getattr(exc.orig, "args", ())}
        return bool(
            {"1205", "1213"} & codes
            or "deadlock" in message
            or "database is locked" in message
            or "lock wait timeout" in message
        )

    async def _write(
        self,
        operation: Callable[[AsyncSession], Awaitable[_T]],
        *,
        actor_id: str = "",
    ) -> _T:
        if actor_id and self._actor_decision_guard is not None:
            async with self._actor_decision_guard(actor_id):
                return await self._write_attempts(operation)
        return await self._write_attempts(operation)

    async def _write_attempts(
        self,
        operation: Callable[[AsyncSession], Awaitable[_T]],
    ) -> _T:
        for attempt in range(_MAX_WRITE_ATTEMPTS):
            try:
                async with self.runtime.unit_of_work() as uow:
                    return await operation(uow.session)
            except DBAPIError as exc:
                if attempt + 1 >= _MAX_WRITE_ATTEMPTS or not self._retryable(exc):
                    raise
                await asyncio.sleep(0.02 * (attempt + 1))
        raise AssertionError("bounded initiative retry loop exhausted")

    async def _lock_domain(self, session: AsyncSession) -> None:
        payload = {"kind": "initiative_projection_lock", "schema_version": 1}
        payload_json, payload_sha256 = _payload_json(payload)
        now = await self._database_now(session)
        insert_prefix = (
            "INSERT IGNORE" if self.backend == BackendKind.MYSQL else "INSERT OR IGNORE"
        )
        await session.execute(
            text(
                f"""{insert_prefix} INTO runtime_states (
                    namespace, state_key, revision, schema_version,
                    payload_json, payload_sha256, updated_at
                ) VALUES (
                    :namespace, :state_key, 1, :schema_version,
                    :payload_json, :payload_sha256, :updated_at
                )"""
            ),
            {
                "namespace": _DOMAIN_LOCK_NAMESPACE,
                "state_key": _DOMAIN_LOCK_KEY,
                "schema_version": _SCHEMA_VERSION,
                "payload_json": payload_json,
                "payload_sha256": payload_sha256,
                "updated_at": self._bind_time(now),
            },
        )
        row = (
            (
                await session.execute(
                    text(
                        """SELECT payload_json, payload_sha256
                        FROM runtime_states
                        WHERE namespace = :namespace AND state_key = :state_key"""
                        + self._for_update
                    ),
                    {
                        "namespace": _DOMAIN_LOCK_NAMESPACE,
                        "state_key": _DOMAIN_LOCK_KEY,
                    },
                )
            )
            .mappings()
            .one()
        )
        if _decode_payload(
            row["payload_json"],
            row["payload_sha256"],
            identity="initiative-domain-lock",
        ) != payload:
            raise InitiativeConflict("initiative authority lock is corrupt")

    async def _assert_active_actor(
        self,
        session: AsyncSession,
        actor_id: str,
        *,
        database_now: datetime,
    ) -> None:
        if self._validate_active_actor is not None:
            if not await self._validate_active_actor(actor_id):
                raise InitiativeActorInactive("initiative actor is not active")
            return
        row = (
            (
                await session.execute(
                    text(
                        """SELECT status, lease_expires_at
                        FROM consciousness_presence
                        WHERE instance_id = :instance_id"""
                        + self._for_update
                    ),
                    {"instance_id": actor_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or str(row["status"]) != "active":
            raise InitiativeActorInactive("initiative actor is not active")
        lease_expires_at = (
            _parse_time(row["lease_expires_at"])
            if row["lease_expires_at"] not in (None, "")
            else None
        )
        if lease_expires_at is not None and lease_expires_at <= database_now:
            raise InitiativeActorInactive("initiative actor lease has expired")

    @staticmethod
    def _event_from_row(row: Any) -> RuntimeEventRecord:
        namespace = str(row["namespace"])
        occurrence_id = str(row["occurrence_id"])
        return RuntimeEventRecord(
            position=int(row["position"]),
            namespace=namespace,
            occurrence_id=occurrence_id,
            event_kind=str(row["event_kind"]),
            payload=_decode_payload(
                row["payload_json"],
                row["payload_sha256"],
                identity=f"{namespace}:{occurrence_id}",
            ),
            payload_sha256=str(row["payload_sha256"]),
            occurred_at=_parse_time(row["occurred_at"]).isoformat(),
            recorded_at=_parse_time(row["recorded_at"]).isoformat(),
        )

    @staticmethod
    def _event_columns() -> str:
        return """position, namespace, occurrence_id, event_kind,
            payload_json, payload_sha256, occurred_at, recorded_at"""

    async def _event_by_occurrence(
        self,
        session: AsyncSession,
        occurrence_id: str,
    ) -> RuntimeEventRecord | None:
        row = (
            (
                await session.execute(
                    text(
                        f"""SELECT {self._event_columns()}
                        FROM runtime_events
                        WHERE occurrence_id = :occurrence_id"""
                        + self._for_update
                    ),
                    {"occurrence_id": occurrence_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._event_from_row(row) if row is not None else None

    async def _append_event(
        self,
        session: AsyncSession,
        *,
        namespace: str,
        occurrence_id: str,
        event_kind: str,
        payload: dict[str, Any],
        occurred_at: str,
        database_now: datetime,
    ) -> RuntimeEventRecord:
        payload_json, payload_sha256 = _payload_json(payload)
        await session.execute(
            text(
                """INSERT INTO runtime_events (
                    namespace, occurrence_id, event_kind, payload_json,
                    payload_sha256, occurred_at, recorded_at
                ) VALUES (
                    :namespace, :occurrence_id, :event_kind, :payload_json,
                    :payload_sha256, :occurred_at, :recorded_at
                )"""
            ),
            {
                "namespace": namespace,
                "occurrence_id": occurrence_id,
                "event_kind": event_kind,
                "payload_json": payload_json,
                "payload_sha256": payload_sha256,
                "occurred_at": self._bind_time(occurred_at),
                "recorded_at": self._bind_time(database_now),
            },
        )
        persisted = await self._event_by_occurrence(session, occurrence_id)
        if persisted is None:
            raise RuntimeError(f"InitiativeEventInsertLost:{occurrence_id}")
        if (
            persisted.namespace != namespace
            or persisted.event_kind != event_kind
            or persisted.payload_sha256 != payload_sha256
        ):
            raise InitiativeConflict(
                "initiative occurrence was reused with different content"
            )
        return persisted

    @staticmethod
    def _view_payload(view: InitiativeSeedView) -> dict[str, Any]:
        payload = asdict(view)
        payload["related_entity_refs"] = list(view.related_entity_refs)
        return {"schema_version": 1, "view": payload}

    @staticmethod
    def _decode_view_payload(payload: dict[str, Any]) -> InitiativeSeedView:
        if int(payload.get("schema_version") or 0) != 1:
            raise RuntimeStateCorrupt("InitiativeHeadSchemaUnsupported")
        raw = payload.get("view")
        if not isinstance(raw, dict):
            raise RuntimeStateCorrupt("InitiativeHeadViewMissing")
        values = dict(raw)
        values["related_entity_refs"] = tuple(values.get("related_entity_refs") or ())
        try:
            return InitiativeSeedView(**values)
        except (TypeError, ValueError) as exc:
            raise RuntimeStateCorrupt("InitiativeHeadInvalid") from exc

    async def _head(
        self,
        session: AsyncSession,
        seed_id: str,
        *,
        for_update: bool,
    ) -> tuple[InitiativeSeedView, int] | None:
        row = (
            (
                await session.execute(
                    text(
                        """SELECT revision, payload_json, payload_sha256
                        FROM runtime_states
                        WHERE namespace = :namespace AND state_key = :state_key"""
                        + (self._for_update if for_update else "")
                    ),
                    {"namespace": _SEED_HEADS, "state_key": seed_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        payload = _decode_payload(
            row["payload_json"],
            row["payload_sha256"],
            identity=f"initiative-head:{seed_id}",
        )
        view = self._decode_view_payload(payload)
        if view.seed_id != seed_id:
            raise RuntimeStateCorrupt("InitiativeHeadIdentityMismatch")
        return view, int(row["revision"])

    async def _write_head(
        self,
        session: AsyncSession,
        view: InitiativeSeedView,
        *,
        expected_storage_revision: int,
        database_now: datetime,
    ) -> None:
        payload_json, payload_sha256 = _payload_json(self._view_payload(view))
        revision = expected_storage_revision + 1
        parameters = {
            "namespace": _SEED_HEADS,
            "state_key": view.seed_id,
            "revision": revision,
            "schema_version": _SCHEMA_VERSION,
            "payload_json": payload_json,
            "payload_sha256": payload_sha256,
            "updated_at": self._bind_time(database_now),
        }
        if expected_storage_revision == 0:
            await session.execute(
                text(
                    """INSERT INTO runtime_states (
                        namespace, state_key, revision, schema_version,
                        payload_json, payload_sha256, updated_at
                    ) VALUES (
                        :namespace, :state_key, :revision, :schema_version,
                        :payload_json, :payload_sha256, :updated_at
                    )"""
                ),
                parameters,
            )
            return
        result = await session.execute(
            text(
                """UPDATE runtime_states SET
                    revision = :revision,
                    schema_version = :schema_version,
                    payload_json = :payload_json,
                    payload_sha256 = :payload_sha256,
                    updated_at = :updated_at
                WHERE namespace = :namespace AND state_key = :state_key
                  AND revision = :expected_revision"""
            ),
            {**parameters, "expected_revision": expected_storage_revision},
        )
        if result.rowcount != 1:
            raise InitiativeConflict(
                "initiative head CAS failed",
                seed_id=view.seed_id,
                current_revision=view.revision,
            )

    async def reconcile(self) -> None:
        """Rebuild/verify heads from immutable legacy-compatible events."""

        async def operation(session: AsyncSession) -> None:
            await self._lock_domain(session)
            seed_rows = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {self._event_columns()}
                            FROM runtime_events
                            WHERE namespace = :namespace
                            ORDER BY position"""
                        ),
                        {"namespace": _SEED_EVENTS},
                    )
                )
                .mappings()
                .all()
            )
            views: dict[str, InitiativeSeedView] = {}
            seen_occurrences: set[str] = set()
            for row in seed_rows:
                record = self._event_from_row(row)
                command = seed_command_from_payload(record.payload)
                if (
                    record.payload.get("command_sha256")
                    != command.canonical_sha256()
                    or command.occurrence_id in seen_occurrences
                ):
                    raise InitiativeConflict("initiative event history is corrupt")
                seen_occurrences.add(command.occurrence_id)
                views[command.seed_id] = apply_seed_event(
                    views.get(command.seed_id),
                    record,
                )

            delivery_rows = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {self._event_columns()}
                            FROM runtime_events
                            WHERE namespace = :namespace
                            ORDER BY position"""
                        ),
                        {"namespace": _REENCOUNTER_DELIVERIES},
                    )
                )
                .mappings()
                .all()
            )
            for row in delivery_rows:
                record = self._event_from_row(row)
                payload = record.payload
                seed_id = str(payload.get("seed_id") or "")
                revision = int(payload.get("seed_revision") or 0)
                occurrence = str(payload.get("occurrence_id") or "")
                if occurrence != reencounter_occurrence(seed_id, revision):
                    raise InitiativeConflict("initiative delivery history is corrupt")
                view = views.get(seed_id)
                if view is None or view.reencounter_revision != revision:
                    continue
                views[seed_id] = replace(
                    view,
                    reencounter_delivered_at=str(payload.get("occurred_at") or ""),
                    reencounter_delivery_event_id=(
                        f"initiative:reencounter:delivery:{record.position}"
                    ),
                )

            persisted_rows = (
                (
                    await session.execute(
                        text(
                            """SELECT state_key, revision, payload_json,
                                payload_sha256
                            FROM runtime_states WHERE namespace = :namespace"""
                            + self._for_update
                        ),
                        {"namespace": _SEED_HEADS},
                    )
                )
                .mappings()
                .all()
            )
            persisted: dict[str, tuple[InitiativeSeedView, int]] = {}
            for row in persisted_rows:
                seed_id = str(row["state_key"])
                persisted[seed_id] = (
                    self._decode_view_payload(
                        _decode_payload(
                            row["payload_json"],
                            row["payload_sha256"],
                            identity=f"initiative-head:{seed_id}",
                        )
                    ),
                    int(row["revision"]),
                )
            if set(persisted) - set(views):
                raise InitiativeConflict("initiative heads contain orphan state")
            database_now = await self._database_now(session)
            for seed_id, rebuilt in views.items():
                existing = persisted.get(seed_id)
                if existing is None:
                    await self._write_head(
                        session,
                        rebuilt,
                        expected_storage_revision=0,
                        database_now=database_now,
                    )
                elif existing[0] != rebuilt:
                    raise InitiativeConflict(
                        "initiative head does not match immutable history",
                        seed_id=seed_id,
                        current_revision=existing[0].revision,
                    )

        await self._write(operation)

    async def decide_seed(
        self,
        command: InitiativeSeedCommand,
    ) -> InitiativeSeedCommit:
        async def operation(session: AsyncSession) -> InitiativeSeedCommit:
            await self._lock_domain(session)
            database_now = await self._database_now(session)
            try:
                await claim_proactive_decision(
                    session,
                    backend=self.backend,
                    occurrence_id=command.occurrence_id,
                    record_family="initiative",
                    command_sha256=command.canonical_sha256(),
                    occurred_at=command.occurred_at,
                    recorded_at=database_now,
                )
            except ProactiveDecisionGuardConflict as exc:
                raise InitiativeConflict(
                    "proactive occurrence was reused across decision families",
                    seed_id=command.seed_id,
                ) from exc
            replay = await self._event_by_occurrence(session, command.occurrence_id)
            if replay is not None:
                if (
                    replay.namespace != _SEED_EVENTS
                    or replay.payload.get("command_sha256")
                    != command.canonical_sha256()
                ):
                    raise InitiativeConflict(
                        "initiative occurrence was reused with different content",
                        seed_id=command.seed_id,
                    )
                return InitiativeSeedCommit(
                    event_id=f"initiative:seed:event:{replay.position}",
                    occurrence_id=command.occurrence_id,
                    seed_id=command.seed_id,
                    revision=command.revision,
                    status=(
                        "released" if command.action == "release" else "open"
                    ),
                    idempotent_replay=True,
                )
            await self._assert_active_actor(
                session,
                command.actor_consciousness_instance_id,
                database_now=database_now,
            )
            head = await self._head(session, command.seed_id, for_update=True)
            current = head[0] if head is not None else None
            storage_revision = head[1] if head is not None else 0
            if command.action == "hold":
                if current is not None:
                    raise InitiativeConflict(
                        "initiative seed already exists",
                        seed_id=command.seed_id,
                        current_revision=current.revision,
                    )
            elif current is None:
                raise InitiativeConflict(
                    "initiative seed does not exist",
                    seed_id=command.seed_id,
                )
            elif command.expected_revision != current.revision:
                raise InitiativeConflict(
                    "initiative expected_revision is stale",
                    seed_id=command.seed_id,
                    current_revision=current.revision,
                )
            record = await self._append_event(
                session,
                namespace=_SEED_EVENTS,
                occurrence_id=command.occurrence_id,
                event_kind=f"seed_{command.action}",
                payload=seed_command_payload(command),
                occurred_at=command.occurred_at,
                database_now=database_now,
            )
            view = apply_seed_event(current, record)
            await self._write_head(
                session,
                view,
                expected_storage_revision=storage_revision,
                database_now=database_now,
            )
            return InitiativeSeedCommit(
                event_id=view.last_event_id,
                occurrence_id=command.occurrence_id,
                seed_id=view.seed_id,
                revision=view.revision,
                status=view.status,
                idempotent_replay=False,
            )

        return await self._write(
            operation,
            actor_id=command.actor_consciousness_instance_id,
        )

    async def get_seed(self, seed_id: str) -> InitiativeSeedView | None:
        identity = str(seed_id or "").strip()
        if not identity:
            raise ValueError("seed_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            result = await self._head(uow.session, identity, for_update=False)
        return result[0] if result is not None else None

    async def list_seeds(
        self,
        *,
        include_released: bool = False,
    ) -> tuple[InitiativeSeedView, ...]:
        async with self.runtime.unit_of_work() as uow:
            return await self._list_seeds_in_session(
                uow.session,
                include_released=include_released,
            )

    async def _list_seeds_in_session(
        self,
        session: AsyncSession,
        *,
        include_released: bool,
    ) -> tuple[InitiativeSeedView, ...]:
        rows = (
            (
                await session.execute(
                    text(
                        """SELECT state_key, revision, payload_json,
                            payload_sha256
                        FROM runtime_states WHERE namespace = :namespace"""
                    ),
                    {"namespace": _SEED_HEADS},
                )
            )
            .mappings()
            .all()
        )
        views: list[InitiativeSeedView] = []
        for row in rows:
            view = self._decode_view_payload(
                _decode_payload(
                    row["payload_json"],
                    row["payload_sha256"],
                    identity=f"initiative-head:{row['state_key']}",
                )
            )
            if view.seed_id != str(row["state_key"]):
                raise RuntimeStateCorrupt("InitiativeHeadIdentityMismatch")
            views.append(view)
        return tuple(
            sorted(
                (
                    view
                    for view in views
                    if include_released or view.status != "released"
                ),
                key=lambda view: (view.last_event_position, view.seed_id),
            )
        )

    async def due_reencounters(
        self,
        *,
        now: str,
    ) -> tuple[InitiativeSeedView, ...]:
        parsed_now = _parse_time(now)
        return tuple(
            view
            for view in await self.list_seeds(include_released=False)
            if view.reencounter_at
            and not view.reencounter_delivered_at
            and _parse_time(view.reencounter_at) <= parsed_now
        )

    async def record_reencounter_delivery(
        self,
        *,
        seed_id: str,
        seed_revision: int,
        life_event_id: str,
        occurred_at: str,
    ) -> InitiativeReencounterReceipt:
        identity = str(seed_id or "").strip()
        revision = int(seed_revision)
        event_identity = str(life_event_id or "").strip()
        delivered_at = _parse_time(occurred_at).isoformat()
        if not identity or revision <= 0 or not event_identity:
            raise ValueError("initiative delivery identity is incomplete")
        occurrence_id = reencounter_occurrence(identity, revision)

        async def operation(session: AsyncSession) -> InitiativeReencounterReceipt:
            await self._lock_domain(session)
            replay = await self._event_by_occurrence(session, occurrence_id)
            if replay is not None:
                payload = replay.payload
                if (
                    replay.namespace != _REENCOUNTER_DELIVERIES
                    or str(payload.get("seed_id") or "") != identity
                    or int(payload.get("seed_revision") or 0) != revision
                    or str(payload.get("life_event_id") or "") != event_identity
                ):
                    raise InitiativeConflict(
                        "reencounter occurrence was reused with different evidence",
                        seed_id=identity,
                    )
                return InitiativeReencounterReceipt(
                    event_id=f"initiative:reencounter:delivery:{replay.position}",
                    occurrence_id=occurrence_id,
                    seed_id=identity,
                    seed_revision=revision,
                    life_event_id=event_identity,
                    idempotent_replay=True,
                )
            database_now = await self._database_now(session)
            head = await self._head(session, identity, for_update=True)
            if head is None or head[0].status != "open":
                raise InitiativeTransitionError(
                    "reencounter delivery references a missing or released seed"
                )
            view, storage_revision = head
            if view.reencounter_revision != revision:
                raise InitiativeConflict(
                    "reencounter revision is stale",
                    seed_id=identity,
                    current_revision=view.revision,
                )
            payload = {
                "schema_version": 1,
                "occurrence_id": occurrence_id,
                "seed_id": identity,
                "seed_revision": revision,
                "life_event_id": event_identity,
                "occurred_at": delivered_at,
            }
            record = await self._append_event(
                session,
                namespace=_REENCOUNTER_DELIVERIES,
                occurrence_id=occurrence_id,
                event_kind="reencounter_delivered",
                payload=payload,
                occurred_at=delivered_at,
                database_now=database_now,
            )
            updated = replace(
                view,
                reencounter_delivered_at=delivered_at,
                reencounter_delivery_event_id=(
                    f"initiative:reencounter:delivery:{record.position}"
                ),
            )
            await self._write_head(
                session,
                updated,
                expected_storage_revision=storage_revision,
                database_now=database_now,
            )
            return InitiativeReencounterReceipt(
                event_id=f"initiative:reencounter:delivery:{record.position}",
                occurrence_id=occurrence_id,
                seed_id=identity,
                seed_revision=revision,
                life_event_id=event_identity,
                idempotent_replay=False,
            )

        return await self._write(operation)

    async def begin_outreach(
        self,
        command: InitiativeOutreachCommand,
    ) -> InitiativeOutreachReceipt:
        async def operation(session: AsyncSession) -> InitiativeOutreachReceipt:
            await self._lock_domain(session)
            database_now = await self._database_now(session)
            try:
                await claim_proactive_decision(
                    session,
                    backend=self.backend,
                    occurrence_id=command.occurrence_id,
                    record_family="outreach",
                    command_sha256=command.canonical_sha256(),
                    occurred_at=command.occurred_at,
                    recorded_at=database_now,
                )
            except ProactiveDecisionGuardConflict as exc:
                raise InitiativeConflict(
                    "proactive occurrence was reused across decision families",
                    seed_id=command.seed_id,
                ) from exc
            replay = await self._event_by_occurrence(session, command.occurrence_id)
            if replay is not None:
                if (
                    replay.namespace != _OUTREACH_EVENTS
                    or replay.payload.get("command_sha256")
                    != command.canonical_sha256()
                ):
                    raise InitiativeConflict(
                        "outreach occurrence was reused with different content"
                    )
                return InitiativeOutreachReceipt(
                    event_id=f"initiative:outreach:event:{replay.position}",
                    occurrence_id=command.occurrence_id,
                    audience_ref=command.audience_ref,
                    surface_ref=command.surface_ref,
                    idempotent_replay=True,
                )
            await self._assert_active_actor(
                session,
                command.actor_consciousness_instance_id,
                database_now=database_now,
            )
            if command.seed_id:
                head = await self._head(
                    session,
                    command.seed_id,
                    for_update=True,
                )
                if head is None or head[0].status != "open":
                    raise InitiativeTransitionError(
                        "outreach references a missing or released seed"
                    )
                if head[0].revision != command.seed_revision:
                    raise InitiativeConflict(
                        "outreach seed_revision is stale",
                        seed_id=command.seed_id,
                        current_revision=head[0].revision,
                    )
            record = await self._append_event(
                session,
                namespace=_OUTREACH_EVENTS,
                occurrence_id=command.occurrence_id,
                event_kind="outreach_begun",
                payload=outreach_command_payload(command),
                occurred_at=command.occurred_at,
                database_now=database_now,
            )
            return InitiativeOutreachReceipt(
                event_id=f"initiative:outreach:event:{record.position}",
                occurrence_id=command.occurrence_id,
                audience_ref=command.audience_ref,
                surface_ref=command.surface_ref,
                idempotent_replay=False,
            )

        return await self._write(
            operation,
            actor_id=command.actor_consciousness_instance_id,
        )

    async def _namespace_events_in_session(
        self,
        session: AsyncSession,
        namespace: str,
    ) -> list[RuntimeEventRecord]:
        rows = (
            (
                await session.execute(
                    text(
                        f"""SELECT {self._event_columns()}
                        FROM runtime_events
                        WHERE namespace = :namespace ORDER BY position"""
                    ),
                    {"namespace": namespace},
                )
            )
            .mappings()
            .all()
        )
        return [self._event_from_row(row) for row in rows]

    async def _namespace_events(self, namespace: str) -> list[RuntimeEventRecord]:
        async with self.runtime.unit_of_work() as uow:
            return await self._namespace_events_in_session(
                uow.session,
                namespace,
            )

    async def pending_outreach(
        self,
        *,
        limit: int = 32,
    ) -> tuple[InitiativePendingOutreach, ...]:
        safe_limit = max(1, min(int(limit), 128))
        async with self.runtime.unit_of_work() as uow:
            outreach = await self._namespace_events_in_session(
                uow.session,
                _OUTREACH_EVENTS,
            )
            delivered = await self._namespace_events_in_session(
                uow.session,
                _OUTREACH_INBOX_RECEIPTS,
            )
        delivered_ids = {
            str(record.payload.get("outreach_occurrence_id") or "")
            for record in delivered
        }
        pending: list[InitiativePendingOutreach] = []
        for record in outreach:
            command = outreach_command_from_payload(record.payload)
            if (
                record.payload.get("command_sha256")
                != command.canonical_sha256()
            ):
                raise InitiativeConflict("outreach event digest mismatch")
            if command.occurrence_id in delivered_ids:
                continue
            pending.append(
                InitiativePendingOutreach(
                    authority_event_id=(
                        f"initiative:outreach:event:{record.position}"
                    ),
                    event_position=record.position,
                    command=command,
                )
            )
            if len(pending) >= safe_limit:
                break
        return tuple(pending)

    @staticmethod
    def _outreach_turn_id(outreach_occurrence_id: str) -> str:
        digest = hashlib.sha256(
            str(outreach_occurrence_id).encode("utf-8")
        ).hexdigest()
        return f"initiative_outreach_turn_{digest}"

    async def _ensure_outreach_inbox(
        self,
        session: AsyncSession,
        *,
        command: InitiativeOutreachCommand,
        stream_id: str,
        trigger_message_id: str,
        platform: str,
        database_now: datetime,
    ) -> tuple[str, str]:
        """Atomically retain the immutable fact and its pending stream turn."""

        inbox_material = {
            "schema_version": 1,
            "outreach_occurrence_id": command.occurrence_id,
            "command_sha256": command.canonical_sha256(),
            "stream_id": stream_id,
            "trigger_message_id": trigger_message_id,
            "platform": platform,
        }
        inbox_payload_sha256 = hashlib.sha256(
            canonical_json(inbox_material).encode("utf-8")
        ).hexdigest()
        source_occurrence = outreach_inbox_occurrence(command.occurrence_id)
        raw_payload_ref = (
            "runtime://initiative/outreach/"
            + hashlib.sha256(command.occurrence_id.encode("utf-8")).hexdigest()
        )
        insert_prefix = (
            "INSERT IGNORE"
            if self.backend == BackendKind.MYSQL
            else "INSERT OR IGNORE"
        )
        await session.execute(
            text(
                f"""{insert_prefix} INTO inbound_messages (
                    message_id, platform, platform_event_id, occurrence_id,
                    payload_sha256, stream_id, reply_target, source,
                    occurred_at, received_at, raw_payload_ref
                ) VALUES (
                    :message_id, :platform, :platform_event_id, :occurrence_id,
                    :payload_sha256, :stream_id, :reply_target, :source,
                    :occurred_at, :received_at, :raw_payload_ref
                )"""
            ),
            {
                "message_id": trigger_message_id,
                "platform": platform,
                "platform_event_id": trigger_message_id,
                "occurrence_id": source_occurrence,
                "payload_sha256": inbox_payload_sha256,
                "stream_id": stream_id,
                "reply_target": command.audience_ref,
                "source": "life_engine.proactive",
                "occurred_at": self._bind_time(command.occurred_at),
                "received_at": self._bind_time(database_now),
                "raw_payload_ref": raw_payload_ref,
            },
        )
        message_row = (
            (
                await session.execute(
                    text(
                        """SELECT message_id, platform, platform_event_id,
                            occurrence_id, payload_sha256, stream_id,
                            reply_target, source, occurred_at, raw_payload_ref
                        FROM inbound_messages
                        WHERE message_id = :message_id
                           OR (platform = :platform
                               AND platform_event_id = :platform_event_id)
                           OR (source = :source
                               AND occurrence_id = :occurrence_id)"""
                    ),
                    {
                        "message_id": trigger_message_id,
                        "platform": platform,
                        "platform_event_id": trigger_message_id,
                        "source": "life_engine.proactive",
                        "occurrence_id": source_occurrence,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )
        if message_row is None or (
            str(message_row["message_id"]) != trigger_message_id
            or str(message_row["platform"]) != platform
            or str(message_row["platform_event_id"]) != trigger_message_id
            or str(message_row["occurrence_id"]) != source_occurrence
            or str(message_row["payload_sha256"]) != inbox_payload_sha256
            or str(message_row["stream_id"]) != stream_id
            or str(message_row["reply_target"]) != command.audience_ref
            or str(message_row["source"]) != "life_engine.proactive"
            or _parse_time(message_row["occurred_at"])
            != _parse_time(command.occurred_at)
            or str(message_row["raw_payload_ref"]) != raw_payload_ref
        ):
            raise InitiativeConflict(
                "outreach durable inbox message identity was reused"
            )

        turn_id = self._outreach_turn_id(command.occurrence_id)
        turn_row = (
            (
                await session.execute(
                    text(
                        """SELECT turn_id, stream_id, source_message_id,
                            status, input_frontier_json
                        FROM stream_turns
                        WHERE turn_id = :turn_id
                           OR source_message_id = :message_id"""
                        + self._for_update
                    ),
                    {"turn_id": turn_id, "message_id": trigger_message_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        input_frontier = {
            "schema_version": 1,
            "kind": "initiative_outreach",
            "outreach_occurrence_id": command.occurrence_id,
            "inbox_payload_sha256": inbox_payload_sha256,
        }
        if turn_row is None:
            await session.execute(
                text(
                    "SELECT stream_id FROM stream_turns "
                    "WHERE stream_id = :stream_id LIMIT 1" + self._for_update
                ),
                {"stream_id": stream_id},
            )
            next_sequence = int(
                await session.scalar(
                    text(
                        """SELECT COALESCE(MAX(stream_sequence), 0) + 1
                        FROM stream_turns WHERE stream_id = :stream_id"""
                    ),
                    {"stream_id": stream_id},
                )
                or 1
            )
            await session.execute(
                text(
                    f"""{insert_prefix} INTO stream_turns (
                        turn_id, stream_id, stream_sequence, source_message_id,
                        status, claim_owner, claim_epoch, lease_until,
                        input_frontier_json, result_ref, result_digest,
                        attempts, created_at, updated_at
                    ) VALUES (
                        :turn_id, :stream_id, :stream_sequence, :source_message_id,
                        'pending', NULL, 0, NULL, :input_frontier_json,
                        NULL, NULL, 0, :created_at, :updated_at
                    )"""
                ),
                {
                    "turn_id": turn_id,
                    "stream_id": stream_id,
                    "stream_sequence": next_sequence,
                    "source_message_id": trigger_message_id,
                    "input_frontier_json": canonical_json(input_frontier),
                    "created_at": self._bind_time(database_now),
                    "updated_at": self._bind_time(database_now),
                },
            )
            turn_row = (
                (
                    await session.execute(
                        text(
                            """SELECT turn_id, stream_id, source_message_id,
                                status, input_frontier_json
                            FROM stream_turns
                            WHERE turn_id = :turn_id
                               OR source_message_id = :message_id"""
                            + self._for_update
                        ),
                        {"turn_id": turn_id, "message_id": trigger_message_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
        raw_frontier = turn_row["input_frontier_json"] if turn_row else None
        try:
            stored_frontier = (
                dict(raw_frontier)
                if isinstance(raw_frontier, dict)
                else json.loads(str(raw_frontier))
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InitiativeConflict(
                "outreach durable inbox turn is corrupt"
            ) from exc
        if turn_row is None or (
            str(turn_row["turn_id"]) != turn_id
            or str(turn_row["stream_id"]) != stream_id
            or str(turn_row["source_message_id"]) != trigger_message_id
            or str(turn_row["status"])
            not in {"pending", "retryable", "completed"}
            or stored_frontier != input_frontier
        ):
            raise InitiativeConflict(
                "outreach durable inbox turn identity was reused"
            )
        return turn_id, inbox_payload_sha256

    async def record_outreach_delivery(
        self,
        *,
        outreach_occurrence_id: str,
        stream_id: str,
        trigger_message_id: str,
        occurred_at: str,
        platform: str = "unknown",
    ) -> InitiativeOutreachDeliveryReceipt:
        outreach_id = str(outreach_occurrence_id or "").strip()
        exact_stream = str(stream_id or "").strip()
        message_id = str(trigger_message_id or "").strip()
        exact_platform = str(platform or "").strip().lower() or "unknown"
        delivered_at = _parse_time(occurred_at).isoformat()
        if not outreach_id or not exact_stream or not message_id:
            raise ValueError("outreach delivery identity is incomplete")
        occurrence_id = outreach_inbox_occurrence(outreach_id)

        async def operation(
            session: AsyncSession,
        ) -> InitiativeOutreachDeliveryReceipt:
            await self._lock_domain(session)
            outreach = await self._event_by_occurrence(session, outreach_id)
            if outreach is None or outreach.namespace != _OUTREACH_EVENTS:
                raise InitiativeTransitionError(
                    "outreach delivery references a missing authority decision"
                )
            command = outreach_command_from_payload(outreach.payload)
            if outreach.payload.get("command_sha256") != command.canonical_sha256():
                raise InitiativeConflict("outreach event digest mismatch")
            database_now = await self._database_now(session)
            turn_id, inbox_payload_sha256 = await self._ensure_outreach_inbox(
                session,
                command=command,
                stream_id=exact_stream,
                trigger_message_id=message_id,
                platform=exact_platform,
                database_now=database_now,
            )
            resolution = await self._event_by_occurrence(
                session,
                outreach_resolution_occurrence(outreach_id),
            )
            resolution_outcome: InitiativeOutreachOutcome | None = None
            if resolution is not None:
                if (
                    resolution.namespace != _OUTREACH_RESOLUTIONS
                    or str(
                        resolution.payload.get("outreach_occurrence_id") or ""
                    )
                    != outreach_id
                    or str(resolution.payload.get("outcome") or "")
                    not in INITIATIVE_OUTREACH_OUTCOMES
                ):
                    raise InitiativeConflict(
                        "outreach resolution evidence is corrupt"
                    )
                resolution_outcome = str(
                    resolution.payload["outcome"]
                )  # type: ignore[assignment]
            replay = await self._event_by_occurrence(session, occurrence_id)
            if replay is not None:
                payload = replay.payload
                if (
                    replay.namespace != _OUTREACH_INBOX_RECEIPTS
                    or str(payload.get("outreach_occurrence_id") or "")
                    != outreach_id
                    or str(payload.get("stream_id") or "") != exact_stream
                    or str(payload.get("trigger_message_id") or "") != message_id
                    or str(payload.get("turn_id") or "") != turn_id
                    or str(payload.get("platform") or "") != exact_platform
                    or str(payload.get("inbox_payload_sha256") or "")
                    != inbox_payload_sha256
                ):
                    raise InitiativeConflict(
                        "outreach inbox receipt occurrence was reused"
                    )
                return InitiativeOutreachDeliveryReceipt(
                    event_id=f"initiative:outreach:inbox:{replay.position}",
                    occurrence_id=occurrence_id,
                    outreach_occurrence_id=outreach_id,
                    stream_id=exact_stream,
                    trigger_message_id=message_id,
                    idempotent_replay=True,
                    turn_id=turn_id,
                    inbox_payload_sha256=inbox_payload_sha256,
                    expression_resolved=resolution is not None,
                    expression_outcome=resolution_outcome,
                )
            payload = {
                "schema_version": 2,
                "occurrence_id": occurrence_id,
                "outreach_occurrence_id": outreach_id,
                "stream_id": exact_stream,
                "trigger_message_id": message_id,
                "turn_id": turn_id,
                "platform": exact_platform,
                "inbox_payload_sha256": inbox_payload_sha256,
                "occurred_at": delivered_at,
            }
            record = await self._append_event(
                session,
                namespace=_OUTREACH_INBOX_RECEIPTS,
                occurrence_id=occurrence_id,
                event_kind="outreach_inbox_accepted",
                payload=payload,
                occurred_at=delivered_at,
                database_now=database_now,
            )
            return InitiativeOutreachDeliveryReceipt(
                event_id=f"initiative:outreach:inbox:{record.position}",
                occurrence_id=occurrence_id,
                outreach_occurrence_id=outreach_id,
                stream_id=exact_stream,
                trigger_message_id=message_id,
                idempotent_replay=False,
                turn_id=turn_id,
                inbox_payload_sha256=inbox_payload_sha256,
                expression_resolved=False,
                expression_outcome=None,
            )

        return await self._write(operation)

    async def pending_expression_outreach(
        self,
        *,
        limit: int = 32,
    ) -> tuple[InitiativePendingExpression, ...]:
        """Return one transactionally coherent projection of open inbox turns."""

        safe_limit = max(1, min(int(limit), 128))
        async with self.runtime.unit_of_work() as uow:
            outreach = await self._namespace_events_in_session(
                uow.session,
                _OUTREACH_EVENTS,
            )
            inbox_receipts = await self._namespace_events_in_session(
                uow.session,
                _OUTREACH_INBOX_RECEIPTS,
            )
            resolutions = await self._namespace_events_in_session(
                uow.session,
                _OUTREACH_RESOLUTIONS,
            )
            claims = await self._namespace_events_in_session(
                uow.session,
                _OUTREACH_CLAIMS,
            )
            database_now = await self._database_now(uow.session)
            outreach_by_occurrence = {
                record.occurrence_id: record for record in outreach
            }
            resolved_ids = {
                str(record.payload.get("outreach_occurrence_id") or "")
                for record in resolutions
            }
            claim_by_outreach = {
                str(record.payload.get("outreach_occurrence_id") or ""): record
                for record in claims
            }
            pending: list[InitiativePendingExpression] = []
            for receipt in inbox_receipts:
                outreach_id = str(
                    receipt.payload.get("outreach_occurrence_id") or ""
                )
                if outreach_id in resolved_ids:
                    continue
                authority_record = outreach_by_occurrence.get(outreach_id)
                if authority_record is None:
                    raise InitiativeConflict(
                        "outreach inbox references missing authority evidence"
                    )
                command = outreach_command_from_payload(authority_record.payload)
                if (
                    authority_record.payload.get("command_sha256")
                    != command.canonical_sha256()
                ):
                    raise InitiativeConflict("outreach event digest mismatch")
                turn_id = str(receipt.payload.get("turn_id") or "")
                trigger_message_id = str(
                    receipt.payload.get("trigger_message_id") or ""
                )
                turn_row = (
                    (
                        await uow.session.execute(
                            text(
                                """SELECT turn_id, stream_id, source_message_id,
                                    status, claim_owner, claim_epoch, lease_until
                                FROM stream_turns
                                WHERE turn_id = :turn_id"""
                            ),
                            {"turn_id": turn_id},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if turn_row is None or (
                    str(turn_row["stream_id"])
                    != str(receipt.payload.get("stream_id") or "")
                    or str(turn_row["source_message_id"]) != trigger_message_id
                    or str(turn_row["status"])
                    not in {"pending", "retryable", "processing"}
                ):
                    raise InitiativeConflict(
                        "outreach inbox turn is missing or inconsistent"
                    )
                claim_record = claim_by_outreach.get(outreach_id)
                claim_action_id = ""
                claim_owner = ""
                claim_lease_until = ""
                claim_expired = False
                if str(turn_row["status"]) == "processing":
                    if claim_record is None:
                        raise InitiativeConflict(
                            "processing outreach turn has no immutable claim"
                        )
                    claim_payload = claim_record.payload
                    claim_action_id = str(claim_payload.get("action_id") or "")
                    claim_owner = str(claim_payload.get("claim_owner") or "")
                    claim_lease_until = str(
                        claim_payload.get("lease_until")
                        or turn_row["lease_until"]
                        or ""
                    )
                    if (
                        claim_record.namespace != _OUTREACH_CLAIMS
                        or str(claim_payload.get("turn_id") or "") != turn_id
                        or int(claim_payload.get("claim_epoch") or 0)
                        != int(turn_row["claim_epoch"] or 0)
                        or (
                            claim_owner
                            and claim_owner
                            != str(turn_row["claim_owner"] or "")
                        )
                    ):
                        raise InitiativeConflict(
                            "processing outreach claim evidence is inconsistent"
                        )
                    # A pre-v2 claim has no boot owner/lease and can only be
                    # recovered conservatively as an expired unknown delivery.
                    claim_expired = not claim_owner or not claim_lease_until
                    if claim_lease_until:
                        claim_expired = (
                            _parse_time(claim_lease_until) <= database_now
                        )
                pending.append(
                    InitiativePendingExpression(
                        authority_event_id=(
                            f"initiative:outreach:event:{authority_record.position}"
                        ),
                        event_position=authority_record.position,
                        command=command,
                        delivery_event_id=(
                            f"initiative:outreach:inbox:{receipt.position}"
                        ),
                        delivery_position=receipt.position,
                        stream_id=str(receipt.payload["stream_id"]),
                        platform=str(receipt.payload.get("platform") or "unknown"),
                        trigger_message_id=trigger_message_id,
                        turn_id=turn_id,
                        delivered_at=str(receipt.payload.get("occurred_at") or ""),
                        status=str(turn_row["status"]),  # type: ignore[arg-type]
                        claimed_action_id=claim_action_id,
                        claim_epoch=int(turn_row["claim_epoch"] or 0),
                        claim_owner=claim_owner,
                        claim_lease_until=claim_lease_until,
                        claim_expired=claim_expired,
                    )
                )
                if len(pending) >= safe_limit:
                    break
            return tuple(pending)

    async def claim_outreach_expression(
        self,
        *,
        outreach_occurrence_id: str,
        action_id: str,
        claim_owner: str,
        lease_seconds: int,
        occurred_at: str,
    ) -> InitiativeOutreachClaimReceipt:
        """Fence one visible platform action before it may leave the process.

        A committed claim is intentionally at-most-once.  If the caller loses
        the first return value, replay reports ``execute_allowed=False`` and
        recovery settles the expression as ``delivery_unknown`` instead of
        risking a duplicate message.
        """

        outreach_id = str(outreach_occurrence_id or "").strip()
        exact_action_id = str(action_id or "").strip()
        exact_claim_owner = str(claim_owner or "").strip()
        exact_lease_seconds = int(lease_seconds)
        claimed_at = _parse_time(occurred_at).isoformat()
        if not outreach_id or not exact_action_id or not exact_claim_owner:
            raise ValueError("outreach expression claim identity is incomplete")
        if len(exact_action_id) > 512:
            raise ValueError("outreach expression action_id is too long")
        if len(exact_claim_owner) > 255:
            raise ValueError("outreach expression claim_owner is too long")
        if not 15 <= exact_lease_seconds <= 900:
            raise ValueError(
                "outreach expression lease_seconds must be between 15 and 900"
            )
        occurrence_id = outreach_claim_occurrence(
            outreach_id,
            exact_action_id,
        )

        async def operation(
            session: AsyncSession,
        ) -> InitiativeOutreachClaimReceipt:
            await self._lock_domain(session)
            inbox = await self._event_by_occurrence(
                session,
                outreach_inbox_occurrence(outreach_id),
            )
            if inbox is None or inbox.namespace != _OUTREACH_INBOX_RECEIPTS:
                raise InitiativeTransitionError(
                    "outreach expression claim requires a durable inbox receipt"
                )
            turn_id = str(inbox.payload.get("turn_id") or "")
            turn_row = (
                (
                    await session.execute(
                        text(
                            """SELECT turn_id, source_message_id, status,
                                claim_owner, claim_epoch, lease_until
                            FROM stream_turns WHERE turn_id = :turn_id"""
                            + self._for_update
                        ),
                        {"turn_id": turn_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if turn_row is None or str(turn_row["source_message_id"]) != str(
                inbox.payload.get("trigger_message_id") or ""
            ):
                raise InitiativeConflict(
                    "outreach expression claim lost its durable inbox turn"
                )

            replay = await self._event_by_occurrence(session, occurrence_id)
            if replay is not None:
                payload = replay.payload
                if (
                    replay.namespace != _OUTREACH_CLAIMS
                    or str(payload.get("outreach_occurrence_id") or "")
                    != outreach_id
                    or str(payload.get("turn_id") or "") != turn_id
                    or str(payload.get("action_id") or "") != exact_action_id
                    or int(payload.get("claim_epoch") or 0) <= 0
                    or str(payload.get("inbox_payload_sha256") or "")
                    != str(inbox.payload.get("inbox_payload_sha256") or "")
                ):
                    raise InitiativeConflict(
                        "outreach expression claim occurrence was reused"
                    )
                status = str(turn_row["status"])
                stored_claim_owner = str(
                    payload.get("claim_owner")
                    or payload.get("action_id")
                    or ""
                )
                if status == "processing" and (
                    str(turn_row["claim_owner"] or "") != stored_claim_owner
                    or int(turn_row["claim_epoch"] or 0)
                    != int(payload["claim_epoch"])
                ):
                    raise InitiativeConflict(
                        "outreach expression claim no longer owns its turn"
                    )
                if status not in {"processing", "completed"}:
                    raise InitiativeConflict(
                        "outreach expression claim replay found invalid turn state"
                    )
                return InitiativeOutreachClaimReceipt(
                    event_id=f"initiative:outreach:claim:{replay.position}",
                    occurrence_id=occurrence_id,
                    outreach_occurrence_id=outreach_id,
                    turn_id=turn_id,
                    action_id=exact_action_id,
                    claim_epoch=int(payload["claim_epoch"]),
                    claim_owner=stored_claim_owner,
                    lease_until=str(payload.get("lease_until") or ""),
                    execute_allowed=False,
                    idempotent_replay=True,
                )

            if str(turn_row["status"]) not in {"pending", "retryable"}:
                raise InitiativeTransitionError(
                    "outreach expression turn is already claimed or resolved"
                )
            database_now = await self._database_now(session)
            lease_until = database_now + timedelta(
                seconds=exact_lease_seconds
            )
            next_epoch = int(turn_row["claim_epoch"] or 0) + 1
            payload = {
                "schema_version": 2,
                "occurrence_id": occurrence_id,
                "outreach_occurrence_id": outreach_id,
                "turn_id": turn_id,
                "action_id": exact_action_id,
                "claim_epoch": next_epoch,
                "claim_owner": exact_claim_owner,
                "lease_until": lease_until.isoformat(),
                "inbox_payload_sha256": str(
                    inbox.payload.get("inbox_payload_sha256") or ""
                ),
                "occurred_at": claimed_at,
            }
            record = await self._append_event(
                session,
                namespace=_OUTREACH_CLAIMS,
                occurrence_id=occurrence_id,
                event_kind="outreach_expression_claimed",
                payload=payload,
                occurred_at=claimed_at,
                database_now=database_now,
            )
            update = await session.execute(
                text(
                    """UPDATE stream_turns SET status = 'processing',
                        claim_owner = :claim_owner,
                        claim_epoch = :claim_epoch,
                        lease_until = :lease_until,
                        attempts = attempts + 1,
                        updated_at = :updated_at
                    WHERE turn_id = :turn_id
                      AND source_message_id = :source_message_id
                      AND status IN ('pending', 'retryable')
                      AND claim_epoch = :expected_claim_epoch"""
                ),
                {
                    "claim_owner": exact_claim_owner,
                    "claim_epoch": next_epoch,
                    "lease_until": self._bind_time(lease_until),
                    "updated_at": self._bind_time(database_now),
                    "turn_id": turn_id,
                    "source_message_id": str(
                        inbox.payload.get("trigger_message_id") or ""
                    ),
                    "expected_claim_epoch": next_epoch - 1,
                },
            )
            if int(update.rowcount or 0) != 1:
                raise InitiativeConflict(
                    "outreach expression claim lost its pending turn"
                )
            return InitiativeOutreachClaimReceipt(
                event_id=f"initiative:outreach:claim:{record.position}",
                occurrence_id=occurrence_id,
                outreach_occurrence_id=outreach_id,
                turn_id=turn_id,
                action_id=exact_action_id,
                claim_epoch=next_epoch,
                claim_owner=exact_claim_owner,
                lease_until=lease_until.isoformat(),
                execute_allowed=True,
                idempotent_replay=False,
            )

        return await self._write(operation)

    async def record_outreach_delivery_proof(
        self,
        *,
        outreach_occurrence_id: str,
        action_id: str,
        delivery_receipt: dict[str, Any],
        occurred_at: str,
    ) -> InitiativePlatformDeliveryProofReceipt:
        """Persist a transport acknowledgement under the fenced action claim."""

        outreach_id = str(outreach_occurrence_id or "").strip()
        exact_action_id = str(action_id or "").strip()
        receipt, receipt_sha256 = _validated_delivery_receipt(
            dict(delivery_receipt or {})
        )
        delivery_message_id = str(receipt["message_id"])
        proved_at = _parse_time(occurred_at).isoformat()
        if not outreach_id or not exact_action_id:
            raise ValueError("outreach delivery proof identity is incomplete")
        occurrence_id = outreach_delivery_proof_occurrence(
            outreach_id,
            exact_action_id,
            delivery_message_id,
        )

        async def operation(
            session: AsyncSession,
        ) -> InitiativePlatformDeliveryProofReceipt:
            await self._lock_domain(session)
            replay = await self._event_by_occurrence(session, occurrence_id)
            if replay is not None:
                payload = replay.payload
                if (
                    replay.namespace != _OUTREACH_DELIVERY_PROOFS
                    or str(payload.get("outreach_occurrence_id") or "")
                    != outreach_id
                    or str(payload.get("action_id") or "") != exact_action_id
                    or str(payload.get("delivery_receipt_sha256") or "")
                    != receipt_sha256
                    or str(payload.get("delivery_message_id") or "")
                    != delivery_message_id
                ):
                    raise InitiativeConflict(
                        "outreach delivery proof occurrence was reused"
                    )
                return InitiativePlatformDeliveryProofReceipt(
                    event_id=f"initiative:outreach:delivery-proof:{replay.position}",
                    occurrence_id=occurrence_id,
                    outreach_occurrence_id=outreach_id,
                    turn_id=str(payload.get("turn_id") or ""),
                    action_id=exact_action_id,
                    claim_epoch=int(payload.get("claim_epoch") or 0),
                    delivery_receipt_sha256=receipt_sha256,
                    delivery_message_id=delivery_message_id,
                    idempotent_replay=True,
                )
            inbox = await self._event_by_occurrence(
                session,
                outreach_inbox_occurrence(outreach_id),
            )
            if inbox is None or inbox.namespace != _OUTREACH_INBOX_RECEIPTS:
                raise InitiativeTransitionError(
                    "outreach delivery proof requires a durable inbox receipt"
                )
            turn_id = str(inbox.payload.get("turn_id") or "")
            turn = (
                (
                    await session.execute(
                        text(
                            """SELECT source_message_id, status, claim_owner,
                                claim_epoch FROM stream_turns
                            WHERE turn_id = :turn_id"""
                            + self._for_update
                        ),
                        {"turn_id": turn_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if (
                turn is None
                or str(turn["source_message_id"])
                != str(inbox.payload.get("trigger_message_id") or "")
                or str(turn["status"]) != "processing"
            ):
                raise InitiativeConflict(
                    "outreach delivery proof has no active expression claim"
                )
            claim = await self._event_by_occurrence(
                session,
                outreach_claim_occurrence(outreach_id, exact_action_id),
            )
            if claim is None or claim.namespace != _OUTREACH_CLAIMS:
                raise InitiativeConflict(
                    "outreach delivery proof has no immutable action claim"
                )
            claim_epoch = int(claim.payload.get("claim_epoch") or 0)
            claim_owner = str(claim.payload.get("claim_owner") or "")
            if (
                str(claim.payload.get("turn_id") or "") != turn_id
                or str(claim.payload.get("action_id") or "") != exact_action_id
                or claim_epoch != int(turn["claim_epoch"] or 0)
                or claim_owner != str(turn["claim_owner"] or "")
            ):
                raise InitiativeConflict(
                    "outreach delivery proof claim evidence is inconsistent"
                )
            database_now = await self._database_now(session)
            payload = {
                "schema_version": 1,
                "occurrence_id": occurrence_id,
                "outreach_occurrence_id": outreach_id,
                "turn_id": turn_id,
                "action_id": exact_action_id,
                "claim_epoch": claim_epoch,
                "delivery_receipt_sha256": receipt_sha256,
                "delivery_message_id": delivery_message_id,
                "receipt_kind": str(receipt["receipt_kind"]),
                "adapter_signature": str(receipt["adapter_signature"]),
                "platform": str(receipt["platform"]),
                "provider_receipt_sha256": hashlib.sha256(
                    canonical_json(receipt["provider_receipt"]).encode("utf-8")
                ).hexdigest(),
                "occurred_at": proved_at,
            }
            record = await self._append_event(
                session,
                namespace=_OUTREACH_DELIVERY_PROOFS,
                occurrence_id=occurrence_id,
                event_kind="outreach_platform_delivery_proved",
                payload=payload,
                occurred_at=proved_at,
                database_now=database_now,
            )
            return InitiativePlatformDeliveryProofReceipt(
                event_id=f"initiative:outreach:delivery-proof:{record.position}",
                occurrence_id=occurrence_id,
                outreach_occurrence_id=outreach_id,
                turn_id=turn_id,
                action_id=exact_action_id,
                claim_epoch=claim_epoch,
                delivery_receipt_sha256=receipt_sha256,
                delivery_message_id=delivery_message_id,
                idempotent_replay=False,
            )

        return await self._write(operation)

    async def _require_delivery_proof(
        self,
        session: AsyncSession,
        *,
        outreach_occurrence_id: str,
        action_id: str,
        claim_epoch: int,
        delivery_receipt_sha256: str,
        delivery_message_id: str,
    ) -> RuntimeEventRecord:
        occurrence_id = outreach_delivery_proof_occurrence(
            outreach_occurrence_id,
            action_id,
            delivery_message_id,
        )
        proof = await self._event_by_occurrence(session, occurrence_id)
        if proof is None or proof.namespace != _OUTREACH_DELIVERY_PROOFS:
            raise InitiativeTransitionError(
                "spoke requires a durable platform delivery proof"
            )
        payload = proof.payload
        if (
            str(payload.get("outreach_occurrence_id") or "")
            != outreach_occurrence_id
            or str(payload.get("action_id") or "") != action_id
            or int(payload.get("claim_epoch") or 0) != int(claim_epoch)
            or str(payload.get("delivery_receipt_sha256") or "")
            != delivery_receipt_sha256
            or str(payload.get("delivery_message_id") or "")
            != delivery_message_id
        ):
            raise InitiativeConflict(
                "spoke delivery proof does not match the claimed action"
            )
        return proof

    async def resolve_outreach_expression(
        self,
        *,
        outreach_occurrence_id: str,
        outcome: InitiativeOutreachOutcome,
        action_id: str = "",
        delivery_receipt_sha256: str = "",
        delivery_message_id: str = "",
        occurred_at: str,
    ) -> InitiativeOutreachResolutionReceipt:
        """Atomically commit a terminal outcome and settle its stream turn."""

        outreach_id = str(outreach_occurrence_id or "").strip()
        normalized_outcome = str(outcome or "").strip().lower()
        exact_action_id = str(action_id or "").strip()
        exact_delivery_receipt = str(delivery_receipt_sha256 or "").strip()
        exact_delivery_message_id = str(delivery_message_id or "").strip()
        resolved_at = _parse_time(occurred_at).isoformat()
        if not outreach_id:
            raise ValueError("outreach resolution identity is incomplete")
        if normalized_outcome not in INITIATIVE_OUTREACH_OUTCOMES:
            raise ValueError("outreach resolution outcome is unsupported")
        if len(exact_action_id) > 512:
            raise ValueError("outreach resolution action_id is too long")
        if len(exact_delivery_message_id) > 512:
            raise ValueError("outreach delivery_message_id is too long")
        if normalized_outcome == "spoke":
            if (
                len(exact_delivery_receipt) != 64
                or any(char not in "0123456789abcdef" for char in exact_delivery_receipt)
                or not exact_delivery_message_id
            ):
                raise InitiativeTransitionError(
                    "spoke requires an exact platform delivery receipt"
                )
        elif exact_delivery_receipt or exact_delivery_message_id:
            raise ValueError(
                "non-spoke outreach resolution must not carry delivery evidence"
            )
        occurrence_id = outreach_resolution_occurrence(outreach_id)

        async def operation(
            session: AsyncSession,
        ) -> InitiativeOutreachResolutionReceipt:
            await self._lock_domain(session)
            replay = await self._event_by_occurrence(session, occurrence_id)
            if replay is not None:
                payload = replay.payload
                if (
                    replay.namespace != _OUTREACH_RESOLUTIONS
                    or str(payload.get("outreach_occurrence_id") or "")
                    != outreach_id
                    or str(payload.get("outcome") or "") != normalized_outcome
                    or str(payload.get("action_id") or "") != exact_action_id
                    or str(payload.get("delivery_receipt_sha256") or "")
                    != exact_delivery_receipt
                    or str(payload.get("delivery_message_id") or "")
                    != exact_delivery_message_id
                ):
                    raise InitiativeConflict(
                        "outreach resolution occurrence was reused"
                    )
                if normalized_outcome == "spoke":
                    await self._require_delivery_proof(
                        session,
                        outreach_occurrence_id=outreach_id,
                        action_id=exact_action_id,
                        claim_epoch=int(payload.get("claim_epoch") or 0),
                        delivery_receipt_sha256=exact_delivery_receipt,
                        delivery_message_id=exact_delivery_message_id,
                    )
                return InitiativeOutreachResolutionReceipt(
                    event_id=f"initiative:outreach:resolution:{replay.position}",
                    occurrence_id=occurrence_id,
                    outreach_occurrence_id=outreach_id,
                    turn_id=str(payload.get("turn_id") or ""),
                    outcome=normalized_outcome,  # type: ignore[arg-type]
                    idempotent_replay=True,
                    action_id=exact_action_id,
                    claim_epoch=int(payload.get("claim_epoch") or 0),
                    delivery_receipt_sha256=exact_delivery_receipt,
                    delivery_message_id=exact_delivery_message_id,
                )
            inbox = await self._event_by_occurrence(
                session,
                outreach_inbox_occurrence(outreach_id),
            )
            if inbox is None or inbox.namespace != _OUTREACH_INBOX_RECEIPTS:
                raise InitiativeTransitionError(
                    "outreach resolution requires a durable inbox receipt"
                )
            turn_id = str(inbox.payload.get("turn_id") or "")
            trigger_message_id = str(
                inbox.payload.get("trigger_message_id") or ""
            )
            turn_row = (
                (
                    await session.execute(
                        text(
                            """SELECT turn_id, source_message_id, status,
                                claim_owner, claim_epoch
                            FROM stream_turns WHERE turn_id = :turn_id"""
                            + self._for_update
                        ),
                        {"turn_id": turn_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if turn_row is None or str(
                turn_row["source_message_id"]
            ) != trigger_message_id:
                raise InitiativeConflict(
                    "outreach resolution lost its durable inbox turn"
                )
            turn_status = str(turn_row["status"])
            claim_owner = str(turn_row["claim_owner"] or "")
            claim_epoch = int(turn_row["claim_epoch"] or 0)
            if turn_status == "processing":
                if not exact_action_id:
                    raise InitiativeConflict(
                        "outreach resolution does not own the processing claim"
                    )
                claim = await self._event_by_occurrence(
                    session,
                    outreach_claim_occurrence(outreach_id, exact_action_id),
                )
                if claim is None or claim.namespace != _OUTREACH_CLAIMS:
                    raise InitiativeConflict(
                        "outreach resolution has no immutable action claim"
                    )
                claim_payload = claim.payload
                stored_claim_owner = str(
                    claim_payload.get("claim_owner")
                    or claim_payload.get("action_id")
                    or ""
                )
                if (
                    str(claim_payload.get("turn_id") or "") != turn_id
                    or int(claim_payload.get("claim_epoch") or 0) != claim_epoch
                    or str(claim_payload.get("action_id") or "")
                    != exact_action_id
                    or claim_owner != stored_claim_owner
                ):
                    raise InitiativeConflict(
                        "outreach resolution claim evidence is inconsistent"
                    )
            elif turn_status in {"pending", "retryable"}:
                if normalized_outcome in {"spoke", "delivery_unknown"}:
                    raise InitiativeTransitionError(
                        "visible outreach outcomes require a fenced action claim"
                    )
                if claim_owner:
                    raise InitiativeConflict(
                        "pending outreach expression unexpectedly has a claim owner"
                    )
            else:
                raise InitiativeConflict(
                    "outreach resolution turn is not open"
                )
            if normalized_outcome == "spoke":
                await self._require_delivery_proof(
                    session,
                    outreach_occurrence_id=outreach_id,
                    action_id=exact_action_id,
                    claim_epoch=claim_epoch,
                    delivery_receipt_sha256=exact_delivery_receipt,
                    delivery_message_id=exact_delivery_message_id,
                )
            database_now = await self._database_now(session)
            payload = {
                "schema_version": 2,
                "occurrence_id": occurrence_id,
                "outreach_occurrence_id": outreach_id,
                "turn_id": turn_id,
                "outcome": normalized_outcome,
                "action_id": exact_action_id,
                "claim_epoch": claim_epoch,
                "delivery_receipt_sha256": exact_delivery_receipt,
                "delivery_message_id": exact_delivery_message_id,
                "inbox_payload_sha256": str(
                    inbox.payload.get("inbox_payload_sha256") or ""
                ),
                "occurred_at": resolved_at,
            }
            record = await self._append_event(
                session,
                namespace=_OUTREACH_RESOLUTIONS,
                occurrence_id=occurrence_id,
                event_kind="outreach_expression_resolved",
                payload=payload,
                occurred_at=resolved_at,
                database_now=database_now,
            )
            result_ref = (
                "initiative://outreach/"
                + hashlib.sha256(outreach_id.encode("utf-8")).hexdigest()
                + "/resolution"
            )
            result_digest = hashlib.sha256(
                canonical_json(payload).encode("utf-8")
            ).hexdigest()
            update_sql = """UPDATE stream_turns SET status = 'completed',
                claim_owner = NULL, lease_until = NULL,
                result_ref = :result_ref,
                result_digest = :result_digest,
                updated_at = :updated_at
            WHERE turn_id = :turn_id
              AND source_message_id = :source_message_id
              AND status = :expected_status
              AND claim_epoch = :claim_epoch"""
            parameters = {
                "result_ref": result_ref,
                "result_digest": result_digest,
                "updated_at": self._bind_time(database_now),
                "turn_id": turn_id,
                "source_message_id": trigger_message_id,
                "expected_status": turn_status,
                "claim_epoch": claim_epoch,
            }
            if turn_status == "processing":
                update_sql += " AND claim_owner = :claim_owner"
                parameters["claim_owner"] = claim_owner
            else:
                update_sql += " AND claim_owner IS NULL"
            update = await session.execute(text(update_sql), parameters)
            if int(update.rowcount or 0) != 1:
                raise InitiativeConflict(
                    "outreach resolution lost its pending stream turn"
                )
            return InitiativeOutreachResolutionReceipt(
                event_id=f"initiative:outreach:resolution:{record.position}",
                occurrence_id=occurrence_id,
                outreach_occurrence_id=outreach_id,
                turn_id=turn_id,
                outcome=normalized_outcome,  # type: ignore[arg-type]
                idempotent_replay=False,
                action_id=exact_action_id,
                claim_epoch=claim_epoch,
                delivery_receipt_sha256=exact_delivery_receipt,
                delivery_message_id=exact_delivery_message_id,
            )

        return await self._write(operation)

    async def health_snapshot(self) -> dict[str, Any]:
        # One UoW is deliberate: health must never combine an old claim view
        # with a new turn or resolution and invent a transient inconsistency.
        head_decode_failed = False
        async with self.runtime.unit_of_work() as uow:
            session = uow.session
            try:
                views = await self._list_seeds_in_session(
                    session,
                    include_released=True,
                )
            except (RuntimeStateCorrupt, TypeError, ValueError):
                views = ()
                head_decode_failed = True
            seed_records = await self._namespace_events_in_session(
                session,
                _SEED_EVENTS,
            )
            reencounter_records = await self._namespace_events_in_session(
                session,
                _REENCOUNTER_DELIVERIES,
            )
            outreach_records = await self._namespace_events_in_session(
                session,
                _OUTREACH_EVENTS,
            )
            inbox_records = await self._namespace_events_in_session(
                session,
                _OUTREACH_INBOX_RECEIPTS,
            )
            claim_records = await self._namespace_events_in_session(
                session,
                _OUTREACH_CLAIMS,
            )
            proof_records = await self._namespace_events_in_session(
                session,
                _OUTREACH_DELIVERY_PROOFS,
            )
            resolution_records = await self._namespace_events_in_session(
                session,
                _OUTREACH_RESOLUTIONS,
            )
            database_now = await self._database_now(session)
            rows = (
                (
                    await session.execute(
                        text(
                            """SELECT namespace, COUNT(*) AS item_count,
                                COALESCE(MAX(position), 0) AS frontier
                            FROM runtime_events
                            WHERE namespace IN (
                                :seed, :outreach, :reencounter,
                                :legacy_outreach_delivery, :outreach_inbox,
                                :outreach_claim, :outreach_delivery_proof,
                                :outreach_resolution
                            ) GROUP BY namespace"""
                        ),
                        {
                            "seed": _SEED_EVENTS,
                            "outreach": _OUTREACH_EVENTS,
                            "reencounter": _REENCOUNTER_DELIVERIES,
                            "legacy_outreach_delivery": _OUTREACH_DELIVERIES,
                            "outreach_inbox": _OUTREACH_INBOX_RECEIPTS,
                            "outreach_claim": _OUTREACH_CLAIMS,
                            "outreach_delivery_proof": (
                                _OUTREACH_DELIVERY_PROOFS
                            ),
                            "outreach_resolution": _OUTREACH_RESOLUTIONS,
                        },
                    )
                )
                .mappings()
                .all()
            )
            head_count = int(
                await session.scalar(
                    text(
                        """SELECT COUNT(*) FROM runtime_states
                        WHERE namespace = :namespace"""
                    ),
                    {"namespace": _SEED_HEADS},
                )
                or 0
            )
            expression_turn_rows = (
                (
                    await session.execute(
                        text(
                            """SELECT t.turn_id, t.stream_id,
                                t.source_message_id, t.status,
                                t.claim_owner, t.claim_epoch, t.lease_until
                            FROM stream_turns AS t
                            JOIN inbound_messages AS m
                              ON m.message_id = t.source_message_id
                            WHERE m.source = :source"""
                        ),
                        {"source": "life_engine.proactive"},
                    )
                )
                .mappings()
                .all()
            )

        consistency_errors: set[str] = set()
        if head_decode_failed:
            consistency_errors.add("seed_head_decode_failed")
        replayed_views: dict[str, InitiativeSeedView] = {}
        try:
            seen_seed_occurrences: set[str] = set()
            for record in seed_records:
                command = seed_command_from_payload(record.payload)
                if (
                    record.payload.get("command_sha256")
                    != command.canonical_sha256()
                    or command.occurrence_id in seen_seed_occurrences
                ):
                    raise InitiativeConflict(
                        "initiative event history is corrupt"
                    )
                seen_seed_occurrences.add(command.occurrence_id)
                replayed_views[command.seed_id] = apply_seed_event(
                    replayed_views.get(command.seed_id),
                    record,
                )
            for record in reencounter_records:
                payload = record.payload
                seed_id = str(payload.get("seed_id") or "")
                revision = int(payload.get("seed_revision") or 0)
                if record.occurrence_id != reencounter_occurrence(
                    seed_id,
                    revision,
                ):
                    raise InitiativeConflict(
                        "initiative delivery history is corrupt"
                    )
                view = replayed_views.get(seed_id)
                if view is None or view.reencounter_revision != revision:
                    continue
                replayed_views[seed_id] = replace(
                    view,
                    reencounter_delivered_at=str(
                        payload.get("occurred_at") or ""
                    ),
                    reencounter_delivery_event_id=(
                        f"initiative:reencounter:delivery:{record.position}"
                    ),
                )
        except Exception:  # noqa: BLE001 - health stays content-free
            consistency_errors.add("seed_event_replay_failed")

        persisted_views = {view.seed_id: view for view in views}
        if len(persisted_views) != len(views):
            consistency_errors.add("duplicate_seed_head")
        if not head_decode_failed and "seed_event_replay_failed" not in consistency_errors:
            if replayed_views.keys() - persisted_views.keys():
                consistency_errors.add("seed_head_missing")
            if persisted_views.keys() - replayed_views.keys():
                consistency_errors.add("seed_head_orphan")
            if any(
                replayed_views[seed_id] != persisted_views[seed_id]
                for seed_id in replayed_views.keys() & persisted_views.keys()
            ):
                consistency_errors.add("seed_head_mismatch")
        outreach_by_id = {
            record.occurrence_id: record for record in outreach_records
        }
        inbox_by_outreach: dict[str, RuntimeEventRecord] = {}
        for record in inbox_records:
            outreach_id = str(record.payload.get("outreach_occurrence_id") or "")
            if not outreach_id or outreach_id not in outreach_by_id:
                consistency_errors.add("orphan_inbox")
            if outreach_id in inbox_by_outreach:
                consistency_errors.add("duplicate_inbox")
            inbox_by_outreach[outreach_id] = record

        claims_by_outreach: dict[str, list[RuntimeEventRecord]] = {}
        for record in claim_records:
            outreach_id = str(record.payload.get("outreach_occurrence_id") or "")
            claims_by_outreach.setdefault(outreach_id, []).append(record)
            if not outreach_id or outreach_id not in inbox_by_outreach:
                consistency_errors.add("orphan_claim")

        proofs_by_identity: dict[
            tuple[str, str, str], RuntimeEventRecord
        ] = {}
        for record in proof_records:
            payload = record.payload
            outreach_id = str(payload.get("outreach_occurrence_id") or "")
            action_id = str(payload.get("action_id") or "")
            message_id = str(payload.get("delivery_message_id") or "")
            receipt_hash = str(
                payload.get("delivery_receipt_sha256") or ""
            )
            identity = (outreach_id, action_id, message_id)
            if (
                not all(identity)
                or record.occurrence_id
                != outreach_delivery_proof_occurrence(*identity)
            ):
                consistency_errors.add("delivery_proof_identity_mismatch")
            if len(receipt_hash) != 64 or any(
                char not in "0123456789abcdef" for char in receipt_hash
            ):
                consistency_errors.add("delivery_proof_receipt_invalid")
            if identity in proofs_by_identity:
                consistency_errors.add("duplicate_delivery_proof")
            proofs_by_identity[identity] = record
            inbox = inbox_by_outreach.get(outreach_id)
            claims = [
                claim
                for claim in claims_by_outreach.get(outreach_id, ())
                if str(claim.payload.get("action_id") or "") == action_id
            ]
            claim = claims[0] if len(claims) == 1 else None
            if inbox is None:
                consistency_errors.add("orphan_delivery_proof")
            if claim is None:
                consistency_errors.add("delivery_proof_without_claim")
            elif (
                str(payload.get("turn_id") or "")
                != str(claim.payload.get("turn_id") or "")
                or int(payload.get("claim_epoch") or 0)
                != int(claim.payload.get("claim_epoch") or 0)
            ):
                consistency_errors.add("delivery_proof_claim_mismatch")

        resolution_by_outreach: dict[str, RuntimeEventRecord] = {}
        for record in resolution_records:
            outreach_id = str(record.payload.get("outreach_occurrence_id") or "")
            if not outreach_id or outreach_id not in inbox_by_outreach:
                consistency_errors.add("orphan_resolution")
            if outreach_id in resolution_by_outreach:
                consistency_errors.add("duplicate_resolution")
            resolution_by_outreach[outreach_id] = record
            outcome = str(record.payload.get("outcome") or "")
            receipt_hash = str(
                record.payload.get("delivery_receipt_sha256") or ""
            )
            message_id = str(record.payload.get("delivery_message_id") or "")
            if outcome == "spoke":
                if len(receipt_hash) != 64 or not message_id:
                    consistency_errors.add("spoke_without_delivery_receipt")
                proof = proofs_by_identity.get(
                    (
                        outreach_id,
                        str(record.payload.get("action_id") or ""),
                        message_id,
                    )
                )
                if proof is None:
                    consistency_errors.add("spoke_without_delivery_proof")
                elif (
                    str(
                        proof.payload.get("delivery_receipt_sha256") or ""
                    )
                    != receipt_hash
                    or int(proof.payload.get("claim_epoch") or 0)
                    != int(record.payload.get("claim_epoch") or 0)
                    or str(proof.payload.get("turn_id") or "")
                    != str(record.payload.get("turn_id") or "")
                ):
                    consistency_errors.add("spoke_delivery_proof_mismatch")
            elif receipt_hash or message_id:
                consistency_errors.add("non_spoke_with_delivery_receipt")
            elif any(key[0] == outreach_id for key in proofs_by_identity):
                consistency_errors.add("delivery_proof_non_spoke_resolution")

        turn_by_id = {
            str(row["turn_id"]): row for row in expression_turn_rows
        }
        inbox_turn_ids = {
            str(record.payload.get("turn_id") or "") for record in inbox_records
        }
        if any(str(row["turn_id"]) not in inbox_turn_ids for row in expression_turn_rows):
            consistency_errors.add("orphan_expression_turn")

        delivered_outreach_ids = {
            str(record.payload.get("outreach_occurrence_id") or "")
            for record in inbox_records
        }
        resolved_outreach_ids = {
            str(record.payload.get("outreach_occurrence_id") or "")
            for record in resolution_records
        }
        pending_outreach_records = [
            record
            for record in outreach_records
            if record.occurrence_id not in delivered_outreach_ids
        ]
        pending_expression_records = [
            record
            for record in inbox_records
            if str(record.payload.get("outreach_occurrence_id") or "")
            not in resolved_outreach_ids
        ]
        expired_processing_count = 0
        for receipt in pending_expression_records:
            outreach_id = str(
                receipt.payload.get("outreach_occurrence_id") or ""
            )
            turn_id = str(receipt.payload.get("turn_id") or "")
            turn = turn_by_id.get(turn_id)
            if turn is None:
                consistency_errors.add("missing_expression_turn")
                continue
            if (
                str(turn["stream_id"])
                != str(receipt.payload.get("stream_id") or "")
                or str(turn["source_message_id"])
                != str(receipt.payload.get("trigger_message_id") or "")
            ):
                consistency_errors.add("expression_turn_identity_mismatch")
            turn_status = str(turn["status"])
            if turn_status == "processing":
                claims = claims_by_outreach.get(outreach_id, [])
                claim = max(claims, key=lambda item: item.position) if claims else None
                if claim is None:
                    consistency_errors.add("processing_without_claim")
                    continue
                payload = claim.payload
                claim_owner = str(payload.get("claim_owner") or "")
                lease_until = str(payload.get("lease_until") or "")
                if (
                    str(payload.get("turn_id") or "") != turn_id
                    or int(payload.get("claim_epoch") or 0)
                    != int(turn["claim_epoch"] or 0)
                    or (claim_owner and claim_owner != str(turn["claim_owner"] or ""))
                ):
                    consistency_errors.add("processing_claim_mismatch")
                if not claim_owner or not lease_until:
                    expired_processing_count += 1
                elif _parse_time(lease_until) <= database_now:
                    expired_processing_count += 1
            elif turn_status in {"pending", "retryable"}:
                if turn["claim_owner"] or turn["lease_until"]:
                    consistency_errors.add("open_turn_has_claim_lease")
            else:
                consistency_errors.add("pending_inbox_has_terminal_turn")

        for outreach_id, resolution in resolution_by_outreach.items():
            receipt = inbox_by_outreach.get(outreach_id)
            if receipt is None:
                continue
            turn = turn_by_id.get(str(receipt.payload.get("turn_id") or ""))
            if turn is None or str(turn["status"]) != "completed":
                consistency_errors.add("resolution_without_completed_turn")

        oldest_pending_at = ""
        oldest_pending_age_seconds = 0.0
        if pending_outreach_records:
            oldest = min(
                _parse_time(record.occurred_at)
                for record in pending_outreach_records
            )
            oldest_pending_at = oldest.isoformat()
            oldest_pending_age_seconds = max(
                0.0,
                (database_now - oldest).total_seconds(),
            )
        oldest_expression_at = ""
        oldest_expression_age_seconds = 0.0
        if pending_expression_records:
            oldest_expression = min(
                _parse_time(record.payload.get("occurred_at") or record.occurred_at)
                for record in pending_expression_records
            )
            oldest_expression_at = oldest_expression.isoformat()
            oldest_expression_age_seconds = max(
                0.0,
                (database_now - oldest_expression).total_seconds(),
            )
        namespaces = {
            str(row["namespace"]): {
                "count": int(row["item_count"]),
                "frontier": int(row["frontier"]),
            }
            for row in rows
        }
        expression_status_counts: dict[str, int] = {}
        for row in expression_turn_rows:
            status_key = str(row["status"])
            expression_status_counts[status_key] = (
                expression_status_counts.get(status_key, 0) + 1
            )
        degraded_reasons: list[str] = []
        if expired_processing_count:
            degraded_reasons.append("expired_expression_claim")
        if oldest_pending_age_seconds >= _BACKLOG_DEGRADED_SECONDS:
            degraded_reasons.append("outreach_delivery_backlog_stale")
        if oldest_expression_age_seconds >= _BACKLOG_DEGRADED_SECONDS:
            degraded_reasons.append("expression_backlog_stale")
        status = (
            "failed"
            if consistency_errors
            else ("degraded" if degraded_reasons else "healthy")
        )
        return {
            "component": "proactive_initiative",
            "status": status,
            "seed_head_count": head_count,
            "open_count": sum(view.status == "open" for view in views),
            "released_count": sum(
                view.status == "released" for view in views
            ),
            "pending_outreach_count": len(pending_outreach_records),
            "oldest_pending_outreach_at": oldest_pending_at,
            "oldest_pending_outreach_age_seconds": round(
                oldest_pending_age_seconds,
                3,
            ),
            "pending_expression_count": len(pending_expression_records),
            "processing_expression_count": sum(
                1
                for row in expression_turn_rows
                if str(row["status"]) == "processing"
            ),
            "expired_processing_expression_count": expired_processing_count,
            "expression_turn_status_counts": expression_status_counts,
            "oldest_pending_expression_at": oldest_expression_at,
            "oldest_pending_expression_age_seconds": round(
                oldest_expression_age_seconds,
                3,
            ),
            "namespaces": namespaces,
            "outreach_claim_count": len(claim_records),
            "outreach_delivery_proof_count": len(proof_records),
            "replayed_seed_count": len(replayed_views),
            "consistency_error_types": tuple(sorted(consistency_errors)),
            "degraded_reasons": tuple(degraded_reasons),
            "database_time": database_now.isoformat(),
        }


__all__ = ["SQLInitiativeRecordStore"]
