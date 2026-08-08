"""SQL operation store for fine-grained multi-writer coordination."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.kernel.storage import canonical_json

from .contracts import StorageBackendRuntime
from .models import BackendKind
from .operation_contracts import (
    OperationClaimLost,
    OperationConflict,
    OperationReceipt,
    OperationRecord,
    OperationStatus,
    RuntimeDelta,
    RuntimeDeltaConflict,
)

_ALLOWED_DELTA_TYPES = frozenset(
    {
        "append_event",
        "append_pending_message",
        "claim_stream_turn",
        "commit_stream_turn",
        "advance_stream_cursor",
        "advance_thought_cursor",
        "append_heartbeat_result",
        "set_pause_checkpoint",
        "set_technical_projection",
        "append_failure_evidence",
    }
)
_APPEND_LIST_DELTAS = frozenset(
    {"append_event", "append_pending_message", "append_heartbeat_result", "append_failure_evidence"}
)


def _identity(value: Any, field: str, maximum: int = 255) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum:
        raise ValueError(f"{field} must be 1..{maximum} characters")
    return result


def _decode(value: Any) -> dict[str, Any]:
    raw = value.decode("utf-8") if isinstance(value, bytes) else str(value or "{}")
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise RuntimeDeltaConflict("persisted operation payload is not an object")
    return decoded


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


class SQLOperationStore:
    """Claim and commit concrete operations without a global writer lease."""

    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled:
            raise RuntimeError("operation store requires enabled storage")
        self.runtime = runtime
        self.backend = runtime.backend

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    async def _now(self, session: Any) -> datetime:
        statement = (
            "SELECT CURRENT_TIMESTAMP(6)"
            if self.backend == BackendKind.MYSQL
            else "SELECT STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
        )
        value = await session.scalar(text(statement))
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)

    def _time(self, value: datetime) -> datetime | str:
        return value.replace(tzinfo=None) if self.backend == BackendKind.MYSQL else value.isoformat()

    @staticmethod
    def _record(row: Any) -> OperationRecord:
        return OperationRecord(
            operation_id=str(row["operation_id"]),
            operation_type=str(row["operation_type"]),
            scope_key=str(row["scope_key"]),
            sequence=int(row["sequence"]),
            status=OperationStatus(str(row["status"])),
            claim_owner=str(row["claim_owner"]) if row["claim_owner"] is not None else None,
            claim_epoch=int(row["claim_epoch"]),
            lease_until=_iso(row["lease_until"]) if row["lease_until"] is not None else None,
            input_frontier=_decode(row["input_frontier_json"]),
            result_ref=str(row["result_ref"]) if row["result_ref"] is not None else None,
            result_sha256=str(row["result_sha256"]) if row["result_sha256"] is not None else None,
            attempts=int(row["attempts"]),
            created_at=_iso(row["created_at"]),
            updated_at=_iso(row["updated_at"]),
        )

    async def register_operation(self, *, operation_id: str, operation_type: str, scope_key: str, sequence: int, input_frontier: dict[str, Any] | None = None) -> OperationRecord:
        operation_id = _identity(operation_id, "operation_id")
        operation_type = _identity(operation_type, "operation_type", 128)
        scope_key = _identity(scope_key, "scope_key")
        sequence = int(sequence)
        frontier_json = canonical_json(input_frontier or {})
        try:
            async with self.runtime.unit_of_work() as uow:
                now = await self._now(uow.session)
                await uow.session.execute(text("""INSERT INTO operations (
                    operation_id, operation_type, scope_key, sequence, status,
                    claim_epoch, input_frontier_json, attempts, created_at, updated_at
                ) VALUES (:operation_id, :operation_type, :scope_key, :sequence,
                    'pending', 0, :frontier, 0, :now, :now)"""), {
                    "operation_id": operation_id, "operation_type": operation_type,
                    "scope_key": scope_key, "sequence": sequence,
                    "frontier": frontier_json, "now": self._time(now),
                })
        except IntegrityError:
            pass
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("SELECT * FROM operations WHERE operation_id = :operation_id"), {"operation_id": operation_id})).mappings().first()
        if row is None:
            raise OperationConflict("operation scope/sequence already belongs to another identity")
        record = self._record(row)
        if (record.operation_type, record.scope_key, record.sequence, record.input_frontier) != (operation_type, scope_key, sequence, input_frontier or {}):
            raise OperationConflict(f"operation identity conflict: {operation_id}")
        return record

    async def claim_operation(self, operation_id: str, *, owner_id: str, lease_seconds: int) -> OperationRecord | None:
        operation_id = _identity(operation_id, "operation_id")
        owner_id = _identity(owner_id, "owner_id")
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self.runtime.unit_of_work() as uow:
            session = uow.session
            row = (await session.execute(text("SELECT * FROM operations WHERE operation_id = :operation_id" + self._for_update), {"operation_id": operation_id})).mappings().first()
            if row is None:
                return None
            record = self._record(row)
            now = await self._now(session)
            expired = record.lease_until is not None and datetime.fromisoformat(record.lease_until) <= now
            if record.status not in {OperationStatus.PENDING, OperationStatus.RETRYABLE} and not expired:
                return None
            lease_expression = "DATE_ADD(:now, INTERVAL :lease SECOND)" if self.backend == BackendKind.MYSQL else "DATETIME(:now, '+' || :lease || ' seconds')"
            await session.execute(text(f"""UPDATE operations SET status = 'claimed', claim_owner = :owner,
                claim_epoch = claim_epoch + 1, lease_until = {lease_expression}, attempts = attempts + 1,
                updated_at = :now WHERE operation_id = :operation_id"""), {
                "owner": owner_id, "lease": int(lease_seconds), "now": self._time(now), "operation_id": operation_id,
            })
            row = (await session.execute(text("SELECT * FROM operations WHERE operation_id = :operation_id"), {"operation_id": operation_id})).mappings().one()
        return self._record(row)

    @staticmethod
    def _apply_delta(current: dict[str, Any], delta: RuntimeDelta) -> dict[str, Any]:
        if delta.delta_type not in _ALLOWED_DELTA_TYPES:
            raise RuntimeDeltaConflict(f"unsupported typed delta: {delta.delta_type}")
        result = dict(current)
        if delta.delta_type in _APPEND_LIST_DELTAS:
            items = list(result.get(delta.delta_type, []))
            identity = str(delta.payload.get("identity") or "").strip()
            if not identity:
                raise RuntimeDeltaConflict("append delta requires payload.identity")
            existing = next((item for item in items if item.get("identity") == identity), None)
            if existing is not None and existing != delta.payload:
                raise RuntimeDeltaConflict(f"append identity conflict: {identity}")
            if existing is None:
                items.append(dict(delta.payload))
            result[delta.delta_type] = items
            return result
        if delta.delta_type in {"advance_stream_cursor", "advance_thought_cursor"}:
            frontier = int(delta.payload.get("frontier", -1))
            previous = int(result.get(delta.delta_type, 0))
            if frontier < previous or frontier > previous + 1:
                raise RuntimeDeltaConflict(f"cursor must advance continuously: {previous}->{frontier}")
            result[delta.delta_type] = frontier
            return result
        result[delta.delta_type] = dict(delta.payload)
        return result

    async def commit_runtime_delta(self, delta: RuntimeDelta, *, owner_id: str, claim_epoch: int, result_ref: str, result_sha256: str) -> OperationReceipt:
        owner_id = _identity(owner_id, "owner_id")
        if delta.delta_type not in _ALLOWED_DELTA_TYPES:
            raise RuntimeDeltaConflict(f"unsupported typed delta: {delta.delta_type}")
        payload_json = canonical_json(delta.payload)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        async with self.runtime.unit_of_work() as uow:
            session = uow.session
            receipt = (await session.execute(text("SELECT * FROM operation_receipts WHERE operation_id = :id" + self._for_update), {"id": delta.operation_id})).mappings().first()
            if receipt is not None:
                if str(receipt["result_sha256"]) != result_sha256:
                    raise OperationConflict(f"operation receipt digest conflict: {delta.operation_id}")
                return OperationReceipt(delta.operation_id, int(receipt["commit_revision"]), str(receipt["result_sha256"]), str(receipt["committed_by"]), _iso(receipt["committed_at"]))
            operation = (await session.execute(text("SELECT * FROM operations WHERE operation_id = :id" + self._for_update), {"id": delta.operation_id})).mappings().first()
            if operation is None or str(operation["claim_owner"] or "") != owner_id or int(operation["claim_epoch"]) != int(claim_epoch) or str(operation["status"]) not in {"claimed", "processing"}:
                raise OperationClaimLost(f"operation claim lost: {delta.operation_id}")
            state = (await session.execute(text("SELECT * FROM runtime_states WHERE namespace = :namespace AND state_key = :state_key" + self._for_update), {"namespace": delta.namespace, "state_key": delta.state_key})).mappings().first()
            current = _decode(state["payload_json"]) if state is not None else {}
            revision = int(state["revision"]) + 1 if state is not None else 1
            updated = self._apply_delta(current, delta)
            updated_json = canonical_json(updated)
            updated_hash = hashlib.sha256(updated_json.encode("utf-8")).hexdigest()
            now = await self._now(session)
            if state is None:
                await session.execute(text("""INSERT INTO runtime_states (namespace, state_key, revision, schema_version, payload_json, payload_sha256, updated_at)
                    VALUES (:namespace, :state_key, :revision, :schema_version, :payload, :digest, :now)"""), {"namespace": delta.namespace, "state_key": delta.state_key, "revision": revision, "schema_version": delta.schema_version, "payload": updated_json, "digest": updated_hash, "now": self._time(now)})
            else:
                await session.execute(text("""UPDATE runtime_states SET revision=:revision, schema_version=:schema_version,
                    payload_json=:payload, payload_sha256=:digest, updated_at=:now
                    WHERE namespace=:namespace AND state_key=:state_key"""), {"revision": revision, "schema_version": delta.schema_version, "payload": updated_json, "digest": updated_hash, "now": self._time(now), "namespace": delta.namespace, "state_key": delta.state_key})
            await session.execute(text("""INSERT INTO runtime_deltas (operation_id, namespace, state_key, delta_type, schema_version, payload_json, payload_sha256, actor, source, causation_id, created_at)
                VALUES (:operation_id, :namespace, :state_key, :delta_type, :schema_version, :payload, :digest, :actor, :source, :causation, :created_at)"""), {"operation_id": delta.operation_id, "namespace": delta.namespace, "state_key": delta.state_key, "delta_type": delta.delta_type, "schema_version": delta.schema_version, "payload": payload_json, "digest": payload_sha256, "actor": delta.actor, "source": delta.source, "causation": delta.causation_id, "created_at": self._time(datetime.fromisoformat(delta.created_at.replace("Z", "+00:00")).astimezone(UTC))})
            await session.execute(text("""INSERT INTO operation_receipts (operation_id, commit_revision, result_sha256, committed_by, committed_at)
                VALUES (:id, :revision, :digest, :owner, :now)"""), {"id": delta.operation_id, "revision": revision, "digest": result_sha256, "owner": owner_id, "now": self._time(now)})
            await session.execute(text("""UPDATE operations SET status='completed', result_ref=:result_ref, result_sha256=:digest,
                lease_until=NULL, updated_at=:now WHERE operation_id=:id"""), {"result_ref": result_ref, "digest": result_sha256, "now": self._time(now), "id": delta.operation_id})
        return OperationReceipt(delta.operation_id, revision, result_sha256, owner_id, now.isoformat())


__all__ = ["SQLOperationStore"]
