"""Traceable epistemic opportunities for the Life Engine.

The historical CuriosityEngine treated a background model's output as if it were
the subject's own curiosity. The compatibility name remains public, but the
authoritative object in this module is now an :class:`EpistemicOpportunity`: an
external, sourced candidate which cannot speak for the subject or mutate an
AttentionThread.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import json_repair

from src.app.plugin_system.api.llm_api import create_llm_request, get_model_set_by_task
from src.kernel.llm import ROLE, LLMPayload, Text

EPISTEMIC_OPPORTUNITY_SCHEMA_VERSION = 1
EPISTEMIC_OPPORTUNITY_ALGORITHM_VERSION = "epistemic-opportunity-v1"
MAX_OPPORTUNITY_FIELD_BYTES = 4 * 1024
MAX_OPPORTUNITY_PAYLOAD_BYTES = 16 * 1024
_REMOTE_EVENT_NAMESPACE = "life_epistemic.opportunities"
_REMOTE_PROJECTION_NAMESPACE = "life_epistemic.projection"
_REMOTE_PROJECTION_KEY = "current"

EPISTEMIC_OPPORTUNITY_GUIDE = """## 认知机会生成器
- 你是系统侧的候选生成器，不是爱莉、不是她的内心，也不代表她正在好奇。
- 你的职责只是从有来源的经历中提出一个仍可检查的开放问题候选。
- 不判断什么对主体“值得”“重要”或“有意义”，不推断人格偏好，不替主体形成意图。
- 不给候选打分、分级、分类或排序；不因为重复、时间、来源或相似度赋予优先级。
- 候选可以指出观察到的缺口、开放问题和一种可选的继续观察方式，但不能命令行动。
- 只有活跃意识实例之后明确注意、追问、改写或通过 AttentionThread 接住它，才构成主体动作。
- 没有清楚的开放问题时返回 candidate_present=false；这不会否定或关闭以前的候选。"""


class EpistemicOpportunityStateError(RuntimeError):
    """Raised when persisted opportunity state cannot be read without guessing."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(
            f"epistemic opportunity state invalid: path={path}, reason={reason}"
        )


