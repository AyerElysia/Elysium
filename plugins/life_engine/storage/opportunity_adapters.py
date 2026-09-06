"""Local/MySQL adapters for the subject-managed opportunity authority."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.kernel.storage import canonical_json

from ._write_base import run_write_attempts
from .contracts import StorageBackendRuntime
from .models import BackendKind
from .opportunity_contracts import (
    OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE,
    OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY,
    OpportunityAction,
    OpportunityActorInactive,
    OpportunityCommit,
    OpportunityConflict,
    OpportunityDeliveryCommit,
    OpportunityDeliveryReceipt,
    OpportunityDeliveryRecord,
    OpportunityDeliveryRejected,
    OpportunityHistoryChunk,
    OpportunityHistoryFamily,
    OpportunityHistoryPage,
    OpportunityHistoryRecord,
    OpportunityOccurrence,
    OpportunityOrigin,
    OpportunityPage,
    OpportunityPublication,
    OpportunityRegistration,
    OpportunityRegistrationCommand,
    OpportunitySchedule,
    OpportunitySchedulerClaimRequired,
    OpportunityStatus,
    OpportunityTransitionError,
    ProviderAction,
    ProviderBinding,
    ProviderBindingCommand,
    ProviderCommit,
    ProviderOpportunityProposal,
    ProviderPage,
    ProviderStatus,
    PublicationStatus,
    WorkflowChunk,
    WorkflowVersion,
    WorkflowVersionCommand,
    _limit,
)
from .proactive_decision_guard import (
    ProactiveDecisionGuardConflict,
    claim_proactive_decision,
)
from .writer_claims import SingletonWriterClaim

_T = TypeVar("_T")
_MAX_CHUNK_BYTES = 256 * 1024


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value or ""))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: Any) -> str:
    return _parse_time(value).isoformat()


def _json_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError("OpportunityStoredSourceOccurrencesCorrupt")
    return tuple(value)


def _bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return str(value).encode("utf-8")


def _transition_provider(
    previous: ProviderBinding | None,
    command: ProviderBindingCommand,
) -> ProviderStatus:
    if previous is None:
        if command.action != ProviderAction.INSTALL:
            raise OpportunityTransitionError("provider must be installed first")
        return ProviderStatus.ENABLED
    if command.action == ProviderAction.INSTALL:
        if previous.status != ProviderStatus.UNINSTALLED:
            raise OpportunityTransitionError(
                "install is only valid for a new/uninstalled provider"
            )
        return ProviderStatus.ENABLED
    if command.action == ProviderAction.BIND_WORKFLOW:
        if previous.status == ProviderStatus.UNINSTALLED:
            raise OpportunityTransitionError(
                "an uninstalled provider must be installed explicitly"
            )
        return previous.status
    if command.action == ProviderAction.PAUSE:
        if previous.status != ProviderStatus.ENABLED:
            raise OpportunityTransitionError("only an enabled provider can be paused")
        return ProviderStatus.PAUSED
    if command.action == ProviderAction.RESUME:
        if previous.status != ProviderStatus.PAUSED:
            raise OpportunityTransitionError("only a paused provider can be resumed")
        return ProviderStatus.ENABLED
    if previous.status == ProviderStatus.UNINSTALLED:
        raise OpportunityTransitionError(
            "an uninstalled provider must be installed explicitly"
        )
    return ProviderStatus.UNINSTALLED


def _transition_opportunity(
    previous: OpportunityRegistration | None,
    action: OpportunityAction,
) -> OpportunityStatus:
    if previous is None:
        if action != OpportunityAction.OPEN:
            raise OpportunityTransitionError("opportunity must be opened first")
        return OpportunityStatus.OPEN
    if action == OpportunityAction.OPEN:
        raise OpportunityTransitionError("existing opportunity cannot be opened again")
    if action == OpportunityAction.CONFIGURE:
        if previous.status == OpportunityStatus.CLOSED:
            raise OpportunityTransitionError("closed opportunity is terminal")
        return previous.status
    if action == OpportunityAction.PAUSE:
        if previous.status != OpportunityStatus.OPEN:
            raise OpportunityTransitionError("only an open opportunity can be paused")
        return OpportunityStatus.PAUSED
    if action == OpportunityAction.RESUME:
        if previous.status != OpportunityStatus.PAUSED:
            raise OpportunityTransitionError("only a paused opportunity can be resumed")
        return OpportunityStatus.OPEN
    if previous.status == OpportunityStatus.CLOSED:
        raise OpportunityTransitionError("closed opportunity is terminal")
    return OpportunityStatus.CLOSED


def _token(kind: str, frontier: int, after: str) -> str:
    body = canonical_json({"kind": kind, "frontier": frontier, "after": after})
    envelope = canonical_json(
        {"body": body, "sha256": hashlib.sha256(body.encode()).hexdigest()}
    )
    return base64.urlsafe_b64encode(envelope.encode()).decode().rstrip("=")


def _parse_token(value: str, kind: str) -> tuple[int, str]:
    if not value:
        return 0, ""
    try:
        padded = value + "=" * (-len(value) % 4)
        envelope = json.loads(base64.urlsafe_b64decode(padded).decode())
        body = str(envelope["body"])
        if hashlib.sha256(body.encode()).hexdigest() != envelope["sha256"]:
            raise ValueError
        payload = json.loads(body)
        if payload["kind"] != kind or int(payload["frontier"]) <= 0:
            raise ValueError
        return int(payload["frontier"]), str(payload["after"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OpportunityConflict("OpportunityContinuationInvalid") from exc


class SQLOpportunityStore:
    """One authority plus optional singleton scheduler on a coherent runtime."""

    def __init__(
        self,
        runtime: StorageBackendRuntime,
        *,
        writer_claim: SingletonWriterClaim | None = None,
        validate_active_actor: Callable[[str], Awaitable[bool]] | None = None,
        actor_decision_guard: (
            Callable[[str], AbstractAsyncContextManager[None]] | None
        ) = None,
    ) -> None:
        if not runtime.enabled or runtime.engine is None:
            raise RuntimeError("opportunity adapter requires enabled storage")
        if writer_claim is not None and (
            writer_claim.namespace != OPPORTUNITY_SCHEDULER_CLAIM_NAMESPACE
            or writer_claim.state_key != OPPORTUNITY_SCHEDULER_CLAIM_STATE_KEY
        ):
            raise ValueError("OpportunitySchedulerClaimScopeMismatch")
        if (
            runtime.backend == BackendKind.LOCAL
            and validate_active_actor is not None
            and actor_decision_guard is None
        ):
            raise ValueError("local opportunity actor validation requires commit gate")
        self.runtime = runtime
        self.backend = runtime.backend
        self.writer_claim = writer_claim
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
        statement = (
            "SELECT CURRENT_TIMESTAMP(6)"
            if self.backend == BackendKind.MYSQL
            else "SELECT STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
        )
        return _parse_time(await session.scalar(text(statement)))

    async def _write(
        self,
        operation: Callable[[AsyncSession], Awaitable[_T]],
        *,
        actor_id: str = "",
        scheduler: bool = False,
    ) -> _T:
        if scheduler and self.writer_claim is None:
            raise OpportunitySchedulerClaimRequired("OpportunitySchedulerClaimRequired")

        async def attempt() -> _T:
            claim = self.writer_claim if scheduler else None
            async with self.runtime.unit_of_work(writer_claim=claim) as uow:
                if claim is not None:
                    await self.runtime.bind_singleton_writer_write(
                        uow.session,
                        claim,
                    )
                try:
                    return await operation(uow.session)
                finally:
                    if claim is not None:
                        await self.runtime.clear_singleton_writer_write(uow.session)

        async def run() -> _T:
            return await run_write_attempts(
                attempt,
                exhaustion_message="bounded opportunity write retries exhausted",
            )

        if actor_id and self._actor_decision_guard is not None:
            async with self._actor_decision_guard(actor_id):
                return await run()
        return await run()

    async def _assert_active_actor(
        self,
        session: AsyncSession,
        actor_id: str,
        database_now: datetime,
    ) -> None:
        if self._validate_active_actor is not None:
            if not await self._validate_active_actor(actor_id):
                raise OpportunityActorInactive(actor_id)
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
            raise OpportunityActorInactive(actor_id)
        lease = row["lease_expires_at"]
        if lease not in (None, "") and _parse_time(lease) <= database_now:
            raise OpportunityActorInactive(actor_id)

    @staticmethod
    def _decode_provider(row: Any) -> ProviderBinding:
        return ProviderBinding(
            provider_id=str(row["provider_id"]),
            status=ProviderStatus(str(row["status"])),
            revision=int(row["revision"]),
            descriptor_version=str(row["descriptor_version"]),
            descriptor_sha256=str(row["descriptor_sha256"]),
            workflow_id=str(row["workflow_id"]),
            workflow_revision=int(row["workflow_revision"]),
            workflow_sha256=str(row["workflow_sha256"]),
            last_occurrence_id=str(row["last_occurrence_id"]),
            last_event_position=int(row["last_event_position"]),
            updated_at=_iso(row["updated_at"]),
        )

    async def _provider(
        self,
        session: AsyncSession,
        provider_id: str,
        *,
        for_update: bool = False,
    ) -> ProviderBinding | None:
        row = (
            (
                await session.execute(
                    text(
                        """SELECT provider_id, status, revision,
                        descriptor_version, descriptor_sha256, workflow_id,
                        workflow_revision, workflow_sha256,
                        last_occurrence_id, last_event_position, updated_at
                        FROM opportunity_provider_heads
                        WHERE provider_id = :provider_id"""
                        + (self._for_update if for_update else "")
                    ),
                    {"provider_id": provider_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._decode_provider(row) if row is not None else None

    async def _provider_event(self, session: AsyncSession, occurrence_id: str) -> Any:
        return (
            (
                await session.execute(
                    text(
                        """SELECT provider_id, action, status, revision, event_sha256
                        FROM opportunity_provider_events
                        WHERE occurrence_id = :occurrence_id"""
                        + self._for_update
                    ),
                    {"occurrence_id": occurrence_id},
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _claim_subject_decision(
        self,
        session: AsyncSession,
        *,
        occurrence_id: str,
        family: str,
        digest: str,
        occurred_at: str,
        recorded_at: datetime,
    ) -> None:
        try:
            await claim_proactive_decision(
                session,
                backend=self.backend,
                occurrence_id=occurrence_id,
                record_family=family,
                command_sha256=digest,
                occurred_at=occurred_at,
                recorded_at=recorded_at,
            )
        except ProactiveDecisionGuardConflict as exc:
            raise OpportunityConflict(
                scope=family,
                identity=occurrence_id,
            ) from exc

    async def manage_provider(
        self,
        command: ProviderBindingCommand,
    ) -> ProviderCommit:
        """Gate actor, append immutable decision, and CAS provider head."""

        async def operation(session: AsyncSession) -> ProviderCommit:
            now = await self._database_now(session)
            digest = command.canonical_sha256()
            await self._claim_subject_decision(
                session,
                occurrence_id=command.occurrence_id,
                family="opportunity.provider",
                digest=digest,
                occurred_at=command.occurred_at,
                recorded_at=now,
            )
            replay = await self._provider_event(session, command.occurrence_id)
            if replay is not None:
                if str(replay["event_sha256"]) != digest:
                    raise OpportunityConflict(
                        scope="provider_occurrence",
                        identity=command.occurrence_id,
                    )
                return ProviderCommit(
                    provider_id=str(replay["provider_id"]),
                    occurrence_id=command.occurrence_id,
                    revision=int(replay["revision"]),
                    status=ProviderStatus(str(replay["status"])),
                    event_sha256=digest,
                    idempotent_replay=True,
                )
            await self._assert_active_actor(
                session,
                command.actor_consciousness_instance_id,
                now,
            )
            workflow = await self._workflow(
                session,
                command.workflow_id,
                command.workflow_revision,
                for_update=False,
            )
            if (
                workflow is None
                or workflow.provider_id != command.provider_id
                or workflow.content_sha256 != command.workflow_sha256
            ):
                raise OpportunityConflict(
                    scope="provider_workflow",
                    identity=command.workflow_id,
                )
            previous = await self._provider(
                session,
                command.provider_id,
                for_update=True,
            )
            actual_revision = previous.revision if previous is not None else 0
            if actual_revision != command.expected_revision:
                raise OpportunityConflict(
                    scope="provider_revision",
                    identity=command.provider_id,
                    expected_revision=command.expected_revision,
                    actual_revision=actual_revision,
                )
            status = _transition_provider(previous, command)
            if previous is not None and command.action != ProviderAction.INSTALL:
                exact_descriptor = (
                    previous.descriptor_version == command.descriptor_version
                    and previous.descriptor_sha256 == command.descriptor_sha256
                )
                exact_workflow = (
                    previous.workflow_id == command.workflow_id
                    and previous.workflow_revision == command.workflow_revision
                    and previous.workflow_sha256 == command.workflow_sha256
                )
                if not exact_descriptor or (
                    command.action != ProviderAction.BIND_WORKFLOW
                    and not exact_workflow
                ):
                    raise OpportunityConflict(
                        scope="provider_binding",
                        identity=command.provider_id,
                    )
            revision = command.expected_revision + 1
            prefix = (
                "INSERT IGNORE"
                if self.backend == BackendKind.MYSQL
                else "INSERT OR IGNORE"
            )
            await session.execute(
                text(
                    f"""{prefix} INTO opportunity_provider_events (
                        occurrence_id, provider_id, action, status,
                        actor_consciousness_instance_id, source_instance_id,
                        source_occurrence_ids_json, causation_occurrence_id,
                        expected_revision, revision, descriptor_version,
                        descriptor_sha256, workflow_id, workflow_revision,
                        workflow_sha256, reason, occurred_at, recorded_at,
                        event_sha256
                    ) VALUES (
                        :occurrence_id, :provider_id, :action, :status, :actor,
                        :source_instance, :sources, :causation,
                        :expected_revision, :revision, :descriptor_version,
                        :descriptor_sha256, :workflow_id, :workflow_revision,
                        :workflow_sha256, :reason, :occurred_at, :recorded_at,
                        :event_sha256
                    )"""
                ),
                {
                    "occurrence_id": command.occurrence_id,
                    "provider_id": command.provider_id,
                    "action": command.action.value,
                    "status": status.value,
                    "actor": command.actor_consciousness_instance_id,
                    "source_instance": command.source_instance_id,
                    "sources": canonical_json(list(command.source_occurrence_ids)),
                    "causation": command.causation_occurrence_id,
                    "expected_revision": command.expected_revision,
                    "revision": revision,
                    "descriptor_version": command.descriptor_version,
                    "descriptor_sha256": command.descriptor_sha256,
                    "workflow_id": command.workflow_id,
                    "workflow_revision": command.workflow_revision,
                    "workflow_sha256": command.workflow_sha256,
                    "reason": command.reason,
                    "occurred_at": self._bind_time(command.occurred_at),
                    "recorded_at": self._bind_time(now),
                    "event_sha256": digest,
                },
            )
            event_row = await self._provider_event(session, command.occurrence_id)
            if event_row is None or str(event_row["event_sha256"]) != digest:
                raise OpportunityConflict(
                    scope="provider_occurrence",
                    identity=command.occurrence_id,
                )
            position = await session.scalar(
                text(
                    """SELECT position FROM opportunity_provider_events
                    WHERE occurrence_id = :occurrence_id"""
                ),
                {"occurrence_id": command.occurrence_id},
            )
            params = {
                "provider_id": command.provider_id,
                "status": status.value,
                "revision": revision,
                "descriptor_version": command.descriptor_version,
                "descriptor_sha256": command.descriptor_sha256,
                "workflow_id": command.workflow_id,
                "workflow_revision": command.workflow_revision,
                "workflow_sha256": command.workflow_sha256,
                "occurrence_id": command.occurrence_id,
                "position": int(position),
                "updated_at": self._bind_time(now),
                "expected_revision": command.expected_revision,
            }
            if previous is None:
                await session.execute(
                    text(
                        """INSERT INTO opportunity_provider_heads (
                            provider_id, status, revision, descriptor_version,
                            descriptor_sha256, workflow_id, workflow_revision,
                            workflow_sha256, last_occurrence_id,
                            last_event_position, updated_at
                        ) VALUES (
                            :provider_id, :status, :revision,
                            :descriptor_version, :descriptor_sha256,
                            :workflow_id, :workflow_revision, :workflow_sha256,
                            :occurrence_id, :position, :updated_at
                        )"""
                    ),
                    params,
                )
            else:
                result = await session.execute(
                    text(
                        """UPDATE opportunity_provider_heads SET
                            status=:status, revision=:revision,
                            descriptor_version=:descriptor_version,
                            descriptor_sha256=:descriptor_sha256,
                            workflow_id=:workflow_id,
                            workflow_revision=:workflow_revision,
                            workflow_sha256=:workflow_sha256,
                            last_occurrence_id=:occurrence_id,
                            last_event_position=:position,
                            updated_at=:updated_at
                        WHERE provider_id=:provider_id
                          AND revision=:expected_revision"""
                    ),
                    params,
                )
                if result.rowcount != 1:
                    raise OpportunityConflict(
                        scope="provider_revision",
                        identity=command.provider_id,
                        expected_revision=command.expected_revision,
                    )
            return ProviderCommit(
                provider_id=command.provider_id,
                occurrence_id=command.occurrence_id,
                revision=revision,
                status=status,
                event_sha256=digest,
                idempotent_replay=False,
            )

        return await self._write(
            operation,
            actor_id=command.actor_consciousness_instance_id,
        )

    async def get_provider(self, provider_id: str) -> ProviderBinding | None:
        identity = str(provider_id or "").strip()
        if not identity:
            raise ValueError("provider_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            return await self._provider(uow.session, identity)

    @staticmethod
    def _decode_workflow(row: Any) -> WorkflowVersion:
        content = _bytes(row["content_bytes"])
        digest = hashlib.sha256(content).hexdigest()
        if digest != str(row["content_sha256"]):
            raise RuntimeError(
                f"OpportunityWorkflowCorrupt:{row['workflow_id']}:{row['revision']}"
            )
        command = WorkflowVersionCommand(
            occurrence_id=str(row["occurrence_id"]),
            workflow_id=str(row["workflow_id"]),
            provider_id=str(row["provider_id"]),
            actor_consciousness_instance_id=str(row["actor_consciousness_instance_id"]),
            source_instance_id=str(row["source_instance_id"]),
            source_occurrence_ids=_json_tuple(row["source_occurrence_ids_json"]),
            causation_occurrence_id=str(row["causation_occurrence_id"]),
            expected_revision=int(row["revision"]) - 1,
            schema_version=int(row["schema_version"]),
            content_bytes=content,
            content_sha256=digest,
            reason=str(row["reason"]),
            occurred_at=_iso(row["occurred_at"]),
        )
        if command.canonical_sha256() != str(row["event_sha256"]):
            raise RuntimeError(
                f"OpportunityWorkflowEventCorrupt:{command.occurrence_id}"
            )
        return WorkflowVersion(
            position=int(row["position"]),
            occurrence_id=command.occurrence_id,
            workflow_id=command.workflow_id,
            provider_id=command.provider_id,
            revision=int(row["revision"]),
            schema_version=command.schema_version,
            actor_consciousness_instance_id=command.actor_consciousness_instance_id,
            source_instance_id=command.source_instance_id,
            source_occurrence_ids=command.source_occurrence_ids,
            causation_occurrence_id=command.causation_occurrence_id,
            content_bytes=content,
            content_sha256=digest,
            reason=command.reason,
            occurred_at=command.occurred_at,
            recorded_at=_iso(row["recorded_at"]),
            event_sha256=str(row["event_sha256"]),
        )

    async def _workflow(
        self,
        session: AsyncSession,
        workflow_id: str,
        revision: int,
        *,
        for_update: bool = False,
    ) -> WorkflowVersion | None:
        row = (
            (
                await session.execute(
                    text(
                        """SELECT position, occurrence_id, workflow_id,
                        provider_id, revision, schema_version,
                        actor_consciousness_instance_id, source_instance_id,
                        source_occurrence_ids_json, causation_occurrence_id,
                        content_bytes, content_sha256, reason, occurred_at,
                        recorded_at, event_sha256
                        FROM opportunity_workflow_versions
                        WHERE workflow_id=:workflow_id AND revision=:revision"""
                        + (self._for_update if for_update else "")
                    ),
                    {"workflow_id": workflow_id, "revision": int(revision)},
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._decode_workflow(row) if row is not None else None

    async def _workflow_by_occurrence(
        self,
        session: AsyncSession,
        occurrence_id: str,
    ) -> WorkflowVersion | None:
        row = (
            (
                await session.execute(
                    text(
                        """SELECT position, occurrence_id, workflow_id,
                        provider_id, revision, schema_version,
                        actor_consciousness_instance_id, source_instance_id,
                        source_occurrence_ids_json, causation_occurrence_id,
                        content_bytes, content_sha256, reason, occurred_at,
                        recorded_at, event_sha256
                        FROM opportunity_workflow_versions
                        WHERE occurrence_id=:occurrence_id"""
                        + self._for_update
                    ),
                    {"occurrence_id": occurrence_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._decode_workflow(row) if row is not None else None

    async def append_workflow(
        self,
        command: WorkflowVersionCommand,
    ) -> WorkflowVersion:
        """Append one exact UTF-8 workflow version with actor and CAS proof."""

        async def operation(session: AsyncSession) -> WorkflowVersion:
            now = await self._database_now(session)
            digest = command.canonical_sha256()
            await self._claim_subject_decision(
                session,
                occurrence_id=command.occurrence_id,
                family="opportunity.workflow",
                digest=digest,
                occurred_at=command.occurred_at,
                recorded_at=now,
            )
            replay = await self._workflow_by_occurrence(
                session,
                command.occurrence_id,
            )
            if replay is not None:
                if replay.event_sha256 != digest:
                    raise OpportunityConflict(
                        scope="workflow_occurrence",
                        identity=command.occurrence_id,
                    )
                return replace(replay, idempotent_replay=True)
            await self._assert_active_actor(
                session,
                command.actor_consciousness_instance_id,
                now,
            )
            latest = (
                (
                    await session.execute(
                        text(
                            """SELECT revision FROM opportunity_workflow_versions
                            WHERE workflow_id=:workflow_id
                            ORDER BY revision DESC LIMIT 1"""
                            + self._for_update
                        ),
                        {"workflow_id": command.workflow_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            actual_revision = int(latest["revision"]) if latest is not None else 0
            if actual_revision != command.expected_revision:
                raise OpportunityConflict(
                    scope="workflow_revision",
                    identity=command.workflow_id,
                    expected_revision=command.expected_revision,
                    actual_revision=actual_revision,
                )
            prefix = (
                "INSERT IGNORE"
                if self.backend == BackendKind.MYSQL
                else "INSERT OR IGNORE"
            )
            await session.execute(
                text(
                    f"""{prefix} INTO opportunity_workflow_versions (
                        occurrence_id, workflow_id, provider_id, revision,
                        schema_version, actor_consciousness_instance_id,
                        source_instance_id, source_occurrence_ids_json,
                        causation_occurrence_id, content_bytes, content_sha256,
                        reason, occurred_at, recorded_at, event_sha256
                    ) VALUES (
                        :occurrence_id, :workflow_id, :provider_id, :revision,
                        :schema_version, :actor, :source_instance, :sources,
                        :causation, :content, :content_sha256, :reason,
                        :occurred_at, :recorded_at, :event_sha256
                    )"""
                ),
                {
                    "occurrence_id": command.occurrence_id,
                    "workflow_id": command.workflow_id,
                    "provider_id": command.provider_id,
                    "revision": command.expected_revision + 1,
                    "schema_version": command.schema_version,
                    "actor": command.actor_consciousness_instance_id,
                    "source_instance": command.source_instance_id,
                    "sources": canonical_json(list(command.source_occurrence_ids)),
                    "causation": command.causation_occurrence_id,
                    "content": command.content_bytes,
                    "content_sha256": command.content_sha256,
                    "reason": command.reason,
                    "occurred_at": self._bind_time(command.occurred_at),
                    "recorded_at": self._bind_time(now),
                    "event_sha256": digest,
                },
            )
            persisted = await self._workflow_by_occurrence(
                session,
                command.occurrence_id,
            )
            if persisted is None or persisted.event_sha256 != digest:
                raise OpportunityConflict(
                    scope="workflow_revision",
                    identity=command.workflow_id,
                    expected_revision=command.expected_revision,
                )
            return persisted

        return await self._write(
            operation,
            actor_id=command.actor_consciousness_instance_id,
        )

    async def get_workflow(
        self,
        workflow_id: str,
        revision: int,
    ) -> WorkflowVersion | None:
        identity = str(workflow_id or "").strip()
        if not identity or int(revision) <= 0:
            raise ValueError("workflow_id/revision must identify an exact version")
        async with self.runtime.unit_of_work() as uow:
            return await self._workflow(uow.session, identity, int(revision))

    async def read_workflow_chunk(
        self,
        workflow_id: str,
        revision: int,
        *,
        offset_bytes: int,
        max_bytes: int,
    ) -> WorkflowChunk:
        offset = int(offset_bytes)
        maximum = int(max_bytes)
        if offset < 0 or maximum <= 0 or maximum > _MAX_CHUNK_BYTES:
            raise ValueError("invalid workflow byte window")
        workflow = await self.get_workflow(workflow_id, revision)
        if workflow is None:
            raise KeyError(f"workflow not found: {workflow_id}:{revision}")
        raw = workflow.content_bytes
        if offset > len(raw):
            raise ValueError("offset_bytes exceeds workflow length")
        try:
            raw[:offset].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("offset_bytes splits a UTF-8 sequence") from exc
        end = min(len(raw), offset + maximum)
        while end > offset:
            try:
                content = raw[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            if offset < len(raw):
                raise ValueError("max_bytes cannot fit the next UTF-8 code point")
            content = ""
        return WorkflowChunk(
            workflow_id=workflow.workflow_id,
            revision=workflow.revision,
            content_sha256=workflow.content_sha256,
            offset_bytes=offset,
            next_offset_bytes=end,
            total_bytes=len(raw),
            content=content,
            complete=end == len(raw),
        )

    @staticmethod
    def _decode_registration(row: Any) -> OpportunityRegistration:
        return OpportunityRegistration(
            opportunity_id=str(row["opportunity_id"]),
            provider_id=str(row["provider_id"]),
            origin=OpportunityOrigin(str(row["origin"])),
            status=OpportunityStatus(str(row["status"])),
            revision=int(row["revision"]),
            referent_kind=str(row["referent_kind"]),
            referent_id=str(row["referent_id"]),
            referent_revision=int(row["referent_revision"]),
            referent_sha256=str(row["referent_sha256"]),
            workflow_id=str(row["workflow_id"]),
            workflow_revision=int(row["workflow_revision"]),
            workflow_sha256=str(row["workflow_sha256"]),
            schedule=OpportunitySchedule(str(row["schedule"])),
            first_due_at=(
                _iso(row["first_due_at"])
                if row["first_due_at"] not in (None, "")
                else ""
            ),
            interval_seconds=int(row["interval_seconds"]),
            last_occurrence_id=str(row["last_occurrence_id"]),
            last_event_position=int(row["last_event_position"]),
            updated_at=_iso(row["updated_at"]),
        )

    async def _registration(
        self,
        session: AsyncSession,
        opportunity_id: str,
        *,
        for_update: bool = False,
    ) -> OpportunityRegistration | None:
        row = (
            (
                await session.execute(
                    text(
                        """SELECT opportunity_id, provider_id, origin, status,
                        revision, referent_kind, referent_id,
                        referent_revision, referent_sha256, workflow_id,
                        workflow_revision, workflow_sha256, schedule,
                        first_due_at, interval_seconds, last_occurrence_id,
                        last_event_position, updated_at
                        FROM opportunity_registration_heads
                        WHERE opportunity_id=:opportunity_id"""
                        + (self._for_update if for_update else "")
                    ),
                    {"opportunity_id": opportunity_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._decode_registration(row) if row is not None else None

    async def _registration_event(
        self,
        session: AsyncSession,
        occurrence_id: str,
    ) -> Any:
        return (
            (
                await session.execute(
                    text(
                        """SELECT opportunity_id, status, revision,
                        event_sha256 FROM opportunity_registration_events
                        WHERE occurrence_id=:occurrence_id"""
                        + self._for_update
                    ),
                    {"occurrence_id": occurrence_id},
                )
            )
            .mappings()
            .one_or_none()
        )

    async def _commit_registration(
        self,
        session: AsyncSession,
        value: OpportunityRegistrationCommand | ProviderOpportunityProposal,
        *,
        origin: OpportunityOrigin,
        now: datetime,
        digest: str,
    ) -> OpportunityCommit:
        replay = await self._registration_event(session, value.occurrence_id)
        if replay is not None:
            if str(replay["event_sha256"]) != digest:
                raise OpportunityConflict(
                    scope="opportunity_occurrence",
                    identity=value.occurrence_id,
                )
            return OpportunityCommit(
                opportunity_id=str(replay["opportunity_id"]),
                occurrence_id=value.occurrence_id,
                revision=int(replay["revision"]),
                status=OpportunityStatus(str(replay["status"])),
                event_sha256=digest,
                idempotent_replay=True,
            )
        subject = isinstance(value, OpportunityRegistrationCommand)
        if subject:
            await self._assert_active_actor(
                session,
                value.actor_consciousness_instance_id,
                now,
            )
        provider = await self._provider(
            session,
            value.provider_id,
            for_update=False,
        )
        if provider is None or provider.status == ProviderStatus.UNINSTALLED:
            raise OpportunityConflict(
                scope="opportunity_provider",
                identity=value.provider_id,
            )
        workflow = await self._workflow(
            session,
            value.workflow_id,
            value.workflow_revision,
            for_update=False,
        )
        if (
            workflow is None
            or workflow.provider_id != value.provider_id
            or workflow.content_sha256 != value.workflow_sha256
        ):
            raise OpportunityConflict(
                scope="opportunity_workflow",
                identity=value.workflow_id,
            )
        if not subject and (
            provider.status != ProviderStatus.ENABLED
            or provider.workflow_id != value.workflow_id
            or provider.workflow_revision != value.workflow_revision
            or provider.workflow_sha256 != value.workflow_sha256
        ):
            raise OpportunityConflict(
                scope="provider_proposal_binding",
                identity=value.provider_id,
            )
        previous = await self._registration(
            session,
            value.opportunity_id,
            for_update=True,
        )
        expected_revision = value.expected_revision if subject else 0
        actual_revision = previous.revision if previous is not None else 0
        if actual_revision != expected_revision:
            raise OpportunityConflict(
                scope="opportunity_revision",
                identity=value.opportunity_id,
                expected_revision=expected_revision,
                actual_revision=actual_revision,
            )
        action = value.action if subject else OpportunityAction.OPEN
        status = _transition_opportunity(previous, action)
        if previous is not None:
            if previous.provider_id != value.provider_id:
                raise OpportunityConflict(
                    scope="opportunity_provider",
                    identity=value.opportunity_id,
                )
            if action != OpportunityAction.CONFIGURE:
                exact = (
                    previous.referent_kind == value.referent_kind
                    and previous.referent_id == value.referent_id
                    and previous.referent_revision == value.referent_revision
                    and previous.referent_sha256 == value.referent_sha256
                    and previous.workflow_id == value.workflow_id
                    and previous.workflow_revision == value.workflow_revision
                    and previous.workflow_sha256 == value.workflow_sha256
                    and previous.schedule == value.schedule
                    and previous.first_due_at == value.first_due_at
                    and previous.interval_seconds == value.interval_seconds
                )
                if not exact:
                    raise OpportunityConflict(
                        scope="opportunity_state",
                        identity=value.opportunity_id,
                    )
        revision = expected_revision + 1
        prefix = (
            "INSERT IGNORE" if self.backend == BackendKind.MYSQL else "INSERT OR IGNORE"
        )
        actor = value.actor_consciousness_instance_id if subject else ""
        reason = value.reason if subject else ""
        await session.execute(
            text(
                f"""{prefix} INTO opportunity_registration_events (
                    occurrence_id, opportunity_id, provider_id, origin,
                    action, status, actor_consciousness_instance_id,
                    source_instance_id, source_occurrence_ids_json,
                    causation_occurrence_id, expected_revision, revision,
                    referent_kind, referent_id, referent_revision,
                    referent_sha256, workflow_id, workflow_revision,
                    workflow_sha256, schedule, first_due_at,
                    interval_seconds, reason, occurred_at, recorded_at,
                    event_sha256
                ) VALUES (
                    :occurrence_id, :opportunity_id, :provider_id, :origin,
                    :action, :status, :actor, :source_instance, :sources,
                    :causation, :expected_revision, :revision,
                    :referent_kind, :referent_id, :referent_revision,
                    :referent_sha256, :workflow_id, :workflow_revision,
                    :workflow_sha256, :schedule, :first_due_at,
                    :interval_seconds, :reason, :occurred_at, :recorded_at,
                    :event_sha256
                )"""
            ),
            {
                "occurrence_id": value.occurrence_id,
                "opportunity_id": value.opportunity_id,
                "provider_id": value.provider_id,
                "origin": (
                    previous.origin.value if previous is not None else origin.value
                ),
                "action": action.value,
                "status": status.value,
                "actor": actor,
                "source_instance": value.source_instance_id,
                "sources": canonical_json(list(value.source_occurrence_ids)),
                "causation": value.causation_occurrence_id,
                "expected_revision": expected_revision,
                "revision": revision,
                "referent_kind": value.referent_kind,
                "referent_id": value.referent_id,
                "referent_revision": value.referent_revision,
                "referent_sha256": value.referent_sha256,
                "workflow_id": value.workflow_id,
                "workflow_revision": value.workflow_revision,
                "workflow_sha256": value.workflow_sha256,
                "schedule": value.schedule.value,
                "first_due_at": (
                    self._bind_time(value.first_due_at)
                    if value.first_due_at
                    else None
                    if self.backend == BackendKind.MYSQL
                    else ""
                ),
                "interval_seconds": value.interval_seconds,
                "reason": reason,
                "occurred_at": self._bind_time(value.occurred_at),
                "recorded_at": self._bind_time(now),
                "event_sha256": digest,
            },
        )
        event = await self._registration_event(session, value.occurrence_id)
        if event is None or str(event["event_sha256"]) != digest:
            raise OpportunityConflict(
                scope="opportunity_occurrence",
                identity=value.occurrence_id,
            )
        position = int(
            await session.scalar(
                text(
                    """SELECT position FROM opportunity_registration_events
                    WHERE occurrence_id=:occurrence_id"""
                ),
                {"occurrence_id": value.occurrence_id},
            )
        )
        params = {
            "opportunity_id": value.opportunity_id,
            "provider_id": value.provider_id,
            "origin": previous.origin.value if previous is not None else origin.value,
            "status": status.value,
            "revision": revision,
            "referent_kind": value.referent_kind,
            "referent_id": value.referent_id,
            "referent_revision": value.referent_revision,
            "referent_sha256": value.referent_sha256,
            "workflow_id": value.workflow_id,
            "workflow_revision": value.workflow_revision,
            "workflow_sha256": value.workflow_sha256,
            "schedule": value.schedule.value,
            "first_due_at": (
                self._bind_time(value.first_due_at)
                if value.first_due_at
                else None
                if self.backend == BackendKind.MYSQL
                else ""
            ),
            "interval_seconds": value.interval_seconds,
            "last_occurrence_id": value.occurrence_id,
            "last_event_position": position,
            "updated_at": self._bind_time(now),
            "expected_revision": expected_revision,
        }
        if previous is None:
            await session.execute(
                text(
                    """INSERT INTO opportunity_registration_heads (
                        opportunity_id, provider_id, origin, status, revision,
                        referent_kind, referent_id, referent_revision,
                        referent_sha256, workflow_id, workflow_revision,
                        workflow_sha256, schedule, first_due_at,
                        interval_seconds, last_occurrence_id,
                        last_event_position, updated_at
                    ) VALUES (
                        :opportunity_id, :provider_id, :origin, :status,
                        :revision, :referent_kind, :referent_id,
                        :referent_revision, :referent_sha256, :workflow_id,
                        :workflow_revision, :workflow_sha256, :schedule,
                        :first_due_at, :interval_seconds, :last_occurrence_id,
                        :last_event_position, :updated_at
                    )"""
                ),
                params,
            )
        else:
            result = await session.execute(
                text(
                    """UPDATE opportunity_registration_heads SET
                        provider_id=:provider_id, origin=:origin,
                        status=:status, revision=:revision,
                        referent_kind=:referent_kind, referent_id=:referent_id,
                        referent_revision=:referent_revision,
                        referent_sha256=:referent_sha256,
                        workflow_id=:workflow_id,
                        workflow_revision=:workflow_revision,
                        workflow_sha256=:workflow_sha256, schedule=:schedule,
                        first_due_at=:first_due_at,
                        interval_seconds=:interval_seconds,
                        last_occurrence_id=:last_occurrence_id,
                        last_event_position=:last_event_position,
                        updated_at=:updated_at
                    WHERE opportunity_id=:opportunity_id
                      AND revision=:expected_revision"""
                ),
                params,
            )
            if result.rowcount != 1:
                raise OpportunityConflict(
                    scope="opportunity_revision",
                    identity=value.opportunity_id,
                    expected_revision=expected_revision,
                )
        return OpportunityCommit(
            opportunity_id=value.opportunity_id,
            occurrence_id=value.occurrence_id,
            revision=revision,
            status=status,
            event_sha256=digest,
            idempotent_replay=False,
        )

    async def decide_opportunity(
        self,
        command: OpportunityRegistrationCommand,
    ) -> OpportunityCommit:
        """Commit one actor-gated subject registration decision."""

        async def operation(session: AsyncSession) -> OpportunityCommit:
            now = await self._database_now(session)
            digest = command.canonical_sha256()
            await self._claim_subject_decision(
                session,
                occurrence_id=command.occurrence_id,
                family="opportunity.registration",
                digest=digest,
                occurred_at=command.occurred_at,
                recorded_at=now,
            )
            return await self._commit_registration(
                session,
                command,
                origin=OpportunityOrigin.SUBJECT,
                now=now,
                digest=digest,
            )

        return await self._write(
            operation,
            actor_id=command.actor_consciousness_instance_id,
        )

    async def propose_opportunity(
        self,
        proposal: ProviderOpportunityProposal,
    ) -> OpportunityCommit:
        """Append a provider proposal without impersonating a subject actor."""

        async def operation(session: AsyncSession) -> OpportunityCommit:
            now = await self._database_now(session)
            return await self._commit_registration(
                session,
                proposal,
                origin=OpportunityOrigin.PROVIDER,
                now=now,
                digest=proposal.canonical_sha256(),
            )

        return await self._write(operation)

    async def get_opportunity(
        self,
        opportunity_id: str,
    ) -> OpportunityRegistration | None:
        identity = str(opportunity_id or "").strip()
        if not identity:
            raise ValueError("opportunity_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            return await self._registration(uow.session, identity)

    async def page_providers(
        self,
        *,
        limit: int = 100,
        continuation: str = "",
    ) -> ProviderPage:
        """Return a stable current-as-of-frontier provider page."""

        bounded = _limit(limit)
        frontier, after = _parse_token(continuation, "providers")
        async with self.runtime.unit_of_work() as uow:
            if not continuation:
                frontier = int(
                    await uow.session.scalar(
                        text(
                            "SELECT COALESCE(MAX(position),0) FROM opportunity_provider_events"
                        )
                    )
                    or 0
                )
            rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT e.provider_id, e.status, e.revision,
                            e.descriptor_version, e.descriptor_sha256,
                            e.workflow_id, e.workflow_revision,
                            e.workflow_sha256,
                            e.occurrence_id AS last_occurrence_id,
                            e.position AS last_event_position,
                            e.recorded_at AS updated_at
                            FROM opportunity_provider_events e
                            WHERE e.position <= :frontier
                              AND e.provider_id > :after
                              AND e.position = (
                                SELECT MAX(x.position)
                                FROM opportunity_provider_events x
                                WHERE x.provider_id=e.provider_id
                                  AND x.position <= :frontier
                              )
                            ORDER BY e.provider_id LIMIT :limit"""
                        ),
                        {
                            "frontier": frontier,
                            "after": after,
                            "limit": bounded + 1,
                        },
                    )
                )
                .mappings()
                .all()
            )
        has_more = len(rows) > bounded
        selected = rows[:bounded]
        items = tuple(self._decode_provider(row) for row in selected)
        next_token = (
            _token("providers", frontier, items[-1].provider_id)
            if has_more and items
            else ""
        )
        return ProviderPage(
            items=items,
            continuation=next_token,
            source_frontier=frontier,
        )

    async def page_opportunities(
        self,
        *,
        limit: int = 100,
        continuation: str = "",
    ) -> OpportunityPage:
        """Return a stable current-as-of-frontier registration page."""

        bounded = _limit(limit)
        frontier, after = _parse_token(continuation, "opportunities")
        async with self.runtime.unit_of_work() as uow:
            if not continuation:
                frontier = int(
                    await uow.session.scalar(
                        text(
                            "SELECT COALESCE(MAX(position),0) FROM opportunity_registration_events"
                        )
                    )
                    or 0
                )
            rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT e.opportunity_id, e.provider_id,
                            e.origin, e.status, e.revision, e.referent_kind,
                            e.referent_id, e.referent_revision,
                            e.referent_sha256, e.workflow_id,
                            e.workflow_revision, e.workflow_sha256,
                            e.schedule, e.first_due_at, e.interval_seconds,
                            e.occurrence_id AS last_occurrence_id,
                            e.position AS last_event_position,
                            e.recorded_at AS updated_at
                            FROM opportunity_registration_events e
                            WHERE e.position <= :frontier
                              AND e.opportunity_id > :after
                              AND e.position = (
                                SELECT MAX(x.position)
                                FROM opportunity_registration_events x
                                WHERE x.opportunity_id=e.opportunity_id
                                  AND x.position <= :frontier
                              )
                            ORDER BY e.opportunity_id LIMIT :limit"""
                        ),
                        {
                            "frontier": frontier,
                            "after": after,
                            "limit": bounded + 1,
                        },
                    )
                )
                .mappings()
                .all()
            )
        has_more = len(rows) > bounded
        selected = rows[:bounded]
        items = tuple(self._decode_registration(row) for row in selected)
        next_token = (
            _token("opportunities", frontier, items[-1].opportunity_id)
            if has_more and items
            else ""
        )
        return OpportunityPage(
            items=items,
            continuation=next_token,
            source_frontier=frontier,
        )

    @staticmethod
    def _history_spec(
        family: OpportunityHistoryFamily,
    ) -> tuple[str, str, str]:
        if family == OpportunityHistoryFamily.PROVIDER:
            return "opportunity_provider_events", "provider_id", "action"
        if family == OpportunityHistoryFamily.WORKFLOW:
            return "opportunity_workflow_versions", "workflow_id", "'replace'"
        return "opportunity_registration_events", "opportunity_id", "action"

    async def page_history(
        self,
        family: OpportunityHistoryFamily,
        *,
        aggregate_id: str = "",
        limit: int = 100,
        continuation: str = "",
    ) -> OpportunityHistoryPage:
        """Page immutable governance history at one stable table frontier."""

        selected_family = OpportunityHistoryFamily(family)
        bounded = _limit(limit)
        aggregate = str(aggregate_id or "").strip()
        if len(aggregate) > 255:
            raise ValueError("aggregate_id exceeds portable limit")
        table, aggregate_column, action_expression = self._history_spec(selected_family)
        kind = (
            f"history:{selected_family.value}:"
            f"{hashlib.sha256(aggregate.encode()).hexdigest()[:16]}"
        )
        frontier, after_text = _parse_token(continuation, kind)
        after = int(after_text or 0)
        async with self.runtime.unit_of_work() as uow:
            if not continuation:
                frontier = int(
                    await uow.session.scalar(
                        text(f"SELECT COALESCE(MAX(position),0) FROM {table}")
                    )
                    or 0
                )
            aggregate_clause = (
                f" AND {aggregate_column}=:aggregate_id" if aggregate else ""
            )
            rows = (
                (
                    await uow.session.execute(
                        text(
                            f"""SELECT position, occurrence_id,
                            {aggregate_column} AS aggregate_id,
                            {action_expression} AS action,
                            actor_consciousness_instance_id,
                            source_instance_id, source_occurrence_ids_json,
                            causation_occurrence_id,
                            revision - 1 AS expected_revision, revision,
                            reason, occurred_at, recorded_at, event_sha256
                            FROM {table}
                            WHERE position > :after AND position <= :frontier
                            {aggregate_clause}
                            ORDER BY position LIMIT :limit"""
                        ),
                        {
                            "after": after,
                            "frontier": frontier,
                            "aggregate_id": aggregate,
                            "limit": bounded + 1,
                        },
                    )
                )
                .mappings()
                .all()
            )
        has_more = len(rows) > bounded
        records: list[OpportunityHistoryRecord] = []
        for row in rows[:bounded]:
            reason = str(row["reason"])
            raw = reason.encode("utf-8")
            records.append(
                OpportunityHistoryRecord(
                    family=selected_family,
                    position=int(row["position"]),
                    occurrence_id=str(row["occurrence_id"]),
                    aggregate_id=str(row["aggregate_id"]),
                    action=str(row["action"]),
                    actor_consciousness_instance_id=str(
                        row["actor_consciousness_instance_id"]
                    ),
                    source_instance_id=str(row["source_instance_id"]),
                    source_occurrence_ids=_json_tuple(
                        row["source_occurrence_ids_json"]
                    ),
                    causation_occurrence_id=str(row["causation_occurrence_id"]),
                    expected_revision=int(row["expected_revision"]),
                    revision=int(row["revision"]),
                    reason_bytes=len(raw),
                    reason_sha256=hashlib.sha256(raw).hexdigest(),
                    occurred_at=_iso(row["occurred_at"]),
                    recorded_at=_iso(row["recorded_at"]),
                    event_sha256=str(row["event_sha256"]),
                )
            )
        items = tuple(records)
        next_token = (
            _token(kind, frontier, str(items[-1].position))
            if has_more and items
            else ""
        )
        return OpportunityHistoryPage(
            items=items,
            continuation=next_token,
            source_frontier=frontier,
        )

    async def read_history_reason_chunk(
        self,
        family: OpportunityHistoryFamily,
        occurrence_id: str,
        *,
        offset_bytes: int,
        max_bytes: int,
    ) -> OpportunityHistoryChunk:
        selected_family = OpportunityHistoryFamily(family)
        identity = str(occurrence_id or "").strip()
        offset = int(offset_bytes)
        maximum = int(max_bytes)
        if not identity or offset < 0 or maximum <= 0 or maximum > _MAX_CHUNK_BYTES:
            raise ValueError("invalid history reason byte window")
        table, _, _ = self._history_spec(selected_family)
        async with self.runtime.unit_of_work() as uow:
            reason = await uow.session.scalar(
                text(f"SELECT reason FROM {table} WHERE occurrence_id=:occurrence_id"),
                {"occurrence_id": identity},
            )
        if reason is None:
            raise KeyError(f"opportunity history occurrence not found: {identity}")
        raw = str(reason).encode("utf-8")
        if offset > len(raw):
            raise ValueError("offset_bytes exceeds reason length")
        try:
            raw[:offset].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("offset_bytes splits a UTF-8 sequence") from exc
        end = min(len(raw), offset + maximum)
        while end > offset:
            try:
                content = raw[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            if offset < len(raw):
                raise ValueError("max_bytes cannot fit the next UTF-8 code point")
            content = ""
        return OpportunityHistoryChunk(
            family=selected_family,
            occurrence_id=identity,
            offset_bytes=offset,
            next_offset_bytes=end,
            total_bytes=len(raw),
            reason_sha256=hashlib.sha256(raw).hexdigest(),
            content=content,
            complete=end == len(raw),
        )

    @staticmethod
    def _decode_occurrence(row: Any) -> OpportunityOccurrence:
        return OpportunityOccurrence(
            position=int(row["position"]),
            occurrence_id=str(row["occurrence_id"]),
            opportunity_id=str(row["opportunity_id"]),
            registration_revision=int(row["registration_revision"]),
            provider_id=str(row["provider_id"]),
            provider_revision=int(row["provider_revision"]),
            workflow_id=str(row["workflow_id"]),
            workflow_revision=int(row["workflow_revision"]),
            workflow_sha256=str(row["workflow_sha256"]),
            referent_kind=str(row["referent_kind"]),
            referent_id=str(row["referent_id"]),
            referent_revision=int(row["referent_revision"]),
            referent_sha256=str(row["referent_sha256"]),
            due_index=int(row["due_index"]),
            scheduled_for=_iso(row["scheduled_for"]),
            available_at=_iso(row["available_at"]),
            source_frontier=int(row["source_frontier"]),
            occurrence_sha256=str(row["occurrence_sha256"]),
        )

    @staticmethod
    def _decode_publication(row: Any) -> OpportunityPublication:
        return OpportunityPublication(
            outbox_id=str(row["outbox_id"]),
            occurrence_id=str(row["occurrence_id"]),
            opportunity_id=str(row["opportunity_id"]),
            status=PublicationStatus(str(row["status"])),
            revision=int(row["revision"]),
            life_event_occurrence_id=str(row["life_event_occurrence_id"]),
            life_event_sha256=str(row["life_event_sha256"]),
            created_at=_iso(row["created_at"]),
            updated_at=_iso(row["updated_at"]),
        )

    @staticmethod
    def _occurrence_columns() -> str:
        return """position, occurrence_id, opportunity_id,
            registration_revision, provider_id, provider_revision,
            workflow_id, workflow_revision, workflow_sha256,
            referent_kind, referent_id, referent_revision, referent_sha256,
            due_index, scheduled_for, available_at, source_frontier,
            occurrence_sha256"""

    @staticmethod
    def _publication_columns(alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        return f"""{prefix}outbox_id, {prefix}occurrence_id,
            {prefix}opportunity_id, {prefix}status, {prefix}revision,
            {prefix}life_event_occurrence_id, {prefix}life_event_sha256,
            {prefix}created_at, {prefix}updated_at"""

    async def _cancel_invalid_pending(
        self,
        session: AsyncSession,
        now: datetime,
    ) -> None:
        rows = (
            (
                await session.execute(
                    text(
                        """SELECT a.opportunity_id, a.registration_revision,
                        a.due_index, a.pending_occurrence_id,
                        a.revision AS activation_revision,
                        o.outbox_id, o.status AS publication_status,
                        o.revision AS publication_revision,
                        r.revision AS current_registration_revision,
                        r.status AS registration_status,
                        p.status AS provider_status
                        FROM opportunity_activation_states a
                        INNER JOIN opportunity_publication_outbox o
                            ON o.occurrence_id=a.pending_occurrence_id
                        LEFT JOIN opportunity_registration_heads r
                            ON r.opportunity_id=a.opportunity_id
                        LEFT JOIN opportunity_provider_heads p
                            ON p.provider_id=r.provider_id
                        WHERE a.pending_occurrence_id <> ''"""
                        + self._for_update
                    )
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            invalid = (
                row["current_registration_revision"] is None
                or int(row["current_registration_revision"])
                != int(row["registration_revision"])
                or str(row["registration_status"] or "") != OpportunityStatus.OPEN.value
                or str(row["provider_status"] or "") != ProviderStatus.ENABLED.value
            )
            if not invalid:
                continue
            publication_status = PublicationStatus(str(row["publication_status"]))
            if publication_status == PublicationStatus.PUBLISHED:
                registration_invalid = (
                    row["current_registration_revision"] is None
                    or int(row["current_registration_revision"])
                    != int(row["registration_revision"])
                    or str(row["registration_status"] or "")
                    != OpportunityStatus.OPEN.value
                )
                if not registration_invalid:
                    continue
                activation = await session.execute(
                    text(
                        """UPDATE opportunity_activation_states SET
                        registration_revision=:registration_revision,
                        due_index=due_index+1, next_due_at=:next_due_at,
                        pending_occurrence_id='', revision=revision+1,
                        updated_at=:updated_at
                        WHERE opportunity_id=:opportunity_id
                          AND revision=:expected_revision"""
                    ),
                    {
                        "registration_revision": int(
                            row["current_registration_revision"] or 0
                        ),
                        "next_due_at": (
                            None if self.backend == BackendKind.MYSQL else ""
                        ),
                        "updated_at": self._bind_time(now),
                        "opportunity_id": str(row["opportunity_id"]),
                        "expected_revision": int(row["activation_revision"]),
                    },
                )
                if activation.rowcount != 1:
                    raise OpportunityConflict(
                        scope="activation_withdraw",
                        identity=str(row["opportunity_id"]),
                    )
                continue
            if publication_status != PublicationStatus.PENDING:
                continue
            publication = await session.execute(
                text(
                    """UPDATE opportunity_publication_outbox SET
                    status='cancelled', revision=revision+1,
                    updated_at=:updated_at
                    WHERE outbox_id=:outbox_id AND status='pending'
                      AND revision=:expected_revision"""
                ),
                {
                    "updated_at": self._bind_time(now),
                    "outbox_id": str(row["outbox_id"]),
                    "expected_revision": int(row["publication_revision"]),
                },
            )
            if publication.rowcount != 1:
                raise OpportunityConflict(
                    scope="publication_cancel",
                    identity=str(row["outbox_id"]),
                )
            activation = await session.execute(
                text(
                    """UPDATE opportunity_activation_states SET
                    registration_revision=:registration_revision,
                    due_index=due_index+1, next_due_at=:next_due_at,
                    pending_occurrence_id='', revision=revision+1,
                    updated_at=:updated_at
                    WHERE opportunity_id=:opportunity_id
                      AND revision=:expected_revision"""
                ),
                {
                    "registration_revision": (
                        0
                        if str(row["provider_status"] or "")
                        != ProviderStatus.ENABLED.value
                        else int(row["current_registration_revision"] or 0)
                    ),
                    "next_due_at": (None if self.backend == BackendKind.MYSQL else ""),
                    "updated_at": self._bind_time(now),
                    "opportunity_id": str(row["opportunity_id"]),
                    "expected_revision": int(row["activation_revision"]),
                },
            )
            if activation.rowcount != 1:
                raise OpportunityConflict(
                    scope="activation_cancel",
                    identity=str(row["opportunity_id"]),
                )

    async def _activation(
        self,
        session: AsyncSession,
        opportunity_id: str,
    ) -> Any:
        return (
            (
                await session.execute(
                    text(
                        """SELECT opportunity_id, registration_revision,
                        due_index, next_due_at, pending_occurrence_id,
                        revision, updated_at
                        FROM opportunity_activation_states
                        WHERE opportunity_id=:opportunity_id"""
                        + self._for_update
                    ),
                    {"opportunity_id": opportunity_id},
                )
            )
            .mappings()
            .one_or_none()
        )

    async def materialize_due(
        self,
        *,
        limit: int = 100,
    ) -> tuple[OpportunityOccurrence, ...]:
        """Materialize due timed opportunities once under the scheduler claim."""

        bounded = _limit(limit)

        async def operation(session: AsyncSession) -> tuple[OpportunityOccurrence, ...]:
            now = await self._database_now(session)
            await self._cancel_invalid_pending(session, now)
            candidates = (
                (
                    await session.execute(
                        text(
                            """SELECT r.opportunity_id, r.provider_id,
                            r.revision AS registration_revision,
                            r.referent_kind, r.referent_id,
                            r.referent_revision, r.referent_sha256,
                            r.workflow_id, r.workflow_revision,
                            r.workflow_sha256, r.schedule, r.first_due_at,
                            r.interval_seconds,
                            r.last_event_position AS registration_position,
                            p.revision AS provider_revision,
                            p.last_event_position AS provider_position
                            FROM opportunity_registration_heads r
                            INNER JOIN opportunity_provider_heads p
                                ON p.provider_id=r.provider_id
                            WHERE r.status='open' AND p.status='enabled'
                              AND r.schedule IN ('at','interval')
                              AND r.first_due_at <= :database_now
                            ORDER BY r.opportunity_id LIMIT :limit"""
                            + self._for_update
                        ),
                        {
                            "database_now": self._bind_time(now),
                            "limit": bounded,
                        },
                    )
                )
                .mappings()
                .all()
            )
            materialized: list[OpportunityOccurrence] = []
            for row in candidates:
                opportunity_id = str(row["opportunity_id"])
                activation = await self._activation(session, opportunity_id)
                first_due = _parse_time(row["first_due_at"])
                if activation is None:
                    await session.execute(
                        text(
                            """INSERT INTO opportunity_activation_states (
                            opportunity_id, registration_revision, due_index,
                            next_due_at, pending_occurrence_id, revision,
                            updated_at
                            ) VALUES (
                            :opportunity_id, :registration_revision, 0,
                            :next_due_at, '', 0, :updated_at
                            )"""
                        ),
                        {
                            "opportunity_id": opportunity_id,
                            "registration_revision": int(row["registration_revision"]),
                            "next_due_at": self._bind_time(first_due),
                            "updated_at": self._bind_time(now),
                        },
                    )
                    activation = await self._activation(session, opportunity_id)
                    if activation is None:
                        raise RuntimeError("OpportunityActivationInsertLost")
                if (
                    int(activation["registration_revision"])
                    != int(row["registration_revision"])
                    and str(activation["pending_occurrence_id"] or "") == ""
                ):
                    result = await session.execute(
                        text(
                            """UPDATE opportunity_activation_states SET
                            registration_revision=:registration_revision,
                            next_due_at=:next_due_at, revision=revision+1,
                            updated_at=:updated_at
                            WHERE opportunity_id=:opportunity_id
                              AND revision=:expected_revision"""
                        ),
                        {
                            "registration_revision": int(row["registration_revision"]),
                            "next_due_at": self._bind_time(first_due),
                            "updated_at": self._bind_time(now),
                            "opportunity_id": opportunity_id,
                            "expected_revision": int(activation["revision"]),
                        },
                    )
                    if result.rowcount != 1:
                        raise OpportunityConflict(
                            scope="activation_revision",
                            identity=opportunity_id,
                        )
                    activation = await self._activation(session, opportunity_id)
                    if activation is None:
                        raise RuntimeError("OpportunityActivationUpdateLost")
                if str(activation["pending_occurrence_id"] or ""):
                    continue
                next_due_raw = activation["next_due_at"]
                if next_due_raw in (None, "") or _parse_time(next_due_raw) > now:
                    continue
                scheduled_for = _parse_time(next_due_raw)
                due_index = int(activation["due_index"])
                workflow = await self._workflow(
                    session,
                    str(row["workflow_id"]),
                    int(row["workflow_revision"]),
                )
                if (
                    workflow is None
                    or workflow.content_sha256 != str(row["workflow_sha256"])
                    or workflow.provider_id != str(row["provider_id"])
                ):
                    raise OpportunityConflict(
                        scope="materialize_workflow",
                        identity=str(row["workflow_id"]),
                    )
                identity_material = {
                    "opportunity_id": opportunity_id,
                    "registration_revision": int(row["registration_revision"]),
                    "due_index": due_index,
                    "scheduled_for": scheduled_for.isoformat(),
                }
                identity_digest = hashlib.sha256(
                    canonical_json(identity_material).encode()
                ).hexdigest()
                occurrence_id = f"opportunity:occurrence:{identity_digest}"
                source_frontier = max(
                    int(row["registration_position"]),
                    int(row["provider_position"]),
                    workflow.position,
                )
                occurrence_material = {
                    **identity_material,
                    "occurrence_id": occurrence_id,
                    "provider_id": str(row["provider_id"]),
                    "provider_revision": int(row["provider_revision"]),
                    "workflow_id": workflow.workflow_id,
                    "workflow_revision": workflow.revision,
                    "workflow_sha256": workflow.content_sha256,
                    "referent_kind": str(row["referent_kind"]),
                    "referent_id": str(row["referent_id"]),
                    "referent_revision": int(row["referent_revision"]),
                    "referent_sha256": str(row["referent_sha256"]),
                    "available_at": now.isoformat(),
                    "source_frontier": source_frontier,
                }
                occurrence_sha256 = hashlib.sha256(
                    canonical_json(occurrence_material).encode()
                ).hexdigest()
                await session.execute(
                    text(
                        """INSERT INTO opportunity_occurrences (
                        occurrence_id, opportunity_id, registration_revision,
                        provider_id, provider_revision, workflow_id,
                        workflow_revision, workflow_sha256, referent_kind,
                        referent_id, referent_revision, referent_sha256,
                        due_index, scheduled_for, available_at,
                        source_frontier, occurrence_sha256
                        ) VALUES (
                        :occurrence_id, :opportunity_id,
                        :registration_revision, :provider_id,
                        :provider_revision, :workflow_id, :workflow_revision,
                        :workflow_sha256, :referent_kind, :referent_id,
                        :referent_revision, :referent_sha256, :due_index,
                        :scheduled_for, :available_at, :source_frontier,
                        :occurrence_sha256
                        )"""
                    ),
                    {
                        **occurrence_material,
                        "scheduled_for": self._bind_time(scheduled_for),
                        "available_at": self._bind_time(now),
                        "occurrence_sha256": occurrence_sha256,
                    },
                )
                outbox_id = f"opportunity:publication:{identity_digest}"
                life_event_occurrence_id = f"opportunity:available:{identity_digest}"
                await session.execute(
                    text(
                        """INSERT INTO opportunity_publication_outbox (
                        outbox_id, occurrence_id, opportunity_id, status,
                        revision, life_event_occurrence_id,
                        life_event_sha256, created_at, updated_at
                        ) VALUES (
                        :outbox_id, :occurrence_id, :opportunity_id,
                        'pending', 1, :life_event_occurrence_id, '',
                        :created_at, :updated_at
                        )"""
                    ),
                    {
                        "outbox_id": outbox_id,
                        "occurrence_id": occurrence_id,
                        "opportunity_id": opportunity_id,
                        "life_event_occurrence_id": life_event_occurrence_id,
                        "created_at": self._bind_time(now),
                        "updated_at": self._bind_time(now),
                    },
                )
                updated = await session.execute(
                    text(
                        """UPDATE opportunity_activation_states SET
                        pending_occurrence_id=:occurrence_id,
                        revision=revision+1, updated_at=:updated_at
                        WHERE opportunity_id=:opportunity_id
                          AND revision=:expected_revision
                          AND pending_occurrence_id=''"""
                    ),
                    {
                        "occurrence_id": occurrence_id,
                        "updated_at": self._bind_time(now),
                        "opportunity_id": opportunity_id,
                        "expected_revision": int(activation["revision"]),
                    },
                )
                if updated.rowcount != 1:
                    raise OpportunityConflict(
                        scope="activation_revision",
                        identity=opportunity_id,
                    )
                occurrence_row = (
                    (
                        await session.execute(
                            text(
                                f"""SELECT {self._occurrence_columns()}
                                FROM opportunity_occurrences
                                WHERE occurrence_id=:occurrence_id"""
                            ),
                            {"occurrence_id": occurrence_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                materialized.append(self._decode_occurrence(occurrence_row))
            return tuple(materialized)

        return await self._write(operation, scheduler=True)

    async def pending_publications(
        self,
        *,
        limit: int = 100,
    ) -> tuple[OpportunityPublication, ...]:
        """Read only publications whose frozen registration is still active."""

        bounded = _limit(limit)
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            f"""SELECT {self._publication_columns("o")}
                            FROM opportunity_publication_outbox o
                            INNER JOIN opportunity_occurrences x
                                ON x.occurrence_id=o.occurrence_id
                            INNER JOIN opportunity_registration_heads r
                                ON r.opportunity_id=x.opportunity_id
                            INNER JOIN opportunity_provider_heads p
                                ON p.provider_id=x.provider_id
                            WHERE o.status='pending'
                              AND r.status='open' AND p.status='enabled'
                              AND r.revision=x.registration_revision
                            ORDER BY o.created_at, o.outbox_id LIMIT :limit"""
                        ),
                        {"limit": bounded},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(self._decode_publication(row) for row in rows)

    async def awaiting_delivery(
        self,
        *,
        limit: int = 100,
    ) -> tuple[OpportunityPublication, ...]:
        """Return published occurrences still awaiting exact perception.

        This is a restart-safe wake source: it exposes the existing immutable
        publication and never materializes a replacement occurrence.
        """

        bounded = _limit(limit)
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            f"""SELECT {self._publication_columns("o")}
                            FROM opportunity_publication_outbox o
                            INNER JOIN opportunity_occurrences x
                                ON x.occurrence_id=o.occurrence_id
                            INNER JOIN opportunity_registration_heads r
                                ON r.opportunity_id=x.opportunity_id
                            INNER JOIN opportunity_provider_heads p
                                ON p.provider_id=x.provider_id
                            LEFT JOIN opportunity_delivery_receipts d
                                ON d.occurrence_id=x.occurrence_id
                            WHERE o.status='published'
                              AND d.occurrence_id IS NULL
                              AND r.status='open' AND p.status='enabled'
                              AND r.revision=x.registration_revision
                            ORDER BY o.created_at, o.outbox_id LIMIT :limit"""
                        ),
                        {"limit": bounded},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(self._decode_publication(row) for row in rows)

    async def get_publication(
        self,
        life_event_occurrence_id: str,
    ) -> OpportunityPublication | None:
        identity = str(life_event_occurrence_id or "").strip()
        if not identity:
            raise ValueError("life_event_occurrence_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            f"""SELECT {self._publication_columns()}
                            FROM opportunity_publication_outbox
                            WHERE life_event_occurrence_id=:identity"""
                        ),
                        {"identity": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._decode_publication(row) if row is not None else None

    async def latest_publication(
        self,
        opportunity_id: str,
    ) -> OpportunityPublication | None:
        """Read the newest actually published occurrence for source replay."""

        identity = str(opportunity_id or "").strip()
        if not identity:
            raise ValueError("opportunity_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            f"""SELECT {self._publication_columns("o")}
                            FROM opportunity_publication_outbox o
                            INNER JOIN opportunity_occurrences x
                                ON x.occurrence_id=o.occurrence_id
                            WHERE o.opportunity_id=:opportunity_id
                              AND o.status='published'
                            ORDER BY x.position DESC LIMIT 1"""
                        ),
                        {"opportunity_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._decode_publication(row) if row is not None else None

    async def _publication_event_evidence_error(
        self,
        session: AsyncSession,
        publication: OpportunityPublication,
        digest: str,
    ) -> str:
        """Validate the exact immutable LifeEvent written before outbox ack."""

        row = (
            (
                await session.execute(
                    text(
                        """SELECT occurrence_id, payload_json, payload_hash
                        FROM raw_life_events
                        WHERE occurrence_id=:occurrence_id"""
                    ),
                    {"occurrence_id": publication.life_event_occurrence_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return "publication_evidence_missing"
        payload_raw = row["payload_json"]
        try:
            if isinstance(payload_raw, bytes):
                payload_raw = payload_raw.decode("utf-8")
            payload = (
                json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "publication_evidence_corrupt"
        if not isinstance(payload, dict):
            return "publication_evidence_corrupt"
        if hashlib.sha256(canonical_json(payload).encode()).hexdigest() != str(
            row["payload_hash"]
        ):
            return "publication_evidence_corrupt"
        content = payload.get("content")
        if not isinstance(content, str):
            return "publication_evidence_mismatch"
        evidence_matches = (
            str(row["occurrence_id"]) == publication.life_event_occurrence_id
            and payload.get("occurrence_id") == publication.life_event_occurrence_id
            and payload.get("event_type") == "opportunity.available"
            and payload.get("source") == "opportunity_runtime"
            and payload.get("causation_id") == publication.occurrence_id
            and hashlib.sha256(content.encode()).hexdigest() == digest
        )
        return "" if evidence_matches else "publication_evidence_mismatch"

    async def _expected_publication_digest(
        self,
        session: AsyncSession,
        publication: OpportunityPublication,
    ) -> str:
        row = (
            (
                await session.execute(
                    text(
                        f"""SELECT {self._occurrence_columns()}
                        FROM opportunity_occurrences
                        WHERE occurrence_id=:occurrence_id"""
                    ),
                    {"occurrence_id": publication.occurrence_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise OpportunityConflict(
                scope="publication_occurrence",
                identity=publication.occurrence_id,
            )
        payload = asdict(self._decode_occurrence(row))
        payload["schema_version"] = 1
        payload["meaning"] = "availability_only"
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    async def _recover_durable_publications(
        self,
        session: AsyncSession,
        now: datetime,
        *,
        limit: int,
    ) -> None:
        """Acknowledge outbox rows whose exact LifeEvent already committed.

        This closes the crash window between the event append and outbox ack.
        A later pause/uninstall may cancel the pending row, but it cannot erase
        the immutable fact that the event was already published. Recovery only
        changes the outbox projection; it neither appends nor wakes anything.
        """

        rows = (
            (
                await session.execute(
                    text(
                        f"""SELECT {self._publication_columns("o")}
                        FROM opportunity_publication_outbox o
                        INNER JOIN raw_life_events e
                            ON e.occurrence_id=o.life_event_occurrence_id
                        WHERE o.status IN ('pending','cancelled')
                        ORDER BY o.created_at, o.outbox_id LIMIT :limit"""
                        + self._for_update
                    ),
                    {"limit": limit},
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            publication = self._decode_publication(row)
            digest = await self._expected_publication_digest(session, publication)
            evidence_error = await self._publication_event_evidence_error(
                session,
                publication,
                digest,
            )
            if evidence_error:
                raise OpportunityConflict(
                    scope=evidence_error,
                    identity=publication.outbox_id,
                    actual_revision=publication.revision,
                )
            updated = await session.execute(
                text(
                    """UPDATE opportunity_publication_outbox SET
                    status='published', revision=revision+1,
                    life_event_sha256=:life_event_sha256,
                    updated_at=:updated_at
                    WHERE outbox_id=:outbox_id AND status=:current_status
                      AND revision=:current_revision"""
                ),
                {
                    "life_event_sha256": digest,
                    "updated_at": self._bind_time(now),
                    "outbox_id": publication.outbox_id,
                    "current_status": publication.status.value,
                    "current_revision": publication.revision,
                },
            )
            if updated.rowcount != 1:
                raise OpportunityConflict(
                    scope="publication_revision",
                    identity=publication.outbox_id,
                    expected_revision=publication.revision,
                )
            if publication.status == PublicationStatus.CANCELLED:
                await self._restore_cancelled_publication_activation(
                    session,
                    publication,
                    now,
                )

    async def _restore_cancelled_publication_activation(
        self,
        session: AsyncSession,
        publication: OpportunityPublication,
        now: datetime,
    ) -> None:
        """Keep a proven raced publication pending without reviving its provider."""

        evidence = (
            (
                await session.execute(
                    text(
                        """SELECT x.registration_revision, x.due_index,
                        x.scheduled_for, r.revision AS current_registration_revision
                        FROM opportunity_occurrences x
                        LEFT JOIN opportunity_registration_heads r
                            ON r.opportunity_id=x.opportunity_id
                        WHERE x.occurrence_id=:occurrence_id"""
                    ),
                    {"occurrence_id": publication.occurrence_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        if evidence is None or evidence["current_registration_revision"] is None:
            return
        if int(evidence["current_registration_revision"]) != int(
            evidence["registration_revision"]
        ):
            return
        await session.execute(
            text(
                """UPDATE opportunity_activation_states SET
                registration_revision=:registration_revision,
                due_index=:due_index, next_due_at=:next_due_at,
                pending_occurrence_id=:pending_occurrence_id,
                revision=revision+1, updated_at=:updated_at
                WHERE opportunity_id=:opportunity_id
                  AND pending_occurrence_id=''"""
            ),
            {
                "registration_revision": int(evidence["registration_revision"]),
                "due_index": int(evidence["due_index"]),
                "next_due_at": self._bind_time(_parse_time(evidence["scheduled_for"])),
                "pending_occurrence_id": publication.occurrence_id,
                "updated_at": self._bind_time(now),
                "opportunity_id": publication.opportunity_id,
            },
        )

    async def mark_published(
        self,
        outbox_id: str,
        *,
        expected_revision: int,
        life_event_sha256: str,
    ) -> OpportunityPublication:
        identity = str(outbox_id or "").strip()
        digest = str(life_event_sha256 or "").strip().lower()
        if not identity:
            raise ValueError("outbox_id must not be empty")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("life_event_sha256 must be a lowercase SHA-256")
        revision = int(expected_revision)
        if revision <= 0:
            raise ValueError("expected_revision must be positive")

        async def operation(
            session: AsyncSession,
        ) -> tuple[OpportunityPublication, str]:
            now = await self._database_now(session)
            await self._cancel_invalid_pending(session, now)
            row = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {self._publication_columns()}
                            FROM opportunity_publication_outbox
                            WHERE outbox_id=:outbox_id"""
                            + self._for_update
                        ),
                        {"outbox_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise KeyError(f"publication not found: {identity}")
            current = self._decode_publication(row)
            evidence_error = await self._publication_event_evidence_error(
                session,
                current,
                digest,
            )
            if current.status == PublicationStatus.PUBLISHED:
                if current.life_event_sha256 != digest:
                    return current, "publication_digest"
                return current, evidence_error
            if current.status == PublicationStatus.CANCELLED:
                if evidence_error:
                    return current, evidence_error
                result = await session.execute(
                    text(
                        """UPDATE opportunity_publication_outbox SET
                        status='published', revision=revision+1,
                        life_event_sha256=:life_event_sha256,
                        updated_at=:updated_at
                        WHERE outbox_id=:outbox_id AND status='cancelled'
                          AND revision=:current_revision"""
                    ),
                    {
                        "life_event_sha256": digest,
                        "updated_at": self._bind_time(now),
                        "outbox_id": identity,
                        "current_revision": current.revision,
                    },
                )
                if result.rowcount != 1:
                    return current, "publication_revision"
                await self._restore_cancelled_publication_activation(
                    session,
                    current,
                    now,
                )
                recovered = (
                    (
                        await session.execute(
                            text(
                                f"""SELECT {self._publication_columns()}
                                FROM opportunity_publication_outbox
                                WHERE outbox_id=:outbox_id"""
                            ),
                            {"outbox_id": identity},
                        )
                    )
                    .mappings()
                    .one()
                )
                return self._decode_publication(recovered), ""
            if current.status != PublicationStatus.PENDING:
                return current, "publication_cancelled"
            if evidence_error:
                return current, evidence_error
            if current.revision != revision:
                raise OpportunityConflict(
                    scope="publication_revision",
                    identity=identity,
                    expected_revision=revision,
                    actual_revision=current.revision,
                )
            result = await session.execute(
                text(
                    """UPDATE opportunity_publication_outbox SET
                    status='published', revision=revision+1,
                    life_event_sha256=:life_event_sha256,
                    updated_at=:updated_at
                    WHERE outbox_id=:outbox_id AND status='pending'
                      AND revision=:expected_revision"""
                ),
                {
                    "life_event_sha256": digest,
                    "updated_at": self._bind_time(now),
                    "outbox_id": identity,
                    "expected_revision": revision,
                },
            )
            if result.rowcount != 1:
                raise OpportunityConflict(
                    scope="publication_revision",
                    identity=identity,
                    expected_revision=revision,
                )
            persisted = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {self._publication_columns()}
                            FROM opportunity_publication_outbox
                            WHERE outbox_id=:outbox_id"""
                        ),
                        {"outbox_id": identity},
                    )
                )
                .mappings()
                .one()
            )
            return self._decode_publication(persisted), ""

        publication, error_scope = await self._write(operation, scheduler=True)
        if error_scope:
            raise OpportunityConflict(
                scope=error_scope,
                identity=identity,
                expected_revision=revision,
                actual_revision=publication.revision,
            )
        return publication

    async def get_occurrence(
        self,
        occurrence_id: str,
    ) -> OpportunityOccurrence | None:
        identity = str(occurrence_id or "").strip()
        if not identity:
            raise ValueError("occurrence_id must not be empty")
        async with self.runtime.unit_of_work() as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            f"""SELECT {self._occurrence_columns()}
                            FROM opportunity_occurrences
                            WHERE occurrence_id=:occurrence_id"""
                        ),
                        {"occurrence_id": identity},
                    )
                )
                .mappings()
                .one_or_none()
            )
        return self._decode_occurrence(row) if row is not None else None

    async def list_occurrences(
        self,
        opportunity_id: str,
        *,
        limit: int = 100,
    ) -> tuple[OpportunityOccurrence, ...]:
        identity = str(opportunity_id or "").strip()
        if not identity:
            raise ValueError("opportunity_id must not be empty")
        bounded = _limit(limit)
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            f"""SELECT {self._occurrence_columns()}
                            FROM opportunity_occurrences
                            WHERE opportunity_id=:opportunity_id
                            ORDER BY position DESC LIMIT :limit"""
                        ),
                        {"opportunity_id": identity, "limit": bounded},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(self._decode_occurrence(row) for row in rows)

    @staticmethod
    def _decode_delivery(row: Any) -> OpportunityDeliveryRecord:
        receipt = OpportunityDeliveryReceipt(
            receipt_id=str(row["receipt_id"]),
            occurrence_id=str(row["occurrence_id"]),
            life_event_occurrence_id=str(row["life_event_occurrence_id"]),
            consumer_consciousness_instance_id=str(
                row["consumer_consciousness_instance_id"]
            ),
            context_delivery_id=str(row["context_delivery_id"]),
            final_request_id=str(row["final_request_id"]),
            final_attempt_id=str(row["final_attempt_id"]),
            exact_present=bool(row["exact_present"]),
            expected_bytes=int(row["expected_bytes"]),
            effective_bytes=int(row["effective_bytes"]),
            expected_sha256=str(row["expected_sha256"]),
            effective_sha256=str(row["effective_sha256"]),
            perceived_at=_iso(row["perceived_at"]),
        )
        persisted = str(row["receipt_sha256"])
        if receipt.canonical_sha256() != persisted:
            raise RuntimeError(
                f"OpportunityDeliveryReceiptCorrupt:{receipt.receipt_id}"
            )
        return OpportunityDeliveryRecord(
            position=int(row["position"]),
            receipt=receipt,
            recorded_at=_iso(row["recorded_at"]),
            receipt_sha256=persisted,
        )

    @staticmethod
    def _delivery_columns(alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        return f"""{prefix}position, {prefix}receipt_id,
            {prefix}occurrence_id, {prefix}life_event_occurrence_id,
            {prefix}consumer_consciousness_instance_id,
            {prefix}context_delivery_id, {prefix}final_request_id,
            {prefix}final_attempt_id, {prefix}exact_present,
            {prefix}expected_bytes, {prefix}effective_bytes,
            {prefix}expected_sha256, {prefix}effective_sha256,
            {prefix}perceived_at, {prefix}recorded_at,
            {prefix}receipt_sha256"""

    async def commit_exact(
        self,
        receipt: OpportunityDeliveryReceipt,
    ) -> OpportunityDeliveryCommit:
        """Append exact perception evidence without requiring scheduler identity."""

        if (
            not receipt.exact_present
            or receipt.expected_bytes != receipt.effective_bytes
            or receipt.expected_sha256 != receipt.effective_sha256
        ):
            raise OpportunityDeliveryRejected("OpportunityDeliveryNotExact")

        async def operation(session: AsyncSession) -> OpportunityDeliveryCommit:
            occurrence = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {self._occurrence_columns()}
                            FROM opportunity_occurrences
                            WHERE occurrence_id=:occurrence_id"""
                            + self._for_update
                        ),
                        {"occurrence_id": receipt.occurrence_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if occurrence is None:
                raise OpportunityDeliveryRejected("OpportunityOccurrenceMissing")
            publication = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {self._publication_columns()}
                            FROM opportunity_publication_outbox
                            WHERE occurrence_id=:occurrence_id"""
                            + self._for_update
                        ),
                        {"occurrence_id": receipt.occurrence_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if publication is None:
                raise OpportunityDeliveryRejected("OpportunityPublicationMissing")
            publication_value = self._decode_publication(publication)
            if (
                publication_value.status != PublicationStatus.PUBLISHED
                or publication_value.life_event_occurrence_id
                != receipt.life_event_occurrence_id
                or not publication_value.life_event_sha256
            ):
                raise OpportunityDeliveryRejected("OpportunityEventNotPublished")
            digest = receipt.canonical_sha256()
            existing_rows = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {self._delivery_columns()}
                            FROM opportunity_delivery_receipts
                            WHERE receipt_id=:receipt_id"""
                            + self._for_update
                        ),
                        {
                            "receipt_id": receipt.receipt_id,
                        },
                    )
                )
                .mappings()
                .all()
            )
            if len(existing_rows) > 1:
                raise OpportunityConflict(
                    scope="delivery_receipt_identity",
                    identity=receipt.receipt_id,
                )
            if existing_rows:
                record = self._decode_delivery(existing_rows[0])
                if record.receipt_sha256 != digest:
                    raise OpportunityConflict(
                        scope="delivery_receipt",
                        identity=receipt.receipt_id,
                    )
                return OpportunityDeliveryCommit(
                    record=replace(record, idempotent_replay=True)
                )
            now = await self._database_now(session)
            insert_prefix = (
                "INSERT IGNORE" if self.backend == BackendKind.MYSQL else "INSERT"
            )
            insert_suffix = (
                ""
                if self.backend == BackendKind.MYSQL
                else " ON CONFLICT(receipt_id) DO NOTHING"
            )
            inserted = await session.execute(
                text(
                    f"""{insert_prefix} INTO opportunity_delivery_receipts (
                    receipt_id, occurrence_id, life_event_occurrence_id,
                    consumer_consciousness_instance_id, context_delivery_id,
                    final_request_id, final_attempt_id, exact_present,
                    expected_bytes, effective_bytes, expected_sha256,
                    effective_sha256, perceived_at, recorded_at,
                    receipt_sha256
                    ) VALUES (
                    :receipt_id, :occurrence_id, :life_event_occurrence_id,
                    :consumer, :context_delivery_id, :final_request_id,
                    :final_attempt_id, :exact_present, :expected_bytes,
                    :effective_bytes, :expected_sha256, :effective_sha256,
                    :perceived_at, :recorded_at, :receipt_sha256
                    )"""
                    + insert_suffix
                ),
                {
                    "receipt_id": receipt.receipt_id,
                    "occurrence_id": receipt.occurrence_id,
                    "life_event_occurrence_id": receipt.life_event_occurrence_id,
                    "consumer": receipt.consumer_consciousness_instance_id,
                    "context_delivery_id": receipt.context_delivery_id,
                    "final_request_id": receipt.final_request_id,
                    "final_attempt_id": receipt.final_attempt_id,
                    "exact_present": True,
                    "expected_bytes": receipt.expected_bytes,
                    "effective_bytes": receipt.effective_bytes,
                    "expected_sha256": receipt.expected_sha256,
                    "effective_sha256": receipt.effective_sha256,
                    "perceived_at": self._bind_time(receipt.perceived_at),
                    "recorded_at": self._bind_time(now),
                    "receipt_sha256": digest,
                },
            )
            row = (
                (
                    await session.execute(
                        text(
                            f"""SELECT {self._delivery_columns()}
                            FROM opportunity_delivery_receipts
                            WHERE receipt_id=:receipt_id"""
                        ),
                        {"receipt_id": receipt.receipt_id},
                    )
                )
                .mappings()
                .one()
            )
            record = self._decode_delivery(row)
            if record.receipt_sha256 != digest:
                raise OpportunityConflict(
                    scope="delivery_receipt",
                    identity=receipt.receipt_id,
                )
            return OpportunityDeliveryCommit(
                record=replace(record, idempotent_replay=inserted.rowcount != 1)
            )

        return await self._write(operation)

    async def list_deliveries(
        self,
        opportunity_id: str,
        *,
        limit: int = 100,
    ) -> tuple[OpportunityDeliveryRecord, ...]:
        identity = str(opportunity_id or "").strip()
        if not identity:
            raise ValueError("opportunity_id must not be empty")
        bounded = _limit(limit)
        async with self.runtime.unit_of_work() as uow:
            rows = (
                (
                    await uow.session.execute(
                        text(
                            f"""SELECT {self._delivery_columns("d")}
                            FROM opportunity_delivery_receipts d
                            INNER JOIN opportunity_occurrences o
                                ON o.occurrence_id=d.occurrence_id
                            WHERE o.opportunity_id=:opportunity_id
                            ORDER BY d.position DESC LIMIT :limit"""
                        ),
                        {"opportunity_id": identity, "limit": bounded},
                    )
                )
                .mappings()
                .all()
            )
        return tuple(self._decode_delivery(row) for row in rows)

    async def reconcile_deliveries(
        self,
        *,
        limit: int = 100,
    ) -> tuple[str, ...]:
        """Project immutable exact receipts into the claimed activation state."""

        bounded = _limit(limit)

        async def operation(session: AsyncSession) -> tuple[str, ...]:
            now = await self._database_now(session)
            await self._recover_durable_publications(
                session,
                now,
                limit=bounded,
            )
            rows = (
                (
                    await session.execute(
                        text(
                            """SELECT a.opportunity_id,
                            a.registration_revision,
                            a.due_index, a.pending_occurrence_id,
                            a.revision AS activation_revision,
                            x.registration_revision AS occurrence_registration_revision,
                            x.due_index AS occurrence_due_index,
                            x.scheduled_for,
                            r.revision AS current_registration_revision,
                            r.status AS registration_status,
                            r.schedule AS current_schedule,
                            r.first_due_at AS current_first_due_at,
                            r.interval_seconds AS current_interval_seconds,
                            p.status AS provider_status
                            FROM opportunity_activation_states a
                            INNER JOIN opportunity_occurrences x
                                ON x.occurrence_id=a.pending_occurrence_id
                            LEFT JOIN opportunity_registration_heads r
                                ON r.opportunity_id=a.opportunity_id
                            LEFT JOIN opportunity_provider_heads p
                                ON p.provider_id=r.provider_id
                            WHERE a.pending_occurrence_id <> ''
                              AND EXISTS (
                                  SELECT 1 FROM opportunity_delivery_receipts d
                                  WHERE d.occurrence_id=x.occurrence_id
                              )
                            ORDER BY x.position LIMIT :limit"""
                            + self._for_update
                        ),
                        {"limit": bounded},
                    )
                )
                .mappings()
                .all()
            )
            reconciled: list[str] = []
            for row in rows:
                current_revision = int(
                    row["current_registration_revision"]
                    or row["occurrence_registration_revision"]
                )
                next_due: datetime | None = None
                active = (
                    str(row["registration_status"] or "")
                    == OpportunityStatus.OPEN.value
                    and str(row["provider_status"] or "")
                    == ProviderStatus.ENABLED.value
                )
                if active:
                    current_schedule = str(row["current_schedule"] or "")
                    if current_revision != int(
                        row["occurrence_registration_revision"]
                    ) and current_schedule in {
                        OpportunitySchedule.AT.value,
                        OpportunitySchedule.INTERVAL.value,
                    }:
                        due = row["current_first_due_at"]
                        next_due = _parse_time(due) if due not in (None, "") else None
                    elif current_schedule == OpportunitySchedule.INTERVAL.value:
                        next_due = _parse_time(row["scheduled_for"]) + timedelta(
                            seconds=int(row["current_interval_seconds"])
                        )
                encoded_due: datetime | str | None
                if next_due is None:
                    encoded_due = None if self.backend == BackendKind.MYSQL else ""
                else:
                    encoded_due = self._bind_time(next_due)
                result = await session.execute(
                    text(
                        """UPDATE opportunity_activation_states SET
                        registration_revision=:registration_revision,
                        due_index=:due_index, next_due_at=:next_due_at,
                        pending_occurrence_id='', revision=revision+1,
                        updated_at=:updated_at
                        WHERE opportunity_id=:opportunity_id
                          AND revision=:expected_revision
                          AND pending_occurrence_id=:pending_occurrence_id"""
                    ),
                    {
                        "registration_revision": current_revision,
                        "due_index": max(
                            int(row["due_index"]) + 1,
                            int(row["occurrence_due_index"]) + 1,
                        ),
                        "next_due_at": encoded_due,
                        "updated_at": self._bind_time(now),
                        "opportunity_id": str(row["opportunity_id"]),
                        "expected_revision": int(row["activation_revision"]),
                        "pending_occurrence_id": str(row["pending_occurrence_id"]),
                    },
                )
                if result.rowcount != 1:
                    raise OpportunityConflict(
                        scope="activation_delivery",
                        identity=str(row["opportunity_id"]),
                    )
                reconciled.append(str(row["pending_occurrence_id"]))
            return tuple(reconciled)

        return await self._write(operation, scheduler=True)

    async def health_snapshot(self) -> dict[str, Any]:
        """Return content-free authority counts and lifecycle states."""

        async with self.runtime.unit_of_work() as uow:
            provider_rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT status, COUNT(*) AS total
                            FROM opportunity_provider_heads GROUP BY status"""
                        )
                    )
                )
                .mappings()
                .all()
            )
            opportunity_rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT status, COUNT(*) AS total
                            FROM opportunity_registration_heads GROUP BY status"""
                        )
                    )
                )
                .mappings()
                .all()
            )
            workflow_count = int(
                await uow.session.scalar(
                    text("SELECT COUNT(*) FROM opportunity_workflow_versions")
                )
                or 0
            )
            provider_frontier = int(
                await uow.session.scalar(
                    text(
                        "SELECT COALESCE(MAX(position),0) FROM opportunity_provider_events"
                    )
                )
                or 0
            )
            opportunity_frontier = int(
                await uow.session.scalar(
                    text(
                        "SELECT COALESCE(MAX(position),0) FROM opportunity_registration_events"
                    )
                )
                or 0
            )
        return {
            "status": "healthy",
            "provider_states": {
                str(row["status"]): int(row["total"]) for row in provider_rows
            },
            "opportunity_states": {
                str(row["status"]): int(row["total"]) for row in opportunity_rows
            },
            "workflow_version_count": workflow_count,
            "provider_event_frontier": provider_frontier,
            "registration_event_frontier": opportunity_frontier,
        }

    async def scheduler_health_snapshot(self) -> dict[str, Any]:
        """Return content-free scheduler backlog and exact-receipt counts."""

        async with self.runtime.unit_of_work() as uow:
            publication_rows = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT status, COUNT(*) AS total
                            FROM opportunity_publication_outbox GROUP BY status"""
                        )
                    )
                )
                .mappings()
                .all()
            )
            counts = (
                (
                    await uow.session.execute(
                        text(
                            """SELECT
                            (SELECT COUNT(*) FROM opportunity_occurrences)
                                AS occurrences,
                            (SELECT COUNT(*) FROM opportunity_delivery_receipts)
                                AS receipts,
                            (SELECT COUNT(*) FROM opportunity_activation_states
                             WHERE pending_occurrence_id <> '') AS pending_delivery"""
                        )
                    )
                )
                .mappings()
                .one()
            )
        claim = self.writer_claim
        return {
            "status": "healthy" if claim is not None else "disabled",
            "publication_states": {
                str(row["status"]): int(row["total"]) for row in publication_rows
            },
            "occurrence_count": int(counts["occurrences"]),
            "receipt_count": int(counts["receipts"]),
            "pending_delivery_count": int(counts["pending_delivery"]),
            "singleton_writer": (
                {
                    "generation_id": claim.generation_id,
                    "namespace": claim.namespace,
                    "state_key": claim.state_key,
                    "owner_instance_id": claim.owner_instance_id,
                    "lease_epoch": claim.lease_epoch,
                }
                if claim is not None
                else None
            ),
        }


class LocalOpportunityStore(SQLOpportunityStore):
    """SQLite implementation of the canonical opportunity contracts."""


class MySQLOpportunityStore(SQLOpportunityStore):
    """MySQL implementation of the canonical opportunity contracts."""


__all__ = [
    "LocalOpportunityStore",
    "MySQLOpportunityStore",
    "SQLOpportunityStore",
]
