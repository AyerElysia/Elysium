"""Life Engine 记忆系统工具集。

为中枢提供仿生记忆能力：
- 语义检索 + 联想
- 建立文件关联
- 查看关联图谱
- 主动遗忘
"""

from __future__ import annotations

from typing import Annotated, Any, List, Optional

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from .edges import EdgeType
from .eligibility import assess_document_path
from .experience import MemorySearchMode
from .lineage import MemoryBundle
from .service import LifeMemoryService

logger = log_api.get_logger("life_engine.memory_tools")


def _eligible_path_or_error(file_path: str) -> tuple[str | None, str | None]:
    """Normalize a tool path without allowing runtime/internal memory domains."""
    decision = assess_document_path(file_path)
    if decision.eligible:
        return decision.path, None
    return None, f"不是可操作的记忆文档: {decision.reason}"


def _bundle_to_payload(bundle: MemoryBundle) -> dict[str, Any]:
    """将可追溯记忆包压成工具返回结构。"""
    return {
        "primary_path": bundle.primary_path,
        "current_understanding": bundle.current_understanding,
        "evidence_files": [
            {
                "file_path": item.file_path,
                "title": item.title,
                "snippet": item.snippet,
                "relevance": round(item.relevance, 3),
                "source": item.source,
                "relation": item.relation,
                "relation_reason": item.relation_reason,
                "exists": item.exists,
            }
            for item in bundle.evidence
        ],
        "history_trace": [
            {
                "direction": item.direction,
                "relation": item.relation,
                "file_path": item.file_path,
                "title": item.title,
                "snippet": item.snippet,
                "reason": item.reason,
                "exists": item.exists,
            }
            for item in bundle.history_trace
        ],
        "corrections": [
            {
                "topic": item.topic,
                "message": item.message,
                "source": item.source,
                "created_at": item.created_at,
            }
            for item in bundle.corrections
        ],
        "uncertainty": bundle.uncertainty,
    }


# ============================================================
# nucleus_search_memory - 语义检索 + 联想
# ============================================================