@dataclass(frozen=True, slots=True)
class EpistemicOpportunity:
    """An immutable, sourced candidate offered to an active consciousness.

    This is infrastructure evidence, not a statement of the subject's interest,
    importance judgement, belief, intention, or AttentionThread state.
    """

    opportunity_id: str
    source_occurrence_id: str
    source_stream_id: str
    source_instance_id: str
    generated_at: str
    algorithm_version: str
    observed_gap: str
    open_question: str
    possible_next_look: str
    generator_note: str
    provenance: str = "generated_candidate"
    legacy_source_sha256: str = ""
    payload_sha256: str = ""
    schema_version: int = EPISTEMIC_OPPORTUNITY_SCHEMA_VERSION
    kind: str = "epistemic_opportunity"

    @classmethod
    def build(
        cls,
        *,
        source_occurrence_id: str,
        source_stream_id: str = "",
        source_instance_id: str = "",
        observed_gap: str = "",
        open_question: str = "",
        possible_next_look: str = "",
        generator_note: str = "",
        generated_at: str = "",
        algorithm_version: str = EPISTEMIC_OPPORTUNITY_ALGORITHM_VERSION,
        provenance: str = "generated_candidate",
        legacy_source_sha256: str = "",
    ) -> EpistemicOpportunity:
        semantic = {
            "source_occurrence_id": _clean_string(
                "source_occurrence_id", source_occurrence_id
            ),
            "source_stream_id": _clean_string("source_stream_id", source_stream_id),
            "source_instance_id": _clean_string(
                "source_instance_id", source_instance_id
            ),
            "algorithm_version": _clean_string("algorithm_version", algorithm_version)
            or EPISTEMIC_OPPORTUNITY_ALGORITHM_VERSION,
            "observed_gap": _clean_string("observed_gap", observed_gap),
            "open_question": _clean_string("open_question", open_question),
            "possible_next_look": _clean_string(
                "possible_next_look", possible_next_look
            ),
            "generator_note": _clean_string("generator_note", generator_note),
            "provenance": _clean_string("provenance", provenance)
            or "generated_candidate",
            "legacy_source_sha256": _clean_string(
                "legacy_source_sha256", legacy_source_sha256
            ),
        }
        _validate_candidate_fields(semantic)
        if not (semantic["observed_gap"] or semantic["open_question"]):
            raise ValueError(
                "epistemic opportunity needs an observed gap or open question"
            )

        identity_bytes = _canonical_json_bytes(semantic)
        opportunity_id = f"eop_{hashlib.sha256(identity_bytes).hexdigest()[:32]}"
        payload = {
            "opportunity_id": opportunity_id,
            **semantic,
            "generated_at": _clean_string("generated_at", generated_at) or _now_iso(),
            "schema_version": EPISTEMIC_OPPORTUNITY_SCHEMA_VERSION,
            "kind": "epistemic_opportunity",
        }
        payload_bytes = _canonical_json_bytes(payload)
        if len(payload_bytes) > MAX_OPPORTUNITY_PAYLOAD_BYTES:
            raise ValueError(
                "epistemic opportunity exceeds hard UTF-8 byte budget: "
                f"bytes={len(payload_bytes)}, max_bytes={MAX_OPPORTUNITY_PAYLOAD_BYTES}"
            )
        return cls(**payload, payload_sha256=hashlib.sha256(payload_bytes).hexdigest())

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> EpistemicOpportunity:
        if raw.get("kind") != "epistemic_opportunity":
            raise ValueError("kind must be epistemic_opportunity")
        if raw.get("schema_version") != EPISTEMIC_OPPORTUNITY_SCHEMA_VERSION:
            raise ValueError("unsupported epistemic opportunity schema_version")
        required_strings = (
            "opportunity_id",
            "source_occurrence_id",
            "source_stream_id",
            "source_instance_id",
            "generated_at",
            "algorithm_version",
            "observed_gap",
            "open_question",
            "possible_next_look",
            "generator_note",
            "provenance",
            "legacy_source_sha256",
            "payload_sha256",
        )
        if any(not isinstance(raw.get(name), str) for name in required_strings):
            raise ValueError(
                "epistemic opportunity fields must use the canonical string schema"
            )
        rebuilt = cls.build(
            source_occurrence_id=raw["source_occurrence_id"],
            source_stream_id=raw["source_stream_id"],
            source_instance_id=raw["source_instance_id"],
            generated_at=raw["generated_at"],
            algorithm_version=raw["algorithm_version"],
            observed_gap=raw["observed_gap"],
            open_question=raw["open_question"],
            possible_next_look=raw["possible_next_look"],
            generator_note=raw["generator_note"],
            provenance=raw["provenance"],
            legacy_source_sha256=raw["legacy_source_sha256"],
        )
        if rebuilt.opportunity_id != raw["opportunity_id"]:
            raise ValueError(
                "opportunity_id does not match canonical candidate identity"
            )
        if rebuilt.payload_sha256 != raw["payload_sha256"]:
            raise ValueError(
                "payload_sha256 does not match canonical candidate payload"
            )
        return rebuilt

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def active(self) -> bool:
        """Legacy compatibility: a materialized candidate is present."""

        return True

    @property
    def anchor(self) -> str:
        return self.observed_gap

    @property
    def why(self) -> str:
        return self.generator_note

    @property
    def unknown(self) -> str:
        return self.open_question

    @property
    def approach(self) -> str:
        return self.possible_next_look

    @property
    def text(self) -> str:
        return format_epistemic_opportunity(self)


@dataclass(slots=True)
class CuriositySignal:
    """Deprecated adapter for historical callers and ``curiosity_state.json``."""

    active: bool = False
    anchor: str = ""
    why: str = ""
    unknown: str = ""
    approach: str = ""
    updated_at: str = ""
    source_event_id: str = ""
    source_stream_id: str = ""
    confidence: float = 0.0
    tags: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls) -> CuriositySignal:
        return cls(active=False, updated_at=_now_iso())

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> CuriositySignal:
        _validate_legacy_signal_mapping(data)
        tags_raw = data.get("tags", [])
        return cls(
            active=data.get("active", False),
            anchor=str(data.get("anchor", "") or "").strip(),
            why=str(data.get("why", "") or "").strip(),
            unknown=str(data.get("unknown", "") or "").strip(),
            approach=str(data.get("approach", "") or "").strip(),
            updated_at=str(data.get("updated_at", "") or "").strip() or _now_iso(),
            source_event_id=str(data.get("source_event_id", "") or "").strip(),
            source_stream_id=str(data.get("source_stream_id", "") or "").strip(),
            confidence=float(data.get("confidence", 0.0) or 0.0),
            tags=[str(item).strip() for item in tags_raw if str(item).strip()][:6],
        )

    @classmethod
    def from_opportunity(
        cls, opportunity: EpistemicOpportunity | None
    ) -> CuriositySignal:
        if opportunity is None:
            return cls.empty()
        return cls(
            active=True,
            anchor=opportunity.observed_gap,
            why=opportunity.generator_note,
            unknown=opportunity.open_question,
            approach=opportunity.possible_next_look,
            updated_at=opportunity.generated_at,
            source_event_id=opportunity.source_occurrence_id,
            source_stream_id=opportunity.source_stream_id,
            confidence=0.0,
            tags=[],
        )

    def normalized(self) -> CuriositySignal:
        if not self.active or not (self.anchor or self.why or self.unknown):
            return CuriositySignal.empty()
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def text(self) -> str:
        return format_curiosity_signal(self)


