"""SQL outbox adapter with claim fencing and explicit unknown semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .contracts import StorageBackendRuntime
from .models import BackendKind
from .outbox_contracts import OutboxAction, OutboxClaimLost, OutboxConflict, OutboxStatus


def _iso(value: Any) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()


class SQLOutboxStore:
    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled:
            raise RuntimeError("outbox store requires enabled storage")
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
    def _record(row: Any) -> OutboxAction:
        return OutboxAction(
            action_id=str(row["action_id"]), idempotency_key=str(row["idempotency_key"]),
            source_event_id=str(row["source_event_id"]), stream_id=str(row["stream_id"]),
            target=str(row["target"]), payload_ref=str(row["payload_ref"]), payload_sha256=str(row["payload_sha256"]),
            status=OutboxStatus(str(row["status"])), claim_owner=str(row["claim_owner"]) if row["claim_owner"] is not None else None,
            claim_epoch=int(row["claim_epoch"]), lease_until=_iso(row["lease_until"]) if row["lease_until"] is not None else None,
            provider_request_id=str(row["provider_request_id"]) if row["provider_request_id"] is not None else None,
            provider_receipt_id=str(row["provider_receipt_id"]) if row["provider_receipt_id"] is not None else None,
            attempts=int(row["attempts"]), last_error_type=str(row["last_error_type"]) if row["last_error_type"] is not None else None,
            created_at=_iso(row["created_at"]), updated_at=_iso(row["updated_at"]),
        )

    async def _get(self, action_id: str, *, locked: bool = False, session: Any | None = None) -> OutboxAction | None:
        sql = "SELECT * FROM outbox_actions WHERE action_id=:id" + (self._for_update if locked else "")
        if session is not None:
            row = (await session.execute(text(sql), {"id": action_id})).mappings().first()
        else:
            async with self.runtime.unit_of_work() as uow:
                row = (await uow.session.execute(text(sql), {"id": action_id})).mappings().first()
        return self._record(row) if row is not None else None

    async def create_action(self, action: OutboxAction) -> OutboxAction:
        if action.status != OutboxStatus.PENDING or action.claim_owner is not None or action.claim_epoch != 0:
            raise ValueError("new outbox action must be unclaimed pending intent")
        try:
            async with self.runtime.unit_of_work() as uow:
                now = await self._now(uow.session)
                await uow.session.execute(text("""INSERT INTO outbox_actions (
                    action_id,idempotency_key,source_event_id,stream_id,target,payload_ref,payload_sha256,status,
                    claim_epoch,attempts,created_at,updated_at) VALUES (:action_id,:key,:source,:stream,:target,:ref,:digest,'pending',0,0,:now,:now)"""),
                    {"action_id": action.action_id, "key": action.idempotency_key, "source": action.source_event_id, "stream": action.stream_id,
                     "target": action.target, "ref": action.payload_ref, "digest": action.payload_sha256, "now": self._time(now)})
        except IntegrityError:
            pass
        existing = await self._get(action.action_id)
        if existing is None:
            raise OutboxConflict("outbox idempotency key belongs to another action")
        immutable = (existing.idempotency_key, existing.source_event_id, existing.stream_id, existing.target, existing.payload_ref, existing.payload_sha256)
        incoming = (action.idempotency_key, action.source_event_id, action.stream_id, action.target, action.payload_ref, action.payload_sha256)
        if immutable != incoming:
            raise OutboxConflict(f"outbox action identity conflict: {action.action_id}")
        return existing

    async def claim_action(self, action_id: str, *, owner_id: str, lease_seconds: int) -> OutboxAction | None:
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        async with self.runtime.unit_of_work() as uow:
            action = await self._get(action_id, locked=True, session=uow.session)
            if action is None:
                return None
            now = await self._now(uow.session)
            expired = action.lease_until is not None and datetime.fromisoformat(action.lease_until) <= now
            if action.status == OutboxStatus.UNKNOWN or (action.status not in {OutboxStatus.PENDING, OutboxStatus.RETRYABLE} and not expired):
                return None
            lease_sql = "DATE_ADD(:now, INTERVAL :lease SECOND)" if self.backend == BackendKind.MYSQL else "DATETIME(:now, '+' || :lease || ' seconds')"
            await uow.session.execute(text(f"""UPDATE outbox_actions SET status='claimed',claim_owner=:owner,claim_epoch=claim_epoch+1,
                lease_until={lease_sql},attempts=attempts+1,updated_at=:now WHERE action_id=:id"""),
                {"owner": owner_id, "lease": int(lease_seconds), "now": self._time(now), "id": action_id})
            return await self._get(action_id, session=uow.session)

    async def _transition(self, action_id: str, *, owner_id: str, claim_epoch: int, status: OutboxStatus, provider_request_id: str | None = None, provider_receipt_id: str | None = None, error_type: str | None = None) -> OutboxAction:
        async with self.runtime.unit_of_work() as uow:
            action = await self._get(action_id, locked=True, session=uow.session)
            if action is None or action.claim_owner != owner_id or action.claim_epoch != int(claim_epoch) or action.status not in {OutboxStatus.CLAIMED, OutboxStatus.SENDING}:
                raise OutboxClaimLost(f"outbox claim lost: {action_id}")
            now = await self._now(uow.session)
            await uow.session.execute(text("""UPDATE outbox_actions SET status=:status,provider_request_id=COALESCE(:request,provider_request_id),
                provider_receipt_id=COALESCE(:receipt,provider_receipt_id),last_error_type=:error,
                lease_until=CASE WHEN :terminal=1 THEN NULL ELSE lease_until END,updated_at=:now WHERE action_id=:id"""),
                {"status": status.value, "request": provider_request_id, "receipt": provider_receipt_id, "error": error_type,
                 "terminal": int(status in {OutboxStatus.SENT, OutboxStatus.RETRYABLE, OutboxStatus.FAILED, OutboxStatus.UNKNOWN}),
                 "now": self._time(now), "id": action_id})
            result = await self._get(action_id, session=uow.session)
            assert result is not None
            return result

    async def mark_sending(self, action_id: str, *, owner_id: str, claim_epoch: int, provider_request_id: str) -> OutboxAction:
        return await self._transition(action_id, owner_id=owner_id, claim_epoch=claim_epoch, status=OutboxStatus.SENDING, provider_request_id=provider_request_id)

    async def mark_sent(self, action_id: str, *, owner_id: str, claim_epoch: int, provider_receipt_id: str) -> OutboxAction:
        return await self._transition(action_id, owner_id=owner_id, claim_epoch=claim_epoch, status=OutboxStatus.SENT, provider_receipt_id=provider_receipt_id)

    async def mark_retryable(self, action_id: str, *, owner_id: str, claim_epoch: int, error_type: str) -> OutboxAction:
        return await self._transition(action_id, owner_id=owner_id, claim_epoch=claim_epoch, status=OutboxStatus.RETRYABLE, error_type=error_type)

    async def mark_unknown(self, action_id: str, *, owner_id: str, claim_epoch: int, error_type: str) -> OutboxAction:
        return await self._transition(action_id, owner_id=owner_id, claim_epoch=claim_epoch, status=OutboxStatus.UNKNOWN, error_type=error_type)


__all__ = ["SQLOutboxStore"]
