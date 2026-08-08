"""Life Engine multi-writer hot-path bridge.

This bridge is the *only* surface through which production hot paths touch the
multi-writer coordination stores.  It is constructed by the Life Engine service
when ``multi_writer_enabled`` is true and registered into the core transport
hook slots; every method is a safe no-op (``None``/``False``) when the bridge
is disabled, so the default single-instance behavior is byte-for-byte unchanged.

Hot paths covered (specification sections 9-12):

- inbound message facts (idempotent immutable facts, cross-instance dedup);
- stream turn claim/commit (per-stream ordering and takeover);
- outbox send intents (durable external-action intent before platform calls);
- heartbeat operations (claimable, ordered, frontier-guarded checkpoints);
- per-node projection progress (continuous frontier for index workers).

Transactions stay short and never contain model calls, platform API calls,
Embedding, Chroma or file I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from src.kernel.storage import canonical_json

from .contracts import StorageBackendRuntime
from .heartbeat_adapters import SQLHeartbeatStore
from .heartbeat_contracts import HeartbeatOperation, HeartbeatStatus
from .instance_identity import InstanceIdentity
from .message_stream_adapters import SQLMessageStreamStore
from .message_stream_contracts import InboundMessage, StreamTurn, TurnStatus
from .outbox_adapters import SQLOutboxStore
from .outbox_contracts import OutboxAction, OutboxStatus
from .projection_progress import ProjectionProgress, SQLProjectionProgressStore

_OWNER_PATTERN_SAFE = "abcdefghijklmnopqrstuvwxyz0123456789"

logger = logging.getLogger("life_engine.hot_path_bridge")


def _utc_iso_time(value: Any) -> str:
    """Normalize a core ``Message`` timestamp into UTC ISO text.

    Core ``Message.time`` is a float epoch (seconds); passing its raw ``str()``
    into a MySQL ``datetime(6)`` column fails under strict mode (Error 1292)
    and silently drops the message.  The row readers parse ISO text via
    ``fromisoformat``, so the UTC ``+00:00`` form is the portable contract.
    """
    if value in (None, ""):
        value = time.time()
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
    try:
        ts = float(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            parsed = parsed.replace(tzinfo=parsed.tzinfo or UTC)
            return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        except (TypeError, ValueError):
            ts = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))


def _stable_digest(*parts: Any) -> str:
    payload = [str(part) for part in parts]
    return hashlib.sha256("\u241f".join(payload).encode("utf-8", errors="replace")).hexdigest()


def _short_digest(*parts: Any, length: int = 32) -> str:
    return _stable_digest(*parts)[: max(8, int(length))]


def _safe_occurrence_id(*parts: Any) -> str:
    """Deterministic occurrence identity for platforms without their own id."""
    return f"occ-{_short_digest(*parts, length=40)}"


class MultiWriterHotPathBridge:
    """Enables only when the runtime is up and a node identity is present."""

    def __init__(
        self,
        runtime: StorageBackendRuntime,
        identity: InstanceIdentity,
    ) -> None:
        self.runtime = runtime
        self.identity = identity
        self.enabled = bool(
            getattr(runtime, "enabled", False) and identity is not None
        )
        self._messages: SQLMessageStreamStore | None = None
        self._outbox: SQLOutboxStore | None = None
        self._heartbeat: SQLHeartbeatStore | None = None
        self._projection: SQLProjectionProgressStore | None = None
        if self.enabled:
            self._messages = SQLMessageStreamStore(runtime)
            self._outbox = SQLOutboxStore(runtime)
            self._heartbeat = SQLHeartbeatStore(runtime)
            self._projection = SQLProjectionProgressStore(runtime)

    @property
    def claim_owner(self) -> str:
        """Content-free owner key used for every claim made by this node."""
        if not self.enabled:
            return ""
        return self.identity.claim_owner

    @property
    def node_id(self) -> str:
        """Short node label for projection progress rows."""
        if not self.enabled:
            return "unknown"
        return f"{self.identity.deployment_id}:{self.identity.instance_id}"

    # ──────────────────────────────────────────
    # Inbound message facts (spec 9.1)
    # ──────────────────────────────────────────

    def _message_fact(self, message: Any) -> InboundMessage | None:
        """Extract a stable immutable fact from a core ``Message``."""
        message_id = str(getattr(message, "message_id", "") or "").strip()
        if not message_id:
            return None
        platform = str(getattr(message, "platform", "") or "").strip() or "unknown"
        stream_id = str(getattr(message, "stream_id", "") or "").strip()
        content = str(
            getattr(message, "processed_plain_text", None)
            or getattr(message, "content", "")
            or ""
        )
        extra = getattr(message, "extra", None) or {}
        if not isinstance(extra, dict):
            extra = {}
        chat_type = str(getattr(message, "chat_type", "") or "").strip() or "private"
        group_id = str(extra.get("group_id") or "").strip()
        user_id = str(getattr(message, "sender_id", "") or "").strip()
        reply_target = group_id if chat_type == "group" and group_id else user_id
        source = str(extra.get("adapter_signature") or platform).strip() or platform
        occurred_at = _utc_iso_time(getattr(message, "time", ""))
        received_at = (
            time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        )
        digest = _stable_digest("message", platform, message_id, content)
        return InboundMessage(
            message_id=message_id,
            platform=platform,
            platform_event_id=message_id,
            occurrence_id=_safe_occurrence_id("message", platform, message_id),
            payload_sha256=digest,
            stream_id=stream_id,
            reply_target=reply_target,
            source=source,
            occurred_at=occurred_at,
            received_at=received_at,
            raw_payload_ref=f"runtime://message/{platform}/{message_id}",
        )

    async def record_inbound_message(self, message: Any) -> bool:
        """Durably record one inbound message fact and claim its stream turn.

        On success the turn identity is attached to ``message.extra`` under
        ``multi_writer_turn`` (``turn_id``/``claim_epoch``/``message_id``) so
        the chatter can commit it once the message is consumed.

        Returns:
            True when the fact is recorded (idempotent replay included) and the
            message may proceed; False when the immutable identity collided
            with different content or the message's turn is owned by another
            instance (the message must be skipped here, never overwritten).
        """
        if not self.enabled or self._messages is None:
            return True
        fact = self._message_fact(message)
        if fact is None:
            return True
        try:
            await self._messages.record_message(fact)
        except Exception as exc:  # noqa: BLE001 - fail closed, never silently proceed
            logger.warning(
                "inbound message fact record failed: "
                f"message_id={fact.message_id}, error={exc}",
                exc_info=True,
            )
            return False
        # A message without a stream identity still gets its immutable fact;
        # only turn coordination requires the stream key.
        if not fact.stream_id:
            return True
        turn = await self.open_stream_turn(
            stream_id=fact.stream_id,
            message_id=fact.message_id,
            reply_target=fact.reply_target,
        )
        if turn is None:
            logger.debug(
                "stream turn claim failed (owned elsewhere or already complete): "
                f"stream_id={fact.stream_id[:12]}, message_id={fact.message_id}"
            )
            return False
        extra = getattr(message, "extra", None)
        if not isinstance(extra, dict):
            try:
                message.extra = {}
                extra = message.extra
            except Exception:
                extra = None
        if isinstance(extra, dict):
            extra["multi_writer_turn"] = {
                "turn_id": turn.turn_id,
                "claim_epoch": turn.claim_epoch,
                "message_id": fact.message_id,
            }
        return True

    # ──────────────────────────────────────────
    # Stream turns (spec 9.2 / 9.3)
    # ──────────────────────────────────────────

    @staticmethod
    def _turn_id(stream_id: str, message_id: str) -> str:
        return f"turn-{_short_digest(stream_id, message_id, length=40)}"

    async def open_stream_turn(
        self,
        *,
        stream_id: str,
        message_id: str,
        reply_target: str = "",
    ) -> StreamTurn | None:
        """Create and claim the turn for one inbound message.

        One message maps to exactly one turn identity
        (``UNIQUE(source_message_id)``), so two instances receiving the same
        event contend on the same database row: only one of them may claim and
        process it.

        Returns:
            The claimed turn when this node owns it; ``None`` when the turn
            already belongs to another owner or is already completed.
        """
        if not self.enabled or self._messages is None:
            return None
        stream_id = str(stream_id or "").strip()
        message_id = str(message_id or "").strip()
        if not stream_id or not message_id:
            return None
        turn_id = self._turn_id(stream_id, message_id)
        frontier: dict[str, Any] = {
            "stream_id": stream_id,
            "message_id": message_id,
            "reply_target": str(reply_target or ""),
            "node": self.node_id,
        }
        try:
            await self._messages.create_turn(
                StreamTurn(
                    turn_id=turn_id,
                    stream_id=stream_id,
                    stream_sequence=0,
                    source_message_id=message_id,
                    status=TurnStatus.PENDING,
                    claim_owner=None,
                    claim_epoch=0,
                    lease_until=None,
                    input_frontier=frontier,
                    result_ref=None,
                    result_digest=None,
                    attempts=0,
                    created_at="",
                    updated_at="",
                )
            )
            turn = await self._messages.claim_turn(
                turn_id,
                owner_id=self.claim_owner,
                lease_seconds=120,
            )
        except Exception:
            return None
        if turn is None or turn.claim_owner != self.claim_owner:
            return None
        return turn

    async def commit_stream_turn(
        self,
        *,
        turn_id: str,
        claim_epoch: int,
        message_id: str = "",
    ) -> bool:
        """Commit a claimed stream turn after its message was consumed.

        Committing is idempotent per operation; a fenced (stale) owner is
        rejected and reported as ``False`` without raising.
        """
        if not self.enabled or self._messages is None or not turn_id:
            return False
        result_ref = f"chatter://turn/{turn_id}"
        result_digest = _stable_digest("turn-result", turn_id, message_id)
        try:
            await self._messages.commit_turn(
                turn_id,
                owner_id=self.claim_owner,
                claim_epoch=int(claim_epoch),
                result_ref=result_ref,
                result_digest=result_digest,
            )
        except Exception:
            return False
        return True

    # ──────────────────────────────────────────
    # Outbox send intents (spec 11)
    # ──────────────────────────────────────────

    async def enqueue_outbox_action(self, message: Any) -> bool:
        """Persist a durable send intent before the platform call.

        Returns:
            True when the intent is durably recorded (or the bridge is
            disabled); False when recording failed and sending must not start.
        """
        if not self.enabled or self._outbox is None:
            return True
        message_id = str(getattr(message, "message_id", "") or "").strip()
        if not message_id:
            return True
        platform = str(getattr(message, "platform", "") or "").strip() or "unknown"
        stream_id = str(getattr(message, "stream_id", "") or "").strip()
        extra = getattr(message, "extra", None) or {}
        if not isinstance(extra, dict):
            extra = {}
        group_id = str(extra.get("group_id") or extra.get("target_group_id") or "").strip()
        user_id = str(
            extra.get("target_user_id")
            or getattr(message, "sender_id", "")
            or ""
        ).strip()
        target = group_id or user_id
        content = str(
            getattr(message, "processed_plain_text", None)
            or getattr(message, "content", "")
            or ""
        )
        action_id = f"outbox-{_short_digest('send', platform, message_id, length=40)}"
        idempotency_key = f"{platform}:{message_id}"
        payload_ref = f"runtime://message/{platform}/{message_id}"
        payload_sha256 = _stable_digest("send", platform, message_id, content)
        try:
            await self._outbox.create_action(
                OutboxAction(
                    action_id=action_id,
                    idempotency_key=idempotency_key,
                    source_event_id=message_id,
                    stream_id=stream_id,
                    target=target,
                    payload_ref=payload_ref,
                    payload_sha256=payload_sha256,
                    status=OutboxStatus.PENDING,
                    claim_owner=None,
                    claim_epoch=0,
                    lease_until=None,
                    provider_request_id=None,
                    provider_receipt_id=None,
                    attempts=0,
                    last_error_type=None,
                    created_at="",
                    updated_at="",
                )
            )
        except Exception:
            return False
        return True

    async def settle_outbox_action(
        self,
        message: Any,
        outcome: dict[str, Any],
    ) -> bool:
        """Finalize one outbox action after the platform call.

        The action identity is re-derived from the message, so no secret or
        message body ever needs to be carried on the wire.  Settlement claims
        the action under this node; when another node already owns or finished
        it, the settlement is treated as already handled.

        Outcome semantics follow the specification's state machine:

        - ``delivery_unknown`` → ``unknown`` (never blindly retried);
        - provider receipt present → ``sent`` with a content-free receipt digest;
        - explicit error type → ``retryable`` (safe to retry later).
        """
        if not self.enabled or self._outbox is None:
            return True
        message_id = str(getattr(message, "message_id", "") or "").strip()
        if not message_id:
            return True
        platform = str(getattr(message, "platform", "") or "").strip() or "unknown"
        action_id = f"outbox-{_short_digest('send', platform, message_id, length=40)}"
        receipt = outcome.get("provider_receipt") or {}
        error_type = str(outcome.get("error_type") or "")
        delivery_unknown = bool(outcome.get("delivery_unknown"))
        try:
            claimed = await self._outbox.claim_action(
                action_id,
                owner_id=self.claim_owner,
                lease_seconds=120,
            )
        except Exception:
            return False
        if claimed is None:
            return True
        try:
            if delivery_unknown:
                await self._outbox.mark_unknown(
                    action_id,
                    owner_id=self.claim_owner,
                    claim_epoch=claimed.claim_epoch,
                    error_type=error_type or "DeliveryUnknown",
                )
            elif receipt:
                receipt_digest = _stable_digest(
                    "receipt", platform, message_id, canonical_json(receipt)
                )
                await self._outbox.mark_sent(
                    action_id,
                    owner_id=self.claim_owner,
                    claim_epoch=claimed.claim_epoch,
                    provider_receipt_id=receipt_digest,
                )
            else:
                await self._outbox.mark_retryable(
                    action_id,
                    owner_id=self.claim_owner,
                    claim_epoch=claimed.claim_epoch,
                    error_type=error_type or "UnknownSendError",
                )
        except Exception:
            return False
        return True

    # ──────────────────────────────────────────
    # Heartbeat operations (spec 10)
    # ──────────────────────────────────────────

    def _heartbeat_operation_id(
        self,
        consciousness_instance_id: str,
        sequence: int,
    ) -> str:
        return f"hb-{_short_digest(consciousness_instance_id, int(sequence), length=40)}"

    async def register_heartbeat_operation(
        self,
        *,
        consciousness_instance_id: str,
        sequence: int,
        input_frontier: dict[str, Any],
        prepared_context_digest: str = "",
    ) -> HeartbeatOperation | None:
        """Register a pending heartbeat operation (idempotent)."""
        if not self.enabled or self._heartbeat is None:
            return None
        operation_id = self._heartbeat_operation_id(
            consciousness_instance_id,
            sequence,
        )
        try:
            return await self._heartbeat.register(
                HeartbeatOperation(
                    heartbeat_operation_id=operation_id,
                    consciousness_instance_id=str(consciousness_instance_id),
                    sequence=int(sequence),
                    input_frontier=dict(input_frontier or {}),
                    prepared_context_digest=str(prepared_context_digest or "") or None,
                    status=HeartbeatStatus.PENDING,
                    claim_owner=None,
                    claim_epoch=0,
                    lease_until=None,
                    model_request_id=None,
                    result_ref=None,
                    result_digest=None,
                    committed_frontier=None,
                    attempts=0,
                    created_at="",
                    updated_at="",
                )
            )
        except Exception:
            return None

    async def claim_heartbeat_operation(
        self,
        *,
        consciousness_instance_id: str,
        sequence: int,
        lease_seconds: int = 120,
    ) -> HeartbeatOperation | None:
        """Claim one heartbeat operation; returns None when another node owns it."""
        if not self.enabled or self._heartbeat is None:
            return None
        operation_id = self._heartbeat_operation_id(
            consciousness_instance_id,
            sequence,
        )
        try:
            turn = await self._heartbeat.claim(
                operation_id,
                owner_id=self.claim_owner,
                lease_seconds=int(lease_seconds),
            )
        except Exception:
            return None
        if turn is None or turn.claim_owner != self.claim_owner:
            return None
        return turn

    async def commit_heartbeat_operation(
        self,
        *,
        consciousness_instance_id: str,
        sequence: int,
        claim_epoch: int,
        input_frontier: int,
        committed_frontier: int,
        result_ref: str,
        result_digest: str,
    ) -> HeartbeatOperation | None:
        """Commit one heartbeat checkpoint; None on fencing or conflict."""
        if not self.enabled or self._heartbeat is None:
            return None
        operation_id = self._heartbeat_operation_id(
            consciousness_instance_id,
            sequence,
        )
        try:
            return await self._heartbeat.commit(
                operation_id,
                owner_id=self.claim_owner,
                claim_epoch=int(claim_epoch),
                input_frontier=int(input_frontier),
                committed_frontier=int(committed_frontier),
                result_ref=str(result_ref),
                result_digest=str(result_digest),
            )
        except Exception:
            return None

    async def mark_heartbeat_operation_failed(
        self,
        *,
        consciousness_instance_id: str,
        sequence: int,
        claim_epoch: int,
        retryable: bool = True,
    ) -> HeartbeatOperation | None:
        """Release a heartbeat claim as retryable/failed; None on fencing."""
        if not self.enabled or self._heartbeat is None:
            return None
        operation_id = self._heartbeat_operation_id(
            consciousness_instance_id,
            sequence,
        )
        try:
            return await self._heartbeat.mark_failed(
                operation_id,
                owner_id=self.claim_owner,
                claim_epoch=int(claim_epoch),
                retryable=bool(retryable),
            )
        except Exception:
            return None

    # ──────────────────────────────────────────
    # Projection progress (spec 12.3)
    # ──────────────────────────────────────────

    async def advance_projection(
        self,
        *,
        projection_name: str,
        expected_frontier: int,
        next_frontier: int,
        source_digest: str,
        config_digest: str,
        backlog: int = 0,
    ) -> ProjectionProgress | None:
        """Advance this node's projection frontier continuously."""
        if not self.enabled or self._projection is None:
            return None
        try:
            return await self._projection.advance(
                projection_name=str(projection_name),
                projection_node_id=self.node_id,
                expected_frontier=int(expected_frontier),
                next_frontier=int(next_frontier),
                source_digest=str(source_digest or ""),
                config_digest=str(config_digest or ""),
                backlog=max(0, int(backlog)),
            )
        except Exception:
            return None


__all__ = ["MultiWriterHotPathBridge"]
