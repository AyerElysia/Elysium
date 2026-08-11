"""Life Engine 仿生记忆服务。

实现基于认知科学的记忆系统：
- 激活扩散 (Spreading Activation)：联想机制
- Hebbian 学习：共同激活强化连接
- 软遗忘：基于 Ebbinghaus 曲线的记忆衰减

本模块为记忆服务的核心入口，整合 nodes、edges、search、decay 模块。
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.app.plugin_system.api import log_api
from src.kernel.concurrency import get_task_manager

from ..storage.contracts import StorageBackendRuntime
from ..storage.memory import MemoryStorageBundle, open_mysql_memory_storage
from ..storage.memory.local import create_local_memory_storage_bundle
from ..storage.memory.mysql import (
    MySQLMemoryReadinessProbeError,
    inspect_mysql_memory_readiness,
)
from ..storage.models import BackendKind, StorageAvailability
from .decay import (
    compute_memory_strength,
    get_file_relations,
)
from .edges import (
    EXPLICIT_RELATION_EDGE_TYPES,
    EdgeType,
    MemoryEdge,
)
from .eligibility import (
    MEMORY_CONTENT_DIRECTORIES,
    assess_document_path,
    assess_indexed_document_path,
    assess_workspace_document,
    read_workspace_document,
    scan_workspace_documents,
)
from .epistemic import (
    ClaimEvidence,
    ClaimSearchResult,
    ClaimState,
    CurrentFactProjection,
    EpistemicConflict,
    MemoryAuditEntry,
    MemoryBelief,
    MemoryClaim,
    MemoryDisposition,
    MemoryStateEvent,
    RetrievalEpisode,
    RetrievalExposure,
    RetrievalFeedback,
    RetrievalPlasticity,
    create_epistemic_schema,
    new_claim,
)
from .experience import (
    EvidenceAwareMemoryResult,
    ExperienceAppendReport,
    ExperienceRecord,
    MemorySearchMode,
    WitnessMemory,
    WitnessSearchResult,
    create_life_memory_schema,
)
from .health import health_snapshot_from_path as collect_health_snapshot
from .indexing import (
    ChunkIndexState,
    DocumentIndexResult,
    IndexJob,
    create_memory_schema,
)
from .lineage import (
    CANONICAL_EDGE_TYPES,
    LINEAGE_EDGE_TYPES,
    MemoryBundle,
    MemoryCorrection,
    MemoryEvidence,
    MemoryTrace,
)
from .living import (
    ArtifactHead,
    ArtifactHeadConflict,
    AssociationEvidence,
    AssociationSelection,
    CoRecallEvent,
    InterpretationSearchResult,
    InterpretationSource,
    MemoryArtifactVersion,
    MemoryDerivation,
    MemoryInterpretation,
    RecallEpisode,
    RecallEvent,
    SemanticRelation,
    create_living_memory_schema,
    new_artifact_version,
)
from .nodes import (
    MemoryNode,
    compute_content_hash,
    generate_file_node_id,
)
from .search import (
    DetailedSearchResult,
    SearchResult,
    get_chroma_collection,
)
from .sqlite_runtime import (
    bind_reader_pool,
    open_memory_connection,
    run_db,
)
from .worker import (
    CHUNK_COLLECTION_PREFIX,
    CHUNK_INDEX_VERSION,
    DEFAULT_RECLAIM_AFTER,
    IndexWorkerReport,
    chunk_collection_metadata,
    get_chunk_collection,
    get_named_chunk_collection,
)
from .workspace_projection_identity import (
    WorkspaceProjectionBinding,
    WorkspaceProjectionDeleteEvidenceError,
    WorkspaceProjectionRevisionConflict,
    WorkspaceProjectionWritePermit,
    authorize_workspace_projection_write,
    bind_workspace_projection,
    build_workspace_projection_identity,
    commit_workspace_projection_inventory,
)

logger = log_api.get_logger("life_engine.memory")

# 启动补索引时一次性读入内存的文档数。这不是行为阈值：分批与否不改变最终被
# 索引的文件集合，只决定峰值内存——整个工作区的正文同时驻留可能是数百 MB。
_RECOVERY_READ_BATCH = 32
_MYSQL_RECOVERY_WRITE_CONCURRENCY = 8
_MYSQL_MEMORY_STARTUP_PROBE_TIMEOUT_SECONDS = 30.0


@dataclass
class _StartupRecoveryProgress:
    """Content-free progress for the service-owned workspace recovery task."""

    status: str = "idle"
    phase: str = "idle"
    started_at: str = ""
    finished_at: str = ""
    total_documents: int = 0
    processed_documents: int = 0
    indexed_documents: int = 0
    requeued_documents: int = 0
    unchanged_documents: int = 0
    legacy_documents: int = 0
    read_failures: int = 0
    ghost_documents: int = 0
    artifact_total: int = 0
    artifact_processed: int = 0
    artifact_versions_appended: int = 0
    error_type: str = ""

    def health_snapshot(self) -> dict[str, Any]:
        """Return bounded technical counters without paths or document content."""

        return {
            "status": self.status,
            "phase": self.phase,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_documents": self.total_documents,
            "processed_documents": self.processed_documents,
            "indexed_documents": self.indexed_documents,
            "requeued_documents": self.requeued_documents,
            "unchanged_documents": self.unchanged_documents,
            "legacy_documents": self.legacy_documents,
            "read_failures": self.read_failures,
            "ghost_documents": self.ghost_documents,
            "artifact_total": self.artifact_total,
            "artifact_processed": self.artifact_processed,
            "artifact_versions_appended": self.artifact_versions_appended,
            "error_type": self.error_type,
        }


@dataclass(frozen=True)
class _BundlePathView:
    """一个候选路径的合规性与存在性判定结果。

    Attributes:
        memory_path: 规范化后的工作区相对路径；不合格时为 ``None``。
        exists: 该路径当前是否是一个真实存在的普通文件。
    """

    memory_path: str | None
    exists: bool


def _assess_bundle_path(workspace: Path, raw_path: str) -> _BundlePathView:
    """判定单个候选路径是否可进入记忆包，并顺带取回存在性。

    合规性判定与存在性判定都要碰文件系统（``is_symlink`` / ``exists`` /
    ``is_file``），合在一处求值可以让调用方一次拿全，而不是先问路径再问存在。

    Args:
        workspace: 工作区根目录。
        raw_path: 检索结果或节点上记录的原始路径。

    Returns:
        _BundlePathView: 判定结果。
    """
    eligibility = assess_indexed_document_path(raw_path)
    if not eligibility.eligible:
        return _BundlePathView(memory_path=None, exists=False)

    candidate = workspace / eligibility.path
    if candidate.is_symlink():
        return _BundlePathView(memory_path=None, exists=False)
    if (
        candidate.exists()
        and not assess_workspace_document(workspace, eligibility.path).eligible
    ):
        return _BundlePathView(memory_path=None, exists=False)
    return _BundlePathView(memory_path=eligibility.path, exists=candidate.is_file())


def _assess_bundle_paths(
    workspace: Path,
    raw_paths: Sequence[str],
) -> dict[str, _BundlePathView]:
    """在一次线程调用里判定一批候选路径。

    Args:
        workspace: 工作区根目录。
        raw_paths: 原始路径序列，调用方保证已去重。

    Returns:
        dict[str, _BundlePathView]: 原始路径到判定结果的映射。
    """
    return {
        raw_path: _assess_bundle_path(workspace, raw_path) for raw_path in raw_paths
    }


def _read_documents(
    workspace: Path,
    paths: Sequence[str],
) -> list[tuple[str, str]]:
    """在线程中读取一批工作区文档。

    Args:
        workspace: 工作区根目录。
        paths: 工作区相对路径。

    Returns:
        list[tuple[str, str]]: ``(path, content)``，不合格或读取失败的文件被跳过。
    """
    documents: list[tuple[str, str]] = []
    for path in paths:
        try:
            decision = assess_workspace_document(workspace, path)
            if not decision.eligible:
                continue
            content, _, _ = read_workspace_document(workspace, path)
        except Exception as exc:
            logger.warning(f"读取工作区文档失败 {path}: {exc}")
            continue
        documents.append((path, content))
    return documents


class LifeMemoryService:
    """仿生记忆服务。"""

    # 算法参数（覆盖各模块的默认值）
    DECAY_LAMBDA = 0.05
    LEARNING_RATE = 0.1
    SPREAD_DECAY = 0.7
    SPREAD_THRESHOLD = 0.3
    PRUNE_THRESHOLD = 0.1
    RRF_K = 60

    def __init__(
        self,
        plugin: Any,
        *,
        clock: Any = None,
        vector_backend_enabled: bool = True,
        storage_runtime: StorageBackendRuntime | None = None,
        memory_storage: MemoryStorageBundle | None = None,
        selectable_storage_enabled: bool = False,
    ) -> None:
        """初始化记忆服务。

        Args:
            plugin: 插件实例（用于获取配置）
            clock: 可选无参时钟，供相对日期查询测试或注入使用。
        """
        self.plugin = plugin
        self._clock = clock or datetime.now
        self._vector_backend_enabled = bool(vector_backend_enabled)
        if storage_runtime is not None and memory_storage is not None:
            raise ValueError(
                "LifeMemoryService accepts storage_runtime or memory_storage, not both"
            )
        self._storage_runtime = storage_runtime
        self._provided_memory_storage = memory_storage
        self._selectable_storage_enabled = bool(selectable_storage_enabled)
        self._workspace_override: Path | None = None
        if isinstance(plugin, (str, Path)):
            self._workspace_override = Path(plugin)
        self._db: sqlite3.Connection | None = None
        self._memory_storage: MemoryStorageBundle | None = None
        self._initialized = False
        self._closing = False
        self._chroma_collection = None
        self._chunk_collection = None
        self._chunk_collection_identity: tuple[str, int] | None = None
        self._chunk_index_state: ChunkIndexState | None = None
        self._chunk_collection_candidate = None
        self._chunk_collection_candidate_identity: tuple[str, int] | None = None
        self._index_write_lock = asyncio.Lock()
        self._index_worker_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._startup_recovery_task: asyncio.Task[None] | None = None
        self._startup_recovery_progress = _StartupRecoveryProgress()
        self._workspace_projection_lock = asyncio.Lock()
        self._workspace_projection_binding: WorkspaceProjectionBinding | None = None
        self._workspace_projection_permit: WorkspaceProjectionWritePermit | None = None

    def _emit_visual_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        source: str = "memory_service",
    ) -> None:
        """向可视化层广播事件，不影响主流程。"""
        try:
            from .router import MemoryRouter

            MemoryRouter.broadcast(event_type, payload, source=source)
        except (ImportError, RuntimeError, ConnectionError, AttributeError) as e:
            # 预期的异常：模块未加载、路由未初始化、网络问题等
            logger.debug(f"可视化事件广播失败 ({event_type}): {e}")
        except Exception as e:
            # 可视化属于非关键路径，不应影响主流程
            logger.debug(f"可视化事件遇到意外错误 ({event_type}): {e}")

    def _get_config(self) -> Any:
        """获取配置。"""
        from ..core.config import LifeEngineConfig

        config = getattr(self.plugin, "config", None)
        if isinstance(config, LifeEngineConfig):
            return config
        return LifeEngineConfig()

    def _get_db_path(self) -> Path:
        """获取数据库路径。"""
        runtime = self._storage_runtime
        if (
            runtime is not None
            and runtime.enabled
            and runtime.backend == BackendKind.LOCAL
            and runtime.engine is not None
            and runtime.engine.url.database
        ):
            return Path(str(runtime.engine.url.database)).resolve()
        if self._workspace_override is not None:
            workspace = self._workspace_override
        else:
            config = self._get_config()
            workspace = Path(config.settings.workspace_path)
        return workspace / ".memory" / "memory.db"

    def _get_vector_db_path(self) -> str:
        """获取向量数据库路径。"""
        if self._workspace_override is not None:
            workspace = self._workspace_override
        else:
            config = self._get_config()
            workspace = Path(config.settings.workspace_path)
        return str(workspace / ".memory" / "chroma")

    def _get_workspace_path(self) -> Path:
        """获取记忆工作空间路径。"""
        if self._workspace_override is not None:
            return self._workspace_override
        config = self._get_config()
        return Path(config.settings.workspace_path)

    def _selected_workspace_projection_owner(self) -> tuple[str, str]:
        """Return the exact storage generation and stable writer owner."""

        runtime = self._storage_runtime
        if (
            runtime is None
            or not runtime.enabled
            or runtime.backend != BackendKind.MYSQL
            or runtime.generation is None
            or runtime.authority_token is None
        ):
            raise RuntimeError("WorkspaceProjectionAuthorityUnavailable")
        return (
            str(runtime.generation.generation_id),
            str(runtime.authority_token.owner_id),
        )

    @staticmethod
    def _workspace_projection_occurrence(
        kind: str,
        *parts: str,
    ) -> str:
        digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:32]
        return f"workspace-{kind}-{digest}"

    async def _prepare_workspace_projection(
        self,
        *,
        scan: Any = None,
    ) -> tuple[Any, WorkspaceProjectionBinding, WorkspaceProjectionWritePermit] | None:
        """Bind one MySQL generation to this exact workspace before any write."""

        storage = self._require_memory_storage()
        if storage.backend != BackendKind.MYSQL:
            return None
        binding_store = storage.workspace_projection
        if binding_store is None:
            # A selected remote projection without a durable source binding can
            # reproduce the cross-workspace mass-retirement incident. Never
            # fall back to an unbound write path.
            if self._storage_runtime is not None and self._storage_runtime.enabled:
                raise RuntimeError("WorkspaceProjectionBindingStoreUnavailable")
            return None

        identity = await asyncio.to_thread(
            build_workspace_projection_identity,
            self._get_workspace_path(),
            scan=scan,
        )
        storage_generation_id, owner_id = self._selected_workspace_projection_owner()
        projection_generation_id = (
            "workspace-documents-v1-" + identity.canonical_root_sha256[:24]
        )
        async with self._workspace_projection_lock:
            binding = await binding_store.load_binding(storage_generation_id)
            if binding is None:
                occurred_at = datetime.now().astimezone().isoformat()
                transition = bind_workspace_projection(
                    identity,
                    storage_generation_id=storage_generation_id,
                    projection_generation_id=projection_generation_id,
                    owner_id=owner_id,
                    actor_id=owner_id,
                    audit_occurrence_id=self._workspace_projection_occurrence(
                        "bind",
                        storage_generation_id,
                        identity.canonical_root_sha256,
                    ),
                    reason_code="initial-workspace-projection-bind",
                    occurred_at=occurred_at,
                )
                try:
                    binding = await binding_store.commit_transition(transition)
                except WorkspaceProjectionRevisionConflict:
                    binding = await binding_store.load_binding(storage_generation_id)
                    if binding is None:
                        raise

            permit = authorize_workspace_projection_write(
                binding,
                identity,
                storage_generation_id=storage_generation_id,
                projection_generation_id=binding.projection_generation_id,
                owner_id=owner_id,
            )
            self._workspace_projection_binding = binding
            self._workspace_projection_permit = permit
            return identity, binding, permit

    async def _commit_workspace_projection_inventory(
        self,
        identity: Any,
        binding: WorkspaceProjectionBinding,
    ) -> WorkspaceProjectionBinding:
        """Append one content-free inventory observation after successful writes."""

        storage_generation_id, owner_id = self._selected_workspace_projection_owner()
        store = self._require_memory_storage().workspace_projection
        if store is None:
            raise RuntimeError("WorkspaceProjectionBindingStoreUnavailable")
        transition = commit_workspace_projection_inventory(
            binding,
            identity,
            expected_revision=binding.revision,
            owner_id=owner_id,
            actor_id=owner_id,
            audit_occurrence_id=self._workspace_projection_occurrence(
                "inventory",
                storage_generation_id,
                identity.source_root_sha256,
                str(binding.revision + 1),
            ),
            reason_code="present-documents-reconciled",
            occurred_at=datetime.now().astimezone().isoformat(),
        )
        committed = await store.commit_transition(transition)
        self._workspace_projection_binding = committed
        self._workspace_projection_permit = authorize_workspace_projection_write(
            committed,
            identity,
            storage_generation_id=storage_generation_id,
            projection_generation_id=committed.projection_generation_id,
            owner_id=owner_id,
        )
        return committed

    async def _get_chroma_collection(self) -> Any:
        """获取并缓存兼容旧节点向量的集合。"""
        if self._chroma_collection is None:
            self._chroma_collection = await get_chroma_collection(
                self._get_vector_db_path()
            )
        return self._chroma_collection

    async def _resolve_chunk_collection(
        self,
        model_name: str,
        dimension: int,
        _metadata: Any = None,
    ) -> Any:
        """按 embedding 模型和维度获取本轮 worker 的候选集合。"""
        identity = (str(model_name or "unknown"), int(dimension))
        if (
            self._chunk_collection is not None
            and self._chunk_collection_identity == identity
        ):
            return self._chunk_collection
        if (
            self._chunk_collection_candidate is not None
            and self._chunk_collection_candidate_identity == identity
        ):
            return self._chunk_collection_candidate

        state = self._chunk_index_state
        if state is not None:
            self._validate_chunk_index_state(state)
            if (state.model_name, state.dimension) != identity:
                raise ValueError("ActiveCollectionIdentityMismatch")
            collection = await get_named_chunk_collection(
                self._get_vector_db_path(),
                state.collection_name,
            )
            self._validate_restored_chunk_collection(state, collection)
        else:
            collection = await get_chunk_collection(
                self._get_vector_db_path(),
                identity[0],
                identity[1],
            )

        self._chunk_collection_candidate = collection
        self._chunk_collection_candidate_identity = identity
        return collection

    @staticmethod
    def _validate_chunk_index_state(state: ChunkIndexState) -> None:
        expected_prefix = f"{CHUNK_COLLECTION_PREFIX}_v{CHUNK_INDEX_VERSION}_"
        if state.version != CHUNK_INDEX_VERSION:
            raise ValueError("ChunkIndexVersionMismatch")
        if not state.collection_name.startswith(expected_prefix):
            raise ValueError("ChunkCollectionNameMismatch")
        if state.dimension <= 0 or not state.model_name:
            raise ValueError("ChunkCollectionIdentityInvalid")

    @classmethod
    def _validate_restored_chunk_collection(
        cls,
        state: ChunkIndexState,
        collection: Any,
    ) -> None:
        cls._validate_chunk_index_state(state)
        actual_name = str(getattr(collection, "name", "") or "").strip()
        if actual_name != state.collection_name:
            raise ValueError("ChunkCollectionNameMismatch")
        metadata = getattr(collection, "metadata", None)
        if not isinstance(metadata, dict) or not metadata:
            raise ValueError("ChunkCollectionMetadataMissing")
        expected = chunk_collection_metadata(state.model_name, state.dimension)
        for key, value in expected.items():
            if key not in metadata or metadata[key] != value:
                raise ValueError(f"ChunkCollectionMetadataMismatch:{key}")

    async def _restore_chunk_collection(self) -> None:
        state = (
            await self._require_memory_storage().document_index.read_chunk_index_state()
        )
        if state is None:
            return
        self._chunk_index_state = state
        self._validate_chunk_index_state(state)
        collection = await get_named_chunk_collection(
            self._get_vector_db_path(),
            state.collection_name,
        )
        self._validate_restored_chunk_collection(state, collection)
        self._chunk_collection = collection
        self._chunk_collection_identity = (state.model_name, state.dimension)
        self._chunk_collection_candidate = collection
        self._chunk_collection_candidate_identity = self._chunk_collection_identity
        logger.info(f"已恢复 chunk 向量集合: {state.collection_name}")

    async def initialize(self) -> None:
        """初始化唯一被选择的 Memory backend；禁止跨 backend 回退。"""
        async with self._lifecycle_lock:
            if self._initialized:
                return
            storage_enabled = self._selectable_storage_enabled or bool(
                getattr(getattr(self._get_config(), "storage", None), "enabled", False)
            )
            if (
                storage_enabled
                and self._provided_memory_storage is None
                and (self._storage_runtime is None or not self._storage_runtime.enabled)
            ):
                raise RuntimeError(
                    "selectable Memory storage is enabled but no coherent runtime was injected"
                )
            local_db: sqlite3.Connection | None = None
            try:
                if self._provided_memory_storage is not None:
                    self._memory_storage = self._provided_memory_storage
                elif (
                    self._storage_runtime is not None
                    and self._storage_runtime.enabled
                    and self._storage_runtime.backend == BackendKind.MYSQL
                ):
                    self._memory_storage = await open_mysql_memory_storage(
                        self._storage_runtime,
                        initialize_schema=False,
                    )
                else:
                    db_path = self._get_db_path()
                    local_db = await run_db(
                        open_memory_connection,
                        db_path,
                        role="writer",
                    )
                    self._db = local_db
                    bind_reader_pool(db_path)
                    await self._create_tables()
                    await run_db(create_memory_schema, local_db)
                    await run_db(create_life_memory_schema, local_db)
                    await run_db(create_epistemic_schema, local_db)
                    await run_db(create_living_memory_schema, local_db)
                    local_runtime = (
                        self._storage_runtime
                        if self._storage_runtime is not None
                        and self._storage_runtime.enabled
                        else None
                    )
                    self._memory_storage = create_local_memory_storage_bundle(
                        self._require_local_db,
                        runtime=local_runtime,
                    )

                await self._validate_storage_availability()
                if self._require_memory_storage().backend == BackendKind.MYSQL:
                    await self._prepare_workspace_projection()

                if self._vector_backend_enabled:
                    try:
                        await self._restore_chunk_collection()
                    except Exception as exc:
                        requeued = await self._require_memory_storage().document_index.invalidate_vector_projection()
                        self._chunk_index_state = None
                        self._chunk_collection = None
                        self._chunk_collection_identity = None
                        self._chunk_collection_candidate = None
                        self._chunk_collection_candidate_identity = None
                        logger.warning(
                            "恢复 chunk 向量集合失败；已废弃可重建投影并将 "
                            f"{requeued} 个文档重新入队: {exc}"
                        )

                    self._chroma_collection = await self._get_chroma_collection()
                else:
                    logger.warning("Life Memory 向量后端已关闭，使用词法检索")
                self._initialized = True
                if self._require_memory_storage().backend == BackendKind.MYSQL:
                    self._start_background_startup_recovery()
                else:
                    await self._run_startup_recovery_owned(propagate_failure=True)
            except BaseException:
                await self._cancel_startup_recovery()
                self._memory_storage = None
                self._db = None
                self._initialized = False
                self._workspace_projection_binding = None
                self._workspace_projection_permit = None
                bind_reader_pool(None)
                if local_db is not None:
                    await run_db(local_db.close)
                raise
            backend = self._require_memory_storage().backend.value
            logger.info(f"记忆服务初始化完成，权威后端: {backend}")

    async def _validate_storage_availability(self) -> None:
        storage = self._require_memory_storage()
        names = (
            "document_index",
            "experiences",
            "witnesses",
            "living",
            "epistemic",
            "legacy_graph",
        )
        runtime = self._storage_runtime
        if (
            storage.backend == BackendKind.MYSQL
            and runtime is not None
            and runtime.enabled
            and runtime.backend == BackendKind.MYSQL
        ):
            try:
                async with asyncio.timeout(
                    _MYSQL_MEMORY_STARTUP_PROBE_TIMEOUT_SECONDS
                ):
                    try:
                        shared_health = await runtime.health()
                    except Exception as exc:  # noqa: BLE001 - sanitize backend error
                        error_type = type(exc).__name__
                        raise RuntimeError(
                            "MemoryBackendUnavailable:"
                            f"shared_runtime=failed,error_type={error_type}"
                        ) from None

                    if not isinstance(shared_health, dict):
                        raise RuntimeError(
                            "MemoryBackendUnavailable:shared_runtime=failed,"
                            "error_type=InvalidHealthPayload"
                        ) from None

                    raw_status = str(shared_health.get("status") or "failed")
                    try:
                        shared_status = StorageAvailability(raw_status)
                    except ValueError:
                        raise RuntimeError(
                            "MemoryBackendUnavailable:shared_runtime=failed,"
                            "error_type=InvalidHealthStatus"
                        ) from None
                    if shared_status not in {
                        StorageAvailability.HEALTHY,
                        StorageAvailability.DEGRADED,
                    }:
                        error_type = self._storage_health_error_type(shared_health)
                        raise RuntimeError(
                            "MemoryBackendUnavailable:"
                            f"shared_runtime={shared_status.value},"
                            f"error_type={error_type}"
                        ) from None

                    try:
                        readiness = await inspect_mysql_memory_readiness(runtime)
                    except MySQLMemoryReadinessProbeError as exc:
                        raise RuntimeError(
                            "MemoryBackendUnavailable:shared_runtime=failed,"
                            f"error_type={exc.error_type}"
                        ) from None
            except TimeoutError:
                raise RuntimeError(
                    "MemoryBackendUnavailable:shared_runtime=failed,"
                    "error_type=TimeoutError"
                ) from None

            failed = [
                f"{name}={readiness.get(name, StorageAvailability.FAILED).value}"
                for name in names
                if readiness.get(name, StorageAvailability.FAILED)
                not in {
                    StorageAvailability.HEALTHY,
                    StorageAvailability.DEGRADED,
                }
            ]
            if failed:
                raise RuntimeError("MemoryBackendUnavailable:" + ",".join(failed))
            return

        statuses = await asyncio.gather(
            *(getattr(storage, name).availability() for name in names)
        )
        failed = [
            f"{name}={status.value}"
            for name, status in zip(names, statuses, strict=True)
            if status not in {StorageAvailability.HEALTHY, StorageAvailability.DEGRADED}
        ]
        if failed:
            raise RuntimeError("MemoryBackendUnavailable:" + ",".join(failed))

    @staticmethod
    def _storage_health_error_type(health: dict[str, Any]) -> str:
        """Extract only a bounded exception class name from shared health metadata."""
        candidates = [health.get("error_type")]
        for component in ("backend_health", "authority_health"):
            detail = health.get(component)
            if isinstance(detail, dict):
                candidates.append(detail.get("error_type"))
        for candidate in candidates:
            value = str(candidate or "")
            if value.isascii() and value.isidentifier() and len(value) <= 64:
                return value
        return "Unavailable"

    async def _create_tables(self) -> None:
        """创建数据库表。"""

        def _do_db_work() -> None:
            cursor = self._db.cursor()

            # 记忆节点表
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_nodes (
                    node_id TEXT PRIMARY KEY,
                    node_type TEXT NOT NULL,
                    file_path TEXT,
                    content_hash TEXT,
                    title TEXT,
                    activation_strength REAL DEFAULT 1.0,
                    access_count INTEGER DEFAULT 0,
                    last_accessed_at REAL,
                    emotional_valence REAL DEFAULT 0.0,
                    emotional_arousal REAL DEFAULT 0.0,
                    importance REAL DEFAULT 0.5,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    embedding_synced INTEGER DEFAULT 0
                )
                """
            )

            # 记忆边表
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_edges (
                    edge_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    weight REAL DEFAULT 0.5,
                    base_strength REAL DEFAULT 0.5,
                    reinforcement REAL DEFAULT 0.0,
                    activation_count INTEGER DEFAULT 0,
                    last_activated_at REAL,
                    reason TEXT,
                    created_at REAL NOT NULL,
                    bidirectional INTEGER DEFAULT 1,
                    FOREIGN KEY (source_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
                    FOREIGN KEY (target_id) REFERENCES memory_nodes(node_id) ON DELETE CASCADE,
                    UNIQUE(source_id, target_id, edge_type)
                )
                """
            )

            # 索引
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_nodes_type ON memory_nodes(node_type)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_nodes_activation ON memory_nodes(activation_strength DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_nodes_file_path ON memory_nodes(file_path)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_edges_source ON memory_edges(source_id, weight DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_edges_target ON memory_edges(target_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_edges_type ON memory_edges(edge_type)"
            )

            # 数据库级自环防线：任何路径（含手工 SQL）都不得写入自指边。
            cursor.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memory_edges_no_self_loop_insert
                BEFORE INSERT ON memory_edges
                WHEN NEW.source_id = NEW.target_id
                BEGIN
                    SELECT RAISE(ABORT, 'MemoryEdgeSelfLoop');
                END
                """
            )
            cursor.execute(
                """
                CREATE TRIGGER IF NOT EXISTS memory_edges_no_self_loop_update
                BEFORE UPDATE ON memory_edges
                WHEN NEW.source_id = NEW.target_id
                BEGIN
                    SELECT RAISE(ABORT, 'MemoryEdgeSelfLoop');
                END
                """
            )

            # 显式修正记录表：不删除旧记忆，只记录“后来如何理解”
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_corrections (
                    correction_id TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    message TEXT NOT NULL,
                    source TEXT DEFAULT 'user',
                    created_at REAL NOT NULL,
                    related_node_id TEXT,
                    query TEXT DEFAULT '',
                    stream_id TEXT,
                    FOREIGN KEY (related_node_id) REFERENCES memory_nodes(node_id) ON DELETE SET NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_corrections_topic ON memory_corrections(topic)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_corrections_related ON memory_corrections(related_node_id)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_corrections_created ON memory_corrections(created_at DESC)"
            )

            # 全文搜索虚拟表（存储文件内容摘要）
            cursor.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    node_id,
                    title,
                    content,
                    tokenize='unicode61'
                )
                """
            )

            self._db.commit()

        await run_db(_do_db_work)
        logger.debug("记忆数据库表创建完成")

    async def _startup_recovery(self) -> None:
        """启动时修复：补 embedding 入队缺口、补索引缺口、清理 ghost 节点和孤立边。

        这一步的工作量与工作区规模成正比——三次全表扫描、一次目录树遍历、
        每个待补文件一次读取。原实现全部直接跑在事件循环上，启动期间整个
        进程（适配器收发、心跳、调度）都在等它。现在读查询走只读连接、
        文件 IO 走线程、写入走写连接，事件循环在整个恢复过程中保持可用。
        """
        await self._startup_recovery_via_ports()

    async def _run_startup_recovery_owned(self, *, propagate_failure: bool) -> None:
        """Run one recovery generation with content-free lifecycle diagnostics."""

        self._startup_recovery_progress = _StartupRecoveryProgress(
            status="running",
            phase="scan",
            started_at=datetime.now().astimezone().isoformat(),
        )
        try:
            await self._startup_recovery()
        except asyncio.CancelledError:
            progress = self._startup_recovery_progress
            progress.status = "cancelled"
            progress.phase = "cancelled"
            progress.finished_at = datetime.now().astimezone().isoformat()
            logger.info(
                "Memory workspace recovery cancelled: "
                f"documents={progress.processed_documents}/{progress.total_documents} "
                f"artifacts={progress.artifact_processed}/{progress.artifact_total}"
            )
            raise
        except Exception as exc:  # noqa: BLE001 - background failure is degraded
            progress = self._startup_recovery_progress
            progress.status = "failed"
            progress.phase = "failed"
            progress.finished_at = datetime.now().astimezone().isoformat()
            progress.error_type = self._startup_recovery_error_type(exc)
            logger.error(
                "Memory workspace recovery failed; authority remains available: "
                f"error_type={progress.error_type} "
                f"documents={progress.processed_documents}/{progress.total_documents} "
                f"artifacts={progress.artifact_processed}/{progress.artifact_total}"
            )
            if propagate_failure:
                raise
        else:
            progress = self._startup_recovery_progress
            progress.status = "completed"
            progress.phase = "completed"
            progress.finished_at = datetime.now().astimezone().isoformat()
            logger.info(
                "Memory workspace recovery completed: "
                f"documents={progress.processed_documents}/{progress.total_documents} "
                f"indexed={progress.indexed_documents} "
                f"requeued={progress.requeued_documents} "
                f"artifacts={progress.artifact_versions_appended}"
            )

    @staticmethod
    def _startup_recovery_error_type(exc: BaseException) -> str:
        """Return one bounded leaf exception class for structured concurrency."""

        current = exc
        while isinstance(current, BaseExceptionGroup) and current.exceptions:
            current = current.exceptions[0]
        candidate = type(current).__name__
        if candidate.isascii() and candidate.isidentifier() and len(candidate) <= 64:
            return candidate
        return "RecoveryError"

    def _start_background_startup_recovery(self) -> None:
        """Start one MySQL recovery task without delaying plugin availability."""

        current = self._startup_recovery_task
        if current is not None and not current.done():
            return
        self._startup_recovery_progress = _StartupRecoveryProgress(
            status="scheduled",
            phase="scheduled",
            started_at=datetime.now().astimezone().isoformat(),
        )
        task_info = get_task_manager().create_task(
            self._run_startup_recovery_owned(propagate_failure=False),
            name="life_memory_startup_recovery",
            daemon=True,
        )
        if task_info.task is None:
            raise RuntimeError("MemoryStartupRecoveryTaskUnavailable")
        self._startup_recovery_task = task_info.task

    async def _cancel_startup_recovery(self) -> None:
        """Cancel and join the exact service-owned recovery task, if any."""

        task = self._startup_recovery_task
        self._startup_recovery_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _startup_recovery_via_ports(self) -> None:
        """Reconcile workspace documents through the selected backend ports."""

        workspace = self._get_workspace_path()
        storage = self._require_memory_storage()
        scan = await asyncio.to_thread(scan_workspace_documents, workspace)
        projection_context = await self._prepare_workspace_projection(scan=scan)
        workspace_paths = {document.path for document in scan.documents}
        indexed_nodes = await storage.document_index.list_indexed_documents()
        indexed = {
            str(node.file_path): node for node in indexed_nodes if node.file_path
        }
        loaded: dict[str, tuple[str, float]] = {}
        mtimes = {document.path: document.source_mtime for document in scan.documents}
        paths = [document.path for document in scan.documents]
        progress = self._startup_recovery_progress
        progress.phase = "document_index"
        progress.total_documents = len(paths)
        write_concurrency = (
            _MYSQL_RECOVERY_WRITE_CONCURRENCY
            if storage.backend == BackendKind.MYSQL
            else 1
        )
        logger.info(
            "Memory workspace recovery started: "
            f"documents={len(paths)} write_concurrency={write_concurrency}"
        )
        write_semaphore = (
            asyncio.Semaphore(write_concurrency) if write_concurrency > 1 else None
        )

        async def _reconcile_document(path: str, content: str) -> None:
            source_mtime = mtimes[path]
            loaded[path] = (content, source_mtime)
            digest = compute_content_hash(content) if content else ""
            node = indexed.get(path)
            if node is None:
                await storage.document_index.upsert_document(
                    path,
                    content,
                    Path(path).stem,
                    source_mtime,
                )
                progress.indexed_documents += 1
            elif node.node_id != generate_file_node_id(path):
                progress.legacy_documents += 1
                legacy_hash = str(node.content_hash or "")
                if not node.embedding_synced and legacy_hash:
                    await storage.document_index.enqueue_job(
                        node.node_id,
                        legacy_hash,
                    )
                    progress.requeued_documents += 1
                logger.warning(
                    "启动扫描保留非规范 legacy 文档身份，等待显式迁移: "
                    f"path={path} node_id={node.node_id}"
                )
            elif (
                str(node.content_hash or "") != digest
                or str(node.fts_content_hash or "") != digest
                or bool(node.legacy_fts_present)
                or (
                    node.embedding_synced
                    and str(node.embedding_content_hash or "") != digest
                )
            ):
                await storage.document_index.upsert_document(
                    path,
                    content,
                    Path(path).stem,
                    source_mtime,
                )
                progress.indexed_documents += 1
            elif not node.embedding_synced and digest:
                await storage.document_index.enqueue_job(node.node_id, digest)
                progress.requeued_documents += 1
            else:
                progress.unchanged_documents += 1
            progress.processed_documents += 1
            if progress.processed_documents % 64 == 0:
                logger.debug(
                    "Memory workspace recovery progress: "
                    f"documents={progress.processed_documents}/"
                    f"{progress.total_documents} indexed={progress.indexed_documents} "
                    f"requeued={progress.requeued_documents}"
                )

        async def _bounded_reconcile(path: str, content: str) -> None:
            if write_semaphore is None:
                await _reconcile_document(path, content)
                return
            async with write_semaphore:
                await _reconcile_document(path, content)

        for start in range(0, len(paths), _RECOVERY_READ_BATCH):
            batch = paths[start : start + _RECOVERY_READ_BATCH]
            documents = await asyncio.to_thread(_read_documents, workspace, batch)
            failed_reads = len(batch) - len(documents)
            if failed_reads:
                progress.read_failures += failed_reads
                progress.processed_documents += failed_reads
            if write_concurrency == 1:
                for path, content in documents:
                    await _reconcile_document(path, content)
            else:
                async with asyncio.TaskGroup() as task_group:
                    for path, content in documents:
                        task_group.create_task(_bounded_reconcile(path, content))

        missing_node_ids = [
            indexed[path].node_id for path in sorted(set(indexed) - workspace_paths)
        ]
        progress.phase = "projection_cleanup"
        progress.ghost_documents = len(missing_node_ids)
        if missing_node_ids:
            # Absence in a scan is not deletion evidence. A second workspace,
            # a partial mount or a transient read failure must never create
            # authority-looking tombstones or erase a searchable projection.
            logger.warning(
                "Memory workspace recovery retained scan-absent documents: "
                f"count={len(missing_node_ids)}; explicit deletion evidence required"
            )
        orphaned = await storage.legacy_graph.prune_orphan_edges()
        if orphaned:
            logger.info(f"启动清理：删除 {orphaned} 条孤立边")

        versioned = await self._reconcile_workspace_artifact_versions_via_ports(
            loaded,
            workspace_paths,
        )
        if versioned:
            logger.info(f"启动扫描：记忆版本账本追加 {versioned} 个观察版本")
        if projection_context is not None:
            identity, binding, _permit = projection_context
            await self._commit_workspace_projection_inventory(identity, binding)

    @staticmethod
    async def _refresh_artifact_head(
        living: Any,
        logical_key: str,
    ) -> tuple[MemoryArtifactVersion | None, ArtifactHead | None]:
        head = await living.get_artifact_head(logical_key)
        if head is None:
            return None, None
        history = await living.list_artifact_history(logical_key)
        version = next(
            (item for item in history if item.artifact_id == head.artifact_id),
            None,
        )
        if version is None:
            raise ArtifactHeadConflict(
                f"artifact head references missing version for {logical_key!r}"
            )
        return version, head

    async def _append_workspace_observation(
        self,
        *,
        living: Any,
        logical_key: str,
        content: str,
        source_mtime: float | None,
        current_version: MemoryArtifactVersion | None,
        current_head: ArtifactHead | None,
    ) -> bool:
        for attempt in range(2):
            if current_version is not None and (
                current_version.artifact_kind == "workspace_memory_document"
                and current_version.content == content
            ):
                return False
            observation = (
                "startup_baseline"
                if current_version is None
                else "startup_observed_change"
            )
            valid_from = (
                datetime.fromtimestamp(source_mtime).astimezone().isoformat()
                if source_mtime is not None
                else ""
            )
            version = new_artifact_version(
                logical_key=logical_key,
                artifact_kind="workspace_memory_document",
                content=content,
                parent_artifact_ids=(current_version.artifact_id,)
                if current_version is not None
                else (),
                authored_by="workspace_reconciler",
                consciousness_instance_id="life_engine",
                valid_from=valid_from,
                metadata={
                    "observation": observation,
                    "source_mtime": source_mtime,
                },
            )
            derivations: tuple[MemoryDerivation, ...] = ()
            if current_version is not None:
                derivations = (
                    MemoryDerivation(
                        derivation_id=f"derivation_{uuid.uuid4().hex}",
                        generated_artifact_id=version.artifact_id,
                        used_artifact_id=current_version.artifact_id,
                        predicate="workspace_change_observed",
                        reason="启动时观察到工作区内容与已知版本不同",
                        actor="workspace_reconciler",
                        recorded_at=version.recorded_at,
                    ),
                )
            try:
                await living.append_artifact(
                    version,
                    derivations=derivations,
                    expected_head_revision=current_head.revision
                    if current_head is not None
                    else 0,
                )
                return True
            except ArtifactHeadConflict:
                if attempt == 1:
                    raise
                current_version, current_head = await self._refresh_artifact_head(
                    living,
                    logical_key,
                )
                logger.info(
                    "启动版本对账检测到并发 head 推进，已刷新后重试一次: "
                    f"path={logical_key}"
                )
        return False

    async def _reconcile_workspace_artifact_versions_via_ports(
        self,
        loaded: dict[str, tuple[str, float]],
        workspace_paths: set[str],
    ) -> int:
        del workspace_paths  # Scan absence is not an authorized deletion event.
        living = self._require_memory_storage().living
        head_records = await living.list_artifact_heads()
        heads = {version.logical_key: (version, head) for version, head in head_records}
        progress = self._startup_recovery_progress
        progress.phase = "artifact_versions"
        progress.artifact_total = len(loaded)
        appended = 0
        for logical_key, (content, source_mtime) in loaded.items():
            current = heads.get(logical_key)
            appended += int(
                await self._append_workspace_observation(
                    living=living,
                    logical_key=logical_key,
                    content=content,
                    source_mtime=source_mtime,
                    current_version=current[0] if current is not None else None,
                    current_head=current[1] if current is not None else None,
                )
            )
            progress.artifact_processed += 1
            progress.artifact_versions_appended = appended
            if progress.artifact_processed % 64 == 0:
                logger.debug(
                    "Memory artifact recovery progress: "
                    f"artifacts={progress.artifact_processed}/"
                    f"{progress.artifact_total} appended={appended}"
                )

        return appended

    async def _reconcile_workspace_artifact_versions(
        self,
        documents: Sequence[Any],
        workspace_paths: set[str],
        *,
        indexed_hashes: dict[str, str] | None = None,
    ) -> int:
        """Append artifact history and refresh externally changed search rows."""

        loaded: dict[str, tuple[str, float]] = {}
        workspace = self._get_workspace_path()
        for document in documents:
            try:
                content, source_mtime, _size = await asyncio.to_thread(
                    read_workspace_document,
                    workspace,
                    document.path,
                )
            except (FileNotFoundError, OSError, ValueError):
                continue
            loaded[document.path] = (content, source_mtime)
        return await self._reconcile_workspace_artifact_versions_via_ports(
            loaded,
            workspace_paths,
        )

    def _require_local_db(self) -> sqlite3.Connection:
        if self._db is None or self._closing:
            raise RuntimeError("记忆服务尚未初始化或正在关闭")
        return self._db

    @property
    def available(self) -> bool:
        """Whether the service can currently serve public memory operations."""

        return (
            self._memory_storage is not None and self._initialized and not self._closing
        )

    async def read_graph_projection(
        self,
        *,
        limit_nodes: int = 80,
        min_weight: float = 0.15,
        focus_id: str | None = None,
    ) -> Dict[str, Any]:
        """Return the rebuildable legacy graph through a public service boundary."""

        return await self._require_memory_storage().document_index.graph_snapshot(
            limit_nodes=max(10, min(int(limit_nodes), 200)),
            min_weight=max(0.0, min(float(min_weight), 1.0)),
            focus_id=focus_id,
        )

    async def read_file_lineage_projection(
        self,
        file_path: str,
    ) -> Dict[str, Any] | None:
        """Return file evolution through the public backend-neutral boundary."""

        graph = self._require_memory_storage().legacy_graph
        node = await graph.get_node_by_file_path(file_path)
        if node is None:
            return None
        outgoing, incoming, corrections = await asyncio.gather(
            graph.get_edges_from(node.node_id, 0.0),
            graph.get_edges_to(node.node_id, 0.0),
            graph.list_corrections(
                related_node_ids=(node.node_id,),
                limit=10,
            ),
        )
        evolution_trace: list[dict[str, Any]] = []
        for direction, edges in (("later", outgoing), ("earlier", incoming)):
            for edge in edges:
                related_id = edge.target_id if direction == "later" else edge.source_id
                related = await graph.get_node_by_id(related_id)
                if related is None or not related.file_path:
                    continue
                evolution_trace.append(
                    {
                        "direction": direction,
                        "relation": edge.edge_type.value,
                        "file_path": related.file_path,
                        "title": related.title,
                        "reason": edge.reason,
                        "weight": round(edge.weight, 2),
                    }
                )
        corrections_data = [
            {
                "topic": item.topic,
                "message": item.message,
                "source": item.source,
                "created_at": item.created_at,
            }
            for item in corrections
        ]
        if not evolution_trace and not corrections_data:
            return None
        return {
            "evolution_trace": evolution_trace,
            "corrections": corrections_data,
            "has_history": True,
        }

    async def read_lineage_edges(
        self,
        node_id: str,
        *,
        min_weight: float = 0.0,
    ) -> tuple[List[MemoryEdge], List[MemoryEdge]]:
        """Read outgoing and incoming legacy lineage edges without exposing DB."""

        graph = self._require_memory_storage().legacy_graph
        outgoing, incoming = await asyncio.gather(
            graph.get_edges_from(node_id, min_weight),
            graph.get_edges_to(node_id, min_weight),
        )
        return outgoing, incoming

    async def read_memory_corrections(
        self,
        *,
        query: str = "",
        related_node_ids: Sequence[str] = (),
        limit: int = 20,
    ) -> List[MemoryCorrection]:
        """Read correction history through the public storage port."""

        return await self._require_memory_storage().legacy_graph.list_corrections(
            query=query,
            related_node_ids=related_node_ids,
            limit=limit,
        )

    def _require_memory_storage(self) -> MemoryStorageBundle:
        storage = self._memory_storage
        if storage is None or self._closing:
            raise RuntimeError("记忆存储尚未初始化或正在关闭")
        return storage

    async def read_chunk_index_state(self) -> ChunkIndexState | None:
        """返回权威 chunk 索引配置（model/dimension）。

        投影 frontier 的 config_digest 必须让空批次（report 无 model/dimension）
        观察到与真实批次相同的配置；本方法提供该权威来源。生产 MySQL 后端
        通过 ``document_index.read_chunk_index_state()`` 读取 memory_index_state。
        """
        return await self._require_memory_storage().document_index.read_chunk_index_state()

    async def close(self) -> None:
        """幂等释放 Memory 自有资源；注入的 coherent runtime 由 Life Engine 关闭。"""
        async with self._lifecycle_lock:
            if self._db is None and self._memory_storage is None:
                self._initialized = False
                self._clear_cached_collections()
                return

            self._closing = True
            try:
                await self._cancel_startup_recovery()
                async with self._index_worker_lock:
                    async with self._index_write_lock:
                        db = self._db
                        self._memory_storage = None
                        self._db = None
                        # 读连接必须先于写连接释放：它们指向同一个文件，
                        # 留着会让 WAL 无法 checkpoint。
                        bind_reader_pool(None)
                        if db is not None:
                            await run_db(db.close)
            finally:
                self._initialized = False
                self._closing = False
                self._workspace_projection_binding = None
                self._workspace_projection_permit = None
                self._clear_cached_collections()

    def _clear_cached_collections(self) -> None:
        self._chroma_collection = None
        self._chunk_collection = None
        self._chunk_collection_identity = None
        self._chunk_index_state = None
        self._chunk_collection_candidate = None
        self._chunk_collection_candidate_identity = None

    async def upsert_document(
        self,
        path: str,
        content: str,
        title: str = "",
        source_mtime: float | None = None,
        *,
        max_chars: int | None = None,
        overlap_chars: int | None = None,
    ) -> DocumentIndexResult:
        """统一写入文档节点、分块 FTS 和待处理 outbox。"""
        return await self._require_memory_storage().document_index.upsert_document(
            path,
            content,
            title,
            source_mtime,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )

    async def delete_document(self, path: str) -> bool:
        """删除文档及其 SQLite 索引、分块和 outbox 记录。"""
        storage = self._require_memory_storage()
        if storage.backend == BackendKind.MYSQL:
            raise WorkspaceProjectionDeleteEvidenceError(
                "selected workspace deletion requires an occurrence-bound "
                "audited deletion port"
            )
        return await storage.document_index.delete_document(path)

    async def move_document(self, old_path: str, new_path: str) -> bool:
        """移动文档索引；目标已有节点时明确拒绝合并。"""
        storage = self._require_memory_storage()
        if storage.backend == BackendKind.MYSQL:
            raise WorkspaceProjectionDeleteEvidenceError(
                "selected workspace moves require an occurrence-bound audited port"
            )
        return await storage.document_index.move_document(old_path, new_path)

    async def enqueue_index_job(self, node_id: str, content_hash: str) -> str:
        """加入一个待处理索引任务，不触发 embedding 或网络请求。"""
        async with self._index_write_lock:
            return await self._require_memory_storage().document_index.enqueue_job(
                node_id,
                content_hash,
            )

    async def process_index_jobs(self, limit: int = 10) -> List[IndexJob]:
        """领取待处理任务，交给外部 worker；本方法不执行 embedding。"""
        return await self._require_memory_storage().document_index.claim_jobs(
            limit=limit
        )

    async def run_index_worker(
        self,
        limit: int = 10,
        *,
        collection: Any = None,
        embed_texts_func: Any = None,
        collection_upsert_func: Any = None,
        retry_failed: bool = True,
        reclaim_after: float | None = DEFAULT_RECLAIM_AFTER,
    ) -> IndexWorkerReport:
        """执行一批 chunk embedding 任务，不阻塞并发文档更新。"""

        async with self._index_worker_lock:
            worker_task = asyncio.create_task(
                self._require_memory_storage().document_index.run_index_worker(
                    limit=limit,
                    collection=collection,
                    embed_texts_func=embed_texts_func,
                    collection_resolver=self._resolve_chunk_collection,
                    collection_upsert_func=collection_upsert_func,
                    retry_failed=retry_failed,
                    reclaim_after=reclaim_after,
                )
            )
            try:
                report = await asyncio.shield(worker_task)
            except asyncio.CancelledError:
                await worker_task
                raise
            if report.completed:
                active_collection = collection or self._chunk_collection_candidate
                if active_collection is not None:
                    self._chunk_collection = active_collection
                    self._chunk_collection_identity = (
                        report.model_name,
                        report.dimension,
                    )
            active_collection = collection or self._chunk_collection
            if active_collection is not None:
                await self._require_memory_storage().document_index.consume_vector_tombstones(
                    active_collection
                )
        return report

    async def list_index_jobs(
        self,
        status: str = "pending",
        limit: int = 100,
    ) -> List[IndexJob]:
        """读取 outbox 状态，供 worker 或测试观察。"""
        return await self._require_memory_storage().document_index.list_jobs(
            status=status,
            limit=limit,
        )

    async def set_index_job_status(
        self,
        job_id: str,
        status: str,
        error: str = "",
    ) -> bool:
        """更新外部索引 worker 的任务状态。"""
        return await self._require_memory_storage().document_index.set_job_status(
            job_id,
            status,
            error=error,
        )

    # --------------------------------------------------------
    # 生命记忆本体：不可变经历与第一人称见证
    # --------------------------------------------------------

    # --------------------------------------------------------
    # 认识论本体：主张、证据、信念与状态事件
    # --------------------------------------------------------

    async def append_memory_claim(self, claim: MemoryClaim) -> MemoryClaim:
        """追加主张；主张正文不可覆盖，后续认识只通过状态事件表达。"""
        return await self._require_memory_storage().epistemic.append_claim(claim)

    async def record_retrieval_episode(
        self,
        episode: RetrievalEpisode,
    ) -> RetrievalEpisode:
        """记录一次检索上下文；候选曝光不是事实证据。"""
        return await self._require_memory_storage().epistemic.append_retrieval_episode(
            episode
        )

    async def record_retrieval_exposure(
        self,
        exposure: RetrievalExposure,
    ) -> RetrievalExposure:
        """记录候选被展示；不自动建立语义边或提高事实状态。"""
        return await self._require_memory_storage().epistemic.append_retrieval_exposure(
            exposure
        )

    async def record_retrieval_feedback(
        self,
        feedback: RetrievalFeedback,
    ) -> RetrievalFeedback:
        """追加主体对候选的采用、拒绝或修正反馈。"""
        return await self._require_memory_storage().epistemic.append_retrieval_feedback(
            feedback
        )

    async def get_retrieval_plasticity(
        self,
        entity_type: str,
        entity_id: str,
    ) -> RetrievalPlasticity:
        """读取仅用于候选排序的可塑性提示，不替代认识论判断。"""
        return await self._require_memory_storage().epistemic.get_retrieval_plasticity(
            entity_type,
            entity_id,
        )

    async def record_epistemic_claim(
        self,
        *,
        subject_key: str,
        content: str,
        claim_kind: str,
        source: str,
        authority: str = "",
        valid_from: str = "",
        valid_to: str = "",
        stream_scope: str = "",
        visibility: str = "private",
        consciousness_instance_id: str = "",
        metadata: Dict[str, Any] | None = None,
        claim_id: str = "",
        recorded_at: str = "",
    ) -> MemoryClaim:
        """兼容式构造并追加一个新主张；不会由文本相似度合并旧主张。"""
        claim = new_claim(
            subject_key=subject_key,
            content=content,
            claim_kind=claim_kind,
            source=source,
            authority=authority,
            valid_from=valid_from,
            valid_to=valid_to,
            stream_scope=stream_scope,
            visibility=visibility,
            consciousness_instance_id=consciousness_instance_id,
            metadata=metadata,
            claim_id=claim_id,
            recorded_at=recorded_at,
        )
        return await self.append_memory_claim(claim)

    async def append_claim_evidence(self, evidence: ClaimEvidence) -> ClaimEvidence:
        """追加一条支持、挑战或语境证据，不将其折算为真值分数。"""
        return await self._require_memory_storage().epistemic.append_evidence(evidence)

    async def append_memory_belief(self, belief: MemoryBelief) -> MemoryBelief:
        """登记一个意识视角与主张的关系；认可状态由事件另行记录。"""
        return await self._require_memory_storage().epistemic.append_belief(belief)

    async def append_epistemic_conflict(
        self,
        conflict: EpistemicConflict,
    ) -> EpistemicConflict:
        """登记冲突而不擅自裁决两个主张中的任何一个。"""
        return await self._require_memory_storage().epistemic.append_conflict(conflict)

    async def append_memory_state_event(
        self,
        event: MemoryStateEvent,
    ) -> MemoryStateEvent:
        """追加可审计状态变化；权限校验和撤销关系由本体层强制。"""
        return await self._require_memory_storage().epistemic.append_state_event(event)

    async def get_memory_disposition(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> MemoryDisposition:
        """读取主体性遗忘状态；它不影响原始记录的保留与审计。"""
        return await self._require_memory_storage().epistemic.get_disposition(
            entity_type,
            entity_id,
            recorded_as_of=recorded_as_of,
        )

    async def get_memory_claim_state(
        self,
        claim_id: str,
        *,
        recorded_as_of: str = "",
    ) -> ClaimState | None:
        """从完整事件历史还原一个主张在指定记录时点的状态。"""
        return await self._require_memory_storage().epistemic.get_claim_state(
            claim_id,
            recorded_as_of=recorded_as_of,
        )

    async def list_memory_claim_states(
        self,
        subject_key: str,
        *,
        recorded_as_of: str = "",
        valid_at: str = "",
        stream_scope: str | None = None,
        visibility: tuple[str, ...] = ("private",),
    ) -> List[ClaimState]:
        """双时间查询主张状态，默认不跨私有流。"""
        return await self._require_memory_storage().epistemic.list_claim_states(
            subject_key,
            recorded_as_of=recorded_as_of,
            valid_at=valid_at,
            stream_scope=stream_scope,
            visibility=visibility,
        )

    async def project_current_memory_facts(
        self,
        subject_key: str,
        *,
        valid_at: str,
        recorded_as_of: str = "",
        stream_scope: str | None = None,
        visibility: tuple[str, ...] = ("private",),
    ) -> CurrentFactProjection:
        """重建一个双时间当前事实投影，冲突和不确定性始终保留。"""
        return await self._require_memory_storage().epistemic.project_current_facts(
            subject_key,
            valid_at=valid_at,
            recorded_as_of=recorded_as_of,
            stream_scope=stream_scope,
            visibility=visibility,
        )

    async def get_memory_audit_trail(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> List[MemoryAuditEntry]:
        """返回事件、操作者、理由、因果来源与补偿关系的完整审计轨迹。"""
        return await self._require_memory_storage().epistemic.get_audit_trail(
            entity_type,
            entity_id,
            recorded_as_of=recorded_as_of,
        )

    async def list_memory_state_events(
        self,
        entity_type: str,
        entity_id: str,
        *,
        recorded_as_of: str = "",
    ) -> List[MemoryStateEvent]:
        """读取完整事件轨迹，供审计、回放和解释使用。"""
        return await self._require_memory_storage().epistemic.list_state_events(
            entity_type,
            entity_id,
            recorded_as_of=recorded_as_of,
        )

    async def list_memory_claim_evidence(
        self,
        claim_id: str,
    ) -> List[ClaimEvidence]:
        """读取一个主张的完整证据链。"""
        return await self._require_memory_storage().epistemic.list_claim_evidence(
            claim_id
        )

    # --------------------------------------------------------
    # 生命记忆本体：不可变经历与第一人称见证
    # --------------------------------------------------------

    async def append_experiences(
        self,
        records: List[ExperienceRecord],
    ) -> int:
        """幂等追加不可变经历证据，绝不覆盖已有事件。"""
        report = await self._require_memory_storage().experiences.append(records)
        return report.inserted_count

    async def append_experiences_detailed(
        self,
        records: List[ExperienceRecord],
    ) -> ExperienceAppendReport:
        """Append occurrences and expose only canonical new evidence."""

        return await self._require_memory_storage().experiences.append(records)

    async def record_memory_artifact_version(
        self,
        version: MemoryArtifactVersion,
        *,
        derivations: Sequence[MemoryDerivation] = (),
        expected_head_revision: int | None = None,
    ) -> MemoryArtifactVersion:
        """Append one immutable memory artifact version and provenance."""

        living = self._require_memory_storage().living
        if expected_head_revision is None:
            head = await living.get_artifact_head(version.logical_key)
            expected_head_revision = head.revision if head is not None else 0
        return await living.append_artifact(
            version,
            derivations=derivations,
            expected_head_revision=expected_head_revision,
        )

    async def version_memory_artifact(
        self,
        *,
        logical_key: str,
        artifact_kind: str,
        content: str,
        authored_by: str = "",
        consciousness_instance_id: str = "",
        stream_scope: str = "",
        visibility: str = "private",
        valid_from: str = "",
        predicate: str = "revises",
        reason: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> MemoryArtifactVersion:
        """Create a new head while preserving the previous artifact version."""

        async with self._index_write_lock:
            living = self._require_memory_storage().living
            head_state = await living.get_artifact_head(logical_key)
            history = await living.list_artifact_history(logical_key)
            head = next(
                (
                    item
                    for item in history
                    if head_state is not None
                    and item.artifact_id == head_state.artifact_id
                ),
                None,
            )
            parents = (head.artifact_id,) if head is not None else ()
            version = new_artifact_version(
                logical_key=logical_key,
                artifact_kind=artifact_kind,
                content=content,
                parent_artifact_ids=parents,
                authored_by=authored_by,
                consciousness_instance_id=consciousness_instance_id,
                stream_scope=stream_scope,
                visibility=visibility,
                valid_from=valid_from,
                metadata=metadata or {},
            )
            derivations: tuple[MemoryDerivation, ...] = ()
            if head is not None:
                derivations = (
                    MemoryDerivation(
                        derivation_id=f"derivation_{uuid.uuid4().hex}",
                        generated_artifact_id=version.artifact_id,
                        used_artifact_id=head.artifact_id,
                        predicate=predicate,
                        reason=reason,
                        actor=authored_by,
                        recorded_at=version.recorded_at,
                    ),
                )
            return await living.append_artifact(
                version,
                derivations=derivations,
                expected_head_revision=(
                    head_state.revision if head_state is not None else 0
                ),
            )

    async def get_memory_artifact_history(
        self,
        logical_key: str,
    ) -> List[MemoryArtifactVersion]:
        """Return every immutable version of one logical memory artifact."""

        return await self._require_memory_storage().living.list_artifact_history(
            logical_key
        )

    async def record_memory_interpretation(
        self,
        interpretation: MemoryInterpretation,
        *,
        sources: Sequence[InterpretationSource] = (),
    ) -> MemoryInterpretation:
        """Append one subject-authored interpretation and its sources."""

        return await self._require_memory_storage().living.append_interpretation(
            interpretation,
            sources=sources,
        )

    async def record_memory_semantic_relation(
        self,
        relation: SemanticRelation,
    ) -> SemanticRelation:
        """Append an explicit relation without a closed predicate taxonomy."""

        return await self._require_memory_storage().living.append_relation(relation)

    async def list_memory_semantic_relations(
        self,
        entity_ref: str,
    ) -> List[SemanticRelation]:
        """Return every explicit relation touching one memory entity."""

        return await self._require_memory_storage().living.list_relations(entity_ref)

    async def list_memory_interpretations(
        self,
        subject_id: str,
        *,
        recorded_as_of: str = "",
    ) -> List[MemoryInterpretation]:
        """Read the interpretation history available at a recorded time."""

        return await self._require_memory_storage().living.list_interpretations(
            subject_id,
            recorded_as_of=recorded_as_of,
        )

    async def search_memory_interpretations(
        self,
        query: str,
        *,
        top_k: int = 5,
        stream_scope: str | None = None,
        visibility: tuple[str, ...] = ("private",),
        recorded_as_of: str = "",
    ) -> List[InterpretationSearchResult]:
        """Search subject-authored interpretations with their source trace."""

        return await self._require_memory_storage().living.search_interpretations(
            query,
            top_k=top_k,
            stream_scope=stream_scope,
            visibility=visibility,
            recorded_as_of=recorded_as_of,
        )

    async def get_memory_interpretation(
        self,
        interpretation_id: str,
    ) -> tuple[MemoryInterpretation, tuple[InterpretationSource, ...]] | None:
        """Read one interpretation and its provenance by stable identity."""

        return await self._require_memory_storage().living.get_interpretation(
            interpretation_id
        )

    async def select_memory_association_neighbours(
        self,
        seed_refs: Sequence[str],
        *,
        context_key: str,
        random_seed: int,
        limit: int,
    ) -> List[AssociationSelection]:
        """Select replayable contextual neighbours across memory entity kinds."""

        return (
            await self._require_memory_storage().living.choose_association_neighbours(
                seed_refs,
                context_key=context_key,
                random_seed=random_seed,
                limit=limit,
            )
        )

    async def begin_memory_recall(
        self,
        **kwargs: Any,
    ) -> RecallEpisode:
        """Start a replayable recall episode with an open retrieval intent."""

        return await self._require_memory_storage().living.begin_recall(**kwargs)

    async def append_memory_recall_events(
        self,
        events: Sequence[RecallEvent],
    ) -> tuple[RecallEvent, ...]:
        """Append objective and subject-authored traces for one recall."""

        return await self._require_memory_storage().living.append_recall_events(events)

    async def append_memory_corecall(
        self,
        event: CoRecallEvent,
    ) -> CoRecallEvent:
        """Append a contextual co-recall hyperedge and update its projection."""

        return await self._require_memory_storage().living.append_corecall(event)

    async def list_memory_association_evidence(
        self,
        entity_ref: str,
        *,
        context_key: str | None = None,
    ) -> List[AssociationEvidence]:
        """Return separate contextual association dimensions."""

        return await self._require_memory_storage().living.list_association_evidence(
            entity_ref,
            context_key=context_key,
        )

    async def rebuild_memory_association_projection(self) -> int:
        """Rebuild derived pairwise accessibility from immutable hyperedges."""

        return (
            await self._require_memory_storage().living.rebuild_association_projection()
        )

    async def expand_living_document_associations(
        self,
        results: Sequence[SearchResult],
        *,
        context_key: str,
        random_seed: int,
        limit: int,
    ) -> List[SearchResult]:
        """Add replayable contextual document neighbours to direct recall."""

        seed_refs = [f"document:{item.file_path}" for item in results if item.file_path]
        selections = (
            await self._require_memory_storage().living.choose_association_neighbours(
                seed_refs,
                context_key=context_key,
                random_seed=random_seed,
                limit=max(0, int(limit)),
            )
        )
        expanded = list(results)
        seen_paths = {item.file_path for item in expanded}
        for index, selection in enumerate(selections):
            if not selection.entity_ref.startswith("document:"):
                continue
            path = selection.entity_ref.removeprefix("document:")
            if not path or path in seen_paths:
                continue
            node = await self.get_node_by_file_path(path, migrate_identity=False)
            if node is None:
                continue
            expanded.append(
                SearchResult(
                    file_path=path,
                    title=node.title,
                    snippet=await self._get_snippet_wrapper(node.node_id),
                    relevance=1.0 / float(self.RRF_K + index + 1),
                    source="associated",
                    association_path=list(seed_refs),
                    association_reason=(
                        "living recall evidence: "
                        + ", ".join(selection.signals)
                        + f"; events={selection.event_count}"
                    ),
                    score_kind="accessibility_rank_not_truth",
                )
            )
            seen_paths.add(path)
        return expanded

    async def list_experiences_after(
        self,
        sequence: int,
        *,
        limit: int = 100,
        stream_scope: str | None = None,
    ) -> List[ExperienceRecord]:
        """按序列读取经历账本，可显式限制聊天流范围。"""
        return await self._require_memory_storage().experiences.list_after(
            sequence,
            limit=limit,
            stream_scope=stream_scope,
        )

    async def record_witness_memory(self, **kwargs: Any) -> WitnessMemory:
        """保存带完整来源链的主体见证，不把它提升为客观事实。"""
        return await self._require_memory_storage().witnesses.append(**kwargs)

    async def mark_witness_projection(
        self,
        witness_id: str,
        *,
        projection_path: str,
        status: str,
        error: str = "",
    ) -> bool:
        """更新可重建 Markdown 投影状态，不修改见证正文。"""
        return await self._require_memory_storage().witnesses.mark_projection(
            witness_id,
            projection_path=projection_path,
            status=status,
            error=error,
        )

    async def list_pending_witness_projections(
        self,
        *,
        limit: int = 100,
    ) -> List[WitnessMemory]:
        """读取待恢复的见证投影。"""
        return await self._require_memory_storage().witnesses.list_pending(limit=limit)

    async def get_witness_by_projection_path(
        self,
        projection_path: str,
    ) -> WitnessMemory | None:
        """用确定性投影路径检查事件窗口是否已见证。"""
        return await self._require_memory_storage().witnesses.get_by_projection_path(
            projection_path
        )

    async def get_witness_state(
        self,
        consciousness_instance_id: str,
    ) -> Dict[str, Any]:
        """读取见证意识的持久化游标与错误状态。"""
        return await self._require_memory_storage().witnesses.get_state(
            consciousness_instance_id
        )

    async def update_witness_state(
        self,
        consciousness_instance_id: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """原子更新见证意识游标；调用方决定何时确认整批成功。"""
        witnesses = self._require_memory_storage().witnesses
        current = await witnesses.get_state(consciousness_instance_id)
        expected_sequence = int(
            kwargs.pop("expected_sequence", current["last_sequence"])
        )
        expected_revision = int(
            kwargs.pop("expected_revision", current.get("revision", 0))
        )
        requested_sequence = kwargs.pop("last_sequence", None)
        next_sequence = (
            int(current["last_sequence"])
            if requested_sequence is None
            else int(requested_sequence)
        )
        allowed = {"last_run_at", "last_success_at", "last_error"}
        unexpected = set(kwargs) - allowed
        if unexpected:
            raise TypeError(
                "unsupported witness state fields: " + ", ".join(sorted(unexpected))
            )
        return await witnesses.compare_and_advance_state(
            consciousness_instance_id,
            expected_sequence=expected_sequence,
            expected_revision=expected_revision,
            next_sequence=next_sequence,
            last_run_at=kwargs.get("last_run_at"),
            last_success_at=kwargs.get("last_success_at"),
            last_error=kwargs.get("last_error"),
        )

    async def search_epistemic_claims(
        self,
        query: str,
        *,
        mode: MemorySearchMode | str = "",
        top_k: int = 5,
        stream_scope: str | None = None,
        visibility: tuple[str, ...] = ("private",),
        valid_at: str = "",
        recorded_as_of: str = "",
    ) -> List[ClaimSearchResult]:
        """检索主张本体并保留状态、证据、冲突和可塑性解释。"""
        mode_text = (
            mode.value if isinstance(mode, MemorySearchMode) else str(mode or "")
        )
        return await self._require_memory_storage().epistemic.search_claims(
            query,
            mode=mode_text,
            top_k=top_k,
            stream_scope=stream_scope,
            visibility=visibility,
            valid_at=valid_at,
            recorded_as_of=recorded_as_of,
        )

    async def search_evidence_aware(
        self,
        query: str,
        *,
        mode: MemorySearchMode | str = "",
        top_k: int = 5,
        stream_scope: str | None = None,
        visibility: tuple[str, ...] = ("private",),
        enable_association: bool = True,
        valid_at: str = "",
        recorded_as_of: str = "",
        document_results: Sequence[SearchResult] | None = None,
        association_context_key: str = "",
        association_random_seed: int | None = None,
    ) -> List[EvidenceAwareMemoryResult]:
        """并行召回文档与见证，并保留来源、视角和认识论边界。"""
        if not self.available:
            claim_task = asyncio.sleep(0, result=[])
        else:
            claim_task = self.search_epistemic_claims(
                query,
                mode=mode,
                top_k=max(1, int(top_k)),
                stream_scope=stream_scope,
                visibility=visibility,
                valid_at=valid_at,
                recorded_as_of=recorded_as_of,
            )
        if document_results is None:
            document_task = self.search_memory(
                query,
                top_k=max(1, int(top_k)),
                enable_association=enable_association,
                return_bundles=False,
            )
        else:
            document_task = asyncio.sleep(0, result=list(document_results))
        witness_task = self.search_witness_memories(
            query,
            mode=mode,
            top_k=max(1, int(top_k)),
            stream_scope=stream_scope,
            visibility=visibility,
        )
        if not self.available:
            interpretation_task = asyncio.sleep(0, result=[])
        else:
            interpretation_task = self.search_memory_interpretations(
                query,
                top_k=max(1, int(top_k)),
                stream_scope=stream_scope,
                visibility=visibility,
                recorded_as_of=recorded_as_of,
            )
        (
            claim_results,
            document_results,
            witness_results,
            interpretation_results,
        ) = await asyncio.gather(
            claim_task,
            document_task,
            witness_task,
            interpretation_task,
        )
        candidates: list[EvidenceAwareMemoryResult] = []
        for result in claim_results:
            claim = result.state.claim
            candidates.append(
                EvidenceAwareMemoryResult(
                    record_id=claim.claim_id,
                    kind="epistemic_claim",
                    content=claim.content,
                    rank_score=float(result.rank_score),
                    confidence=None,
                    source=f"claim_{claim.source}",
                    valid_from=claim.valid_from,
                    valid_to=claim.valid_to,
                    recorded_at=claim.recorded_at,
                    stream_scope=claim.stream_scope,
                    visibility=claim.visibility,
                    status=result.state.status,
                    provenance=tuple(item.evidence_ref for item in result.evidence),
                    metadata={
                        "subject_key": claim.subject_key,
                        "claim_kind": claim.claim_kind,
                        "authority": claim.authority,
                        "conflict_ids": [item.conflict_id for item in result.conflicts],
                        "superseded_by": list(result.state.superseded_by),
                        "retrieval_affinity": (
                            result.plasticity.retrieval_affinity
                            if result.plasticity is not None
                            else 0.0
                        ),
                        "epistemic_note": (
                            "rank and retrieval feedback are not truth confidence"
                        ),
                    },
                )
            )
        for result in document_results:
            if result.file_path.startswith("diaries/witness/"):
                projected = await self.get_witness_by_projection_path(result.file_path)
                if projected is None:
                    continue
                if projected.visibility not in visibility:
                    continue
                if projected.status in {"privacy_sealed", "suppressed"}:
                    continue
                if stream_scope is None and projected.stream_scope:
                    continue
                if stream_scope is not None and projected.stream_scope not in {
                    "",
                    stream_scope,
                }:
                    continue
                candidates.append(
                    EvidenceAwareMemoryResult(
                        record_id=projected.witness_id,
                        kind=projected.epistemic_kind,
                        content=projected.content,
                        rank_score=float(result.relevance),
                        confidence=None,
                        source=f"witness_document_{result.source}",
                        valid_from=projected.valid_from,
                        valid_to=projected.valid_to,
                        recorded_at=projected.recorded_at,
                        stream_scope=projected.stream_scope,
                        visibility=projected.visibility,
                        status=projected.status,
                        provenance=projected.source_event_ids,
                        metadata={
                            "epistemic_note": (
                                "subjective witness, not objective truth"
                            ),
                            "projection_path": projected.projection_path,
                            "score_kind": getattr(
                                result,
                                "score_kind",
                                "rank",
                            ),
                        },
                    )
                )
                continue
            candidates.append(
                EvidenceAwareMemoryResult(
                    record_id=result.file_path,
                    kind="document_evidence",
                    content=result.snippet,
                    rank_score=float(result.relevance),
                    confidence=None,
                    source=f"document_{result.source}",
                    provenance=(result.file_path,),
                    metadata={
                        "title": result.title,
                        "score_kind": getattr(result, "score_kind", "rank"),
                        "association_path": list(result.association_path),
                        "association_reason": result.association_reason,
                    },
                )
            )
        for result in witness_results:
            witness = result.witness
            candidates.append(
                EvidenceAwareMemoryResult(
                    record_id=witness.witness_id,
                    kind=witness.epistemic_kind,
                    content=witness.content,
                    rank_score=float(result.rank_score),
                    confidence=None,
                    source=result.retrieval_source,
                    valid_from=witness.valid_from,
                    valid_to=witness.valid_to,
                    recorded_at=witness.recorded_at,
                    stream_scope=witness.stream_scope,
                    visibility=witness.visibility,
                    status=witness.status,
                    provenance=witness.source_event_ids,
                    metadata={
                        "epistemic_note": result.epistemic_note,
                        "consciousness_instance_id": (
                            witness.consciousness_instance_id
                        ),
                        "source_kind": witness.source_kind,
                        "projection_path": witness.projection_path,
                    },
                )
            )
        for result in interpretation_results:
            interpretation = result.interpretation
            candidates.append(
                EvidenceAwareMemoryResult(
                    record_id=interpretation.interpretation_id,
                    kind="memory_interpretation",
                    content=interpretation.content,
                    rank_score=float(result.rank_score),
                    confidence=None,
                    source=result.retrieval_source,
                    valid_from=interpretation.valid_from,
                    valid_to=interpretation.valid_to,
                    recorded_at=interpretation.recorded_at,
                    stream_scope=interpretation.stream_scope,
                    visibility=interpretation.visibility,
                    provenance=tuple(item.entity_ref for item in result.sources),
                    metadata={
                        "subject_id": interpretation.subject_id,
                        "authored_by": interpretation.authored_by,
                        "consciousness_instance_id": (
                            interpretation.consciousness_instance_id
                        ),
                        "source_predicates": [
                            item.predicate for item in result.sources
                        ],
                        "epistemic_note": (
                            "subject-authored interpretation, not source truth"
                        ),
                    },
                )
            )
        if (
            enable_association
            and association_context_key
            and association_random_seed is not None
            and candidates
        ):
            seed_refs = tuple(
                dict.fromkeys(
                    (
                        f"document:{item.record_id}"
                        if item.kind == "document_evidence"
                        else f"{item.kind}:{item.record_id}"
                    )
                    for item in candidates
                )
            )
            neighbours = await self.select_memory_association_neighbours(
                seed_refs,
                context_key=association_context_key,
                random_seed=association_random_seed,
                limit=max(1, int(top_k)),
            )
            existing_refs = set(seed_refs)
            for ordinal, neighbour in enumerate(neighbours, start=1):
                entity_ref = neighbour.entity_ref
                if entity_ref in existing_refs:
                    continue
                if entity_ref.startswith("memory_interpretation:"):
                    interpretation_id = entity_ref.split(":", 1)[1]
                    loaded = await self.get_memory_interpretation(interpretation_id)
                    if loaded is None:
                        continue
                    interpretation, sources = loaded
                    if interpretation.visibility not in visibility:
                        continue
                    if stream_scope is None and interpretation.stream_scope:
                        continue
                    if stream_scope is not None and interpretation.stream_scope not in {
                        "",
                        stream_scope,
                    }:
                        continue
                    candidates.append(
                        EvidenceAwareMemoryResult(
                            record_id=interpretation.interpretation_id,
                            kind="memory_interpretation",
                            content=interpretation.content,
                            rank_score=1.0 / float(80 + ordinal),
                            confidence=None,
                            source="contextual_corecall",
                            valid_from=interpretation.valid_from,
                            valid_to=interpretation.valid_to,
                            recorded_at=interpretation.recorded_at,
                            stream_scope=interpretation.stream_scope,
                            visibility=interpretation.visibility,
                            provenance=tuple(item.entity_ref for item in sources),
                            metadata={
                                "subject_id": interpretation.subject_id,
                                "association_signals": list(neighbour.signals),
                                "association_event_count": neighbour.event_count,
                                "epistemic_note": (
                                    "co-recall changes accessibility, not truth"
                                ),
                            },
                        )
                    )
                    existing_refs.add(entity_ref)
                elif entity_ref.startswith("epistemic_claim:"):
                    claim_id = entity_ref.split(":", 1)[1]
                    state = await self.get_memory_claim_state(claim_id)
                    if state is None:
                        continue
                    claim = state.claim
                    if claim.visibility not in visibility:
                        continue
                    if stream_scope is None and claim.stream_scope:
                        continue
                    if stream_scope is not None and claim.stream_scope not in {
                        "",
                        stream_scope,
                    }:
                        continue
                    candidates.append(
                        EvidenceAwareMemoryResult(
                            record_id=claim.claim_id,
                            kind="epistemic_claim",
                            content=claim.content,
                            rank_score=1.0 / float(80 + ordinal),
                            confidence=None,
                            source="contextual_corecall",
                            valid_from=claim.valid_from,
                            valid_to=claim.valid_to,
                            recorded_at=claim.recorded_at,
                            stream_scope=claim.stream_scope,
                            visibility=claim.visibility,
                            status=state.status,
                            metadata={
                                "subject_key": claim.subject_key,
                                "association_signals": list(neighbour.signals),
                                "association_event_count": neighbour.event_count,
                                "epistemic_note": (
                                    "co-recall changes accessibility, not truth"
                                ),
                            },
                        )
                    )
                    existing_refs.add(entity_ref)
        candidates.sort(
            key=lambda item: (
                -item.rank_score,
                item.recorded_at,
                item.record_id,
            )
        )
        deduplicated: list[EvidenceAwareMemoryResult] = []
        seen_ids: set[str] = set()
        for item in candidates:
            if item.record_id in seen_ids:
                continue
            seen_ids.add(item.record_id)
            deduplicated.append(item)
        return deduplicated[: max(1, int(top_k)) * 2]

    async def search_witness_memories(
        self,
        query: str,
        *,
        mode: MemorySearchMode | str = "",
        top_k: int = 5,
        stream_scope: str | None = None,
        visibility: tuple[str, ...] = ("private",),
    ) -> List[WitnessSearchResult]:
        """按明确认识论模式检索见证，rank 不等同 truth/confidence。"""
        return await self._require_memory_storage().witnesses.search(
            query,
            mode=mode,
            top_k=top_k,
            stream_scope=stream_scope,
            visibility=visibility,
        )

    async def migrate_legacy_witness(self, **kwargs: Any) -> WitnessMemory | None:
        """原子迁移一条旧日记；旧文件保持只读且不删除。"""
        async with self._index_write_lock:
            return await self._require_memory_storage().witnesses.migrate_legacy(
                **kwargs
            )

    async def witness_migration_exists(self, migration_key: str) -> bool:
        """检查旧日记迁移键，保证迁移幂等。"""
        return await self._require_memory_storage().witnesses.migration_exists(
            migration_key
        )

    async def record_witness_migration(self, **kwargs: Any) -> None:
        """记录旧日记来源哈希与新见证 ID，不删除旧文件。"""
        async with self._index_write_lock:
            await self._require_memory_storage().witnesses.record_migration(**kwargs)

    # --------------------------------------------------------
    # 节点操作（封装模块函数）
    # --------------------------------------------------------

    async def get_or_create_file_node(
        self,
        file_path: str,
        title: str = "",
        content: str = "",
    ) -> MemoryNode:
        """Create a reference node or atomically index one document body."""
        async with self._index_write_lock:
            return await self._require_memory_storage().legacy_graph.get_or_create_file_node(
                file_path,
                title,
                content,
            )

    async def get_or_create_workspace_document_node(self, file_path: str) -> MemoryNode:
        """Index an eligible workspace document before using it in a relation."""
        return await self._get_or_create_file_node_from_workspace(file_path)

    async def get_node_by_file_path(
        self,
        file_path: str,
        migrate_identity: bool = False,
    ) -> Optional[MemoryNode]:
        """Read one canonical node without automatic identity migration."""
        del migrate_identity
        return await self._require_memory_storage().legacy_graph.get_node_by_file_path(
            file_path
        )

    async def migrate_file_path(self, old_path: str, new_path: str) -> bool:
        """Explicitly move a canonical document identity inside SQLite."""
        return await self.move_document(old_path, new_path)

    async def increment_access(self, node_id: str) -> None:
        """增加节点访问计数并更新激活强度。"""
        await self._require_memory_storage().legacy_graph.increment_access(node_id)

    async def _get_node_by_id_wrapper(self, node_id: str) -> Optional[MemoryNode]:
        """根据 ID 获取节点的包装函数。"""
        return await self._require_memory_storage().legacy_graph.get_node_by_id(node_id)

    # 保持与旧 API 兼容（router 等外部调用使用此名称）
    _get_node_by_id = _get_node_by_id_wrapper

    async def _get_snippet_wrapper(self, node_id: str) -> str:
        """获取摘要的包装函数。"""
        return await self._require_memory_storage().document_index.get_snippet(node_id)

    async def _filter_existing_scores_wrapper(
        self,
        scores: List[tuple],
    ) -> tuple:
        """过滤存在节点的包装函数。"""
        return (
            await self._require_memory_storage().document_index.filter_existing_scores(
                scores
            )
        )

    # --------------------------------------------------------
    # 边操作（封装模块函数）
    # --------------------------------------------------------

    async def create_or_update_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: EdgeType,
        reason: str = "",
        strength: float = 0.5,
        bidirectional: bool = True,
    ) -> MemoryEdge:
        """创建或更新边。"""
        return await self._require_memory_storage().legacy_graph.create_or_update_edge(
            source_id,
            target_id,
            edge_type.value,
            reason=reason,
            strength=strength,
            bidirectional=bidirectional,
        )

    async def get_edges_from(
        self, node_id: str, min_weight: float = 0.0
    ) -> List[MemoryEdge]:
        """获取从指定节点出发的边。"""
        return await self._require_memory_storage().legacy_graph.get_edges_from(
            node_id,
            min_weight,
        )

    async def get_edges_to(
        self, node_id: str, min_weight: float = 0.0
    ) -> List[MemoryEdge]:
        """获取指向指定节点的边。"""
        return await self._require_memory_storage().legacy_graph.get_edges_to(
            node_id,
            min_weight,
        )

    async def delete_edge(
        self,
        source_path: str,
        target_path: str,
        edge_type: Optional[EdgeType] = None,
    ) -> bool:
        """删除边。"""
        return await self._require_memory_storage().legacy_graph.delete_edge(
            source_path,
            target_path,
            edge_type=edge_type,
        )

    async def _reinforce_coactivated_wrapper(self, node_ids: List[str]) -> None:
        """Hebbian 强化的包装函数。"""
        await self._require_memory_storage().legacy_graph.reinforce_coactivated(
            node_ids,
            learning_rate=self.LEARNING_RATE,
        )

    # --------------------------------------------------------
    # 检索操作（封装模块函数）
    # --------------------------------------------------------

    async def search_memory(
        self,
        query: str,
        top_k: int = 5,
        enable_association: bool = True,
        file_types: Optional[List[str]] = None,
        time_range_days: int = 0,
        *,
        now: Any = None,
        workspace_path: str | Path | None = None,
        return_bundles: bool = True,
    ) -> List[MemoryBundle] | List[SearchResult]:
        """混合检索 + 联想，默认返回完整的可追溯记忆包。

        Args:
            query: 检索查询
            top_k: 返回结果数量
            enable_association: 是否启用联想扩散
            file_types: 文件类型过滤
            time_range_days: 时间范围过滤（天数）
            now: 时间基准（测试用）
            workspace_path: 工作空间路径（测试用）
            return_bundles: 是否返回完整记忆包（默认 True，完美架构）

        Returns:
            List[MemoryBundle]: 完整认知包（默认，包含演化历史）
            List[SearchResult]: 简单搜索结果（仅当 return_bundles=False）
        """
        detailed = await self._require_memory_storage().document_index.search_detailed(
            query,
            collection=self._chroma_collection,
            chunk_collection=self._chunk_collection,
            top_k=top_k,
            enable_association=enable_association,
            file_types=file_types,
            time_range_days=time_range_days,
            emit_visual_event=self._emit_visual_event,
            now=self._clock if now is None else now,
            workspace_path=(
                self._get_workspace_path() if workspace_path is None else workspace_path
            ),
        )
        simple_results = detailed.results

        if not return_bundles:
            # 降级模式：返回简单结果列表
            return simple_results

        # 完美架构：默认返回完整记忆包
        return await self.build_memory_bundles(
            query=query,
            results=simple_results,
            top_k=top_k,
        )

    async def search_memory_detailed(
        self,
        query: str,
        top_k: int = 5,
        enable_association: bool = True,
        file_types: Optional[List[str]] = None,
        time_range_days: int = 0,
        *,
        now: Any = None,
        workspace_path: str | Path | None = None,
    ) -> DetailedSearchResult:
        """返回检索结果及各阶段只读诊断。"""
        return await self._require_memory_storage().document_index.search_detailed(
            query,
            collection=self._chroma_collection,
            chunk_collection=self._chunk_collection,
            top_k=top_k,
            enable_association=enable_association,
            file_types=file_types,
            time_range_days=time_range_days,
            emit_visual_event=self._emit_visual_event,
            now=self._clock if now is None else now,
            workspace_path=(
                self._get_workspace_path() if workspace_path is None else workspace_path
            ),
        )

    async def vector_search(self, query: str, top_k: int = 10) -> List[tuple]:
        """向量相似度检索，优先聚合 chunk 命中到节点。"""
        return await self._require_memory_storage().document_index.vector_search(
            query,
            collection=self._chroma_collection,
            chunk_collection=self._chunk_collection,
            top_k=top_k,
        )

    async def fts_search(self, query: str, top_k: int = 10) -> List[tuple]:
        """全文搜索。"""
        return await self._require_memory_storage().document_index.fts_search(
            query,
            top_k=top_k,
        )

    async def sync_embedding(self, file_path: str, content: str) -> None:
        """Queue document indexing; only the outbox worker may touch Chroma."""
        await self.upsert_document(file_path, content)

    async def spread_activation(
        self,
        seed_ids: List[str],
        max_depth: int = 2,
        max_results: int = 10,
        allowed_edge_types: Optional[List[EdgeType | str]] = None,
    ) -> List[tuple]:
        """激活扩散联想，默认只读取显式关系。"""
        return await self._require_memory_storage().legacy_graph.spread_activation(
            seed_ids,
            max_depth=max_depth,
            max_results=max_results,
            spread_decay=self.SPREAD_DECAY,
            spread_threshold=self.SPREAD_THRESHOLD,
            allowed_edge_types=(
                EXPLICIT_RELATION_EDGE_TYPES
                if allowed_edge_types is None
                else allowed_edge_types
            ),
        )

    # --------------------------------------------------------
    # 记忆演化链路
    # --------------------------------------------------------

    async def create_memory_lineage_edge(
        self,
        source_path: str,
        target_path: str,
        relation_type: str | EdgeType,
        reason: str = "",
        strength: float = 0.7,
    ) -> MemoryEdge:
        """记录一条从旧理解到新理解的演化关系。"""
        if isinstance(relation_type, EdgeType):
            edge_type = relation_type
        else:
            edge_type = EdgeType(str(relation_type).strip().lower())
        if edge_type not in LINEAGE_EDGE_TYPES:
            raise ValueError(f"{edge_type.value} 不是记忆演化关系")

        source_node = await self._get_or_create_file_node_from_workspace(source_path)
        target_node = await self._get_or_create_file_node_from_workspace(target_path)
        return await self.create_or_update_edge(
            source_id=source_node.node_id,
            target_id=target_node.node_id,
            edge_type=edge_type,
            reason=reason.strip(),
            strength=max(0.1, min(1.0, float(strength))),
            bidirectional=False,
        )

    async def record_memory_correction(
        self,
        topic: str,
        message: str,
        related_paths: Optional[List[str]] = None,
        source: str = "user",
        query: str = "",
        stream_id: str | None = None,
    ) -> List[MemoryCorrection]:
        """记录显式修正，不删除旧记忆。"""
        topic_text = str(topic or "").strip()
        message_text = str(message or "").strip()
        if not topic_text or not message_text:
            raise ValueError("topic 和 message 不能为空")

        corrections: list[MemoryCorrection] = []
        canonical_paths: list[str] = []
        for raw_path in related_paths or []:
            if not raw_path:
                continue
            eligibility = assess_document_path(raw_path)
            if not eligibility.eligible:
                raise ValueError(f"不支持索引的记忆文档路径: {eligibility.reason}")
            canonical_paths.append(eligibility.path)
        if not canonical_paths:
            corrections.append(
                await self._require_memory_storage().legacy_graph.insert_correction(
                    topic=topic_text,
                    message=message_text,
                    source=source,
                    related_node_id=None,
                    query=query,
                    stream_id=stream_id,
                )
            )
            await self._record_correction_claims(corrections)
            return corrections

        for path in canonical_paths:
            node = await self._get_or_create_file_node_from_workspace(path)
            corrections.append(
                await self._require_memory_storage().legacy_graph.insert_correction(
                    topic=topic_text,
                    message=message_text,
                    source=source,
                    related_node_id=node.node_id,
                    query=query,
                    stream_id=stream_id,
                )
            )
        await self._record_correction_claims(corrections)
        return corrections

    async def _record_correction_claims(
        self,
        corrections: List[MemoryCorrection],
    ) -> None:
        """把旧 correction 兼容记录投影到新本体；候选来源不获确认权。

        同一逻辑修正可能绑定多个文件节点（多行 correction），但认识论层
        只需一条 claim。按 (topic, message) 去重。
        """
        seen_claims: set[tuple[str, str]] = set()
        for correction in corrections:
            dedup_key = (correction.topic, correction.message)
            if dedup_key in seen_claims:
                continue
            seen_claims.add(dedup_key)
            source = str(correction.source or "unknown").strip().lower()
            claim = new_claim(
                claim_id=f"correction_{correction.correction_id}",
                subject_key=f"correction:{correction.topic}",
                content=correction.message,
                claim_kind="correction_candidate",
                source=source,
                authority="unasserted",
                stream_scope=correction.stream_id or "",
                metadata={
                    "legacy_correction_id": correction.correction_id,
                    "related_node_id": correction.related_node_id or "",
                    "query": correction.query,
                },
                recorded_at=datetime.fromtimestamp(correction.created_at)
                .astimezone()
                .isoformat(),
            )
            await self.append_memory_claim(claim)

    async def resolve_canonical_path(
        self,
        file_path: str,
        max_depth: int = 4,
        persist_lineage: bool = False,
        *,
        allow_heuristic: bool = False,
    ) -> Dict[str, Any]:
        """沿显式演化链解析旧路径当前对应的文件。"""
        eligibility = assess_document_path(file_path)
        requested_path = eligibility.path
        workspace = self._get_workspace_path()
        if not eligibility.eligible:
            return {
                "requested_path": requested_path,
                "resolved_path": requested_path,
                "resolved": False,
                "lineage": [],
                "note": f"不是可用于记忆演化的文档: {eligibility.reason}",
            }

        node = await self.get_node_by_file_path(
            file_path,
            migrate_identity=False,
        )
        if node is not None:
            resolved = await self._resolve_canonical_from_node(
                node, max_depth=max_depth
            )
            if resolved is not None:
                return {
                    "requested_path": requested_path,
                    "resolved_path": resolved["path"],
                    "resolved": True,
                    "lineage": resolved["lineage"],
                    "note": "显式记忆演化链指向了当前文件",
                }

        requested_abs = workspace / requested_path
        if requested_abs.exists():
            return {
                "requested_path": requested_path,
                "resolved_path": requested_path,
                "resolved": False,
                "lineage": [],
                "note": "请求路径当前存在",
            }

        if allow_heuristic:
            candidate_path = await run_db(
                self._find_missing_file_candidate,
                requested_path,
            )
            if candidate_path:
                reason = "旧路径当前不存在；工作空间中发现同主题的当前文件候选。"
                if persist_lineage and allow_heuristic:
                    reason = "旧路径当前不存在；工作空间中发现同主题的当前文件候选，保留旧记忆并建立迁移链路。"
                    await self.create_memory_lineage_edge(
                        source_path=requested_path,
                        target_path=candidate_path,
                        relation_type=EdgeType.RENAMES,
                        reason=reason,
                        strength=0.75,
                    )
                return {
                    "requested_path": requested_path,
                    "resolved_path": candidate_path,
                    "resolved": True,
                    "lineage": [
                        {
                            "relation": EdgeType.RENAMES.value,
                            "from": requested_path,
                            "to": candidate_path,
                            "reason": reason,
                        }
                    ],
                    "note": "请求路径不在工作空间中，已按候选路径解析到当前文件",
                }

        return {
            "requested_path": requested_path,
            "resolved_path": requested_path,
            "resolved": False,
            "lineage": [],
            "note": "未找到可确认的当前文件",
        }

    async def build_memory_bundles(
        self,
        query: str,
        results: List[SearchResult],
        top_k: int = 5,
    ) -> List[MemoryBundle]:
        """把普通检索结果聚合成“当前理解 + 历史轨迹”的记忆包。

        路径判定与节点读取都是成批做的。逐条做的话，每条血缘边要两次数据库
        往返（取节点、取摘要）外加几次同步 ``stat``——一次召回轻易就是上百次
        往返，而且 ``stat`` 直接跑在事件循环上。现在每条检索结果最多一次节点
        批量查询加一次路径批量判定。

        Args:
            query: 触发这次召回的查询串。
            results: 底层检索结果。
            top_k: 最多产出多少个记忆包。

        Returns:
            List[MemoryBundle]: 记忆包列表，按 ``results`` 的顺序产出。
        """
        workspace = self._get_workspace_path()
        path_views: dict[str, _BundlePathView] = {}

        async def _resolve_paths(raw_paths: Sequence[str]) -> None:
            """把尚未判定过的路径批量判定并写入缓存。"""
            pending = [
                raw_path
                for raw_path in dict.fromkeys(raw_paths)
                if raw_path and raw_path not in path_views
            ]
            if not pending:
                return
            path_views.update(
                await asyncio.to_thread(_assess_bundle_paths, workspace, pending)
            )

        def _memory_path(raw_path: str) -> str | None:
            """返回已判定路径的规范形式；未判定或不合格时为 ``None``。"""
            view = path_views.get(raw_path)
            return view.memory_path if view is not None else None

        def _path_exists(raw_path: str) -> bool:
            """返回已判定路径对应的文件是否存在。"""
            view = path_views.get(raw_path)
            return view.exists if view is not None else False

        # 所有检索结果的路径一次判定完：它们是循环的准入条件，逐条判定意味着
        # 逐条阻塞事件循环做 stat。
        await _resolve_paths([result.file_path for result in results])

        bundles: list[MemoryBundle] = []
        seen_primary_paths: set[str] = set()

        for result in results:
            if len(bundles) >= max(1, top_k):
                break
            source_path = _memory_path(result.file_path)
            if source_path is None:
                continue
            node = await self.get_node_by_file_path(
                source_path,
                migrate_identity=False,
            )
            if node is None:
                continue

            evidence = [
                MemoryEvidence(
                    file_path=source_path,
                    title=result.title,
                    snippet=result.snippet,
                    relevance=result.relevance,
                    source=result.source,
                    exists=_path_exists(result.file_path),
                )
            ]
            trace: list[MemoryTrace] = []
            related_node_ids = [node.node_id]

            outgoing, incoming = await self.read_lineage_edges(node.node_id)
            # 两个方向的邻居一次取全：节点与摘要来自同一批查询，路径判定来自
            # 同一次线程调用。循环体内因此不再有 await。
            lineage_ids = list(
                dict.fromkeys(
                    [edge.target_id for edge in outgoing]
                    + [edge.source_id for edge in incoming]
                )
            )

            lineage_views = await self._require_memory_storage().legacy_graph.get_lineage_node_views(
                lineage_ids
            )
            await _resolve_paths([view.file_path for view in lineage_views.values()])

            for edge, direction in (
                *((edge, "later") for edge in outgoing),
                *((edge, "earlier") for edge in incoming),
            ):
                neighbour_id = (
                    edge.target_id if direction == "later" else edge.source_id
                )
                neighbour = lineage_views.get(neighbour_id)
                if neighbour is None or not neighbour.file_path:
                    continue
                neighbour_path = _memory_path(neighbour.file_path)
                if neighbour_path is None:
                    continue
                related_node_ids.append(neighbour.node_id)
                exists = _path_exists(neighbour.file_path)
                trace.append(
                    MemoryTrace(
                        relation=edge.edge_type.value,
                        file_path=neighbour_path,
                        title=neighbour.title,
                        snippet=neighbour.snippet,
                        reason=edge.reason,
                        direction=direction,
                        exists=exists,
                    )
                )
                evidence.append(
                    MemoryEvidence(
                        file_path=neighbour_path,
                        title=neighbour.title,
                        snippet=neighbour.snippet,
                        relevance=result.relevance * edge.weight,
                        source="lineage",
                        relation=edge.edge_type.value,
                        relation_reason=edge.reason,
                        exists=exists,
                    )
                )

            canonical = await self._resolve_canonical_from_node(node)
            resolution = (
                {
                    "requested_path": source_path,
                    "resolved_path": canonical["path"],
                    "resolved": True,
                    "lineage": canonical["lineage"],
                    "note": "记忆演化链指向了后续整理文件",
                }
                if canonical is not None
                else {
                    "requested_path": source_path,
                    "resolved_path": source_path,
                    "resolved": False,
                    "lineage": [],
                    "note": "未找到显式记忆演化链",
                }
            )
            resolved_raw = str(resolution.get("resolved_path") or "")
            await _resolve_paths([resolved_raw])
            primary_path = _memory_path(resolved_raw)
            if primary_path is None:
                continue
            if primary_path in seen_primary_paths:
                continue
            seen_primary_paths.add(primary_path)

            if primary_path != source_path and not any(
                item.file_path == primary_path for item in evidence
            ):
                primary_node = await self.get_node_by_file_path(
                    primary_path,
                    migrate_identity=False,
                )
                if primary_node is not None:
                    related_node_ids.append(primary_node.node_id)
                    evidence.append(
                        MemoryEvidence(
                            file_path=primary_path,
                            title=primary_node.title,
                            snippet=await self._get_snippet_wrapper(
                                primary_node.node_id
                            ),
                            relevance=result.relevance,
                            source="lineage",
                            relation="canonical",
                            relation_reason=str(resolution.get("note") or ""),
                            exists=_path_exists(resolved_raw),
                        )
                    )

            corrections = await self.read_memory_corrections(
                query=query,
                related_node_ids=list(dict.fromkeys(related_node_ids)),
                limit=5,
            )
            current_understanding = self._build_current_understanding(
                primary_path=primary_path,
                evidence=evidence,
                corrections=corrections,
            )
            uncertainty = self._build_bundle_uncertainty(
                requested_path=result.file_path,
                primary_path=primary_path,
                evidence=evidence,
                corrections=corrections,
            )
            bundles.append(
                MemoryBundle(
                    query=query,
                    current_understanding=current_understanding,
                    primary_path=primary_path,
                    evidence=evidence,
                    history_trace=trace,
                    corrections=corrections,
                    uncertainty=uncertainty,
                )
            )

        return bundles

    async def search_memory_bundles(
        self,
        query: str,
        top_k: int = 5,
        enable_association: bool = True,
        file_types: Optional[List[str]] = None,
        time_range_days: int = 0,
    ) -> List[MemoryBundle]:
        """检索并返回可追溯记忆包。

        注意：这个方法现在是 search_memory() 的别名（向后兼容）。
        推荐直接使用 search_memory()，它默认返回 MemoryBundle。
        """
        return await self.search_memory(
            query=query,
            top_k=top_k,
            enable_association=enable_association,
            file_types=file_types,
            time_range_days=time_range_days,
            return_bundles=True,
        )

    async def search_memory_simple(
        self,
        query: str,
        top_k: int = 5,
        enable_association: bool = True,
        file_types: Optional[List[str]] = None,
        time_range_days: int = 0,
        *,
        now: Any = None,
        workspace_path: str | Path | None = None,
    ) -> List[SearchResult]:
        """简单检索模式，返回无演化历史的搜索结果列表。

        警告：这是降级版本，仅用于特殊场景（如性能敏感的内部操作）。
        正常情况下应该使用 search_memory()，它默认返回完整记忆包。
        """
        return await self.search_memory(
            query=query,
            top_k=top_k,
            enable_association=enable_association,
            file_types=file_types,
            time_range_days=time_range_days,
            now=now,
            workspace_path=workspace_path,
            return_bundles=False,
        )

    async def _get_or_create_file_node_from_workspace(
        self, file_path: str
    ) -> MemoryNode:
        """Load an eligible workspace document, or reuse an indexed historical node.

        This helper is used by explicit lineage and correction writes.  It must
        never turn a typo or an internal runtime path into a new empty memory
        node.  A missing file is only valid when the node already exists as
        historical lineage evidence.
        """
        path_eligibility = assess_document_path(file_path)
        if not path_eligibility.eligible:
            raise ValueError(f"不支持索引的记忆文档路径: {path_eligibility.reason}")

        workspace = self._get_workspace_path()
        eligibility = assess_workspace_document(workspace, path_eligibility.path)
        if eligibility.eligible:
            try:
                content, source_mtime, _ = read_workspace_document(
                    workspace,
                    eligibility.path,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise ValueError(f"无法读取记忆文档: {eligibility.path}") from exc
            await self.upsert_document(
                eligibility.path,
                content,
                title=Path(eligibility.path).stem,
                source_mtime=source_mtime,
            )
            node = await self.get_node_by_file_path(eligibility.path)
            if node is None:
                raise RuntimeError(f"记忆文档写入后未找到节点: {eligibility.path}")
            return node

        if eligibility.reason == "stat_error":
            historical_node = await self.get_node_by_file_path(path_eligibility.path)
            if historical_node is not None:
                return historical_node
            raise ValueError(f"记忆文档不存在或不可访问: {path_eligibility.path}")

        raise ValueError(f"不支持索引的记忆文档路径: {eligibility.reason}")

    async def _resolve_canonical_from_node(
        self,
        node: MemoryNode,
        max_depth: int = 4,
    ) -> Dict[str, Any] | None:
        workspace = self._get_workspace_path()
        if max_depth <= 0:
            return None

        frontier: list[
            tuple[
                MemoryNode,
                list[dict[str, str]],
                frozenset[str],
                float,
                float,
                tuple[str, ...],
            ]
        ] = [(node, [], frozenset({node.node_id}), 0.0, 0.0, ())]
        resolved: list[
            tuple[int, float, float, tuple[str, ...], str, list[dict[str, str]]]
        ] = []

        for depth in range(1, max_depth + 1):
            next_frontier: list[
                tuple[
                    MemoryNode,
                    list[dict[str, str]],
                    frozenset[str],
                    float,
                    float,
                    tuple[str, ...],
                ]
            ] = []
            for current, lineage, visited, weight, created_at, edge_ids in sorted(
                frontier,
                key=lambda item: (-item[3], -item[4], item[5], item[0].node_id),
            ):
                outgoing, _ = await self.read_lineage_edges(current.node_id)
                candidates = [
                    edge
                    for edge in outgoing
                    if edge.edge_type in CANONICAL_EDGE_TYPES
                    and edge.target_id not in visited
                ]
                for edge in sorted(
                    candidates,
                    key=lambda item: (
                        -float(item.weight),
                        -float(item.created_at),
                        item.edge_id,
                    ),
                ):
                    target = await self._get_node_by_id_wrapper(edge.target_id)
                    if target is None or not target.file_path:
                        continue
                    target_eligibility = assess_indexed_document_path(target.file_path)
                    if not target_eligibility.eligible:
                        continue
                    target_path = target_eligibility.path
                    current_eligibility = assess_indexed_document_path(
                        current.file_path
                    )
                    current_path = (
                        current_eligibility.path if current_eligibility.eligible else ""
                    )
                    target_lineage = [
                        *lineage,
                        {
                            "relation": edge.edge_type.value,
                            "from": current_path or current.node_id,
                            "to": target_path,
                            "reason": edge.reason,
                        },
                    ]
                    target_weight = weight + float(edge.weight)
                    target_created_at = created_at + float(edge.created_at)
                    target_edge_ids = (*edge_ids, edge.edge_id)
                    next_frontier.append(
                        (
                            target,
                            target_lineage,
                            visited | {target.node_id},
                            target_weight,
                            target_created_at,
                            target_edge_ids,
                        )
                    )
                    if assess_workspace_document(workspace, target_path).eligible:
                        resolved.append(
                            (
                                depth,
                                target_weight,
                                target_created_at,
                                target_edge_ids,
                                target_path,
                                target_lineage,
                            )
                        )

            if not next_frontier:
                break
            frontier = next_frontier

        if not resolved:
            return None
        _, _, _, _, path, lineage = sorted(
            resolved,
            key=lambda item: (-item[0], -item[1], -item[2], item[3], item[4]),
        )[0]
        return {"path": path, "lineage": lineage}

    def _find_missing_file_candidate(self, requested_path: str) -> str | None:
        """Find one eligible same-stem candidate without traversing runtime storage."""
        requested_eligibility = assess_document_path(requested_path)
        if not requested_eligibility.eligible:
            return None

        workspace = self._get_workspace_path()
        requested = Path(requested_eligibility.path)
        suffix = requested.suffix
        stem = requested.stem
        candidate_stems = {stem}
        for marker in (
            "_research",
            "-research",
            "_draft",
            "-draft",
            "_old",
            "-old",
            "_notes",
            "-notes",
        ):
            if stem.endswith(marker):
                candidate_stems.add(stem[: -len(marker)])
        candidate_stems.discard("")

        search_roots: list[Path] = []
        parent = workspace / requested.parent
        if requested.parent != Path(".") and parent.is_dir():
            search_roots.append(parent)
        search_roots.extend(
            workspace / directory
            for directory in sorted(MEMORY_CONTENT_DIRECTORIES)
            if (workspace / directory).is_dir()
        )

        candidates: set[str] = set()
        seen_roots: set[Path] = set()
        for root in search_roots:
            if root in seen_roots:
                continue
            seen_roots.add(root)
            for path in root.rglob(f"*{suffix}"):
                try:
                    candidate_path = path.relative_to(workspace).as_posix()
                except ValueError:
                    continue
                if path.stem not in candidate_stems:
                    continue
                eligibility = assess_workspace_document(workspace, candidate_path)
                if eligibility.eligible:
                    candidates.add(eligibility.path)

        return next(iter(candidates)) if len(candidates) == 1 else None

    def _build_current_understanding(
        self,
        primary_path: str,
        evidence: List[MemoryEvidence],
        corrections: List[MemoryCorrection],
    ) -> str:
        authoritative_sources = {"user", "explicit_user", "verified", "authoritative"}
        authoritative = [
            item
            for item in corrections
            if str(item.source or "").strip().lower() in authoritative_sources
        ]
        if authoritative:
            latest = sorted(
                authoritative,
                key=lambda item: item.created_at,
                reverse=True,
            )[0]
            return f"已确认修正：{latest.message}"

        primary = next(
            (item for item in evidence if item.file_path == primary_path), None
        )
        if primary is None and evidence:
            primary = evidence[0]
        snippet = " ".join(((primary.snippet if primary else "") or "").split())
        if snippet:
            return f"当前以 {primary_path} 为主要依据：{snippet}"
        return f"当前以 {primary_path} 为主要依据；需要读取全文确认细节。"

    def _build_bundle_uncertainty(
        self,
        requested_path: str,
        primary_path: str,
        evidence: List[MemoryEvidence],
        corrections: List[MemoryCorrection],
    ) -> str:
        notes: list[str] = []
        if requested_path != primary_path:
            notes.append(
                "命中的早期路径已经有后续整理/迁移，回答时应同时承认早期记录和当前文件。"
            )
        if any(not item.exists for item in evidence):
            notes.append("部分证据文件当前不在工作空间中，只能作为历史轨迹参考。")
        authoritative_sources = {"user", "explicit_user", "verified", "authoritative"}
        authoritative = [
            item
            for item in corrections
            if str(item.source or "").strip().lower() in authoritative_sources
        ]
        candidates = [item for item in corrections if item not in authoritative]
        if authoritative:
            notes.append("存在有来源权限的明确修正，应与原始证据一起呈现。")
        if candidates:
            notes.append(
                "存在自动反思或其他候选解释；它们尚未被确认，不能覆盖原始经历。"
            )
        return " ".join(notes)

    # --------------------------------------------------------
    # 衰减与统计（封装模块函数）
    # --------------------------------------------------------

    def compute_memory_strength(self, node: MemoryNode) -> float:
        """计算记忆强度。"""
        return compute_memory_strength(node, self.DECAY_LAMBDA)

    async def apply_decay(self) -> int:
        """应用遗忘衰减。"""
        return await self._require_memory_storage().legacy_graph.apply_decay()

    async def get_file_relations(
        self,
        file_path: str,
        depth: int = 1,
        min_strength: float = 0.2,
    ) -> Dict[str, Any]:
        """获取文件的关联图谱。"""
        return await get_file_relations(
            db=None,
            file_path=file_path,
            depth=depth,
            min_strength=min_strength,
            get_node_by_file_path_func=self.get_node_by_file_path,
            get_edges_from_func=self.get_edges_from,
            get_edges_to_func=self.get_edges_to,
            get_node_by_id_func=self._get_node_by_id_wrapper,
        )

    async def get_stats(self) -> Dict[str, Any]:
        """获取记忆系统统计信息。"""
        return await self._require_memory_storage().legacy_graph.stats()

    async def health_snapshot(self) -> Dict[str, Any]:
        """获取隔离的只读记忆健康快照，不修复或删除任何数据。"""
        async with self._lifecycle_lock:
            storage = self._require_memory_storage()
            collection = self._chunk_collection or self._chroma_collection
            local_db_open = self._db is not None
            recovery = self._startup_recovery_progress.health_snapshot()
            runtime = self._storage_runtime
        names = (
            "document_index",
            "experiences",
            "witnesses",
            "living",
            "epistemic",
            "legacy_graph",
        )
        if (
            storage.backend == BackendKind.MYSQL
            and runtime is not None
            and runtime.enabled
            and runtime.backend == BackendKind.MYSQL
        ):
            try:
                runtime_health = await runtime.health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - health must be content-free
                runtime_health = {
                    "status": "failed",
                    "backend": "mysql",
                    "error_type": type(exc).__name__,
                }
            if not isinstance(runtime_health, dict):
                runtime_health = {
                    "status": "failed",
                    "backend": "mysql",
                    "error_type": "InvalidHealthPayload",
                }
            raw_status = str(runtime_health.get("status") or "failed")
            try:
                runtime_status = StorageAvailability(raw_status)
            except ValueError:
                runtime_status = StorageAvailability.FAILED
                runtime_health = {
                    "status": "failed",
                    "backend": "mysql",
                    "error_type": "InvalidHealthStatus",
                }

            if runtime_status in {
                StorageAvailability.HEALTHY,
                StorageAvailability.DEGRADED,
            }:
                try:
                    readiness = await inspect_mysql_memory_readiness(runtime)
                except asyncio.CancelledError:
                    raise
                except MySQLMemoryReadinessProbeError as exc:
                    ports = {name: "failed" for name in names}
                    runtime_status = StorageAvailability.FAILED
                    runtime_health = {
                        "status": "failed",
                        "backend": "mysql",
                        "error_type": exc.error_type,
                    }
                else:
                    ports = {
                        name: readiness.get(
                            name,
                            StorageAvailability.FAILED,
                        ).value
                        for name in names
                    }
                    if not set(ports.values()) <= {"healthy", "degraded"}:
                        runtime_status = StorageAvailability.FAILED
            else:
                ports = {name: "failed" for name in names}

            status = runtime_status.value
            if status == "healthy" and recovery["status"] in {
                "scheduled",
                "running",
                "failed",
            }:
                status = "degraded"
            return {
                "status": status,
                "backend": storage.backend.value,
                "ports": ports,
                "runtime": runtime_health,
                "vector_expected": self._vector_backend_enabled,
                "vector_collection_loaded": collection is not None,
                "startup_recovery": recovery,
            }

        statuses = await asyncio.gather(
            *(getattr(storage, name).availability() for name in names)
        )
        ports = {
            name: status.value for name, status in zip(names, statuses, strict=True)
        }
        if storage.backend == BackendKind.LOCAL and local_db_open:
            snapshot = await collect_health_snapshot(
                self._get_db_path(),
                self._get_workspace_path(),
                collection,
                vector_expected=self._vector_backend_enabled,
            )
            snapshot.update(backend=storage.backend.value, ports=ports)
            snapshot["startup_recovery"] = recovery
            return snapshot
        runtime_health = (
            await self._storage_runtime.health()
            if self._storage_runtime is not None
            else {
                "status": "healthy"
                if set(ports.values()) <= {"healthy", "degraded"}
                else "failed",
                "backend": storage.backend.value,
                "reason": "injected MemoryStorageBundle",
            }
        )
        status = str(runtime_health.get("status", "failed"))
        if status == "healthy" and recovery["status"] in {
            "scheduled",
            "running",
            "failed",
        }:
            status = "degraded"
        return {
            "status": status,
            "backend": storage.backend.value,
            "ports": ports,
            "runtime": runtime_health,
            "vector_expected": self._vector_backend_enabled,
            "vector_collection_loaded": collection is not None,
            "startup_recovery": recovery,
        }

    # --------------------------------------------------------
    # 做梦系统接口（封装模块函数）
    # --------------------------------------------------------

    async def dream_walk(
        self,
        num_seeds: int = 5,
        seed_ids: Optional[List[str]] = None,
        max_depth: int = 3,
        decay_factor: float = 0.6,
        learning_rate: float = 0.05,
        persist_learning: bool = False,
    ) -> Dict[str, Any]:
        """REM 做梦游走，默认只读。"""
        return await self._require_memory_storage().legacy_graph.dream_walk(
            num_seeds=num_seeds,
            seed_ids=seed_ids,
            max_depth=max_depth,
            decay_factor=decay_factor,
            learning_rate=learning_rate,
            emit_visual_event=self._emit_visual_event,
            persist_learning=persist_learning,
        )

    async def list_dream_candidate_nodes(self, limit: int = 12) -> List[Dict[str, Any]]:
        """列出适合做梦选种的长期主题候选节点。"""
        return await self._require_memory_storage().legacy_graph.list_dream_candidate_nodes(
            limit
        )

    async def list_random_file_nodes(self, limit: int = 15) -> List[Dict[str, Any]]:
        """随机采样文件节点。"""
        return await self._require_memory_storage().legacy_graph.list_random_file_nodes(
            limit
        )

    async def prune_weak_edges(self, threshold: float = 0.08) -> int:
        """修剪弱 ASSOCIATES 边。"""
        return await self._require_memory_storage().legacy_graph.prune_weak_edges(
            threshold
        )
