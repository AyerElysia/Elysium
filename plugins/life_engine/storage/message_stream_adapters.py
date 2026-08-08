"""SQL facts and ordered stream turn coordination."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.kernel.storage import canonical_json

from .contracts import StorageBackendRuntime
from .models import BackendKind
from .message_stream_contracts import InboundMessage, MessageConflict, StreamTurn, TurnClaimLost, TurnStatus


def _iso(value: Any) -> str:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()


class SQLMessageStreamStore:
    def __init__(self, runtime: StorageBackendRuntime) -> None:
        if not runtime.enabled:
            raise RuntimeError("message stream store requires enabled storage")
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
    def _message(row: Any) -> InboundMessage:
        # occurred_at/received_at 在 MySQL 是 DATETIME 列，读回为 datetime 对象；
        # str() 会丢失 ISO 偏移与分隔符（"2026-08-08 12:08:39"），与 fact 写入的
        # UTC ISO 文本（"2026-08-08T12:08:39+00:00"）永不相等，导致 record_message
        # 的不可变相等校验对 MySQL 上的每条消息误报 MessageConflict。
        # 与 _turn 的读回一致：统一经 _iso() 规范化为 UTC ISO 文本再比较。
        return InboundMessage(
            message_id=str(row["message_id"]),
            platform=str(row["platform"]),
            platform_event_id=str(row["platform_event_id"]),
            occurrence_id=str(row["occurrence_id"]),
            payload_sha256=str(row["payload_sha256"]),
            stream_id=str(row["stream_id"]),
            reply_target=str(row["reply_target"]),
            source=str(row["source"]),
            occurred_at=_iso(row["occurred_at"]),
            received_at=_iso(row["received_at"]),
            raw_payload_ref=str(row["raw_payload_ref"]),
        )

    @staticmethod
    def _turn(row: Any) -> StreamTurn:
        return StreamTurn(
            turn_id=str(row["turn_id"]), stream_id=str(row["stream_id"]), stream_sequence=int(row["stream_sequence"]), source_message_id=str(row["source_message_id"]),
            status=TurnStatus(str(row["status"])), claim_owner=str(row["claim_owner"]) if row["claim_owner"] is not None else None, claim_epoch=int(row["claim_epoch"]),
            lease_until=_iso(row["lease_until"]) if row["lease_until"] is not None else None, input_frontier=json.loads(str(row["input_frontier_json"])),
            result_ref=str(row["result_ref"]) if row["result_ref"] is not None else None, result_digest=str(row["result_digest"]) if row["result_digest"] is not None else None,
            attempts=int(row["attempts"]), created_at=_iso(row["created_at"]), updated_at=_iso(row["updated_at"]),
        )

    async def record_message(self, message: InboundMessage) -> InboundMessage:
        try:
            async with self.runtime.unit_of_work() as uow:
                await uow.session.execute(text("""INSERT INTO inbound_messages
                    (message_id,platform,platform_event_id,occurrence_id,payload_sha256,stream_id,reply_target,source,occurred_at,received_at,raw_payload_ref)
                    VALUES (:id,:platform,:event,:occurrence,:digest,:stream,:target,:source,:occurred,:received,:ref)"""), {
                        "id": message.message_id, "platform": message.platform, "event": message.platform_event_id, "occurrence": message.occurrence_id,
                        "digest": message.payload_sha256, "stream": message.stream_id, "target": message.reply_target, "source": message.source,
                        "occurred": message.occurred_at, "received": message.received_at, "ref": message.raw_payload_ref,
                    })
        except IntegrityError:
            pass
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("SELECT * FROM inbound_messages WHERE message_id=:id OR (platform=:platform AND platform_event_id=:event) OR (source=:source AND occurrence_id=:occurrence)"), {"id": message.message_id, "platform": message.platform, "event": message.platform_event_id, "source": message.source, "occurrence": message.occurrence_id})).mappings().first()
        if row is None:
            raise MessageConflict("message identity collision could not be resolved")
        existing = self._message(row)
        if existing != message:
            raise MessageConflict(f"immutable message conflict: {message.message_id}")
        return existing

    async def create_turn(self, turn: StreamTurn) -> StreamTurn:
        """Create one stream turn, allocating the next sequence atomically.

        ``stream_sequence`` on the input is advisory: the real per-stream
        sequence is computed inside the same transaction (``MAX + 1`` guarded
        by a row lock on the stream scope).  Two nodes racing for the same
        stream are serialized by the database; an IntegrityError from a
        sequence race is retried once, while a source-message identity race
        falls through to the idempotent read-back path.
        """
        for _attempt in range(2):
            try:
                async with self.runtime.unit_of_work() as uow:
                    now = await self._now(uow.session)
                    # Lock the stream scope (InnoDB gap/next-key lock when the
                    # stream has no turns yet; SQLite serializes writers).
                    await uow.session.execute(
                        text(
                            "SELECT stream_id FROM stream_turns WHERE stream_id=:stream LIMIT 1"
                            + self._for_update
                        ),
                        {"stream": turn.stream_id},
                    )
                    seq_row = (
                        await uow.session.execute(
                            text(
                                "SELECT COALESCE(MAX(stream_sequence), 0) + 1 AS next_seq "
                                "FROM stream_turns WHERE stream_id=:stream"
                            ),
                            {"stream": turn.stream_id},
                        )
                    ).mappings().first()
                    next_seq = int(seq_row["next_seq"]) if seq_row is not None else 1
                    await uow.session.execute(text("""INSERT INTO stream_turns
                        (turn_id,stream_id,stream_sequence,source_message_id,status,claim_epoch,input_frontier_json,attempts,created_at,updated_at)
                        VALUES (:id,:stream,:seq,:message,'pending',0,:frontier,0,:now,:now)"""), {"id": turn.turn_id, "stream": turn.stream_id, "seq": next_seq, "message": turn.source_message_id, "frontier": canonical_json(turn.input_frontier), "now": self._time(now)})
            except IntegrityError:
                # Sequence race: another node inserted a turn for the same
                # stream concurrently. Re-read and retry allocation once.
                continue
            break
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("SELECT * FROM stream_turns WHERE turn_id=:id"), {"id": turn.turn_id})).mappings().first()
        if row is None:
            raise MessageConflict("stream turn identity collision")
        existing = self._turn(row)
        if existing.stream_id != turn.stream_id or existing.source_message_id != turn.source_message_id:
            raise MessageConflict(f"stream turn conflict: {turn.turn_id}")
        return existing

    async def claim_turn(self, turn_id: str, *, owner_id: str, lease_seconds: int) -> StreamTurn | None:
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("SELECT * FROM stream_turns WHERE turn_id=:id" + self._for_update), {"id": turn_id})).mappings().first()
            if row is None:
                return None
            turn = self._turn(row)
            now = await self._now(uow.session)
            expired = turn.lease_until is not None and datetime.fromisoformat(turn.lease_until) <= now
            if turn.status not in {TurnStatus.PENDING, TurnStatus.RETRYABLE} and not expired:
                return None
            lease_sql = "DATE_ADD(:now, INTERVAL :lease SECOND)" if self.backend == BackendKind.MYSQL else "DATETIME(:now, '+' || :lease || ' seconds')"
            await uow.session.execute(text(f"""UPDATE stream_turns SET status='claimed',claim_owner=:owner,claim_epoch=claim_epoch+1,
                lease_until={lease_sql},attempts=attempts+1,updated_at=:now WHERE turn_id=:id"""), {"owner": owner_id, "lease": int(lease_seconds), "now": self._time(now), "id": turn_id})
            row = (await uow.session.execute(text("SELECT * FROM stream_turns WHERE turn_id=:id"), {"id": turn_id})).mappings().one()
        return self._turn(row)

    async def commit_turn(self, turn_id: str, *, owner_id: str, claim_epoch: int, result_ref: str, result_digest: str) -> StreamTurn:
        async with self.runtime.unit_of_work() as uow:
            row = (await uow.session.execute(text("SELECT * FROM stream_turns WHERE turn_id=:id" + self._for_update), {"id": turn_id})).mappings().one()
            turn = self._turn(row)
            if turn.claim_owner != owner_id or turn.claim_epoch != int(claim_epoch) or turn.status not in {TurnStatus.CLAIMED, TurnStatus.PROCESSING}:
                raise TurnClaimLost(f"stream turn claim lost: {turn_id}")
            now = await self._now(uow.session)
            await uow.session.execute(text("""UPDATE stream_turns SET status='completed',result_ref=:ref,result_digest=:digest,lease_until=NULL,updated_at=:now WHERE turn_id=:id"""), {"ref": result_ref, "digest": result_digest, "now": self._time(now), "id": turn_id})
            row = (await uow.session.execute(text("SELECT * FROM stream_turns WHERE turn_id=:id"), {"id": turn_id})).mappings().one()
        return self._turn(row)


__all__ = ["SQLMessageStreamStore"]
