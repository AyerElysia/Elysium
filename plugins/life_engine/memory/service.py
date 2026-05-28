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
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.app.plugin_system.api import log_api

from .nodes import (
    MemoryNode,
    generate_file_node_id,
    get_or_create_file_node,
    get_node_by_file_path,
    migrate_node_identity,
    migrate_file_path,
    update_fts,
    increment_access,
    normalize_file_path,
)
from .edges import (
    MemoryEdge,
    EdgeType,
    create_or_update_edge,
    get_edges_from,
    get_edges_to,
    delete_edge,
    reinforce_coactivated,
)
from .search import (
    SearchResult,
    get_chroma_collection,
    search_memory,
    sync_embedding,
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

    def __init__(self, plugin: Any) -> None:
        """初始化记忆服务。

        Args:
            plugin: 插件实例（用于获取配置）
        """
        self.plugin = plugin
        self._workspace_override: Path | None = None
        if isinstance(plugin, (str, Path)):
            self._workspace_override = Path(plugin)
        self._db: sqlite3.Connection | None = None
        self._initialized = False
        self._chroma_collection = None

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
        """获取并缓存向量集合。"""
        if self._chroma_collection is None:
            self._chroma_collection = await get_chroma_collection(self._get_vector_db_path())
        return self._chroma_collection

    async def initialize(self) -> None:
        """初始化记忆服务。"""
        if self._initialized:
            return

        db_path = self._get_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row

        # 启用外键约束（SQLite 默认关闭）
        self._db.execute("PRAGMA foreign_keys = ON")

        await self._create_tables()

        # 初始化 ChromaDB collection
        self._chroma_collection = await self._get_chroma_collection()

        self._initialized = True
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

    # --------------------------------------------------------
    # 节点操作（封装模块函数）
    # --------------------------------------------------------

    async def get_or_create_file_node(
        self,
        file_path: str,
        title: str = "",
        content: str = "",
    ) -> MemoryNode:
        """获取或创建文件节点。"""
        return await get_or_create_file_node(
            db=self._db,
            file_path=file_path,
            title=title,
            content=content,
            emit_visual_event=self._emit_visual_event,
            update_fts_func=self._update_fts_wrapper,
            migrate_node_identity_func=self._migrate_node_identity_wrapper,
        )

    async def get_node_by_file_path(self, file_path: str) -> Optional[MemoryNode]:
        """根据文件路径获取节点。"""
        return await get_node_by_file_path(
            db=self._db,
            file_path=file_path,
            migrate_node_identity_func=self._migrate_node_identity_wrapper,
        )

    async def migrate_file_path(self, old_path: str, new_path: str) -> bool:
        """迁移文件路径对应的记忆身份。"""
        return await migrate_file_path(
            db=self._db,
            old_path=old_path,
            new_path=new_path,
            migrate_node_identity_func=self._migrate_node_identity_wrapper,
        )

    async def increment_access(self, node_id: str) -> None:
        """增加节点访问计数并更新激活强度。"""
        await increment_access(
            db=self._db,
            node_id=node_id,
            emit_visual_event=self._emit_visual_event,
        )

    async def _migrate_node_identity_wrapper(
        self,
        old_node_id: str,
        new_node_id: str,
        new_file_path: str,
    ) -> bool:
        """节点身份迁移的包装函数。"""
        return await migrate_node_identity(
            db=self._db,
            old_node_id=old_node_id,
            new_node_id=new_node_id,
            new_file_path=new_file_path,
            emit_visual_event=self._emit_visual_event,
            migrate_vector_identity_func=self._migrate_vector_identity,
        )

    async def _migrate_vector_identity(
        self,
        old_node_id: str,
        new_node_id: str,
        new_file_path: str,
    ) -> None:
        """迁移向量库中的节点 ID（尽力而为，不阻塞主流程）。"""
        try:
            old_data = self._chroma_collection.get(
                ids=[old_node_id],
                include=["embeddings", "documents", "metadatas"],
            )
            ids = old_data.get("ids") or []
            if not ids:
                return

            embeddings = old_data.get("embeddings") or []
            documents = old_data.get("documents") or []
            metadatas = old_data.get("metadatas") or []
            if not embeddings:
                return

            metadata = metadatas[0] if metadatas else {}
            if not isinstance(metadata, dict):
                metadata = {}
            metadata["file_path"] = new_file_path

            upsert_kwargs: Dict[str, Any] = {
                "ids": [new_node_id],
                "embeddings": [embeddings[0]],
                "metadatas": [metadata],
            }
            if documents:
                upsert_kwargs["documents"] = [documents[0]]

            self._chroma_collection.upsert(**upsert_kwargs)
            self._chroma_collection.delete(ids=[old_node_id])
        except Exception as e:
            logger.debug(f"向量身份迁移失败 {old_node_id} -> {new_node_id}: {e}")

    async def _update_fts_wrapper(self, node_id: str, title: str, content: str) -> None:
        """FTS 更新的包装函数。"""
        await update_fts(self._db, node_id, title, content)

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
    ) -> List[SearchResult]:
        """混合检索 + 联想。"""
        return await search_memory(
            db=self._db,
            query=query,
            collection=self._chroma_collection,
            top_k=top_k,
            enable_association=enable_association,
            file_types=file_types,
            time_range_days=time_range_days,
            emit_visual_event=self._emit_visual_event,
            increment_access_func=self.increment_access,
            reinforce_coactivated_func=self._reinforce_coactivated_wrapper,
        )

    async def vector_search(self, query: str, top_k: int = 10) -> List[tuple]:
        """向量相似度检索。"""
        return await vector_search(
            query=query,
            collection=self._chroma_collection,
            top_k=top_k,
            filter_existing_func=self._filter_existing_scores_wrapper,
        )

    async def fts_search(self, query: str, top_k: int = 10) -> List[tuple]:
        """全文搜索。"""
        return await fts_search(self._db, query, top_k)

    async def sync_embedding(self, file_path: str, content: str) -> None:
        """同步文件的 embedding 到向量数据库。"""
        await sync_embedding(
            db=self._db,
            collection=self._chroma_collection,
            file_path=file_path,
            content=content,
            get_node_by_file_path_func=self.get_node_by_file_path,
        )

    async def spread_activation(
        self,
        seed_ids: List[str],
        max_depth: int = 2,
        max_results: int = 10,
    ) -> List[tuple]:
        """激活扩散联想。"""
        return await spread_activation(
            db=self._db,
            seed_ids=seed_ids,
            max_depth=max_depth,
            max_results=max_results,
            spread_decay=self.SPREAD_DECAY,
            spread_threshold=self.SPREAD_THRESHOLD,
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
        normalized_paths = [normalize_file_path(path) for path in (related_paths or []) if path]
        if not normalized_paths:
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

        for path in normalized_paths:
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
    ) -> Dict[str, Any]:
        """沿演化链解析旧路径当前对应的文件。

        返回值不会说旧文件“失效”，只说明它后来迁移/整理到了哪里。
        """
        requested_path = normalize_file_path(file_path)
        workspace = self._get_workspace_path()
        if not requested_path:
            return {
                "requested_path": requested_path,
                "resolved_path": requested_path,
                "resolved": False,
                "lineage": [],
                "note": "路径为空",
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

        node = await self.get_node_by_file_path(requested_path)
        if node is not None:
            resolved = await self._resolve_canonical_from_node(node, max_depth=max_depth)
            if resolved is not None:
                return {
                    "requested_path": requested_path,
                    "resolved_path": resolved["path"],
                    "resolved": True,
                    "lineage": resolved["lineage"],
                    "note": "请求路径不在工作空间中，但记忆演化链指向了当前文件",
                }

        candidate_path = await self._find_missing_file_candidate(requested_path)
        if candidate_path:
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
                "note": "请求路径不在工作空间中，已按记忆演化链解析到当前文件",
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
        bundles: list[MemoryBundle] = []
        seen_primary_paths: set[str] = set()

        for result in results:
            if len(bundles) >= max(1, top_k):
                break
            node = await self.get_node_by_file_path(result.file_path)
            if node is None:
                continue

            evidence = [
                MemoryEvidence(
                    file_path=result.file_path,
                    title=result.title,
                    snippet=result.snippet,
                    relevance=result.relevance,
                    source=result.source,
                    exists=(self._get_workspace_path() / result.file_path).exists(),
                )
            ]
            trace: list[MemoryTrace] = []
            related_node_ids = [node.node_id]

            outgoing, incoming = await get_lineage_edges(self._db, node.node_id)
            for edge in outgoing:
                target = await self._get_node_by_id_wrapper(edge.target_id)
                if target is None or not target.file_path:
                    continue
                related_node_ids.append(target.node_id)
                snippet = await self._get_snippet_wrapper(target.node_id)
                exists = (self._get_workspace_path() / target.file_path).exists()
                trace.append(
                    MemoryTrace(
                        relation=edge.edge_type.value,
                        file_path=target.file_path,
                        title=target.title,
                        snippet=snippet,
                        reason=edge.reason,
                        direction="later",
                        exists=exists,
                    )
                )
                evidence.append(
                    MemoryEvidence(
                        file_path=target.file_path,
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
                if source is None or not source.file_path:
                    continue
                related_node_ids.append(source.node_id)
                snippet = await self._get_snippet_wrapper(source.node_id)
                exists = (self._get_workspace_path() / source.file_path).exists()
                trace.append(
                    MemoryTrace(
                        relation=edge.edge_type.value,
                        file_path=source.file_path,
                        title=source.title,
                        snippet=snippet,
                        reason=edge.reason,
                        direction="earlier",
                        exists=exists,
                    )
                )
                evidence.append(
                    MemoryEvidence(
                        file_path=source.file_path,
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
            if canonical is not None:
                resolution = {
                    "requested_path": result.file_path,
                    "resolved_path": canonical["path"],
                    "resolved": True,
                    "lineage": canonical["lineage"],
                    "note": "记忆演化链指向了后续整理文件",
                }
            else:
                resolution = await self.resolve_canonical_path(result.file_path)
            primary_path = str(resolution.get("resolved_path") or result.file_path)
            if primary_path in seen_primary_paths:
                continue
            seen_primary_paths.add(primary_path)

            if primary_path != result.file_path and not any(item.file_path == primary_path for item in evidence):
                primary_node = await self.get_node_by_file_path(primary_path)
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
                            exists=(self._get_workspace_path() / primary_path).exists(),
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
        """检索并返回可追溯记忆包。"""
        results = await self.search_memory(
            query=query,
            top_k=top_k,
            enable_association=enable_association,
            file_types=file_types,
            time_range_days=time_range_days,
        )
        return await self.build_memory_bundles(query=query, results=results, top_k=top_k)

    async def _get_or_create_file_node_from_workspace(self, file_path: str) -> MemoryNode:
        normalized = normalize_file_path(file_path)
        workspace_file = self._get_workspace_path() / normalized
        content = ""
        title = Path(normalized).stem
        if workspace_file.is_file():
            try:
                content = workspace_file.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                logger.debug(f"读取记忆文件用于建链失败 {normalized}: {exc}")
        return await self.get_or_create_file_node(
            normalized,
            title=title,
            content=content,
        )

    async def _resolve_canonical_from_node(
        self,
        node: MemoryNode,
        max_depth: int = 4,
    ) -> Dict[str, Any] | None:
        workspace = self._get_workspace_path()
        visited = {node.node_id}
        current = node
        lineage: list[dict[str, str]] = []

        for _ in range(max_depth):
            outgoing, _ = await get_lineage_edges(self._db, current.node_id)
            candidates = [
                edge
                for edge in outgoing
                if edge.edge_type in CANONICAL_EDGE_TYPES and edge.target_id not in visited
            ]
            if not candidates:
                break
            edge = sorted(candidates, key=lambda item: (item.weight, item.created_at), reverse=True)[0]
            target = await self._get_node_by_id_wrapper(edge.target_id)
            if target is None or not target.file_path:
                break
            visited.add(target.node_id)
            lineage.append(
                {
                    "relation": edge.edge_type.value,
                    "from": current.file_path or current.node_id,
                    "to": target.file_path,
                    "reason": edge.reason,
                }
            )
            current = target
            if (workspace / target.file_path).exists():
                return {"path": target.file_path, "lineage": lineage}

        return None

    async def _find_missing_file_candidate(self, requested_path: str) -> str | None:
        workspace = self._get_workspace_path()
        requested = Path(requested_path)
        suffix = requested.suffix
        stem = requested.stem
        candidate_stems = [stem]
        for marker in ("_research", "-research", "_draft", "-draft", "_old", "-old", "_notes", "-notes"):
            if stem.endswith(marker):
                candidate_stems.append(stem[: -len(marker)])

        search_roots = []
        parent = workspace / requested.parent
        if parent.exists() and parent.is_dir():
            search_roots.append(parent)
        search_roots.append(workspace)

        seen_roots: set[Path] = set()
        for root in search_roots:
            if root in seen_roots:
                continue
            seen_roots.add(root)
            for candidate_stem in candidate_stems:
                if not candidate_stem:
                    continue
                direct = root / f"{candidate_stem}{suffix}"
                if direct.exists() and direct.is_file():
                    return normalize_file_path(str(direct.relative_to(workspace)))

            for path in root.rglob(f"*{suffix}"):
                if not path.is_file():
                    continue
                if path.stem in candidate_stems:
                    return normalize_file_path(str(path.relative_to(workspace)))
        return None

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
    ) -> Dict[str, Any]:
        """REM 做梦游走。"""
        return await dream_walk(
            db=self._db,
            num_seeds=num_seeds,
            seed_ids=seed_ids,
            max_depth=max_depth,
            decay_factor=decay_factor,
            learning_rate=learning_rate,
            emit_visual_event=self._emit_visual_event,
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
