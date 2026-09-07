"""Service-owned procedural memory and subject-candidate projection runtime.

The Learning scheduler may borrow this object, but never opens or closes its
injected storage backend.  This keeps durable skills and explicit subject
decisions available when the optional semantic Learning worker is paused or
uninstalled.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from ..storage.learning_contracts import LearningStorePort
from ..storage.subject_contracts import SubjectAuthorityCommit, SubjectDocumentPath
from .decisions import LearningDecisionLedger, SubjectAuthorityPort
from .selectable import (
    LearningMutationContext,
    SelectedInsightStore,
    SelectedLearningPersistence,
    SelectedSkillStore,
)
from .skill_store import SkillStore
from .store import InsightStore

ProjectSubjectCommit = Callable[
    [SubjectDocumentPath, SubjectAuthorityCommit], Awaitable[None]
]


class SharedLearningProjectionRuntime:
    """One service-owned view of skills, insights, and candidate decisions.

    ``learning_store`` is already bound to the service's single
    ``StorageBackendRuntime``.  A caller without the exact projector claim may
    pass the unclaimed read handle with ``writable=False``; projection reads
    remain available while every in-memory compatibility mutation fails before
    changing an object.
    """

    def __init__(
        self,
        workspace_path: str | Path,
        *,
        learning_store: LearningStorePort | None = None,
        subject_authority: SubjectAuthorityPort | None = None,
        project_subject_commit: ProjectSubjectCommit | None = None,
        writer_instance_id: str = "",
        writable: bool | None = None,
    ) -> None:
        self._workspace = Path(workspace_path).resolve()
        self._learning_store = learning_store
        self._subject_authority = subject_authority
        self._writer_instance_id = (
            str(writer_instance_id or "").strip() or f"learning_shared_{uuid4().hex}"
        )
        self._writable = True if writable is None else bool(writable)
        self._initialized = False
        self._closed = False
        self._quiesce_reason = ""
        self._quiesce_error_type = ""
        self._selected_persistence: SelectedLearningPersistence | None = None

        if learning_store is None:
            self._store: InsightStore = InsightStore(self._workspace)
            self._skill_store: SkillStore = SkillStore(self._workspace)
            self._decision_ledger: LearningDecisionLedger | None = None
        else:
            persistence = SelectedLearningPersistence(
                learning_store,
                writer_instance_id=self._writer_instance_id,
                writable=self._writable,
                write_disabled_reason=(
                    "projector claim is not owned" if not self._writable else ""
                ),
            )
            insight_store = SelectedInsightStore(self._workspace, persistence)
            skill_store = SelectedSkillStore(self._workspace, persistence)
            persistence.bind(insight_store, skill_store)
            self._selected_persistence = persistence
            self._store = insight_store
            self._skill_store = skill_store
            self._decision_ledger = LearningDecisionLedger(
                learning_store,
                subject_authority=subject_authority,
                project_subject_commit=project_subject_commit,
            )

    @property
    def store(self) -> InsightStore:
        return self._store

    @property
    def skill_store(self) -> SkillStore:
        return self._skill_store

    @property
    def decision_ledger(self) -> LearningDecisionLedger | None:
        return self._decision_ledger

    @property
    def selected_persistence(self) -> SelectedLearningPersistence | None:
        """Compatibility adapter borrowed by the legacy scheduler."""

        return self._selected_persistence

    @property
    def storage_runtime(self) -> object | None:
        return getattr(self._learning_store, "runtime", None)

    @property
    def subject_revision_reader(self) -> Callable[[], Awaitable[str]] | None:
        """Expose the injected authority's read-only unified revision call."""

        reader = getattr(self._subject_authority, "current_subject_revision", None)
        return reader if callable(reader) else None

    @property
    def writer_instance_id(self) -> str:
        return self._writer_instance_id

    @property
    def writable(self) -> bool:
        if self._closed or not self._writable:
            return False
        persistence = self._selected_persistence
        return persistence.writable if persistence is not None else True

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def closed(self) -> bool:
        return self._closed

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("SharedLearningProjectionRuntimeClosed")

    def require_writable(self) -> None:
        self._require_open()
        if not self._initialized:
            raise RuntimeError("SharedLearningProjectionRuntimeNotInitialized")
        if not self.writable:
            reason = self._quiesce_reason or "projector claim is not owned"
            raise RuntimeError(f"SharedLearningProjectionRuntimeReadOnly:{reason}")

    async def initialize(self) -> None:
        """Hydrate once; never open the injected store or backend runtime."""

        self._require_open()
        if self._initialized:
            return
        persistence = self._selected_persistence
        if persistence is not None:
            await persistence.initialize()
            if self._writable:
                self._store.reconcile_knowledge_versions()
                await persistence.flush()
        self._initialized = True

    @contextmanager
    def mutation_context(
        self,
        context: LearningMutationContext,
    ) -> Iterator[None]:
        """Bind immutable attribution only after the write gate is proven."""

        self.require_writable()
        persistence = self._selected_persistence
        if persistence is None:
            yield
            return
        with persistence.mutation_context(context):
            yield

    async def flush(self) -> None:
        """Flush borrowed selected projections at an explicit async boundary."""

        self.require_writable()
        if self._selected_persistence is not None:
            await self._selected_persistence.flush()

    def quiesce(self, *, reason: str, error_type: str = "") -> None:
        """Stop shared projection writes without release, takeover, or rebase."""

        self._writable = False
        self._quiesce_reason = str(reason or "").strip() or "shared owner quiesced"
        self._quiesce_error_type = str(error_type or "").strip()
        if self._selected_persistence is not None:
            self._selected_persistence.quiesce(
                reason=self._quiesce_reason,
                error_type=self._quiesce_error_type,
            )

    async def close(self) -> None:
        """Close this logical owner without closing its injected store/runtime."""

        if self._closed:
            return
        try:
            if self._initialized and self.writable:
                await self.flush()
        finally:
            self._closed = True

    def health_snapshot(self) -> dict[str, object]:
        persistence = self._selected_persistence
        return {
            "status": (
                "closed"
                if self._closed
                else "healthy"
                if self._initialized and self.writable
                else "read_only"
                if self._initialized
                else "initializing"
            ),
            "backend": "selected" if persistence is not None else "legacy_local",
            "initialized": self._initialized,
            "closed": self._closed,
            "writable": self.writable,
            "writer_instance_id": self._writer_instance_id,
            "quiesce_reason": self._quiesce_reason,
            "quiesce_error_type": self._quiesce_error_type,
            "selected_persistence": (
                persistence.health_snapshot() if persistence is not None else None
            ),
        }


__all__ = ["ProjectSubjectCommit", "SharedLearningProjectionRuntime"]
