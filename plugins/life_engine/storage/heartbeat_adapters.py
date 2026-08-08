"""SQL heartbeat operation store; model calls stay outside its transactions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.kernel.storage import canonical_json

from .contracts import StorageBackendRuntime
from .models import BackendKind
from .heartbeat_contracts import HeartbeatClaimLost, HeartbeatConflict, HeartbeatOperation, HeartbeatStatus


def _iso(value: Any) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()


class SQLHeartbeatStore:
    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled:
            raise RuntimeError("heartbeat store requires enabled storage")
        self.runtime = runtime
        self.backend = runtime.backend

    @property
    def _for_update(self) -> str:
        return " FOR UPDATE" if self.backend == BackendKind.MYSQL else ""

    async def _now(self, session: Any) -> datetime:
        sql = "SELECT CURRENT_TIMESTAMP(6)" if self.backend == BackendKind.MYSQL else "SELECT STRFTIME('%Y-%m-%dT%H:%M:%f+00:00', 'now')"
        value = await session.scalar(text(sql))
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)

    def _time(self, value: datetime) -> datetime | str:
        return value.replace(tzinfo=None) if self.backend == BackendKind.MYSQL else value.isoformat()

    @staticmethod
    def _record(row: Any) -> HeartbeatOperation:
        return HeartbeatOperation(
            str(row["heartbeat_operation_id"]), str(row["consciousness_instance_id"]), int(row["sequence"]),
            json.loads(str(row["input_frontier_json"])), str(row["prepared_context_digest"]) if row["prepared_context_digest"] is not None else None,
            HeartbeatStatus(str(row["status"])), str(row["claim_owner"]) if row["claim_owner"] is not None else None, int(row["claim_epoch"]),
            _iso(row["lease_until"]) if row["lease_until"] is not None else None, str(row["model_request_id"]) if row["model_request_id"] is not None else None,
            str(row["result_ref"]) if row["result_ref"] is not None else None, str(row["result_digest"]) if row["result_digest"] is not None else None,
            int(row["committed_frontier"]) if row["committed_frontier"] is not None else None, int(row["attempts"]), _iso(row["created_at"]), _iso(row["updated_at"]),
        )

    async def register(self, operation: HeartbeatOperation) -> HeartbeatOperation:
        try:
            async with self.runtime.unit_of_work() as uow:
                now = await self._now(uow.session)
                await uow.session.execute(text("""INSERT INTO heartbeat_operations
                    (heartbeat_operation_id,consciousness_instance_id,sequence,input_frontier_json,prepared_context_digest,status,claim_epoch,attempts,created_at,updated_at)
                    VALUES (:id,:instance,:sequence,:frontier,:digest,'pending',0,0,:now,:now)"""), {"id": operation.heartbeat_operation_id, "instance": operation.consciousness_instance_id, "sequence": operation.sequence, "frontier": canonical_json(operation.input_frontier), "digest": operation.prepared_context_digest, "now": self._time(now)})
        except IntegrityError:
            pass
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("SELECT * FROM heartbeat_operations WHERE heartbeat_operation_id=:id"), {"id": operation.heartbeat_operation_id})).mappings().first()
        if row is None:
            raise HeartbeatConflict("heartbeat identity collision")
        existing = self._record(row)
        if existing.consciousness_instance_id != operation.consciousness_instance_id or existing.sequence != operation.sequence or existing.input_frontier != operation.input_frontier:
            raise HeartbeatConflict(f"heartbeat conflict: {operation.heartbeat_operation_id}")
        return existing

    async def claim(self, operation_id: str, *, owner_id: str, lease_seconds: int) -> HeartbeatOperation | None:
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("SELECT * FROM heartbeat_operations WHERE heartbeat_operation_id=:id" + self._for_update), {"id": operation_id})).mappings().first()
            if row is None:
                return None
            operation = self._record(row)
            now = await self._now(uow.session)
            expired = operation.lease_until is not None and datetime.fromisoformat(operation.lease_until) <= now
            if operation.status not in {HeartbeatStatus.PENDING, HeartbeatStatus.RETRYABLE} and not expired:
                return None
            lease_sql = "DATE_ADD(:now, INTERVAL :lease SECOND)" if self.backend == BackendKind.MYSQL else "DATETIME(:now, '+' || :lease || ' seconds')"
            await uow.session.execute(text(f"""UPDATE heartbeat_operations SET status='claimed',claim_owner=:owner,claim_epoch=claim_epoch+1,
                lease_until={lease_sql},attempts=attempts+1,updated_at=:now WHERE heartbeat_operation_id=:id"""), {"owner": owner_id, "lease": int(lease_seconds), "now": self._time(now), "id": operation_id})
            row = (await uow.session.execute(text("SELECT * FROM heartbeat_operations WHERE heartbeat_operation_id=:id"), {"id": operation_id})).mappings().one()
        return self._record(row)

    async def commit(self, operation_id: str, *, owner_id: str, claim_epoch: int, input_frontier: int, committed_frontier: int, result_ref: str, result_digest: str) -> HeartbeatOperation:
        if int(committed_frontier) < int(input_frontier):
            raise HeartbeatConflict("heartbeat frontier cannot regress")
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("SELECT * FROM heartbeat_operations WHERE heartbeat_operation_id=:id" + self._for_update), {"id": operation_id})).mappings().one()
            operation = self._record(row)
            if operation.claim_owner != owner_id or operation.claim_epoch != int(claim_epoch) or operation.status not in {HeartbeatStatus.CLAIMED, HeartbeatStatus.PROCESSING}:
                raise HeartbeatClaimLost(f"heartbeat claim lost: {operation_id}")
            if operation.committed_frontier is not None:
                if operation.result_digest != result_digest:
                    raise HeartbeatConflict(f"heartbeat result conflict: {operation_id}")
                return operation
            now = await self._now(uow.session)
            await uow.session.execute(text("""UPDATE heartbeat_operations SET status='completed',model_request_id=COALESCE(model_request_id,:request),result_ref=:ref,result_digest=:digest,
                committed_frontier=:frontier,lease_until=NULL,updated_at=:now WHERE heartbeat_operation_id=:id"""), {"request": result_ref, "ref": result_ref, "digest": result_digest, "frontier": int(committed_frontier), "now": self._time(now), "id": operation_id})
            row = (await uow.session.execute(text("SELECT * FROM heartbeat_operations WHERE heartbeat_operation_id=:id"), {"id": operation_id})).mappings().one()
        return self._record(row)

    async def mark_failed(self, operation_id: str, *, owner_id: str, claim_epoch: int, retryable: bool) -> HeartbeatOperation:
        status = HeartbeatStatus.RETRYABLE if retryable else HeartbeatStatus.FAILED
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("SELECT * FROM heartbeat_operations WHERE heartbeat_operation_id=:id" + self._for_update), {"id": operation_id})).mappings().one()
            operation = self._record(row)
            if operation.claim_owner != owner_id or operation.claim_epoch != int(claim_epoch) or operation.status not in {HeartbeatStatus.CLAIMED, HeartbeatStatus.PROCESSING}:
                raise HeartbeatClaimLost(f"heartbeat claim lost: {operation_id}")
            now = await self._now(uow.session)
            await uow.session.execute(text("UPDATE heartbeat_operations SET status=:status,lease_until=NULL,updated_at=:now WHERE heartbeat_operation_id=:id"), {"status": status.value, "now": self._time(now), "id": operation_id})
            row = (await uow.session.execute(text("SELECT * FROM heartbeat_operations WHERE heartbeat_operation_id=:id"), {"id": operation_id})).mappings().one()
        return self._record(row)


__all__ = ["SQLHeartbeatStore"]