class CuriosityEngine:
    """Deprecated facade over the epistemic-opportunity generator and ledger."""

    def __init__(
        self,
        *,
        workspace_path: str,
        model_task_name: str = "life",
        timeout_seconds: float = 30.0,
        runtime_store: Any | None = None,
    ) -> None:
        self.workspace_path = str(workspace_path or "")
        self.model_task_name = str(model_task_name or "life")
        self.timeout_seconds = max(3.0, float(timeout_seconds or 30.0))
        self.runtime_store = runtime_store
        self._state_revision = 0
        self._remote_opportunity_cache: dict[str, EpistemicOpportunity] = {}
        self._lock = asyncio.Lock()
        self._opportunity_index: dict[str, tuple[int, int, str]] | None = None

    @property
    def storage_dir(self) -> Path:
        return Path(self.workspace_path).resolve() / ".life_epistemic"

    @property
    def ledger_path(self) -> Path:
        return self.storage_dir / "opportunities.jsonl"

    @property
    def state_path(self) -> Path:
        """Current content-free projection path (legacy public property)."""

        return self.storage_dir / "current.json"

    @property
    def legacy_state_path(self) -> Path:
        return Path(self.workspace_path).resolve() / "curiosity_state.json"

    async def load_opportunity(self) -> EpistemicOpportunity | None:
        async with self._lock:
            if self.runtime_store is not None:
                return await self._load_remote_opportunity_unlocked()
            return self._load_opportunity_unlocked()

    async def save_opportunity(self, opportunity: EpistemicOpportunity) -> None:
        async with self._lock:
            if self.runtime_store is not None:
                await self._append_remote_opportunity_unlocked(opportunity)
                await self._write_remote_projection_unlocked(
                    opportunity.opportunity_id,
                    reason_code="candidate_available",
                )
                return
            self._append_opportunity_unlocked(opportunity)
            self._write_projection_unlocked(opportunity.opportunity_id)

    async def load_signal(self) -> CuriositySignal:
        """Read through the legacy API without reintroducing subjective semantics."""

        return CuriositySignal.from_opportunity(await self.load_opportunity())

    async def save_signal(self, signal: CuriositySignal) -> None:
        """Persist a historical signal as a sourced legacy candidate."""

        normalized = signal.normalized()
        if not normalized.active:
            await self.clear()
            return
        opportunity = EpistemicOpportunity.build(
            source_occurrence_id=normalized.source_event_id,
            source_stream_id=normalized.source_stream_id,
            observed_gap=normalized.anchor,
            open_question=normalized.unknown,
            possible_next_look=normalized.approach,
            generator_note=normalized.why,
            generated_at=normalized.updated_at,
            algorithm_version="legacy-curiosity-adapter-v1",
            provenance="legacy_api_adapter",
        )
        await self.save_opportunity(opportunity)

    async def clear(self) -> None:
        """Retire only the transport projection; immutable candidates remain."""

        async with self._lock:
            if self.runtime_store is not None:
                await self._write_remote_projection_unlocked(
                    None,
                    reason_code="legacy_adapter_clear",
                )
                return
            self._write_projection_unlocked(None, reason_code="legacy_adapter_clear")

    async def review(
        self,
        *,
        prefix_prompt: str,
        history_text: str,
        new_event_text: str,
        source_event_id: str = "",
        source_stream_id: str = "",
        source_instance_id: str = "",
    ) -> CuriositySignal:
        """Compatibility wrapper returning the historical view type."""

        opportunity = await self.review_opportunity(
            prefix_prompt=prefix_prompt,
            history_text=history_text,
            new_event_text=new_event_text,
            source_occurrence_id=source_event_id,
            source_stream_id=source_stream_id,
            source_instance_id=source_instance_id,
        )
        return CuriositySignal.from_opportunity(opportunity)

    async def review_opportunity(
        self,
        *,
        prefix_prompt: str,
        history_text: str,
        new_event_text: str,
        source_occurrence_id: str = "",
        source_stream_id: str = "",
        source_instance_id: str = "",
    ) -> EpistemicOpportunity | None:
        """Generate one candidate without deciding whether the subject values it."""

        previous = await self.load_opportunity()
        request = create_llm_request(
            get_model_set_by_task(self.model_task_name),
            request_name="life_epistemic_opportunity",
        )
        request.add_payload(
            LLMPayload(ROLE.SYSTEM, Text(self._build_system_prompt(prefix_prompt)))
        )
        request.add_payload(
            LLMPayload(
                ROLE.USER,
                Text(
                    self._build_user_prompt(
                        history_text=history_text,
                        new_event_text=new_event_text,
                        previous=previous,
                    )
                ),
            )
        )
        response = await asyncio.wait_for(
            request.send(auto_append_response=False, stream=False),
            timeout=self.timeout_seconds,
        )
        raw_text = await asyncio.wait_for(response, timeout=self.timeout_seconds)
        opportunity = self._parse_opportunity(
            raw_text,
            source_occurrence_id=source_occurrence_id,
            source_stream_id=source_stream_id,
            source_instance_id=source_instance_id,
        )
        if opportunity is not None:
            await self.save_opportunity(opportunity)
            return opportunity
        # A generator's lack of a new candidate cannot close an older candidate.
        return None

    async def format_for_prompt(self, *, max_chars: int = 1200) -> str:
        opportunity = await self.load_opportunity()
        text = format_epistemic_opportunity(opportunity)
        if max_chars <= 0 or len(text.encode("utf-8")) <= max_chars:
            return text
        return _fit_projection_to_utf8_budget(
            text,
            max_bytes=max_chars,
            opportunity_id=opportunity.opportunity_id if opportunity else "",
        )

    async def _load_remote_opportunity_unlocked(
        self,
    ) -> EpistemicOpportunity | None:
        record = await self.runtime_store.get_state(
            _REMOTE_PROJECTION_NAMESPACE,
            _REMOTE_PROJECTION_KEY,
        )
        if record is None:
            self._state_revision = 0
            return None
        self._state_revision = int(record.revision)
        projection = self._validate_remote_projection(record.payload, record.revision)
        opportunity_id = projection["current_opportunity_id"]
        event_position = projection["current_event_position"]
        if opportunity_id is None:
            return None
        found = await self._find_remote_opportunity_unlocked(
            opportunity_id,
            event_position=event_position,
        )
        if found is None:
            raise RuntimeError(
                "EpistemicOpportunityRemoteProjectionMissingEvent:"
                f"{opportunity_id}:{event_position}"
            )
        return found[0]

    async def _append_remote_opportunity_unlocked(
        self,
        opportunity: EpistemicOpportunity,
    ) -> int:
        cached = self._remote_opportunity_cache.get(opportunity.opportunity_id)
        if cached is not None:
            if _identity_fields(cached) != _identity_fields(opportunity):
                raise RuntimeError(
                    "EpistemicOpportunityRemoteIdentityConflict:"
                    f"{opportunity.opportunity_id}"
                )
            found = await self._find_remote_opportunity_unlocked(
                opportunity.opportunity_id
            )
            if found is not None:
                return found[1]

        try:
            record = await self.runtime_store.append_event(
                namespace=_REMOTE_EVENT_NAMESPACE,
                occurrence_id=opportunity.opportunity_id,
                event_kind="epistemic_opportunity.recorded",
                payload=opportunity.to_dict(),
                occurred_at=opportunity.generated_at,
            )
        except Exception:
            # The candidate identity intentionally excludes generated_at. A replay
            # may therefore carry different transport bytes while representing
            # the same immutable semantic candidate. Resolve that case from the
            # authoritative event stream; all other storage conflicts propagate.
            found = await self._find_remote_opportunity_unlocked(
                opportunity.opportunity_id
            )
            if found is None or _identity_fields(found[0]) != _identity_fields(
                opportunity
            ):
                raise
            return found[1]

        persisted = EpistemicOpportunity.from_mapping(record.payload)
        if _identity_fields(persisted) != _identity_fields(opportunity):
            raise RuntimeError(
                "EpistemicOpportunityRemoteIdentityConflict:"
                f"{opportunity.opportunity_id}"
            )
        self._remote_opportunity_cache[persisted.opportunity_id] = persisted
        return int(record.position)

    async def _find_remote_opportunity_unlocked(
        self,
        opportunity_id: str,
        *,
        event_position: int | None = None,
    ) -> tuple[EpistemicOpportunity, int] | None:
        if event_position is not None:
            records = await self.runtime_store.read_events(
                _REMOTE_EVENT_NAMESPACE,
                after_position=max(0, int(event_position) - 1),
                limit=1,
            )
            if not records or int(records[0].position) != int(event_position):
                return None
        else:
            records = []
            after_position = 0
            while True:
                page = await self.runtime_store.read_events(
                    _REMOTE_EVENT_NAMESPACE,
                    after_position=after_position,
                    limit=1000,
                )
                if not page:
                    break
                records.extend(page)
                after_position = int(page[-1].position)
                if len(page) < 1000:
                    break

        for record in records:
            if str(record.occurrence_id) != opportunity_id:
                continue
            if str(record.event_kind) != "epistemic_opportunity.recorded":
                raise RuntimeError(
                    "EpistemicOpportunityRemoteEventKindInvalid:"
                    f"{opportunity_id}:{record.event_kind}"
                )
            opportunity = EpistemicOpportunity.from_mapping(record.payload)
            if opportunity.opportunity_id != opportunity_id:
                raise RuntimeError(
                    "EpistemicOpportunityRemoteEventIdentityInvalid:"
                    f"{opportunity_id}"
                )
            self._remote_opportunity_cache[opportunity_id] = opportunity
            return opportunity, int(record.position)
        return None

    async def _write_remote_projection_unlocked(
        self,
        opportunity_id: str | None,
        *,
        reason_code: str,
    ) -> None:
        event_position: int | None = None
        if opportunity_id is not None:
            found = await self._find_remote_opportunity_unlocked(opportunity_id)
            if found is None:
                raise RuntimeError(
                    "EpistemicOpportunityRemoteProjectionTargetMissing:"
                    f"{opportunity_id}"
                )
            event_position = found[1]

        current = await self.runtime_store.get_state(
            _REMOTE_PROJECTION_NAMESPACE,
            _REMOTE_PROJECTION_KEY,
        )
        expected_revision = int(current.revision) if current is not None else 0
        if current is not None:
            previous = self._validate_remote_projection(
                current.payload,
                current.revision,
            )
            if (
                previous["current_opportunity_id"] == opportunity_id
                and previous["current_event_position"] == event_position
                and previous["reason_code"] == reason_code
            ):
                self._state_revision = expected_revision
                return

        next_revision = expected_revision + 1
        projection = {
            "kind": "epistemic_opportunity_projection",
            "schema_version": 1,
            "projection_revision": next_revision,
            "current_opportunity_id": opportunity_id,
            "current_event_position": event_position,
            "reason_code": reason_code,
            "updated_at": _now_iso(),
        }
        written = await self.runtime_store.put_state(
            namespace=_REMOTE_PROJECTION_NAMESPACE,
            state_key=_REMOTE_PROJECTION_KEY,
            expected_revision=expected_revision,
            schema_version=1,
            payload=projection,
        )
        self._state_revision = int(written.revision)

    @staticmethod
    def _validate_remote_projection(
        payload: Any,
        record_revision: int,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError("EpistemicOpportunityRemoteProjectionNotObject")
        expected_keys = {
            "kind",
            "schema_version",
            "projection_revision",
            "current_opportunity_id",
            "current_event_position",
            "reason_code",
            "updated_at",
        }
        if set(payload) != expected_keys:
            raise RuntimeError("EpistemicOpportunityRemoteProjectionShapeInvalid")
        if (
            payload.get("kind") != "epistemic_opportunity_projection"
            or payload.get("schema_version") != 1
            or payload.get("projection_revision") != int(record_revision)
        ):
            raise RuntimeError("EpistemicOpportunityRemoteProjectionSchemaInvalid")
        opportunity_id = payload.get("current_opportunity_id")
        event_position = payload.get("current_event_position")
        if opportunity_id is not None and (
            not isinstance(opportunity_id, str) or not opportunity_id
        ):
            raise RuntimeError("EpistemicOpportunityRemoteProjectionIdInvalid")
        if event_position is not None and (
            not isinstance(event_position, int)
            or isinstance(event_position, bool)
            or event_position <= 0
        ):
            raise RuntimeError(
                "EpistemicOpportunityRemoteProjectionPositionInvalid"
            )
        if (opportunity_id is None) != (event_position is None):
            raise RuntimeError("EpistemicOpportunityRemoteProjectionPairInvalid")
        if not isinstance(payload.get("reason_code"), str) or not isinstance(
            payload.get("updated_at"), str
        ):
            raise TypeError(
                "EpistemicOpportunityRemoteProjectionMetadataInvalid"
            )
        return payload

    def _load_opportunity_unlocked(self) -> EpistemicOpportunity | None:
        if self.state_path.exists():
            projection = _read_json_object(self.state_path)
            if projection.get("kind") != "epistemic_opportunity_projection":
                raise EpistemicOpportunityStateError(
                    self.state_path, "unexpected projection kind"
                )
            current_id = projection.get("current_opportunity_id")
            if current_id is None:
                return None
            if not isinstance(current_id, str) or not current_id:
                raise EpistemicOpportunityStateError(
                    self.state_path, "current_opportunity_id must be a string or null"
                )
            found = self._find_opportunity_unlocked(current_id)
            if found is None:
                raise EpistemicOpportunityStateError(
                    self.state_path, "projection points to a missing ledger record"
                )
            return found
        if self.legacy_state_path.exists():
            return self._migrate_legacy_state_unlocked()
        return None

    def _migrate_legacy_state_unlocked(self) -> EpistemicOpportunity | None:
        path = self.legacy_state_path
        try:
            raw_bytes = path.read_bytes()
            raw_text = raw_bytes.decode("utf-8", errors="strict")
            raw = json.loads(raw_text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EpistemicOpportunityStateError(path, type(exc).__name__) from exc
        if not isinstance(raw, dict):
            raise EpistemicOpportunityStateError(
                path, "legacy top level must be an object"
            )
        try:
            signal = CuriositySignal.from_mapping(raw).normalized()
        except (TypeError, ValueError) as exc:
            raise EpistemicOpportunityStateError(path, str(exc)) from exc
        if not signal.active:
            return None
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        opportunity = EpistemicOpportunity.build(
            source_occurrence_id=signal.source_event_id,
            source_stream_id=signal.source_stream_id,
            observed_gap=signal.anchor,
            open_question=signal.unknown,
            possible_next_look=signal.approach,
            generator_note=signal.why,
            generated_at=signal.updated_at,
            algorithm_version="legacy-curiosity-snapshot-v1",
            provenance="legacy_curiosity_signal",
            legacy_source_sha256=raw_sha256,
        )
        self._append_opportunity_unlocked(opportunity)
        self._write_projection_unlocked(opportunity.opportunity_id)
        return opportunity

    def _append_opportunity_unlocked(self, opportunity: EpistemicOpportunity) -> None:
        existing = self._find_opportunity_unlocked(opportunity.opportunity_id)
        if existing is not None:
            if _identity_fields(existing) != _identity_fields(opportunity):
                raise EpistemicOpportunityStateError(
                    self.ledger_path, "same opportunity_id has different payload"
                )
            return
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        line_bytes = (
            json.dumps(
                opportunity.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        with self.ledger_path.open("ab") as handle:
            offset = handle.tell()
            handle.write(line_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        assert self._opportunity_index is not None
        self._opportunity_index[opportunity.opportunity_id] = (
            offset,
            len(line_bytes),
            opportunity.payload_sha256,
        )

    def _find_opportunity_unlocked(
        self, opportunity_id: str
    ) -> EpistemicOpportunity | None:
        self._ensure_opportunity_index_unlocked()
        assert self._opportunity_index is not None
        entry = self._opportunity_index.get(opportunity_id)
        if entry is None:
            return None
        offset, length, indexed_sha256 = entry
        try:
            with self.ledger_path.open("rb") as handle:
                handle.seek(offset)
                line_bytes = handle.read(length)
        except OSError as exc:
            raise EpistemicOpportunityStateError(
                self.ledger_path, type(exc).__name__
            ) from exc
        item = self._decode_ledger_record(line_bytes, ordinal=None)
        if item.payload_sha256 != indexed_sha256:
            raise EpistemicOpportunityStateError(
                self.ledger_path, "indexed payload hash changed"
            )
        return item

    def _ensure_opportunity_index_unlocked(self) -> None:
        if self._opportunity_index is not None:
            return
        index: dict[str, tuple[int, int, str]] = {}
        if not self.ledger_path.exists():
            self._opportunity_index = index
            return
        try:
            with self.ledger_path.open("rb") as handle:
                ordinal = 0
                while True:
                    offset = handle.tell()
                    line_bytes = handle.readline()
                    if not line_bytes:
                        break
                    ordinal += 1
                    item = self._decode_ledger_record(line_bytes, ordinal=ordinal)
                    existing = index.get(item.opportunity_id)
                    if existing is not None and existing[2] != item.payload_sha256:
                        raise EpistemicOpportunityStateError(
                            self.ledger_path,
                            "same opportunity_id has different ledger payloads",
                        )
                    index.setdefault(
                        item.opportunity_id,
                        (offset, len(line_bytes), item.payload_sha256),
                    )
        except OSError as exc:
            raise EpistemicOpportunityStateError(
                self.ledger_path, type(exc).__name__
            ) from exc
        self._opportunity_index = index

    def _decode_ledger_record(
        self, line_bytes: bytes, *, ordinal: int | None
    ) -> EpistemicOpportunity:
        location = f" at ordinal {ordinal}" if ordinal is not None else ""
        if not line_bytes.endswith(b"\n"):
            raise EpistemicOpportunityStateError(
                self.ledger_path, f"incomplete JSONL record{location}"
            )
        try:
            line = line_bytes[:-1].decode("utf-8", errors="strict")
            if not line:
                raise TypeError("record must not be empty")
            raw = json.loads(line)
            if not isinstance(raw, dict):
                raise TypeError("record must be an object")
            return EpistemicOpportunity.from_mapping(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            reason = f"invalid JSONL record{location}: {type(exc).__name__}"
            raise EpistemicOpportunityStateError(self.ledger_path, reason) from exc

    def _write_projection_unlocked(
        self, opportunity_id: str | None, *, reason_code: str = "candidate_available"
    ) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        revision = 1
        if self.state_path.exists():
            raw = _read_json_object(self.state_path)
            if (
                raw.get("kind") != "epistemic_opportunity_projection"
                or raw.get("schema_version") != 1
            ):
                raise EpistemicOpportunityStateError(
                    self.state_path, "unsupported current projection schema"
                )
            if (
                raw.get("current_opportunity_id") == opportunity_id
                and raw.get("reason_code") == reason_code
            ):
                return
            old_revision = raw.get("projection_revision")
            if (
                not isinstance(old_revision, int)
                or isinstance(old_revision, bool)
                or old_revision < 0
            ):
                raise EpistemicOpportunityStateError(
                    self.state_path,
                    "projection_revision must be a non-negative integer",
                )
            revision = old_revision + 1
        projection = {
            "kind": "epistemic_opportunity_projection",
            "schema_version": 1,
            "projection_revision": revision,
            "current_opportunity_id": opportunity_id,
            "reason_code": reason_code,
            "updated_at": _now_iso(),
        }
        temp_path = self.state_path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(projection, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_path.replace(self.state_path)

    @staticmethod
    def _build_system_prompt(prefix_prompt: str) -> str:
        reference = str(prefix_prompt or "").strip()
        parts = [
            "你正在运行一个外部的认知机会候选生成过程。不要冒充主体，也不要输出主体决定。",
            EPISTEMIC_OPPORTUNITY_GUIDE,
        ]
        if reference:
            parts.append(
                "<subject_context_reference>\n"
                "以下仅帮助理解来源，不授予你代表主体判断重要性或意图的权力。\n"
                f"{reference}\n"
                "</subject_context_reference>"
            )
        parts.append(
            "## 输出格式\n"
            "只输出 JSON：\n"
            "{\n"
            '  "candidate_present": true/false,\n'
            '  "observed_gap": "来源中仍可检查的缺口",\n'
            '  "open_question": "一个开放问题候选",\n'
            '  "possible_next_look": "可选的继续观察方式；不是命令",\n'
            '  "generator_note": "为何生成器识别到一个未闭合处；不评价重要性"\n'
            "}\n"
            "candidate_present=false 时其余字段留空。不要输出 score、confidence、tags、priority 或主体状态。"
        )
        return "\n\n".join(parts)

    @staticmethod
    def _build_user_prompt(
        *,
        history_text: str,
        new_event_text: str,
        previous: EpistemicOpportunity | None,
    ) -> str:
        previous_text = (
            format_epistemic_opportunity(previous)
            if previous is not None
            else "（暂无上一条认知机会候选）"
        )
        history = str(history_text or "").strip() or "（暂无最近聊天历史）"
        event_text = str(new_event_text or "").strip() or "（暂无新增事件）"
        return (
            "<previous_epistemic_opportunity>\n"
            f"{previous_text}\n"
            "</previous_epistemic_opportunity>\n\n"
            "<source_history>\n"
            f"{history}\n"
            "</source_history>\n\n"
            "<new_source_occurrence>\n"
            f"{event_text}\n"
            "</new_source_occurrence>\n\n"
            "如来源中存在仍可检查的开放问题，只生成一个候选；不要判断主体是否会好奇或是否应该处理。"
        )

    @staticmethod
    def _parse_opportunity(
        raw_text: str,
        *,
        source_occurrence_id: str,
        source_stream_id: str,
        source_instance_id: str,
    ) -> EpistemicOpportunity | None:
        try:
            parsed: Any = json.loads(raw_text)
        except json.JSONDecodeError:
            parsed = json_repair.repair_json(raw_text, return_objects=True)
        if not isinstance(parsed, dict):
            return None
        candidate_present = parsed.get("candidate_present", parsed.get("active", False))
        if not isinstance(candidate_present, bool):
            raise TypeError("candidate_present must be a boolean")
        if not candidate_present:
            return None
        return EpistemicOpportunity.build(
            source_occurrence_id=source_occurrence_id,
            source_stream_id=source_stream_id,
            source_instance_id=source_instance_id,
            observed_gap=parsed.get("observed_gap", parsed.get("anchor", "")),
            open_question=parsed.get("open_question", parsed.get("unknown", "")),
            possible_next_look=parsed.get(
                "possible_next_look", parsed.get("approach", "")
            ),
            generator_note=parsed.get("generator_note", parsed.get("why", "")),
        )

    @staticmethod
    def _parse_signal(
        raw_text: str,
        *,
        source_event_id: str,
        source_stream_id: str,
    ) -> CuriositySignal:
        """Deprecated parser adapter for historical tests and callers."""

        opportunity = CuriosityEngine._parse_opportunity(
            raw_text,
            source_occurrence_id=source_event_id,
            source_stream_id=source_stream_id,
            source_instance_id="",
        )
        return CuriositySignal.from_opportunity(opportunity)


def format_epistemic_opportunity(opportunity: EpistemicOpportunity | None) -> str:
    """Render a candidate without attributing it to the subject."""

    if opportunity is None:
        return ""
    lines = [
        "### 认知机会候选（epistemic_opportunity）",
        (
            "这是系统根据有来源经历生成的开放问题候选，不是你的好奇、想法、偏好或任务，"
            "也不表示它重要、真实或应该被处理。"
        ),
        (
            "只有你此刻亲自选择注意、追问、改写或通过 AttentionThread 留下它，才构成主体动作；"
            "保持开放、忽略或不回应都不会被写成你的选择。"
        ),
        f"- 候选 ID：{opportunity.opportunity_id}",
    ]
    if opportunity.source_occurrence_id:
        lines.append(f"- 来源 occurrence：{opportunity.source_occurrence_id}")
    if opportunity.observed_gap:
        lines.append(f"- 观察到的缺口：{opportunity.observed_gap}")
    if opportunity.open_question:
        lines.append(f"- 开放问题：{opportunity.open_question}")
    if opportunity.possible_next_look:
        lines.append(f"- 可选的继续观察：{opportunity.possible_next_look}")
    if opportunity.generator_note:
        lines.append(f"- 生成说明：{opportunity.generator_note}")
    return "\n".join(lines).strip()


def format_curiosity_signal(
    signal: CuriositySignal | EpistemicOpportunity,
) -> str:
    """Deprecated renderer which now emits epistemic-opportunity semantics."""

    if isinstance(signal, EpistemicOpportunity):
        return format_epistemic_opportunity(signal)
    normalized = signal.normalized()
    if not normalized.active:
        return ""
    opportunity = EpistemicOpportunity.build(
        source_occurrence_id=normalized.source_event_id,
        source_stream_id=normalized.source_stream_id,
        observed_gap=normalized.anchor,
        open_question=normalized.unknown,
        possible_next_look=normalized.approach,
        generator_note=normalized.why,
        generated_at=normalized.updated_at,
        algorithm_version="legacy-curiosity-renderer-v1",
        provenance="legacy_render_adapter",
    )
    return format_epistemic_opportunity(opportunity)


def _validate_candidate_fields(values: dict[str, str]) -> None:
    for name, value in values.items():
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        byte_length = len(value.encode("utf-8"))
        if byte_length > MAX_OPPORTUNITY_FIELD_BYTES:
            raise ValueError(
                f"{name} exceeds UTF-8 byte budget: "
                f"bytes={byte_length}, max_bytes={MAX_OPPORTUNITY_FIELD_BYTES}"
            )


def _clean_string(name: str, value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value.strip()


def _validate_legacy_signal_mapping(data: dict[str, Any]) -> None:
    if "active" in data and not isinstance(data["active"], bool):
        raise ValueError("legacy active must be a boolean")
    for name in (
        "anchor",
        "why",
        "unknown",
        "approach",
        "updated_at",
        "source_event_id",
        "source_stream_id",
    ):
        if name in data and not isinstance(data[name], str):
            raise ValueError(f"legacy {name} must be a string")
    if "confidence" in data and (
        isinstance(data["confidence"], bool)
        or not isinstance(data["confidence"], (int, float))
    ):
        raise ValueError("legacy confidence must be numeric")
    if "tags" in data and (
        not isinstance(data["tags"], list)
        or any(not isinstance(item, str) for item in data["tags"])
    ):
        raise ValueError("legacy tags must be a list of strings")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EpistemicOpportunityStateError(path, type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise EpistemicOpportunityStateError(path, "top level must be an object")
    return raw


def _fit_projection_to_utf8_budget(
    text: str, *, max_bytes: int, opportunity_id: str
) -> str:
    if max_bytes <= 0:
        return ""
    original_bytes = len(text.encode("utf-8"))
    marker = (
        "\n[传输投影已按 UTF-8 字节预算省略；"
        f"opportunity_id={opportunity_id}; original_bytes={original_bytes}; "
        f"max_bytes={max_bytes}]"
    )
    marker_bytes = marker.encode("utf-8")
    if len(marker_bytes) >= max_bytes:
        return _utf8_prefix(marker, max_bytes)
    prefix = _utf8_prefix(text, max_bytes - len(marker_bytes))
    return prefix.rstrip() + marker


def _utf8_prefix(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _identity_fields(opportunity: EpistemicOpportunity) -> dict[str, str]:
    return {
        "source_occurrence_id": opportunity.source_occurrence_id,
        "source_stream_id": opportunity.source_stream_id,
        "source_instance_id": opportunity.source_instance_id,
        "algorithm_version": opportunity.algorithm_version,
        "observed_gap": opportunity.observed_gap,
        "open_question": opportunity.open_question,
        "possible_next_look": opportunity.possible_next_look,
        "generator_note": opportunity.generator_note,
        "provenance": opportunity.provenance,
        "legacy_source_sha256": opportunity.legacy_source_sha256,
    }


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat()