class LifeEngineSearchMemoryTool(BaseTool):
    """语义检索 + 联想工具。"""

    tool_name: str = "nucleus_search_memory"
    tool_description: str = (
        "搜索记忆并触发联想。结合关键词和语义检索，找到相关的记忆。"
        "\n\n"
        "**何时使用：**\n"
        "- ✓ 想回忆「我之前对XX有过什么想法」\n"
        "- ✓ 搜索一个主题的所有相关记忆\n"
        "- ✓ 探索记忆之间的潜在联系\n"
        "\n"
        "**何时不用：**\n"
        "- ✗ 知道确切文件路径 → 用 nucleus_read_file\n"
        "- ✗ 搜索文件中的具体关键词 → 用 nucleus_grep_file\n"
        "\n"
        "**💡 联想结果怎么看：**\n"
        "- source='direct'：直接命中的记忆\n"
        "- source='associated'：通过关联路径联想到的，association_path 显示联想路线\n"
        "- memory_bundles：当前理解 + 历史轨迹 + 修正记录；旧记忆不会被删除，会作为演化证据保留\n"
        "\n"
        "**认识论边界：** search_mode 必须显式选择；相关性排名不等于事实置信度。"
        "第一人称见证表达爱莉如何经历，不自动证明其中的外部事实。\n\n"
        "**注意：** 搜索和联想是只读操作，不会自动增强激活强度或创建/强化关联边。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def _get_service(self) -> LifeMemoryService:
        """获取记忆服务实例。"""
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        if service is None or service._memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return service._memory_service

    async def execute(
        self,
        query: Annotated[str, "搜索问题"],
        top_k: Annotated[int, "返回数量"] = 5,
        enable_association: Annotated[bool, "是否启用联想"] = True,
        file_types: Annotated[Optional[List[str]], "限定文件类型"] = None,
        time_range_days: Annotated[int, "时间范围（天），0=不限"] = 0,
        search_mode: Annotated[
            str,
            "检索模式: current_fact/autobiographical/historical/exploratory",
        ] = "exploratory",
        stream_scope: Annotated[
            Optional[str],
            "可见的聊天流范围；不提供时不跨流读取私有见证",
        ] = None,
        valid_at: Annotated[
            str,
            "查询现实世界中哪个时点有效的主张；ISO 8601，留空表示不限",
        ] = "",
        recorded_as_of: Annotated[
            str,
            "查询系统在何时已经知道的记录；ISO 8601，留空表示当前",
        ] = "",
    ) -> tuple[bool, dict[str, Any]]:
        """执行记忆搜索。"""
        if not query or not query.strip():
            return False, {"error": "query 不能为空"}

        try:
            mode = MemorySearchMode(str(search_mode or "exploratory"))
        except ValueError:
            return False, {
                "error": (
                    "search_mode 必须是 current_fact/autobiographical/"
                    "historical/exploratory 之一"
                )
            }

        try:
            service = await self._get_service()
            # search_memory 默认返回 MemoryBundle（return_bundles=True）。
            # 不再走"先拿 SearchResult、再 build_memory_bundles"的两步走——
            # 那条路要求 service 的返回类型在两次调用之间保持一致，
            # 实际上会因运行时版本差异触发 'MemoryBundle has no attribute file_path'。
            # 直接消费 bundle，从 evidence 里重建 direct/associated 摘要列表。
            bundles = await service.search_memory(
                query.strip(),
                top_k=top_k,
                enable_association=enable_association,
                file_types=file_types,
                time_range_days=time_range_days,
            )
            evidence_results = await service.search_evidence_aware(
                query.strip(),
                mode=mode,
                top_k=top_k,
                stream_scope=stream_scope,
                enable_association=enable_association,
                valid_at=valid_at,
                recorded_as_of=recorded_as_of,
            )

            # 从 bundle.evidence 提取检索摘要，保持和原有 JSON 结构兼容
            seen_paths: set[str] = set()
            direct_results: list[dict] = []
            associated_results: list[dict] = []
            for bundle in bundles:
                for ev in bundle.evidence:
                    fp = str(getattr(ev, "file_path", "") or "").strip()
                    if not fp or fp in seen_paths:
                        continue
                    seen_paths.add(fp)
                    item: dict = {
                        "file_path": fp,
                        "title": getattr(ev, "title", "") or "",
                        "snippet": getattr(ev, "snippet", "") or "",
                        "relevance": round(float(getattr(ev, "relevance", 0) or 0), 3),
                    }
                    src = str(getattr(ev, "source", "") or "")
                    if src == "associated":
                        item["association_path"] = getattr(ev, "relation", "") or ""
                        item["association_reason"] = getattr(ev, "relation_reason", "") or ""
                        associated_results.append(item)
                    else:
                        direct_results.append(item)

            return True, {
                "action": "search_memory",
                "query": query,
                "direct_results": direct_results,
                "associated_results": associated_results,
                "memory_bundles": [_bundle_to_payload(bundle) for bundle in bundles],
                "search_mode": mode.value,
                "stream_scope": stream_scope,
                "valid_at": valid_at,
                "recorded_as_of": recorded_as_of,
                "evidence_results": [
                    {
                        "record_id": item.record_id,
                        "kind": item.kind,
                        "content": item.content,
                        "rank_score": round(item.rank_score, 6),
                        "confidence": item.confidence,
                        "source": item.source,
                        "valid_from": item.valid_from,
                        "valid_to": item.valid_to,
                        "recorded_at": item.recorded_at,
                        "stream_scope": item.stream_scope,
                        "visibility": item.visibility,
                        "status": item.status,
                        "provenance": list(item.provenance),
                        "metadata": item.metadata,
                    }
                    for item in evidence_results
                ],
                "total_found": len(evidence_results),
            }

        except Exception as e:
            logger.error(f"记忆搜索失败: {e}", exc_info=True)
            return False, {"error": f"搜索失败: {e}"}


# ============================================================
# nucleus_relate_file - 建立文件关联
# ============================================================

class LifeEngineRelateFileTool(BaseTool):
    """建立文件关联工具。"""

    tool_name: str = "nucleus_relate_file"
    tool_description: str = (
        "建立两个记忆之间的关联——让你的记忆真正「活起来」的核心操作。"
        "\n\n"
        "**关联是什么？**\n"
        "就像人类大脑在回忆一件事时会自然联想到相关的事。"
        "你建立的每一条关联，都会在未来检索记忆时帮你「顺藤摸瓜」。"
        "\n\n"
        "**何时应该 relate：**\n"
        "- ✓ 写完日记后，如果日记提到了某个愿景/TODO，relate 它们\n"
        "- ✓ 完成一个 TODO 后，relate 这个 TODO 和记录了感受的日记\n"
        "- ✓ 读到一篇笔记，发现它和之前的想法有延续关系\n"
        "- ✓ 两个文件谈及相同的人、地点、主题\n"
        "- ✓ 一个经历启发了一个新想法\n"
        "\n"
        "**关联类型选择：**\n"
        "- relates：双向关联，最常用（'这两件事都和我对音乐的热爱有关'）\n"
        "- causes：因果，A 直接催生了 B（'那篇反思日记让我下定决心创建这个目标'）\n"
        "- continues：延续，B 是 A 的后续故事（'三月的日记是二月那篇的续集'）\n"
        "- contrasts：对比，A 和 B 有有趣的张力（'一年前我觉得...现在我觉得...'）\n"
        "- refines/corrects/renames/reinterprets：记忆演化链。用于记录旧理解后来如何被整理、修正、迁移或重新解释\n"
        "\n"
        "**reason 怎么写：**\n"
        "❌ 错误：reason='可能有关系' 或 '都是日记'\n"
        "✅ 正确：reason='三月记录了我开始学吉他的决定，这首 TODO 是因此而来的'\n"
        "✅ 正确：reason='去年的反思和今天的日记都在处理同一个关于独处的问题'\n"
        "\n"
        "**🌱 培养习惯：**\n"
        "1. 写了新文件后 → 想一想「它和我记忆里的什么相关？」\n"
        "2. 完成了一件事 → relate 过程文件和感受文件\n"
        "3. 看到了有趣的联系 → 立刻记录下来，不要遗忘"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def _get_service(self) -> LifeMemoryService:
        """获取记忆服务实例。"""
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        if service is None or service._memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return service._memory_service

    async def execute(
        self,
        source_path: Annotated[str, "源文件路径"],
        target_path: Annotated[str, "目标文件路径"],
        relation_type: Annotated[str, "关联类型: relates/causes/continues/contrasts/refines/corrects/renames/reinterprets"],
        reason: Annotated[str, "关联原因（必填，请具体描述）"],
        strength: Annotated[float, "关联强度 0.1-1.0"] = 0.5,
    ) -> tuple[bool, dict[str, Any]]:
        """建立文件关联。"""
        # 参数验证
        if not source_path or not target_path:
            return False, {"error": "source_path 和 target_path 不能为空"}

        if not reason or len(reason.strip()) < 5:
            return False, {"error": "reason 必填且至少 5 个字符，请具体描述关联原因"}

        # 检查模糊原因
        vague_patterns = ["可能", "也许", "大概", "不确定", "或许"]
        if any(p in reason for p in vague_patterns):
            return False, {"error": f"reason 不够具体，避免使用模糊词汇：{vague_patterns}"}

        # 验证关联类型
        valid_types = [
            "relates",
            "causes",
            "continues",
            "contrasts",
            "refines",
            "corrects",
            "renames",
            "reinterprets",
        ]
        if relation_type not in valid_types:
            return False, {"error": f"relation_type 必须是 {valid_types} 之一"}

        source_path, source_error = _eligible_path_or_error(source_path)
        target_path, target_error = _eligible_path_or_error(target_path)
        if source_error or target_error:
            return False, {"error": source_error or target_error}

        # 验证强度
        strength = max(0.1, min(1.0, strength))

        try:
            service = await self._get_service()

            # 只允许真实的、合格的工作区记忆文档进入关联图。
            source_node = await service.get_or_create_workspace_document_node(source_path)
            target_node = await service.get_or_create_workspace_document_node(target_path)

            # 创建边
            edge_type = EdgeType(relation_type)
            edge = await service.create_or_update_edge(
                source_id=source_node.node_id,
                target_id=target_node.node_id,
                edge_type=edge_type,
                reason=reason.strip(),
                strength=strength,
                bidirectional=(relation_type in {"relates", "contrasts"})
            )

            logger.info(f"建立关联: {source_path} --[{relation_type}]--> {target_path}")

            return True, {
                "action": "relate_file",
                "source_path": source_path,
                "target_path": target_path,
                "relation_type": relation_type,
                "reason": reason,
                "strength": strength,
                "edge_id": edge.edge_id
            }

        except Exception as e:
            logger.error(f"建立关联失败: {e}", exc_info=True)
            return False, {"error": f"建立关联失败: {e}"}


# ============================================================
# nucleus_view_relations - 查看关联图谱
# ============================================================

class LifeEngineViewRelationsTool(BaseTool):
    """查看文件关联图谱工具。"""

    tool_name: str = "nucleus_view_relations"
    tool_description: str = (
        "查看文件的关联图谱：这个文件和哪些记忆有关联。\n\n"
        "**何时使用：**\n"
        "- ✓ 想看一篇日记连接了哪些记忆\n"
        "- ✓ 探索一个主题在你记忆网络中的位置\n"
        "- ✓ depth=2 可以看到“朋友的朋友”层级的关联"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def _get_service(self) -> LifeMemoryService:
        """获取记忆服务实例。"""
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        if service is None or service._memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return service._memory_service

    async def execute(
        self,
        file_path: Annotated[str, "文件路径"],
        depth: Annotated[int, "遍历深度 1-3"] = 1,
        min_strength: Annotated[float, "最小关联强度阈值"] = 0.2,
    ) -> tuple[bool, dict[str, Any]]:
        """查看关联图谱。"""
        if not file_path:
            return False, {"error": "file_path 不能为空"}
        file_path, path_error = _eligible_path_or_error(file_path)
        if path_error:
            return False, {"error": path_error}

        depth = max(1, min(3, depth))
        min_strength = max(0.0, min(1.0, min_strength))

        try:
            service = await self._get_service()
            relations = await service.get_file_relations(
                file_path=file_path,
                depth=depth,
                min_strength=min_strength
            )

            if "error" in relations:
                return False, relations

            return True, {
                "action": "view_relations",
                "file_path": file_path,
                **relations
            }

        except Exception as e:
            logger.error(f"查看关联失败: {e}", exc_info=True)
            return False, {"error": f"查看关联失败: {e}"}


# ============================================================
# nucleus_forget_relation - 删除/弱化关联
# ============================================================

class LifeEngineForgetRelationTool(BaseTool):
    """删除或弱化关联工具。"""

    tool_name: str = "nucleus_forget_relation"
    tool_description: str = (
        "删除或弱化两个文件之间的关联。\n\n"
        "- weaken(默认)：将关联强度降低 50%\n"
        "- delete：完全删除关联\n\n"
        "**注意：** 自动建立的 ASSOCIATES 关联会随时间自然衰减，通常无需手动删除。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def _get_service(self) -> LifeMemoryService:
        """获取记忆服务实例。"""
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        if service is None or service._memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return service._memory_service

    async def execute(
        self,
        source_path: Annotated[str, "源文件路径"],
        target_path: Annotated[str, "目标文件路径"],
        mode: Annotated[str, "操作模式: delete/weaken"] = "weaken",
    ) -> tuple[bool, dict[str, Any]]:
        """删除或弱化关联。"""
        if not source_path or not target_path:
            return False, {"error": "source_path 和 target_path 不能为空"}

        if mode not in ("delete", "weaken"):
            return False, {"error": "mode 必须是 'delete' 或 'weaken'"}

        source_path, source_error = _eligible_path_or_error(source_path)
        target_path, target_error = _eligible_path_or_error(target_path)
        if source_error or target_error:
            return False, {"error": source_error or target_error}

        try:
            service = await self._get_service()

            if mode == "delete":
                deleted = await service.delete_edge(source_path, target_path)
                if deleted:
                    return True, {
                        "action": "forget_relation",
                        "mode": "delete",
                        "source_path": source_path,
                        "target_path": target_path,
                        "result": "关联已删除"
                    }
                else:
                    return False, {"error": "未找到关联"}
            else:
                # weaken 模式：降低强度
                source_node = await service.get_node_by_file_path(source_path)
                target_node = await service.get_node_by_file_path(target_path)

                if not source_node or not target_node:
                    return False, {"error": "文件节点不存在"}

                # 获取并更新边
                edges = await service.get_edges_from(source_node.node_id)
                weakened = 0
                for edge in edges:
                    if edge.target_id == target_node.node_id:
                        new_strength = edge.weight * 0.5
                        await service.create_or_update_edge(
                            source_id=edge.source_id,
                            target_id=edge.target_id,
                            edge_type=edge.edge_type,
                            reason=edge.reason,
                            strength=new_strength,
                            bidirectional=edge.bidirectional
                        )
                        weakened += 1

                if weakened > 0:
                    return True, {
                        "action": "forget_relation",
                        "mode": "weaken",
                        "source_path": source_path,
                        "target_path": target_path,
                        "result": f"已弱化 {weakened} 条关联"
                    }
                else:
                    return False, {"error": "未找到关联"}

        except Exception as e:
            logger.error(f"遗忘关联失败: {e}", exc_info=True)
            return False, {"error": f"遗忘关联失败: {e}"}


# ============================================================
# nucleus_memory_stats - 记忆系统统计
# ============================================================

class LifeEngineMemoryStatsTool(BaseTool):
    """记忆系统统计工具。"""

    tool_name: str = "nucleus_memory_stats"
    tool_description: str = "获取记忆系统的统计信息：节点数量、关联数量、平均激活强度等。用于了解记忆网络的整体状态。"
    chatter_allow: list[str] = ["life_engine_internal"]

    def __init__(self, plugin):
        super().__init__(plugin)

    async def _get_service(self) -> LifeMemoryService:
        """获取记忆服务实例。"""
        from ..service import LifeEngineService

        service = LifeEngineService.get_instance()
        if service is None or service._memory_service is None:
            raise RuntimeError("记忆服务未初始化")
        return service._memory_service

    async def execute(self) -> tuple[bool, dict[str, Any]]:
        """获取统计信息。"""
        try:
            service = await self._get_service()
            stats = await service.get_stats()

            return True, {
                "action": "memory_stats",
                **stats
            }

        except Exception as e:
            logger.error(f"获取统计失败: {e}", exc_info=True)
            return False, {"error": f"获取统计失败: {e}"}


# ============================================================
# 工具注册列表
# ============================================================

class NucleusRelationsTool(BaseTool):
    """统一的记忆关系工具（合并原 relate_file + view_relations + forget_relation）。"""

    tool_name: str = "nucleus_relations"
    tool_description: str = (
        "管理记忆文件之间的关系。\n\n"
        "action=add：为文件添加关系/联想\n"
        "action=view：查看某个文件的关系\n"
        "action=forget：删除某个关系"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(self, action: Annotated[str, "操作：add/view/forget"] = "view", **kwargs: object) -> tuple[bool, str | dict]:
        action_value = str(action or "view").strip().lower()
        tool_map = {
            "add": LifeEngineRelateFileTool,
            "relate": LifeEngineRelateFileTool,
            "view": LifeEngineViewRelationsTool,
            "list": LifeEngineViewRelationsTool,
            "forget": LifeEngineForgetRelationTool,
            "remove": LifeEngineForgetRelationTool,
            "delete": LifeEngineForgetRelationTool,
        }
        cls = tool_map.get(action_value, LifeEngineViewRelationsTool)
        tool = cls(plugin=self.plugin)
        return await tool.execute(**kwargs)  # type: ignore[arg-type]


MEMORY_TOOLS = [
    LifeEngineSearchMemoryTool,
    NucleusRelationsTool,
    LifeEngineMemoryStatsTool,
]
