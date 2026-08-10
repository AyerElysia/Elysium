"""Traceable learning candidates and subject-owned decision boundaries."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

from ..storage.learning_contracts import (
    LearningEventDraft,
    LearningProjection,
    LearningProjectionWrite,
    LearningStorePort,
)
from ..storage.subject_contracts import (
    AcceptSubjectCandidate,
    SubjectAuthorityCommit,
    SubjectAuthorityPort,
    SubjectDocumentPath,
)

LearningDecisionKind = Literal["accept_requested", "rejected", "kept_open"]
_SUBJECT_PATHS = frozenset({"SOUL.md", "USER.md", "MEMORY.md"})
_PROJECTION_NAME = "learning_candidate_decisions"
_PROJECTION_VERSION = "learning-candidate-decisions-v1"
_MAX_CANDIDATE_BYTES = 4 * 1024 * 1024


class LearningCandidateConflict(RuntimeError):
    """Raised when a candidate identity/revision is not an exact continuation."""


class LearningDecisionConflict(RuntimeError):
    """Raised when a decision does not match its immutable candidate."""


class SubjectAuthorityUnavailable(RuntimeError):
    """Raised when acceptance is requested without the authority owner Port."""


@dataclass(frozen=True, slots=True)
class LearningCandidate:
    """A suggestion outside subject authority, backed by exact bytes."""

    candidate_id: str
    candidate_revision: int
    candidate_kind: str
    candidate_occurrence_id: str
    source_occurrence_id: str
    source: str
    actor_consciousness_instance_id: str
    subject_revision: str
    target_path: SubjectDocumentPath | None
    candidate_content_bytes: bytes
    candidate_sha256: str
    occurred_at: str
    provenance: dict[str, object]

    @classmethod
    def create(
        cls,
        *,
        candidate_kind: str,
        candidate_content_bytes: bytes,
        source_occurrence_id: str,
        source: str,
        subject_revision: str,
        candidate_id: str = "",
        candidate_revision: int = 1,
        candidate_occurrence_id: str = "",
        actor_consciousness_instance_id: str = "",
        target_path: SubjectDocumentPath | None = None,
        occurred_at: str = "",
        provenance: dict[str, object] | None = None,
    ) -> LearningCandidate:
        content = bytes(candidate_content_bytes)
        identity = candidate_id or f"learning_candidate_{uuid4().hex}"
        occurrence = candidate_occurrence_id or (
            f"learning_candidate:{identity}:{candidate_revision}:{uuid4().hex}"
        )
        return cls(
            candidate_id=identity,
            candidate_revision=int(candidate_revision),
            candidate_kind=str(candidate_kind),
            candidate_occurrence_id=occurrence,
            source_occurrence_id=str(source_occurrence_id),
            source=str(source),
            actor_consciousness_instance_id=str(actor_consciousness_instance_id),
            subject_revision=str(subject_revision).lower(),
            target_path=target_path,
            candidate_content_bytes=content,
            candidate_sha256=hashlib.sha256(content).hexdigest(),
            occurred_at=occurred_at or datetime.now(UTC).isoformat(),
            provenance=dict(provenance or {}),
        )


@dataclass(frozen=True, slots=True)
class LearningDecision:
    """One explicit consciousness-instance decision about a candidate."""

    decision_occurrence_id: str
    decision_kind: LearningDecisionKind
    candidate_id: str
    candidate_revision: int
    candidate_sha256: str
    candidate_occurrence_id: str
    actor_consciousness_instance_id: str
    expected_subject_revision: str
    occurred_at: str
    reason: str
    target_path: SubjectDocumentPath | None = None
    accepted_content_bytes: bytes = b""
    accepted_content_sha256: str = ""
    provenance: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class LearningDecisionReceipt:
    """Content-free persisted decision status."""

    candidate_id: str
    candidate_revision: int
    candidate_sha256: str
    status: str
    decision_occurrence_id: str
    authority_occurrence_id: str = ""


def _validate_revision(value: str, *, field: str) -> str:
    revision = str(value).strip().lower()
    if len(revision) != 64 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        raise ValueError(f"{field} must be a 64-hex digest")
    return revision


class LearningDecisionLedger:
    """Persist candidate/decision evidence without usurping subject authority."""

    def __init__(
        self,
        store: LearningStorePort,
        *,
        subject_authority: SubjectAuthorityPort | None = None,
        project_subject_commit: (
            Callable[[SubjectDocumentPath, SubjectAuthorityCommit], Awaitable[None]]
            | None
        ) = None,
    ) -> None:
        self._store = store
        self._subject_authority = subject_authority
        self._project_subject_commit = project_subject_commit

    async def _projection(self) -> LearningProjection | None:
        projection = await self._store.get_projection(_PROJECTION_NAME)
        if projection is not None and projection.rebuild_state != "ready":
            raise RuntimeError(
                f"LearningDecisionProjectionUnavailable: {projection.rebuild_state}"
            )
        return projection

    @staticmethod
    def _payload(projection: LearningProjection | None) -> dict[str, object]:
        if projection is None:
            return {"candidates": {}, "decisions": {}}
        payload = dict(projection.payload)
        candidates = payload.get("candidates")
        decisions = payload.get("decisions")
        if (
            not isinstance(candidates, dict)
            or not isinstance(decisions, dict)
            or any(not isinstance(value, dict) for value in candidates.values())
            or any(not isinstance(value, dict) for value in decisions.values())
        ):
            raise RuntimeError("LearningDecisionProjectionCorrupt")
        return {
            "candidates": {str(key): dict(value) for key, value in candidates.items()},
            "decisions": {str(key): dict(value) for key, value in decisions.items()},
        }

    @staticmethod
    def _projection_write(
        projection: LearningProjection | None,
        payload: dict[str, object],
    ) -> LearningProjectionWrite:
        return LearningProjectionWrite(
            projection_name=_PROJECTION_NAME,
            expected_revision=projection.revision if projection else 0,
            expected_source_frontier=(projection.source_frontier if projection else 0),
            schema_version=1,
            projector_version=_PROJECTION_VERSION,
            rebuild_state="ready",
            payload=payload,
        )

    @staticmethod
    def _candidate_event(candidate: LearningCandidate) -> LearningEventDraft:
        if candidate.candidate_revision <= 0:
            raise ValueError("candidate_revision must be positive")
        if not candidate.candidate_id or not candidate.candidate_occurrence_id:
            raise ValueError("candidate identities must not be empty")
        if (
            not candidate.candidate_kind
            or not candidate.source
            or not candidate.source_occurrence_id
        ):
            raise ValueError("candidate kind/source occurrence must not be empty")
        if len(candidate.candidate_content_bytes) > _MAX_CANDIDATE_BYTES:
            raise ValueError("candidate content exceeds the explicit storage limit")
        if (
            candidate.target_path is not None
            and candidate.target_path not in _SUBJECT_PATHS
        ):
            raise ValueError("candidate target_path is not a subject document")
        digest = hashlib.sha256(candidate.candidate_content_bytes).hexdigest()
        if digest != candidate.candidate_sha256:
            raise ValueError("candidate content hash mismatch")
        revision = _validate_revision(
            candidate.subject_revision,
            field="subject_revision",
        )
        return LearningEventDraft(
            occurrence_id=candidate.candidate_occurrence_id,
            event_kind="candidate.proposed",
            occurred_at=candidate.occurred_at,
            source=candidate.source,
            actor_consciousness_instance_id=(candidate.actor_consciousness_instance_id),
            subject_revision=revision,
            provenance={
                "source_occurrence_id": candidate.source_occurrence_id,
                **dict(candidate.provenance),
            },
            payload={
                "candidate_id": candidate.candidate_id,
                "candidate_revision": candidate.candidate_revision,
                "candidate_kind": candidate.candidate_kind,
                "candidate_sha256": candidate.candidate_sha256,
                "target_path": candidate.target_path or "",
                "candidate_content_base64": base64.b64encode(
                    candidate.candidate_content_bytes
                ).decode("ascii"),
            },
        )

    async def append_candidate(
        self,
        candidate: LearningCandidate,
    ) -> LearningDecisionReceipt:
        """Append a suggestion; never modify subject documents."""

        event = self._candidate_event(candidate)
        projection = await self._projection()
        payload = self._payload(projection)
        candidates = payload["candidates"]
        assert isinstance(candidates, dict)
        existing = candidates.get(candidate.candidate_id)
        if isinstance(existing, dict):
            same = all(
                (
                    int(existing.get("candidate_revision", 0))
                    == candidate.candidate_revision,
                    str(existing.get("candidate_sha256", ""))
                    == candidate.candidate_sha256,
                    str(existing.get("candidate_occurrence_id", ""))
                    == candidate.candidate_occurrence_id,
                )
            )
            if same:
                await self._store.commit(events=[event], projections=[])
                return LearningDecisionReceipt(
                    candidate_id=candidate.candidate_id,
                    candidate_revision=candidate.candidate_revision,
                    candidate_sha256=candidate.candidate_sha256,
                    status=str(existing.get("status", "open")),
                    decision_occurrence_id=str(
                        existing.get("decision_occurrence_id", "")
                    ),
                    authority_occurrence_id=str(
                        existing.get("authority_occurrence_id", "")
                    ),
                )
            if int(existing.get("candidate_revision", 0)) + 1 != (
                candidate.candidate_revision
            ):
                raise LearningCandidateConflict(candidate.candidate_id)
            if str(existing.get("status", "")) == "committed":
                raise LearningCandidateConflict(
                    f"committed candidate cannot be silently revised: {candidate.candidate_id}"
                )

        candidates[candidate.candidate_id] = {
            "candidate_revision": candidate.candidate_revision,
            "candidate_sha256": candidate.candidate_sha256,
            "candidate_occurrence_id": candidate.candidate_occurrence_id,
            "candidate_kind": candidate.candidate_kind,
            "target_path": candidate.target_path or "",
            "subject_revision": candidate.subject_revision,
            "status": "open",
            "decision_occurrence_id": "",
            "authority_occurrence_id": "",
        }
        await self._store.commit(
            events=[event],
            projections=[self._projection_write(projection, payload)],
        )
        return LearningDecisionReceipt(
            candidate_id=candidate.candidate_id,
            candidate_revision=candidate.candidate_revision,
            candidate_sha256=candidate.candidate_sha256,
            status="open",
            decision_occurrence_id="",
        )

    async def _candidate_from_projection(
        self,
        decision: LearningDecision,
    ) -> tuple[LearningProjection | None, dict[str, object], dict[str, object]]:
        projection = await self._projection()
        payload = self._payload(projection)
        candidates = payload["candidates"]
        assert isinstance(candidates, dict)
        candidate = candidates.get(decision.candidate_id)
        if not isinstance(candidate, dict):
            raise LearningDecisionConflict(
                f"unknown candidate: {decision.candidate_id}"
            )
        if not all(
            (
                int(candidate.get("candidate_revision", 0))
                == decision.candidate_revision,
                str(candidate.get("candidate_sha256", "")) == decision.candidate_sha256,
                str(candidate.get("candidate_occurrence_id", ""))
                == decision.candidate_occurrence_id,
            )
        ):
            raise LearningDecisionConflict(decision.candidate_id)
        candidate_event = await self._store.event_by_occurrence(
            decision.candidate_occurrence_id
        )
        if (
            candidate_event is None
            or candidate_event.event_kind != "candidate.proposed"
        ):
            raise LearningDecisionConflict(
                f"candidate occurrence is missing: {decision.candidate_occurrence_id}"
            )
        return projection, payload, candidate

    async def list_candidates(
        self,
        *,
        status: str = "open",
        limit: int = 20,
    ) -> list[dict[str, object]]:
        """Return a stable, content-free candidate page for subject review."""

        normalized = str(status or "open").strip().lower()
        allowed = {
            "open",
            "accept_requested",
            "rejected",
            "kept_open",
            "committed",
            "all",
        }
        if normalized not in allowed:
            raise ValueError("invalid learning candidate status")
        bounded = max(1, min(100, int(limit)))
        payload = self._payload(await self._projection())
        candidates = payload["candidates"]
        assert isinstance(candidates, dict)
        values: list[dict[str, object]] = []
        for candidate_id, candidate in sorted(candidates.items()):
            if not isinstance(candidate, dict):  # guarded by _payload
                raise RuntimeError("LearningDecisionProjectionCorrupt")
            candidate_status = str(candidate.get("status", "open"))
            if normalized != "all" and candidate_status != normalized:
                continue
            values.append(
                {
                    "candidate_id": str(candidate_id),
                    "candidate_revision": int(candidate.get("candidate_revision", 0)),
                    "candidate_sha256": str(candidate.get("candidate_sha256", "")),
                    "candidate_occurrence_id": str(
                        candidate.get("candidate_occurrence_id", "")
                    ),
                    "candidate_kind": str(candidate.get("candidate_kind", "")),
                    "target_path": str(candidate.get("target_path", "")),
                    "subject_revision": str(candidate.get("subject_revision", "")),
                    "status": candidate_status,
                    "decision_occurrence_id": str(
                        candidate.get("decision_occurrence_id", "")
                    ),
                    "authority_occurrence_id": str(
                        candidate.get("authority_occurrence_id", "")
                    ),
                }
            )
            if len(values) >= bounded:
                break
        return values

    async def read_candidate(self, candidate_id: str) -> LearningCandidate | None:
        """Reconstruct and verify exact candidate bytes from immutable evidence."""

        identity = str(candidate_id or "").strip()
        if not identity:
            raise ValueError("candidate_id must not be empty")
        payload = self._payload(await self._projection())
        candidates = payload["candidates"]
        assert isinstance(candidates, dict)
        current = candidates.get(identity)
        if not isinstance(current, dict):
            return None
        occurrence_id = str(current.get("candidate_occurrence_id", ""))
        event = await self._store.event_by_occurrence(occurrence_id)
        if event is None or event.event_kind != "candidate.proposed":
            raise LearningCandidateConflict(
                f"candidate evidence is missing: {identity}"
            )
        event_payload = event.payload
        try:
            content = base64.b64decode(
                str(event_payload.get("candidate_content_base64", "")),
                validate=True,
            )
        except ValueError as exc:
            raise LearningCandidateConflict(
                f"candidate content is corrupt: {identity}"
            ) from exc
        digest = hashlib.sha256(content).hexdigest()
        if not all(
            (
                str(event_payload.get("candidate_id", "")) == identity,
                int(event_payload.get("candidate_revision", 0))
                == int(current.get("candidate_revision", 0)),
                str(event_payload.get("candidate_sha256", "")) == digest,
                str(current.get("candidate_sha256", "")) == digest,
                event.subject_revision == str(current.get("subject_revision", "")),
                str(event_payload.get("target_path", ""))
                == str(current.get("target_path", "")),
            )
        ):
            raise LearningCandidateConflict(
                f"candidate evidence does not match projection: {identity}"
            )
        target = str(event_payload.get("target_path", "")) or None
        if target is not None and target not in _SUBJECT_PATHS:
            raise LearningCandidateConflict(f"candidate target is invalid: {identity}")
        return LearningCandidate(
            candidate_id=identity,
            candidate_revision=int(event_payload["candidate_revision"]),
            candidate_kind=str(event_payload.get("candidate_kind", "")),
            candidate_occurrence_id=event.occurrence_id,
            source_occurrence_id=str(event.provenance.get("source_occurrence_id", "")),
            source=event.source,
            actor_consciousness_instance_id=(event.actor_consciousness_instance_id),
            subject_revision=event.subject_revision,
            target_path=cast(SubjectDocumentPath | None, target),
            candidate_content_bytes=content,
            candidate_sha256=digest,
            occurred_at=event.occurred_at,
            provenance=dict(event.provenance),
        )

    @staticmethod
    def _decision_event(
        decision: LearningDecision,
        candidate: dict[str, object],
    ) -> LearningEventDraft:
        if decision.decision_kind not in {
            "accept_requested",
            "rejected",
            "kept_open",
        }:
            raise ValueError(f"invalid decision kind: {decision.decision_kind}")
        if not decision.decision_occurrence_id:
            raise ValueError("decision_occurrence_id must not be empty")
        if not decision.actor_consciousness_instance_id:
            raise ValueError("decision actor consciousness instance is required")
        if not str(decision.reason or "").strip():
            raise ValueError("decision reason must not be empty")
        revision = _validate_revision(
            decision.expected_subject_revision,
            field="expected_subject_revision",
        )
        if revision != str(candidate.get("subject_revision", "")):
            raise LearningDecisionConflict(
                "decision must be based on the candidate subject revision"
            )
        accepted_hash = ""
        accepted_base64 = ""
        target_path = decision.target_path
        if decision.decision_kind == "accept_requested":
            if target_path not in _SUBJECT_PATHS:
                raise ValueError("accept_requested requires a subject target_path")
            if target_path != str(candidate.get("target_path", "")):
                raise LearningDecisionConflict(
                    "decision target does not match candidate"
                )
            accepted_hash = hashlib.sha256(decision.accepted_content_bytes).hexdigest()
            if accepted_hash != decision.accepted_content_sha256:
                raise ValueError("accepted content hash mismatch")
            if str(
                candidate.get("candidate_kind", "")
            ) == "memory_continuity_document_revision" and accepted_hash != str(
                candidate.get("candidate_sha256", "")
            ):
                raise LearningDecisionConflict(
                    "memory continuity candidates must be accepted byte-for-byte; "
                    "submit a new candidate for any rewrite"
                )
            if len(decision.accepted_content_bytes) > _MAX_CANDIDATE_BYTES:
                raise ValueError("accepted content exceeds the explicit storage limit")
            accepted_base64 = base64.b64encode(decision.accepted_content_bytes).decode(
                "ascii"
            )
        elif (
            decision.accepted_content_bytes
            or decision.accepted_content_sha256
            or target_path is not None
        ):
            raise ValueError(
                "rejected/kept_open decisions cannot carry accepted content"
            )
        return LearningEventDraft(
            occurrence_id=decision.decision_occurrence_id,
            event_kind=f"candidate.{decision.decision_kind}",
            occurred_at=decision.occurred_at,
            source="learning.subject_decision",
            actor_consciousness_instance_id=(decision.actor_consciousness_instance_id),
            subject_revision=revision,
            provenance=dict(decision.provenance or {}),
            payload={
                "candidate_id": decision.candidate_id,
                "candidate_revision": decision.candidate_revision,
                "candidate_sha256": decision.candidate_sha256,
                "candidate_occurrence_id": decision.candidate_occurrence_id,
                "decision_kind": decision.decision_kind,
                "reason": decision.reason,
                "target_path": target_path or "",
                "accepted_content_base64": accepted_base64,
                "accepted_content_sha256": accepted_hash,
            },
        )

    async def record_decision(
        self,
        decision: LearningDecision,
    ) -> LearningDecisionReceipt:
        """Record will evidence; acceptance remains only requested here."""

        projection, payload, candidate = await self._candidate_from_projection(decision)
        event = self._decision_event(decision, candidate)
        decisions = payload["decisions"]
        assert isinstance(decisions, dict)
        existing_decision = decisions.get(decision.decision_occurrence_id)
        if isinstance(existing_decision, dict):
            await self._store.commit(events=[event], projections=[])
            return LearningDecisionReceipt(
                candidate_id=decision.candidate_id,
                candidate_revision=decision.candidate_revision,
                candidate_sha256=decision.candidate_sha256,
                status=str(existing_decision.get("status", "")),
                decision_occurrence_id=decision.decision_occurrence_id,
                authority_occurrence_id=str(
                    existing_decision.get("authority_occurrence_id", "")
                ),
            )
        if str(candidate.get("status", "")) == "committed":
            raise LearningDecisionConflict(
                f"candidate is already committed: {decision.candidate_id}"
            )
        decisions[decision.decision_occurrence_id] = {
            "candidate_id": decision.candidate_id,
            "candidate_revision": decision.candidate_revision,
            "candidate_sha256": decision.candidate_sha256,
            "actor_consciousness_instance_id": (
                decision.actor_consciousness_instance_id
            ),
            "status": decision.decision_kind,
            "authority_occurrence_id": "",
        }
        candidate["status"] = decision.decision_kind
        candidate["decision_occurrence_id"] = decision.decision_occurrence_id
        await self._store.commit(
            events=[event],
            projections=[self._projection_write(projection, payload)],
        )
        return LearningDecisionReceipt(
            candidate_id=decision.candidate_id,
            candidate_revision=decision.candidate_revision,
            candidate_sha256=decision.candidate_sha256,
            status=decision.decision_kind,
            decision_occurrence_id=decision.decision_occurrence_id,
        )

    async def record_authority_commit(
        self,
        commit: SubjectAuthorityCommit,
    ) -> LearningDecisionReceipt:
        """Project a SubjectAuthorityCommit; never infer success locally."""

        projection = await self._projection()
        payload = self._payload(projection)
        candidates = payload["candidates"]
        decisions = payload["decisions"]
        assert isinstance(candidates, dict)
        assert isinstance(decisions, dict)
        decision = decisions.get(commit.decision_occurrence_id)
        candidate = candidates.get(commit.candidate_id)
        if not isinstance(decision, dict) or not isinstance(candidate, dict):
            raise LearningDecisionConflict("authority commit has no accept request")
        previous_revision = _validate_revision(
            commit.previous_subject_revision,
            field="previous_subject_revision",
        )
        new_revision = _validate_revision(
            commit.new_subject_revision,
            field="new_subject_revision",
        )
        if (
            not commit.authority_occurrence_id
            or not commit.document_version_id
            or commit.document_revision <= 0
            or len(commit.accepted_content_sha256) != 64
        ):
            raise LearningDecisionConflict("authority commit proof is incomplete")
        if not all(
            (
                str(decision.get("candidate_id", "")) == commit.candidate_id,
                str(decision.get("status", "")) in {"accept_requested", "committed"},
                str(decision.get("actor_consciousness_instance_id", ""))
                == commit.actor_consciousness_instance_id,
                str(candidate.get("candidate_sha256", ""))
                == str(decision.get("candidate_sha256", "")),
            )
        ):
            raise LearningDecisionConflict("authority commit does not match request")
        if str(decision.get("status", "")) == "committed":
            if (
                str(decision.get("authority_occurrence_id", ""))
                != commit.authority_occurrence_id
            ):
                raise LearningDecisionConflict("conflicting authority occurrence")
            return LearningDecisionReceipt(
                candidate_id=commit.candidate_id,
                candidate_revision=int(candidate["candidate_revision"]),
                candidate_sha256=str(candidate["candidate_sha256"]),
                status="committed",
                decision_occurrence_id=commit.decision_occurrence_id,
                authority_occurrence_id=commit.authority_occurrence_id,
            )

        event = LearningEventDraft(
            occurrence_id=f"learning_authority:{commit.authority_occurrence_id}",
            event_kind="candidate.committed",
            occurred_at=datetime.now(UTC).isoformat(),
            source="subject_authority",
            actor_consciousness_instance_id=(commit.actor_consciousness_instance_id),
            subject_revision=commit.new_subject_revision,
            provenance={
                "authority_occurrence_id": commit.authority_occurrence_id,
                "decision_occurrence_id": commit.decision_occurrence_id,
            },
            payload={
                "candidate_id": commit.candidate_id,
                "decision_occurrence_id": commit.decision_occurrence_id,
                "previous_subject_revision": previous_revision,
                "new_subject_revision": new_revision,
                "document_version_id": commit.document_version_id,
                "document_revision": commit.document_revision,
                "accepted_content_sha256": commit.accepted_content_sha256,
                "idempotent_replay": commit.idempotent_replay,
            },
        )
        decision["status"] = "committed"
        decision["authority_occurrence_id"] = commit.authority_occurrence_id
        candidate["status"] = "committed"
        candidate["authority_occurrence_id"] = commit.authority_occurrence_id
        await self._store.commit(
            events=[event],
            projections=[self._projection_write(projection, payload)],
        )
        return LearningDecisionReceipt(
            candidate_id=commit.candidate_id,
            candidate_revision=int(candidate["candidate_revision"]),
            candidate_sha256=str(candidate["candidate_sha256"]),
            status="committed",
            decision_occurrence_id=commit.decision_occurrence_id,
            authority_occurrence_id=commit.authority_occurrence_id,
        )

    async def accept_subject_candidate(
        self,
        decision: LearningDecision,
    ) -> LearningDecisionReceipt:
        """Record intent, call the authority owner, then project its proof."""

        if decision.decision_kind != "accept_requested":
            raise ValueError("accept_subject_candidate requires accept_requested")
        requested = await self.record_decision(decision)
        if requested.status == "committed":
            return requested
        if self._subject_authority is None:
            raise SubjectAuthorityUnavailable(
                "SubjectAuthorityPort is not injected; acceptance remains requested"
            )
        if decision.target_path is None:
            raise ValueError("accept_requested requires target_path")
        command = AcceptSubjectCandidate(
            candidate_id=decision.candidate_id,
            candidate_revision=decision.candidate_revision,
            candidate_sha256=decision.candidate_sha256,
            candidate_occurrence_id=decision.candidate_occurrence_id,
            decision_occurrence_id=decision.decision_occurrence_id,
            actor_consciousness_instance_id=(decision.actor_consciousness_instance_id),
            expected_subject_revision=decision.expected_subject_revision,
            target_path=decision.target_path,
            accepted_content_bytes=decision.accepted_content_bytes,
            accepted_content_sha256=decision.accepted_content_sha256,
            occurred_at=decision.occurred_at,
        )
        commit = await self._subject_authority.accept_candidate(command)
        if not all(
            (
                commit.candidate_id == decision.candidate_id,
                commit.decision_occurrence_id == decision.decision_occurrence_id,
                commit.actor_consciousness_instance_id
                == decision.actor_consciousness_instance_id,
                commit.previous_subject_revision == decision.expected_subject_revision,
                commit.accepted_content_sha256 == decision.accepted_content_sha256,
            )
        ):
            raise LearningDecisionConflict(
                "SubjectAuthorityCommit does not match the accept request"
            )
        if self._project_subject_commit is not None:
            await self._project_subject_commit(decision.target_path, commit)
        return await self.record_authority_commit(commit)


__all__ = [
    "AcceptSubjectCandidate",
    "LearningCandidate",
    "LearningCandidateConflict",
    "LearningDecision",
    "LearningDecisionConflict",
    "LearningDecisionKind",
    "LearningDecisionLedger",
    "LearningDecisionReceipt",
    "SubjectAuthorityCommit",
    "SubjectAuthorityPort",
    "SubjectAuthorityUnavailable",
    "SubjectDocumentPath",
]
