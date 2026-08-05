"""Bounded, receipt-gated delivery of presence and subjective World state.

Preparation never advances a perception cursor. A caller may commit only the
exact projection proven present in the final successful model attempt.
Oversized durable values stay queryable through stable references and UTF-8
chunks instead of being expanded into prompts.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .consciousness import ConsciousnessRegistry
from .event_bus import RawEventStore
from .world_projection import (
    PerceptionCursorConflict,
    PromptProjectionValue,
    WorldAssertionReference,
    WorldAssertionReferencePage,
    WorldChangeReference,
    WorldChangeReferencePage,
    WorldProjectionStore,
)

PERCEPTION_PROJECTION_ALGORITHM = "world-perception-page-v2"
DEFAULT_PERCEPTION_MAX_BYTES = 32 * 1024
MIN_PERCEPTION_MAX_BYTES = 4 * 1024
_REFERENCE_INLINE_BYTES = 512
_REFERENCE_PAGE_SIZE = 256
_WRAPPER_RESERVE_BYTES = 1024


class PerceptionDeliveryUnverified(RuntimeError):
    """Raised when a caller cannot prove exact provider delivery."""


@dataclass(frozen=True, slots=True)
class PerceptionDeliveryReceipt:
    """Content-free proof for the projection seen by a successful model."""

    delivery_id: str
    projection_sha256: str
    delivered_bytes: int
    exact: bool
    transport_request_id: str = ""


@dataclass(frozen=True, slots=True)
class PerceptionCommitCheckpoint:
    """Content-free durable identity sufficient for cursor CAS replay."""

    instance_id: str
    from_position: int
    through_position: int
    cursor_revision: int
    delivery_id: str
    projection_sha256: str
    delivered_bytes: int

    def __post_init__(self) -> None:
        if not self.instance_id.strip():
            raise ValueError("perception checkpoint instance_id must not be empty")
        if self.from_position < 0 or self.through_position < self.from_position:
            raise ValueError("perception checkpoint cursor window is invalid")
        if self.cursor_revision < 0:
            raise ValueError("perception checkpoint revision must not be negative")
        if not self.delivery_id.strip():
            raise ValueError("perception checkpoint delivery_id must not be empty")
        digest = self.projection_sha256.strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("perception checkpoint sha256 must be 64 hex characters")
        if self.delivered_bytes <= 0:
            raise ValueError("perception checkpoint delivered_bytes must be positive")


@dataclass(frozen=True, slots=True)
class PreparedPerception:
    """One bounded World snapshot and cursor window prepared for an instance."""

    instance_id: str
    projection_kind: str
    from_position: int
    through_position: int
    source_frontier: int
    cursor_revision: int
    content: str
    assertion_ids: tuple[str, ...]
    change_positions: tuple[int, ...]
    delivery_id: str
    projection_sha256: str
    algorithm_version: str
    delivered_bytes: int
    source_payload_bytes: int
    omitted_assertion_count: int
    omitted_change_count: int
    omitted_source_bytes: int
    snapshot_continuation_token: str
    has_more_changes: bool

    @property
    def delivery_marker(self) -> str:
        """Return the marker registered with the effective-context receipt."""

        return f"world-perception:{self.delivery_id}"

    def as_prompt_projection(self) -> PromptProjectionValue:
        """Return the typed transport-only value rejected by durable writes."""

        return PromptProjectionValue(
            delivery_id=self.delivery_id,
            projection_sha256=self.projection_sha256,
            content=self.content,
        )

    def commit_checkpoint(self) -> PerceptionCommitCheckpoint:
        """Return the content-free durable replay form of this delivery."""

        return PerceptionCommitCheckpoint(
            instance_id=self.instance_id,
            from_position=self.from_position,
            through_position=self.through_position,
            cursor_revision=self.cursor_revision,
            delivery_id=self.delivery_id,
            projection_sha256=self.projection_sha256,
            delivered_bytes=self.delivered_bytes,
        )


class PerceptionGateway:
    """Coordinate projector catch-up and reliable transient perception."""

    def __init__(
        self,
        registry: ConsciousnessRegistry,
        ledger: RawEventStore,
        projection: WorldProjectionStore,
    ) -> None:
        self._registry = registry
        self._ledger = ledger
        self._projection = projection

    @property
    def projection(self) -> WorldProjectionStore:
        """Expose the store for diagnostics and explicit chunk queries."""

        return self._projection

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _utf8_size(value: str) -> int:
        return len(value.encode("utf-8"))

    @classmethod
    def _bounded_field(cls, value: Any, *, max_bytes: int = 192) -> str:
        """Render metadata with an explicit digest-backed reference tail."""

        rendered = str(value or "-")
        encoded = rendered.encode("utf-8")
        if len(encoded) <= max_bytes:
            return rendered
        digest = hashlib.sha256(encoded).hexdigest()
        suffix = f"…[bytes={len(encoded)};sha256={digest}]"
        end = max(0, max_bytes - cls._utf8_size(suffix))
        end = min(end, len(encoded))
        while end > 0 and encoded[end] & 0xC0 == 0x80:
            end -= 1
        return encoded[:end].decode("utf-8") + suffix

    @classmethod
    def _fit_lines(
        cls,
        lines: Iterable[str],
        *,
        max_bytes: int,
    ) -> tuple[list[str], int]:
        """Take complete UTF-8 lines inside one layer quota."""

        selected: list[str] = []
        used = 0
        for line in lines:
            addition = line if not selected else f"\n{line}"
            size = cls._utf8_size(addition)
            if used + size > max_bytes:
                break
            selected.append(line)
            used += size
        return selected, used

    def _presence_lines(self, instance_id: str) -> list[str]:
        active = sorted(
            self._registry.get_active(),
            key=lambda item: item.instance_id,
        )
        lines = [
            "### 同一主体当前存在的意识窗口",
            "这些窗口属于同一个你；它们是不同场景中的局部运行视角，不是其他人格。",
        ]
        for item in active:
            relation = "当前窗口" if item.instance_id == instance_id else "同时存在"
            lines.append(
                f"- {relation}: instance_id="
                f"{self._bounded_field(item.instance_id)}; kind="
                f"{self._bounded_field(item.kind)}; name="
                f"{self._bounded_field(item.display_name or item.instance_id)}; "
                f"streams={self._bounded_field(self._json(item.stream_ids))}; "
                f"last_active_at={self._bounded_field(item.last_active_at)}; "
                f"presence_revision={int(item.revision)}"
            )
        if not active:
            lines.append("- 当前 Presence Registry 没有 active 窗口。")
        return lines

    def _assertion_line(self, item: WorldAssertionReference) -> str:
        value_ref = f"assertion:{item.assertion_id}"
        if item.transport_echo:
            value_text = (
                f"transport_echo=quarantined; value_ref={value_ref}; "
                f"value_bytes={item.value_bytes}"
            )
        elif item.value_inlined:
            value_text = f"value={self._json(item.value)}"
        else:
            value_text = f"value_ref={value_ref}; value_bytes={item.value_bytes}"
        return (
            f"- assertion_id={self._bounded_field(item.assertion_id)}; "
            f"domain={self._bounded_field(item.domain)}; "
            f"subject={self._bounded_field(item.subject)}; "
            f"predicate={self._bounded_field(item.predicate)}; {value_text}; "
            f"status={self._bounded_field(item.status)}; "
            f"source_instance_id={self._bounded_field(item.source_instance_id)}; "
            f"source_event_id={self._bounded_field(item.source_event_id)}; "
            f"occurrence_id={self._bounded_field(item.occurrence_id)}; "
            f"observed_at={self._bounded_field(item.observed_at)}; "
            f"supersedes={self._bounded_field(item.supersedes_assertion_id)}"
        )

    def _change_line(self, item: WorldChangeReference) -> str:
        payload_ref = f"change:{item.ingest_position}"
        if item.transport_echo:
            payload_text = (
                f"transport_echo=quarantined; payload_ref={payload_ref}; "
                f"payload_bytes={item.payload_bytes}"
            )
        elif item.payload_inlined:
            payload_text = f"payload={self._json(item.payload)}"
        else:
            payload_text = (
                f"payload_ref={payload_ref}; payload_bytes={item.payload_bytes}"
            )
        return (
            f"- ingest_position={item.ingest_position}; "
            f"change_kind={self._bounded_field(item.change_kind)}; "
            f"event_type={self._bounded_field(item.event_type)}; "
            f"event_id={self._bounded_field(item.event_id)}; "
            f"source_instance_id={self._bounded_field(item.source_instance_id)}; "
            f"stream_id={self._bounded_field(item.stream_id)}; "
            f"occurred_at={self._bounded_field(item.occurred_at)}; {payload_text}"
        )

    @classmethod
    def _continuation_token(
        cls,
        *,
        projection_kind: str,
        source_frontier: int,
        observed_at: str,
        assertion_id: str,
    ) -> str:
        if not observed_at and not assertion_id:
            return ""
        payload = cls._json(
            {
                "algorithm": PERCEPTION_PROJECTION_ALGORITHM,
                "projection_kind": projection_kind,
                "source_frontier": source_frontier,
                "after_observed_at": observed_at,
                "after_assertion_id": assertion_id,
            }
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def decode_snapshot_continuation_token(
        cls,
        token: str,
    ) -> dict[str, Any]:
        """Decode and validate one content-free assertion continuation token."""

        value = str(token or "").strip()
        if not value:
            raise ValueError("world snapshot continuation token must not be empty")
        padding = "=" * (-len(value) % 4)
        try:
            payload = json.loads(
                base64.urlsafe_b64decode(value + padding).decode("utf-8")
            )
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("world snapshot continuation token is invalid") from exc
        if not isinstance(payload, dict):
            raise TypeError("world snapshot continuation payload must be an object")
        if payload.get("algorithm") != PERCEPTION_PROJECTION_ALGORITHM:
            raise ValueError("world snapshot continuation algorithm is unsupported")
        if not str(payload.get("projection_kind") or "").strip():
            raise ValueError("world snapshot continuation projection_kind is missing")
        if int(payload.get("source_frontier") or -1) < 0:
            raise ValueError("world snapshot continuation frontier is invalid")
        if not str(payload.get("after_assertion_id") or "").strip():
            raise ValueError("world snapshot continuation assertion cursor is missing")
        return payload

    def _build_prepared(
        self,
        *,
        identity: str,
        projection_kind: str,
        max_bytes: int,
        from_position: int,
        cursor_revision: int,
        source_frontier: int,
        assertion_page: WorldAssertionReferencePage,
        change_page: WorldChangeReferencePage,
    ) -> PreparedPerception:
        """Project compact reference pages into independently bounded layers."""

        body_budget = max_bytes - _WRAPPER_RESERVE_BYTES
        presence_budget = min(4 * 1024, max(512, body_budget // 6))
        assertion_budget = min(12 * 1024, max(512, body_budget * 3 // 8))
        change_budget = max(
            512,
            body_budget - presence_budget - assertion_budget - 512,
        )
        presence_lines, _ = self._fit_lines(
            self._presence_lines(identity),
            max_bytes=presence_budget,
        )

        assertion_header = [
            "### 潜意识世界投影（有界当前快照）",
            "矛盾记录保持并存；大型值与历史传输回声只提供稳定引用，可按需分页读取。",
        ]
        assertion_records = [
            self._assertion_line(item) for item in assertion_page.items
        ]
        assertion_lines, assertion_used = self._fit_lines(
            assertion_header,
            max_bytes=assertion_budget,
        )
        fitted_assertions, _ = self._fit_lines(
            assertion_records,
            max_bytes=max(0, assertion_budget - assertion_used - 1),
        )
        assertion_lines.extend(fitted_assertions)
        delivered_assertions = assertion_page.items[: len(fitted_assertions)]

        change_header = ["### 自上次成功感知以来的有界变化"]
        change_records = [self._change_line(item) for item in change_page.items]
        change_lines, change_used = self._fit_lines(
            change_header,
            max_bytes=change_budget,
        )
        fitted_changes, _ = self._fit_lines(
            change_records,
            max_bytes=max(0, change_budget - change_used - 1),
        )
        change_lines.extend(fitted_changes)
        delivered_changes = change_page.items[: len(fitted_changes)]

        omitted_assertions = max(
            0,
            assertion_page.total_items - len(delivered_assertions),
        )
        omitted_changes = max(
            0,
            change_page.total_items - len(delivered_changes),
        )
        if omitted_assertions:
            assertion_lines.append(
                f"- snapshot_continuation: omitted_assertions={omitted_assertions}"
            )
        if omitted_changes:
            change_lines.append(
                f"- change_continuation: omitted_changes={omitted_changes}"
            )

        body = "\n".join(
            [
                *presence_lines,
                "",
                *assertion_lines,
                "",
                *change_lines,
                "",
                "这是只属于本轮的可替换运行态感知；不得原样写入长期历史或 World assertion。",
            ]
        )
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        identity_seed = (
            f"{PERCEPTION_PROJECTION_ALGORITHM}:{projection_kind}:{identity}:"
            f"{from_position}:{cursor_revision}:{source_frontier}:{body_sha256}"
        )
        delivery_id = hashlib.sha256(identity_seed.encode("utf-8")).hexdigest()[:32]
        marker = f"world-perception:{delivery_id}"
        content = (
            f'<world_perception_delivery marker="{marker}" '
            f'algorithm="{PERCEPTION_PROJECTION_ALGORITHM}">\n'
            f"{body}\n"
            "</world_perception_delivery>"
        )
        delivered_bytes = self._utf8_size(content)
        if delivered_bytes > max_bytes:
            raise RuntimeError(
                "bounded World projection exceeded its hard byte budget: "
                f"delivered={delivered_bytes}, max={max_bytes}"
            )

        if change_page.total_items == len(delivered_changes):
            through_position = source_frontier
        elif delivered_changes:
            through_position = delivered_changes[-1].ingest_position
        else:
            through_position = from_position
        delivered_source_bytes = sum(
            item.value_bytes for item in delivered_assertions if item.value_inlined
        ) + sum(
            item.payload_bytes for item in delivered_changes if item.payload_inlined
        )
        source_payload_bytes = (
            assertion_page.total_value_bytes + change_page.total_payload_bytes
        )
        last_assertion = delivered_assertions[-1] if delivered_assertions else None
        continuation = ""
        if omitted_assertions:
            continuation = self._continuation_token(
                projection_kind=projection_kind,
                source_frontier=source_frontier,
                observed_at=last_assertion.observed_at if last_assertion else "",
                assertion_id=last_assertion.assertion_id if last_assertion else "",
            )
        return PreparedPerception(
            instance_id=identity,
            projection_kind=projection_kind,
            from_position=from_position,
            through_position=through_position,
            source_frontier=source_frontier,
            cursor_revision=cursor_revision,
            content=content,
            assertion_ids=tuple(item.assertion_id for item in delivered_assertions),
            change_positions=tuple(item.ingest_position for item in delivered_changes),
            delivery_id=delivery_id,
            projection_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            algorithm_version=PERCEPTION_PROJECTION_ALGORITHM,
            delivered_bytes=delivered_bytes,
            source_payload_bytes=source_payload_bytes,
            omitted_assertion_count=omitted_assertions,
            omitted_change_count=omitted_changes,
            omitted_source_bytes=max(
                0,
                source_payload_bytes - delivered_source_bytes,
            ),
            snapshot_continuation_token=continuation,
            has_more_changes=through_position < source_frontier,
        )

    @staticmethod
    def _validate_receipt(
        prepared: PreparedPerception | PerceptionCommitCheckpoint,
        receipt: PerceptionDeliveryReceipt | None,
    ) -> None:
        if receipt is None:
            raise PerceptionDeliveryUnverified(
                "perception cursor requires an exact delivery receipt"
            )
        if not receipt.exact:
            raise PerceptionDeliveryUnverified(
                "effective model context did not contain the exact World projection"
            )
        if (
            receipt.delivery_id != prepared.delivery_id
            or receipt.projection_sha256 != prepared.projection_sha256
            or int(receipt.delivered_bytes) != prepared.delivered_bytes
        ):
            raise PerceptionDeliveryUnverified(
                "perception delivery receipt does not match the prepared projection"
            )

    @staticmethod
    def _normalize_prepare_options(
        instance_id: str,
        projection_kind: str,
        max_bytes: int,
    ) -> tuple[str, str, int]:
        identity = str(instance_id or "").strip()
        kind = str(projection_kind or "").strip()
        budget = int(max_bytes)
        if not identity:
            raise ValueError("perception instance_id must not be empty")
        if not kind:
            raise ValueError("perception projection_kind must not be empty")
        if budget < MIN_PERCEPTION_MAX_BYTES:
            raise ValueError(
                f"perception max_bytes must be at least {MIN_PERCEPTION_MAX_BYTES}"
            )
        return identity, kind, budget

    def prepare(
        self,
        instance_id: str,
        *,
        projection_kind: str = "default",
        max_bytes: int = DEFAULT_PERCEPTION_MAX_BYTES,
    ) -> PreparedPerception:
        """Prepare one bounded snapshot without advancing the cursor."""

        identity, kind, budget = self._normalize_prepare_options(
            instance_id,
            projection_kind,
            max_bytes,
        )
        self._projection.ensure_deliverable()
        source_frontier = self._projection.catch_up(self._ledger)
        self._projection.ensure_deliverable()
        from_position, revision = self._projection.perception_cursor(identity)
        assertion_page = self._projection.list_assertion_references_page(
            include_retracted=True,
            limit=_REFERENCE_PAGE_SIZE,
            inline_max_bytes=_REFERENCE_INLINE_BYTES,
        )
        change_page = self._projection.change_references_page(
            from_position,
            through_position=source_frontier,
            limit=_REFERENCE_PAGE_SIZE,
            inline_max_bytes=_REFERENCE_INLINE_BYTES,
        )
        return self._build_prepared(
            identity=identity,
            projection_kind=kind,
            max_bytes=budget,
            from_position=from_position,
            cursor_revision=revision,
            source_frontier=source_frontier,
            assertion_page=assertion_page,
            change_page=change_page,
        )

    def commit(
        self,
        prepared: PreparedPerception,
        receipt: PerceptionDeliveryReceipt | None = None,
    ) -> tuple[int, int]:
        """CAS-advance only the projection proven present upstream."""

        return self.commit_delivery(prepared.commit_checkpoint(), receipt)

    def commit_delivery(
        self,
        checkpoint: PerceptionCommitCheckpoint,
        receipt: PerceptionDeliveryReceipt | None = None,
    ) -> tuple[int, int]:
        """CAS-advance from a content-free durable delivery checkpoint."""

        self._validate_receipt(checkpoint, receipt)
        try:
            return self._projection.commit_perception_cursor(
                checkpoint.instance_id,
                expected_position=checkpoint.from_position,
                expected_revision=checkpoint.cursor_revision,
                through_position=checkpoint.through_position,
            )
        except PerceptionCursorConflict:
            current = self._projection.perception_cursor(checkpoint.instance_id)
            replayed_revision = checkpoint.cursor_revision + int(
                checkpoint.through_position > checkpoint.from_position
            )
            if current == (checkpoint.through_position, replayed_revision):
                return current
            raise

    def query(
        self,
        instance_id: str,
        query: str,
        *,
        max_bytes: int = DEFAULT_PERCEPTION_MAX_BYTES,
    ) -> str:
        """Return a bounded attributable projection without cursor mutation."""

        question = str(query or "").strip()
        if not question:
            raise ValueError("world query must not be empty")
        prepared = self.prepare(
            instance_id,
            projection_kind="inner_query",
            max_bytes=max_bytes,
        )
        return (
            f"当前意识窗口提出的内在查询：{question}\n\n"
            f"{prepared.content}\n\n"
            "请结合来源、时间、矛盾记录和稳定引用自行判断；查询本身不会改写投影。"
        )


class AsyncPerceptionGateway(PerceptionGateway):
    """Async selected-backend perception with gap-intolerant replay."""

    def __init__(self, registry: Any, ledger: Any, projection: Any) -> None:
        self._registry = registry
        self._ledger = ledger
        self._projection = projection

    @property
    def projection(self) -> Any:
        """Expose the selected async World Port for chunk queries."""

        return self._projection

    @staticmethod
    def _ensure_deliverable(contract: dict[str, Any]) -> None:
        state = str(contract.get("rebuild_state") or "")
        if state != "idle":
            from .world_projection import WorldProjectionUnavailable

            raise WorldProjectionUnavailable(
                "world projection is not deliverable: "
                f"rebuild_state={state or '<missing>'}"
            )

    async def catch_up(
        self,
        *,
        batch_size: int = 500,
        max_batches: int = 1000,
    ) -> int:
        """Replay a bounded ledger prefix without skipping positions."""

        if batch_size <= 0 or max_batches <= 0:
            raise ValueError("world catch-up limits must be positive")
        contract = await self._projection.projector_contract()
        self._ensure_deliverable(contract)
        position = int(contract.get("as_of_ingest_position") or 0)
        for _ in range(max_batches):
            batch = await self._ledger.read_since(position, limit=batch_size)
            if not batch:
                return position
            position = await self._projection.apply_events(batch)
        raise RuntimeError(
            "WorldProjectionCatchUpLimit: ledger backlog exceeded the bounded "
            f"replay window after position {position}"
        )

    async def rebuild(
        self,
        *,
        batch_size: int = 500,
        max_batches: int = 1000,
    ) -> int:
        """Rebuild derived World rows while preserving cursors."""

        if batch_size <= 0 or max_batches <= 0:
            raise ValueError("world rebuild limits must be positive")
        await self._projection.begin_rebuild()
        position = 0
        try:
            for _ in range(max_batches):
                batch = await self._ledger.read_since(position, limit=batch_size)
                if not batch:
                    await self._projection.finish_rebuild(expected_frontier=position)
                    return position
                position = await self._projection.apply_events(batch)
            raise RuntimeError(
                "WorldProjectionRebuildLimit: ledger exceeded the bounded replay "
                f"window after position {position}"
            )
        except BaseException as primary:
            try:
                await self._projection.fail_rebuild()
            except Exception as state_error:  # noqa: BLE001
                primary.add_note(
                    "world rebuild state could not be marked failed: "
                    f"{type(state_error).__name__}"
                )
            raise

    async def prepare(
        self,
        instance_id: str,
        *,
        projection_kind: str = "default",
        max_bytes: int = DEFAULT_PERCEPTION_MAX_BYTES,
    ) -> PreparedPerception:
        """Prepare one bounded selected-backend snapshot."""

        identity, kind, budget = self._normalize_prepare_options(
            instance_id,
            projection_kind,
            max_bytes,
        )
        await self._registry.refresh()
        source_frontier = await self.catch_up()
        contract = await self._projection.projector_contract()
        self._ensure_deliverable(contract)
        from_position, revision = await self._projection.perception_cursor(identity)
        assertion_page = await self._projection.list_assertion_references_page(
            include_retracted=True,
            limit=_REFERENCE_PAGE_SIZE,
            inline_max_bytes=_REFERENCE_INLINE_BYTES,
        )
        change_page = await self._projection.change_references_page(
            from_position,
            through_position=source_frontier,
            limit=_REFERENCE_PAGE_SIZE,
            inline_max_bytes=_REFERENCE_INLINE_BYTES,
        )
        return self._build_prepared(
            identity=identity,
            projection_kind=kind,
            max_bytes=budget,
            from_position=from_position,
            cursor_revision=revision,
            source_frontier=source_frontier,
            assertion_page=assertion_page,
            change_page=change_page,
        )

    async def commit(
        self,
        prepared: PreparedPerception,
        receipt: PerceptionDeliveryReceipt | None = None,
    ) -> tuple[int, int]:
        """CAS-advance only one exactly delivered async snapshot."""

        return await self.commit_delivery(prepared.commit_checkpoint(), receipt)

    async def commit_delivery(
        self,
        checkpoint: PerceptionCommitCheckpoint,
        receipt: PerceptionDeliveryReceipt | None = None,
    ) -> tuple[int, int]:
        """CAS-advance from a content-free durable delivery checkpoint."""

        self._validate_receipt(checkpoint, receipt)
        try:
            return await self._projection.commit_perception_cursor(
                checkpoint.instance_id,
                expected_position=checkpoint.from_position,
                expected_revision=checkpoint.cursor_revision,
                through_position=checkpoint.through_position,
            )
        except PerceptionCursorConflict:
            current = await self._projection.perception_cursor(checkpoint.instance_id)
            replayed_revision = checkpoint.cursor_revision + int(
                checkpoint.through_position > checkpoint.from_position
            )
            if current == (checkpoint.through_position, replayed_revision):
                return current
            raise

    async def query(
        self,
        instance_id: str,
        query: str,
        *,
        max_bytes: int = DEFAULT_PERCEPTION_MAX_BYTES,
    ) -> str:
        """Return a bounded attributable projection without cursor mutation."""

        question = str(query or "").strip()
        if not question:
            raise ValueError("world query must not be empty")
        prepared = await self.prepare(
            instance_id,
            projection_kind="inner_query",
            max_bytes=max_bytes,
        )
        return (
            f"当前意识窗口提出的内在查询：{question}\n\n"
            f"{prepared.content}\n\n"
            "请结合来源、时间、矛盾记录和稳定引用自行判断；查询本身不会改写投影。"
        )


__all__ = [
    "DEFAULT_PERCEPTION_MAX_BYTES",
    "PERCEPTION_PROJECTION_ALGORITHM",
    "AsyncPerceptionGateway",
    "PerceptionCommitCheckpoint",
    "PerceptionDeliveryReceipt",
    "PerceptionDeliveryUnverified",
    "PerceptionGateway",
    "PreparedPerception",
]
