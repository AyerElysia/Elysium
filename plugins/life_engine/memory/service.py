"""Life Engine 仿生记忆服务。

实现基于认知科学的记忆系统：
- 激活扩散 (Spreading Activation)：联想机制
- Hebbian 学习：共同激活强化连接
- 软遗忘：基于 Ebbinghaus 曲线的记忆衰减

本模块为记忆服务的核心入口，整合 nodes、edges、search、decay 模块。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.app.plugin_system.api import log_api

from .nodes import (
    MemoryNode,
    generate_file_node_id,
    get_or_create_file_node,
    get_node_by_file_path,
    increment_access,
)
from .edges import (
    MemoryEdge,
    EdgeType,
    EXPLICIT_RELATION_EDGE_TYPES,
    create_or_update_edge,
    get_edges_from,
    get_edges_to,
    delete_edge,
    reinforce_coactivated,
)
from .search import (
    DetailedSearchResult,
    SearchResult,
    get_chroma_collection,
    search_memory,
    search_memory_detailed,
    vector_search,
    fts_search,
    spread_activation,
    filter_existing_scores,
    get_node_by_id,
    get_snippet,
)
from .lineage import (
    CANONICAL_EDGE_TYPES,
    LINEAGE_EDGE_TYPES,
    MemoryBundle,
    MemoryCorrection,
    MemoryEvidence,
    MemoryTrace,
    get_lineage_edges,
    insert_memory_correction,
    list_memory_corrections,
)
from .decay import (
    compute_memory_strength,
    apply_decay,
    dream_walk,
    list_dream_candidate_nodes,
    list_random_file_nodes,
    prune_weak_edges,
    get_file_relations,
    get_stats,
)
from .indexing import (
    ChunkIndexState,
    DocumentIndexResult,
    IndexJob,
    claim_index_jobs,
    create_memory_schema,
    delete_document_rows,
    enqueue_index_job,
    list_index_jobs,
    move_document_rows,
    read_active_chunk_index_state,
    set_index_job_status,
    upsert_document_rows,
)
from .eligibility import (
    MEMORY_CONTENT_DIRECTORIES,
    assess_document_path,
    assess_indexed_document_path,
    assess_workspace_document,
    read_workspace_document,
    register_indexed_path_sql_function,
)
from .health import health_snapshot_from_path as collect_health_snapshot
from .worker import (
    CHUNK_COLLECTION_PREFIX,
    CHUNK_INDEX_VERSION,
    DEFAULT_RECLAIM_AFTER,
    IndexWorkerReport,
    chunk_collection_metadata,
    consume_vector_tombstones,
    get_chunk_collection,
    get_named_chunk_collection,
    process_index_jobs as run_chunk_index_jobs,
)

logger = log_api.get_logger("life_engine.memory")


class LifeMemoryService:
    """仿生记忆服务。"""

    # 算法参数（覆盖各模块的默认值）
    DECAY_LAMBDA = 0.05
    LEARNING_RATE = 0.1
    SPREAD_DECAY = 0.7
    SPREAD_THRESHOLD = 0.3
    PRUNE_THRESHOLD = 0.1
    RRF_K = 60

    def __init__(self, plugin: Any, *, clock: Any = None) -> None:
        """初始化记忆服务。

        Args:
            plugin: 插件实例（用于获取配置）
            clock: 可选无参时钟，供相对日期查询测试或注入使用。
        """
        self.plugin = plugin
        self._clock = clock or datetime.now
        self._workspace_override: Path | None = None
        if isinstance(plugin, (str, Path)):
            self._workspace_override = Path(plugin)
        self._db: sqlite3.Connection | None = None
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

    async def _get_chroma_collection(self) -> Any:
        """获取并缓存兼容旧节点向量的集合。"""
        if self._chroma_collection is None:
            self._chroma_collection = await get_chroma_collection(self._get_vector_db_path())
        return self._chroma_collection

    async def _resolve_chunk_collection(
        self,
        model_name: str,
        dimension: int,
        _metadata: Any = None,
    ) -> Any:
        """按 embedding 模型和维度获取本轮 worker 的候选集合。"""
        identity = (str(model_name or "unknown"), int(dimension))
        if self._chunk_collection is not None and self._chunk_collection_identity == identity:
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
        db = self._require_db()
        state = await asyncio.to_thread(read_active_chunk_index_state, db)
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
        """初始化记忆服务。"""
        async with self._lifecycle_lock:
            if self._initialized:
                return

            db_path = self._get_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)

            db = sqlite3.connect(str(db_path), check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA busy_timeout = 5000")
            register_indexed_path_sql_function(db)
            self._db = db

            try:
                await self._create_tables()
                await asyncio.to_thread(create_memory_schema, db)

                try:
                    await self._restore_chunk_collection()
                except Exception as exc:
                    logger.warning(f"恢复 chunk 向量集合失败，已降级到 legacy: {exc}")

                self._chroma_collection = await self._get_chroma_collection()
                self._initialized = True
            except BaseException:
                self._db = None
                await asyncio.to_thread(db.close)
                raise
            logger.info(f"记忆服务初始化完成，数据库: {db_path}")

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
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_type ON memory_nodes(node_type)")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_nodes_activation ON memory_nodes(activation_strength DESC)"
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_file_path ON memory_nodes(file_path)")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_edges_source ON memory_edges(source_id, weight DESC)"
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON memory_edges(target_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_type ON memory_edges(edge_type)")

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

        await asyncio.to_thread(_do_db_work)
        logger.debug("记忆数据库表创建完成")

    def _require_db(self) -> sqlite3.Connection:
        if self._db is None or self._closing:
            raise RuntimeError("记忆服务尚未初始化或正在关闭")
        return self._db

    async def close(self) -> None:
        """幂等关闭 SQLite；共享向量服务的生命周期由 kernel 管理。"""
        async with self._lifecycle_lock:
            if self._db is None:
                self._initialized = False
                self._clear_cached_collections()
                return

            self._closing = True
            try:
                async with self._index_worker_lock:
                    async with self._index_write_lock:
                        db = self._db
                        self._db = None
                        if db is not None:
                            await asyncio.to_thread(db.close)
            finally:
                self._initialized = False
                self._closing = False
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
        async with self._index_write_lock:
            db = self._require_db()
            return await asyncio.to_thread(
                upsert_document_rows,
                db,
                path,
                content,
                title,
                source_mtime,
                **({} if max_chars is None else {"max_chars": max_chars}),
                **({} if overlap_chars is None else {"overlap_chars": overlap_chars}),
            )

    async def delete_document(self, path: str) -> bool:
        """删除文档及其 SQLite 索引、分块和 outbox 记录。"""
        async with self._index_write_lock:
            db = self._require_db()
            return await asyncio.to_thread(delete_document_rows, db, path)

    async def move_document(self, old_path: str, new_path: str) -> bool:
        """移动文档索引；目标已有节点时明确拒绝合并。"""
        async with self._index_write_lock:
            db = self._require_db()
            return await asyncio.to_thread(move_document_rows, db, old_path, new_path)

    async def enqueue_index_job(self, node_id: str, content_hash: str) -> str:
        """加入一个待处理索引任务，不触发 embedding 或网络请求。"""
        async with self._index_write_lock:
            db = self._require_db()
            return await asyncio.to_thread(enqueue_index_job, db, node_id, content_hash)

    async def process_index_jobs(self, limit: int = 10) -> List[IndexJob]:
        """领取待处理任务，交给外部 worker；本方法不执行 embedding。"""
        async with self._index_write_lock:
            db = self._require_db()
            return await asyncio.to_thread(claim_index_jobs, db, limit=limit)

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

        def _open_worker_db() -> sqlite3.Connection:
            worker_db = sqlite3.connect(str(self._get_db_path()), check_same_thread=False)
            worker_db.row_factory = sqlite3.Row
            worker_db.execute("PRAGMA foreign_keys = ON")
            worker_db.execute("PRAGMA busy_timeout = 5000")
            register_indexed_path_sql_function(worker_db)
            return worker_db

        async with self._index_worker_lock:
            self._require_db()
            worker_db = _open_worker_db()
            worker_task = asyncio.create_task(
                run_chunk_index_jobs(
                    worker_db,
                    collection,
                    limit=limit,
                    embed_texts_func=embed_texts_func,
                    collection_resolver=self._resolve_chunk_collection,
                    collection_upsert_func=collection_upsert_func,
                    retry_failed=retry_failed,
                    reclaim_after=reclaim_after,
                )
            )
            try:
                report = await asyncio.shield(worker_task)
                tombstone_collection = collection or self._chunk_collection
                if tombstone_collection is not None:
                    try:
                        await consume_vector_tombstones(worker_db, tombstone_collection)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"向量 tombstone 清理失败: {exc}")
            except asyncio.CancelledError:
                await worker_task
                raise
            finally:
                worker_db.close()
            if report.completed:
                active_collection = collection or self._chunk_collection_candidate
                if active_collection is not None:
                    self._chunk_collection = active_collection
                    self._chunk_collection_identity = (report.model_name, report.dimension)
        return report

    async def list_index_jobs(
        self,
        status: str = "pending",
        limit: int = 100,
    ) -> List[IndexJob]:
        """读取 outbox 状态，供 worker 或测试观察。"""
        async with self._index_write_lock:
            db = self._require_db()
            return await asyncio.to_thread(list_index_jobs, db, status=status, limit=limit)

    async def set_index_job_status(
        self,
        job_id: str,
        status: str,
        error: str = "",
    ) -> bool:
        """更新外部索引 worker 的任务状态。"""
        async with self._index_write_lock:
            db = self._require_db()
            return await asyncio.to_thread(
                set_index_job_status,
                db,
                job_id,
                status,
                error=error,
            )

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
            db = self._require_db()
            return await get_or_create_file_node(
                db=db,
                file_path=file_path,
                title=title,
                content=content,
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
        db = self._require_db()
        return await get_node_by_file_path(db=db, file_path=file_path)

    async def migrate_file_path(self, old_path: str, new_path: str) -> bool:
        """Explicitly move a canonical document identity inside SQLite."""
        async with self._index_write_lock:
            db = self._require_db()
            return await asyncio.to_thread(move_document_rows, db, old_path, new_path)

    async def increment_access(self, node_id: str) -> None:
        """增加节点访问计数并更新激活强度。"""
        await increment_access(
            db=self._db,
            node_id=node_id,
            emit_visual_event=self._emit_visual_event,
        )

    async def _get_node_by_id_wrapper(self, node_id: str) -> Optional[MemoryNode]:
        """根据 ID 获取节点的包装函数。"""
        return await get_node_by_id(self._db, node_id)

    # 保持与旧 API 兼容（router 等外部调用使用此名称）
    _get_node_by_id = _get_node_by_id_wrapper

    async def _get_snippet_wrapper(self, node_id: str) -> str:
        """获取摘要的包装函数。"""
        return await get_snippet(self._db, node_id)

    async def _filter_existing_scores_wrapper(
        self,
        scores: List[tuple],
    ) -> tuple:
        """过滤存在节点的包装函数。"""
        return await filter_existing_scores(self._db, scores)

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
        return await create_or_update_edge(
            db=self._db,
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            reason=reason,
            strength=strength,
            bidirectional=bidirectional,
            emit_visual_event=self._emit_visual_event,
        )

    async def get_edges_from(self, node_id: str, min_weight: float = 0.0) -> List[MemoryEdge]:
        """获取从指定节点出发的边。"""
        return await get_edges_from(self._db, node_id, min_weight)

    async def get_edges_to(self, node_id: str, min_weight: float = 0.0) -> List[MemoryEdge]:
        """获取指向指定节点的边。"""
        return await get_edges_to(self._db, node_id, min_weight)

    async def delete_edge(
        self,
        source_path: str,
        target_path: str,
        edge_type: Optional[EdgeType] = None,
    ) -> bool:
        """删除边。"""
        return await delete_edge(
            db=self._db,
            source_path=source_path,
            target_path=target_path,
            edge_type=edge_type,
            generate_file_node_id_func=generate_file_node_id,
        )

    async def _reinforce_coactivated_wrapper(self, node_ids: List[str]) -> None:
        """Hebbian 强化的包装函数。"""
        await reinforce_coactivated(
            db=self._db,
            node_ids=node_ids,
            learning_rate=self.LEARNING_RATE,
            filter_existing_func=self._filter_existing_scores_wrapper,
            emit_visual_event=self._emit_visual_event,
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
        simple_results = await search_memory(
            db=self._db,
            query=query,
            collection=self._chroma_collection,
            top_k=top_k,
            enable_association=enable_association,
            file_types=file_types,
            time_range_days=time_range_days,
            emit_visual_event=self._emit_visual_event,
            now=self._clock if now is None else now,
            workspace_path=(self._get_workspace_path() if workspace_path is None else workspace_path),
            chunk_collection=self._chunk_collection,
        )

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
        return await search_memory_detailed(
            db=self._db,
            query=query,
            collection=self._chroma_collection,
            top_k=top_k,
            enable_association=enable_association,
            file_types=file_types,
            time_range_days=time_range_days,
            emit_visual_event=self._emit_visual_event,
            now=self._clock if now is None else now,
            workspace_path=(self._get_workspace_path() if workspace_path is None else workspace_path),
            chunk_collection=self._chunk_collection,
        )

    async def vector_search(self, query: str, top_k: int = 10) -> List[tuple]:
        """向量相似度检索，优先聚合 chunk 命中到节点。"""
        return await vector_search(
            query=query,
            collection=self._chroma_collection,
            top_k=top_k,
            filter_existing_func=self._filter_existing_scores_wrapper,
            db=self._db,
            chunk_collection=self._chunk_collection,
        )

    async def fts_search(self, query: str, top_k: int = 10) -> List[tuple]:
        """全文搜索。"""
        return await fts_search(self._db, query, top_k)

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
        return await spread_activation(
            db=self._db,
            seed_ids=seed_ids,
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
                await insert_memory_correction(
                    db=self._db,
                    topic=topic_text,
                    message=message_text,
                    source=source,
                    related_node_id=None,
                    query=query,
                    stream_id=stream_id,
                )
            )
            return corrections

        for path in canonical_paths:
            node = await self._get_or_create_file_node_from_workspace(path)
            corrections.append(
                await insert_memory_correction(
                    db=self._db,
                    topic=topic_text,
                    message=message_text,
                    source=source,
                    related_node_id=node.node_id,
                    query=query,
                    stream_id=stream_id,
                )
            )
        return corrections

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
            resolved = await self._resolve_canonical_from_node(node, max_depth=max_depth)
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
            candidate_path = await asyncio.to_thread(
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
        """把普通检索结果聚合成“当前理解 + 历史轨迹”的记忆包。"""
        workspace = self._get_workspace_path()

        def _bundle_memory_path(file_path: str) -> str | None:
            eligibility = assess_indexed_document_path(file_path)
            if not eligibility.eligible:
                return None
            candidate = workspace / eligibility.path
            if candidate.is_symlink():
                return None
            if candidate.exists() and not assess_workspace_document(workspace, eligibility.path).eligible:
                return None
            return eligibility.path

        bundles: list[MemoryBundle] = []
        seen_primary_paths: set[str] = set()

        for result in results:
            if len(bundles) >= max(1, top_k):
                break
            source_path = _bundle_memory_path(result.file_path)
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
                    exists=(workspace / source_path).is_file(),
                )
            ]
            trace: list[MemoryTrace] = []
            related_node_ids = [node.node_id]

            outgoing, incoming = await get_lineage_edges(self._db, node.node_id)
            for edge in outgoing:
                target = await self._get_node_by_id_wrapper(edge.target_id)
                target_path = _bundle_memory_path(target.file_path) if target and target.file_path else None
                if target is None or target_path is None:
                    continue
                related_node_ids.append(target.node_id)
                snippet = await self._get_snippet_wrapper(target.node_id)
                exists = (workspace / target_path).is_file()
                trace.append(
                    MemoryTrace(
                        relation=edge.edge_type.value,
                        file_path=target_path,
                        title=target.title,
                        snippet=snippet,
                        reason=edge.reason,
                        direction="later",
                        exists=exists,
                    )
                )
                evidence.append(
                    MemoryEvidence(
                        file_path=target_path,
                        title=target.title,
                        snippet=snippet,
                        relevance=result.relevance * edge.weight,
                        source="lineage",
                        relation=edge.edge_type.value,
                        relation_reason=edge.reason,
                        exists=exists,
                    )
                )

            for edge in incoming:
                source = await self._get_node_by_id_wrapper(edge.source_id)
                incoming_path = _bundle_memory_path(source.file_path) if source and source.file_path else None
                if source is None or incoming_path is None:
                    continue
                related_node_ids.append(source.node_id)
                snippet = await self._get_snippet_wrapper(source.node_id)
                exists = (workspace / incoming_path).is_file()
                trace.append(
                    MemoryTrace(
                        relation=edge.edge_type.value,
                        file_path=incoming_path,
                        title=source.title,
                        snippet=snippet,
                        reason=edge.reason,
                        direction="earlier",
                        exists=exists,
                    )
                )
                evidence.append(
                    MemoryEvidence(
                        file_path=incoming_path,
                        title=source.title,
                        snippet=snippet,
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
            primary_path = _bundle_memory_path(str(resolution.get("resolved_path") or ""))
            if primary_path is None:
                continue
            if primary_path in seen_primary_paths:
                continue
            seen_primary_paths.add(primary_path)

            if primary_path != source_path and not any(item.file_path == primary_path for item in evidence):
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
                            snippet=await self._get_snippet_wrapper(primary_node.node_id),
                            relevance=result.relevance,
                            source="lineage",
                            relation="canonical",
                            relation_reason=str(resolution.get("note") or ""),
                            exists=(workspace / primary_path).is_file(),
                        )
                    )

            corrections = await list_memory_corrections(
                self._db,
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

    async def _get_or_create_file_node_from_workspace(self, file_path: str) -> MemoryNode:
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
            tuple[MemoryNode, list[dict[str, str]], frozenset[str], float, float, tuple[str, ...]]
        ] = [(node, [], frozenset({node.node_id}), 0.0, 0.0, ())]
        resolved: list[tuple[int, float, float, tuple[str, ...], str, list[dict[str, str]]]] = []

        for depth in range(1, max_depth + 1):
            next_frontier: list[
                tuple[MemoryNode, list[dict[str, str]], frozenset[str], float, float, tuple[str, ...]]
            ] = []
            for current, lineage, visited, weight, created_at, edge_ids in sorted(
                frontier,
                key=lambda item: (-item[3], -item[4], item[5], item[0].node_id),
            ):
                outgoing, _ = await get_lineage_edges(self._db, current.node_id)
                candidates = [
                    edge
                    for edge in outgoing
                    if edge.edge_type in CANONICAL_EDGE_TYPES and edge.target_id not in visited
                ]
                for edge in sorted(
                    candidates,
                    key=lambda item: (-float(item.weight), -float(item.created_at), item.edge_id),
                ):
                    target = await self._get_node_by_id_wrapper(edge.target_id)
                    if target is None or not target.file_path:
                        continue
                    target_eligibility = assess_indexed_document_path(target.file_path)
                    if not target_eligibility.eligible:
                        continue
                    target_path = target_eligibility.path
                    current_eligibility = assess_indexed_document_path(current.file_path)
                    current_path = current_eligibility.path if current_eligibility.eligible else ""
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
        for marker in ("_research", "-research", "_draft", "-draft", "_old", "-old", "_notes", "-notes"):
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
        if corrections:
            latest = sorted(corrections, key=lambda item: item.created_at, reverse=True)[0]
            return f"最新修正：{latest.message}"

        primary = next((item for item in evidence if item.file_path == primary_path), None)
        if primary is None and evidence:
            primary = evidence[0]
        snippet = " ".join(((primary.snippet if primary else "") or "").split())
        if snippet:
            return f"当前以 {primary_path} 为主要依据：{snippet[:220]}"
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
            notes.append("命中的早期路径已经有后续整理/迁移，回答时应同时承认早期记录和当前文件。")
        if any(not item.exists for item in evidence):
            notes.append("部分证据文件当前不在工作空间中，只能作为历史轨迹参考。")
        if corrections:
            notes.append("存在显式修正，最新修正优先于早期笔记的字面结论。")
        return " ".join(notes)

    # --------------------------------------------------------
    # 衰减与统计（封装模块函数）
    # --------------------------------------------------------

    def compute_memory_strength(self, node: MemoryNode) -> float:
        """计算记忆强度。"""
        return compute_memory_strength(node, self.DECAY_LAMBDA)

    async def apply_decay(self) -> int:
        """应用遗忘衰减。"""
        return await apply_decay(self._db)

    async def get_file_relations(
        self,
        file_path: str,
        depth: int = 1,
        min_strength: float = 0.2,
    ) -> Dict[str, Any]:
        """获取文件的关联图谱。"""
        return await get_file_relations(
            db=self._db,
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
        return await get_stats(self._db)

    async def health_snapshot(self) -> Dict[str, Any]:
        """获取隔离的只读记忆健康快照，不修复或删除任何数据。"""
        async with self._lifecycle_lock:
            self._require_db()
            db_path = self._get_db_path()
            collection = self._chunk_collection or self._chroma_collection
        return await collect_health_snapshot(
            db_path,
            self._get_workspace_path(),
            collection,
        )

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
        return await dream_walk(
            db=self._db,
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
        return await list_dream_candidate_nodes(self._db, limit)

    async def list_random_file_nodes(self, limit: int = 15) -> List[Dict[str, Any]]:
        """随机采样文件节点。"""
        return await list_random_file_nodes(self._db, limit)

    async def prune_weak_edges(self, threshold: float = 0.08) -> int:
        """修剪弱 ASSOCIATES 边。"""
        return await prune_weak_edges(self._db, threshold)
