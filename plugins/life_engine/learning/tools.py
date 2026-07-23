"""自学习系统工具：暴露给主体的主动学习接口。

让她可以主动参与学习过程：
- nucleus_reflect_now: 主动触发反思
- nucleus_list_insights: 查看洞察账本
- nucleus_challenge_insight: 质疑某条洞察
- nucleus_view_knowledge: 查看自我认知文档
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..core.config import LifeEngineConfig
from .models import Evidence, EvidenceKind, InsightStatus
from .store import InsightStore

logger = log_api.get_logger("life_engine.learning")


def _get_workspace(plugin: Any) -> Path:
    config = getattr(plugin, "config", None)
    if isinstance(config, LifeEngineConfig):
        workspace = config.settings.workspace_path
    else:
        workspace = str(
            Path(__file__).parent.parent.parent.parent / "data" / "life_engine_workspace"
        )
    path = Path(workspace).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _get_scheduler(plugin: Any) -> Any:
    """获取 LearningScheduler 实例。"""
    try:
        from ..service.registry import get_life_engine_service
        service = get_life_engine_service()
        if service is not None:
            return getattr(service, "_learning_scheduler", None)
    except Exception:
        pass
    return None


class LifeReflectNowTool(BaseTool):
    """主动触发一次反思。"""

    tool_name: str = "nucleus_reflect_now"
    tool_description: str = (
        "主动停下来反思。你可以指定反思的内容（最近的一段经历、一个困惑、一次感受），"
        "系统会从中提取可能的洞察。反思有冷却时间，不必频繁使用。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        reflection_text: Annotated[
            str, "你想反思的内容：最近的一段经历、一个困惑、或一次感受"
        ] = "",
        reflection_type: Annotated[
            str, "反思类型：'interaction'（关于与人互动）或 'introspection'（关于自己）"
        ] = "introspection",
    ) -> tuple[bool, str | dict]:
        text = str(reflection_text or "").strip()
        if not text:
            return False, "请告诉我你想反思什么。"

        scheduler = _get_scheduler(self.plugin)
        if scheduler is None:
            return False, "学习系统未初始化。"

        if not scheduler.reflection.can_reflect:
            return False, "反思还在冷却中，稍后再试。"

        try:
            rtype = str(reflection_type or "introspection").strip().lower()
            if rtype == "interaction":
                insights = await scheduler.reflection.reflect_on_interaction(
                    interaction_text=text,
                    context="主体主动发起的反思",
                )
            else:
                insights = await scheduler.reflection.reflect_on_internal(
                    internal_text=text,
                    context="主体主动发起的内省",
                )

            if not insights:
                return True, {
                    "action": "reflect_now",
                    "insights_count": 0,
                    "note": "反思完成，这次没有产生新的洞察。这也很正常。",
                }
            return True, {
                "action": "reflect_now",
                "insights_count": len(insights),
                "insights": [
                    {"id": ins.insight_id, "claim": ins.claim, "category": ins.category}
                    for ins in insights
                ],
                "note": f"反思产生了 {len(insights)} 条新洞察。",
            }
        except Exception as exc:
            return False, f"反思失败: {exc}"


class LifeListInsightsTool(BaseTool):
    """查看洞察账本。"""

    tool_name: str = "nucleus_list_insights"
    tool_description: str = (
        "查看你的洞察实验账本：你从经历中学到了什么、正在验证什么、已经确认了什么。"
        "可以按状态筛选，也可以查看全部。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        status_filter: Annotated[
            str, "按状态筛选：'candidate'/'validated'/'rejected'/'all'（默认 all）"
        ] = "all",
        limit: Annotated[int, "最多显示条数（默认 10）"] = 10,
    ) -> tuple[bool, str | dict]:
        workspace = _get_workspace(self.plugin)
        store = InsightStore(workspace)
        store.load()

        status = str(status_filter or "all").strip().lower()
        max_items = max(1, min(50, int(limit or 10)))

        if status == "all":
            insights = store.list_all()
        else:
            insights = store.list_by_status(status)

        if not insights:
            return True, {
                "action": "list_insights",
                "count": 0,
                "insights": [],
                "stats": store.get_stats(),
                "note": "账本里还没有洞察。",
            }

        # 按更新时间排序
        insights = sorted(insights, key=lambda i: i.updated_at, reverse=True)[:max_items]
        items = []
        for ins in insights:
            items.append({
                "id": ins.insight_id,
                "category": ins.category,
                "claim": ins.claim,
                "status": ins.status,
                "confidence": round(ins.confidence, 2),
                "evidence_count": len(ins.evidence),
                "topic": ins.topic_key,
            })

        return True, {
            "action": "list_insights",
            "count": len(items),
            "insights": items,
            "stats": store.get_stats(),
        }


class LifeChallengeInsightTool(BaseTool):
    """主动质疑某条洞察（添加反面证据）。"""

    tool_name: str = "nucleus_challenge_insight"
    tool_description: str = (
        "主动质疑你的一条洞察。如果你觉得某条认知可能不对、有例外、或过于绝对，"
        "用这个工具为它添加反面证据。这是防止自欺的重要方式。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        insight_id: Annotated[str, "要质疑的洞察 ID"],
        challenge: Annotated[str, "反面证据/质疑理由：为什么你觉得这条可能不对"],
    ) -> tuple[bool, str | dict]:
        iid = str(insight_id or "").strip()
        text = str(challenge or "").strip()
        if not iid:
            return False, "请指定要质疑的洞察 ID。"
        if not text:
            return False, "请说明你的质疑理由。"

        workspace = _get_workspace(self.plugin)
        store = InsightStore(workspace)
        store.load()

        insight = store.get_insight(iid)
        if insight is None:
            return False, f"未找到洞察 {iid}。"

        evidence = Evidence.create(
            kind=EvidenceKind.COUNTER_EXAMPLE,
            description=text,
            supports=False,
            weight=1.5,  # 反面证据权重稍高
            context="主体主动质疑",
        )
        store.add_evidence(iid, evidence)

        # 如果反面证据足够多，降低置信度
        if insight.negative_evidence_count >= 2:
            insight.confidence = max(0.1, insight.confidence - 0.2)
            store.update_insight(insight)

        return True, {
            "action": "challenge_insight",
            "insight_id": iid,
            "claim": insight.claim,
            "evidence_added": evidence.evidence_id,
            "total_evidence": len(insight.evidence),
            "positive": insight.positive_evidence_count,
            "negative": insight.negative_evidence_count,
            "note": "反面证据已记录。质疑自己是勇气，不是软弱。",
        }


class LifeViewKnowledgeTool(BaseTool):
    """查看当前自我认知文档。"""

    tool_name: str = "nucleus_view_knowledge"
    tool_description: str = (
        "查看你当前的自我认知文档——基于你验证过的经历整理出的对自己的理解。"
        "也可以查看学习系统的整体状态。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        show_stats: Annotated[bool, "是否同时显示学习统计（默认 true）"] = True,
    ) -> tuple[bool, str | dict]:
        workspace = _get_workspace(self.plugin)
        store = InsightStore(workspace)
        store.load()

        knowledge = store.read_current_knowledge()
        manifest = store.load_knowledge_manifest()
        version = int(manifest.get("current_version", 0))

        result: dict[str, Any] = {
            "action": "view_knowledge",
            "version": version,
            "knowledge": knowledge or "（还没有形成自我认知文档。需要积累并验证更多洞察。）",
        }

        if show_stats:
            from .metrics import LearningMetrics
            metrics = LearningMetrics(store=store)
            result["stats_summary"] = metrics.format_summary()

        return True, result


LEARNING_TOOLS = [
    LifeReflectNowTool,
    LifeListInsightsTool,
    LifeChallengeInsightTool,
    LifeViewKnowledgeTool,
]
