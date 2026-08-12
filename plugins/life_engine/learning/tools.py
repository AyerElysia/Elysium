"""自学习系统工具：暴露给主体的主动学习接口。

让她可以主动参与学习过程：
- nucleus_reflect_now: 主动触发反思
- nucleus_list_insights: 查看洞察账本
- nucleus_challenge_insight: 质疑某条洞察
- nucleus_reconsider_insight: 把已验证的认知拿回来重新想
- nucleus_view_knowledge: 查看自我认知文档
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import uuid4

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..storage.subject_contracts import SubjectDocumentPath
from .decisions import LearningCandidate, LearningDecision
from .models import Evidence, EvidenceKind
from .store import InsightStore

logger = log_api.get_logger("life_engine.learning")


def _get_workspace(plugin: Any) -> Path:
    config = getattr(plugin, "config", None)
    settings = getattr(config, "settings", None)
    workspace = str(getattr(settings, "workspace_path", "") or "").strip()
    if not workspace:
        raise RuntimeError(
            "LearningWorkspaceUnavailable: refusing an implicit production "
            "workspace fallback"
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


def _get_store(plugin: Any) -> InsightStore:
    """获取洞察账本。

    优先复用调度器那一个长生命周期实例。原因是 InsightStore.load() 在
    _loaded 为真时直接 return，而 _save() 整文件覆盖：如果工具各建一个
    store，工具写入的证据会被调度器下一次落盘用它的陈旧内存静默抹掉，
    而且没有任何日志会告诉她东西丢了。共享同一实例，写入即对双方可见。

    拿不到调度器时（未初始化、或单元测试）退回按工作区自建。
    """
    scheduler = _get_scheduler(plugin)
    store = getattr(scheduler, "store", None)
    if isinstance(store, InsightStore):
        store.load()
        return store

    config = getattr(plugin, "config", None)
    storage = getattr(config, "storage", None)
    if bool(getattr(storage, "enabled", False)):
        raise RuntimeError(
            "SelectedLearningStoreUnavailable: the active LearningScheduler "
            "must expose the selected backend; local fallback is forbidden"
        )

    store = InsightStore(_get_workspace(plugin))
    store.load()
    return store


async def _flush_scheduler(plugin: Any) -> None:
    scheduler = _get_scheduler(plugin)
    flush = getattr(scheduler, "flush", None)
    if callable(flush):
        await flush()


def _decision_actor(tool: BaseTool) -> tuple[Any, str]:
    """Resolve the caller from the runtime-bound stream, never from arguments."""

    from ..service.registry import get_life_engine_service

    service = get_life_engine_service()
    if service is None:
        raise RuntimeError("LifeEngineServiceUnavailable")
    actor = service.resolve_consciousness_instance(tool.get_current_stream_id())
    instance = service.consciousness_registry.get(actor)
    if instance is None or not instance.is_active:
        raise PermissionError("LearningDecisionActorIsNotActive")
    return service, actor


def _decision_occurrence(tool: BaseTool, material: str) -> str:
    message_identity = str(
        getattr(tool.trigger_message, "message_id", "") or ""
    ).strip()
    if not message_identity:
        return f"learning_skill_decision:{uuid4().hex}"
    digest = hashlib.sha256(
        f"{message_identity}\0{tool.get_current_stream_id()}\0{material}".encode()
    ).hexdigest()
    return f"learning_skill_decision:{digest}"


def _subject_review_occurrence(tool: BaseTool, material: str) -> str:
    message_identity = str(
        getattr(tool.trigger_message, "message_id", "") or ""
    ).strip()
    if not message_identity:
        return f"subject_review:{uuid4().hex}"
    digest = hashlib.sha256(
        f"{message_identity}\0{tool.get_current_stream_id()}\0{material}".encode()
    ).hexdigest()
    return f"subject_review:{digest}"


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

        try:
            _, actor = _decision_actor(self)
            rtype = str(reflection_type or "introspection").strip().lower()
            if rtype == "interaction":
                insights = await scheduler.submit_reflection(
                    reflection_kind="interaction",
                    reflection_text=text,
                    context="主体主动发起的反思",
                    actor_consciousness_instance_id=actor,
                )
            else:
                insights = await scheduler.submit_reflection(
                    reflection_kind="introspection",
                    reflection_text=text,
                    context="主体主动发起的内省",
                    actor_consciousness_instance_id=actor,
                )
            await scheduler.flush()

            if insights is None:
                return True, {
                    "action": "reflect_now",
                    "insights_count": 0,
                    "queued": True,
                    "note": "反思请求已保存，会在冷却结束后继续。",
                }
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
            return False, f"反思失败: {type(exc).__name__}"


class LifeListInsightsTool(BaseTool):
    """查看洞察账本。"""

    tool_name: str = "nucleus_list_insights"
    tool_description: str = (
        "查看学习系统的洞察实验账本：候选、审计支持、审计不支持都只是可修订的"
        "账本状态，不等于主体真值或身份结论。可以按状态筛选，也可以查看全部。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        status_filter: Annotated[
            str, "按状态筛选：'candidate'/'validated'/'rejected'/'all'（默认 all）"
        ] = "all",
        limit: Annotated[int, "最多显示条数（默认 10）"] = 10,
    ) -> tuple[bool, str | dict]:
        store = _get_store(self.plugin)

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
        insights = sorted(insights, key=lambda i: i.updated_at, reverse=True)[
            :max_items
        ]
        items = []
        for ins in insights:
            item: dict[str, Any] = {
                "id": ins.insight_id,
                "category": ins.category,
                "claim": ins.claim,
                "status": ins.status,
                "confidence": round(ins.confidence, 2),
                "evidence_count": len(ins.evidence),
                "topic": ins.topic_key,
            }
            # 反例数量：有反例的时候才提，避免每条都挂个"你要不要怀疑一下"
            if ins.negative_evidence_count:
                item["negative_evidence"] = ins.negative_evidence_count
            # 这条进过哪些版本的自我认知文档——想重新想它的时候需要知道
            if ins.knowledge_versions:
                item["in_knowledge_versions"] = list(ins.knowledge_versions)
            if ins.revision_note:
                item["revision_note"] = ins.revision_note
            items.append(item)

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

        store = _get_store(self.plugin)

        insight = store.get_insight(iid)
        if insight is None:
            return False, f"未找到洞察 {iid}。"

        evidence = Evidence.create(
            kind=EvidenceKind.COUNTER_EXAMPLE,
            description=text,
            supports=False,
            context="主体主动质疑",
        )
        store.add_evidence(iid, evidence)
        await _flush_scheduler(self.plugin)

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


class LifeReconsiderInsightTool(BaseTool):
    """把一条已经下过判断的认知拿回来重新想。

    学习是螺旋上升的：验证过不等于永远为真。她可以随时把任何一条
    已经 validated（哪怕已经写进自我认知文档）的认知拿回来重新审视。
    旧的知识文档版本不会被删改——修正会体现在下一个版本里。
    """

    tool_name: str = "nucleus_reconsider_insight"
    tool_description: str = (
        "把一条已经验证过的认知拿回来重新想一想。如果你觉得某条认知不再符合现在的你、"
        "或者你想重新检验它，用这个工具让它重新进入审视流程。"
        "已发布的自我认知文档不会被改动，修正会出现在下一个版本里。"
        "这不是承认自己错了，只是保留改变想法的余地。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        insight_id: Annotated[str, "要重新审视的洞察 ID"],
        reason: Annotated[str, "为什么想重新想这条：现在的你怎么看它"] = "",
    ) -> tuple[bool, str | dict]:
        iid = str(insight_id or "").strip()
        note = str(reason or "").strip()
        if not iid:
            return False, "请指定要重新审视的洞察 ID。"

        store = _get_store(self.plugin)
        insight = store.get_insight(iid)
        if insight is None:
            return False, f"未找到洞察 {iid}。"

        old_status = insight.status
        kvs = list(insight.knowledge_versions)

        ok = store.reconsider_insight(iid, reason=note)
        if not ok:
            return False, f"重新审视 {iid} 失败。"
        await _flush_scheduler(self.plugin)

        return True, {
            "action": "reconsider_insight",
            "insight_id": iid,
            "claim": insight.claim,
            "from_status": old_status,
            "to_status": insight.status,
            "in_knowledge_versions": kvs,
            "evidence_kept": len(insight.evidence),
            "negative_evidence": insight.negative_evidence_count,
            "revision_note": insight.revision_note,
            "note": (
                "这条已经回到待审视队列，证据和审计历史都保留着。"
                + (
                    f"\n它出现在自我认知 v{kvs[-1]} 里。那个版本仍然是原样保存的历史，"
                    "下一次整理时会带上你这次的想法。"
                    if kvs
                    else ""
                )
            ),
        }


class LifeViewKnowledgeTool(BaseTool):
    """查看当前学习派生观察账本。"""

    tool_name: str = "nucleus_view_knowledge"
    tool_description: str = (
        "查看学习系统根据历史证据整理的派生观察账本。它不是 SOUL/USER/MEMORY "
        "主体权威，只能作为当前意识可质疑、可重审的材料；也可查看账本整体状态。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        show_stats: Annotated[bool, "是否同时显示学习统计（默认 true）"] = True,
    ) -> tuple[bool, str | dict]:
        store = _get_store(self.plugin)

        knowledge = store.read_current_knowledge()
        manifest = store.load_knowledge_manifest()
        version = int(manifest.get("current_version", 0))

        result: dict[str, Any] = {
            "action": "view_knowledge",
            "version": version,
            "authority": "derived_learning_observation",
            "authoritative": False,
            "knowledge": knowledge or "（还没有形成学习派生观察账本。）",
        }

        if show_stats:
            from .metrics import LearningMetrics

            metrics = LearningMetrics(store=store)
            result["stats_summary"] = metrics.format_summary()

        return True, result


class LifeReviewSubjectDocumentTool(BaseTool):
    """Review SOUL/USER without granting a generic MEMORY write path."""

    tool_name = "nucleus_review_subject_document"
    tool_description = (
        "读取并复盘当前权威 SOUL.md 或 USER.md。status 返回统一 revision、"
        "精确内容哈希，并可按 UTF-8 字节窗口返回目标文档原文；unchanged 明确记录当前版本"
        "保持不变；snooze 表示稍后再看；propose 只提交完整新版本候选，绝不会自动接受。"
        "MEMORY.md 的读取、短文字整理、长记忆索引、候选核对和决定统一使用 "
        "nucleus_memory_continuity_review。"
    )
    chatter_allow = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        action: Annotated[str, "status|unchanged|snooze|propose"] = "status",
        target_path: Annotated[
            str,
            "SOUL.md|USER.md；status 留空时返回全部文档的内容无关健康摘要",
        ] = "",
        expected_subject_revision: Annotated[
            str,
            "status 返回的统一 SOUL+USER+MEMORY revision",
        ] = "",
        reviewed_content_sha256: Annotated[
            str,
            "status 返回的目标文件精确内容 SHA-256",
        ] = "",
        reason: Annotated[str, "当前意识作出此复盘选择的理由"] = "",
        proposed_content: Annotated[
            str,
            "仅 propose：当前意识拟议的完整目标文档，不是局部补丁",
        ] = "",
        snooze_hours: Annotated[
            float,
            "仅 snooze：延后多久再次邀请，1-720 小时",
        ] = 24.0,
        offset_bytes: Annotated[
            int,
            "仅 status 且指定 target_path：从哪个 UTF-8 字节偏移开始读取原文",
        ] = 0,
        max_bytes: Annotated[
            int,
            "仅 status 且指定 target_path：本次最多读取 1024-32768 字节",
        ] = 16384,
    ) -> tuple[bool, str | dict]:
        scheduler = _get_scheduler(self.plugin)
        if scheduler is None:
            return False, "学习系统未初始化。"
        normalized = str(action or "status").strip().lower()
        if normalized not in {"status", "unchanged", "snooze", "propose"}:
            return False, "action 必须是 status/unchanged/snooze/propose。"

        if normalized == "status":
            try:
                snapshot = await scheduler.get_subject_review_snapshot(
                    mark_offered=False
                )
            except Exception as exc:  # noqa: BLE001 - explicit tool refusal
                return False, f"主体复盘状态不可用: {type(exc).__name__}"
            selected_path = str(target_path or "").strip()
            if selected_path == "MEMORY.md":
                return False, (
                    "MemoryContinuityReviewRequired: MEMORY.md 只通过 "
                    "nucleus_memory_continuity_review 读取和治理。"
                )
            if selected_path:
                documents = [
                    item
                    for item in snapshot["documents"]
                    if item.get("target_path") == selected_path
                ]
                if not documents:
                    return False, "target_path 必须是 SOUL.md/USER.md。"
                raw = await scheduler.read_subject_document(
                    cast(SubjectDocumentPath, selected_path)
                )
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    return False, "当前主体文档不是 UTF-8 文本，拒绝通过文本工具呈现。"
                offset = max(0, int(offset_bytes or 0))
                budget = max(1024, min(32768, int(max_bytes or 16384)))
                if offset > len(raw):
                    return False, "offset_bytes 超出主体文档原文长度。"
                try:
                    raw[:offset].decode("utf-8")
                except UnicodeDecodeError:
                    return False, "offset_bytes 必须位于 UTF-8 字符边界。"
                end = min(len(raw), offset + budget)
                while end > offset:
                    try:
                        content = raw[offset:end].decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        end -= 1
                else:
                    content = ""
                snapshot = {
                    **snapshot,
                    "documents": documents,
                    "content": content,
                    "offset_bytes": offset,
                    "next_offset": end if end < len(raw) else None,
                    "has_more": end < len(raw),
                    "total_bytes": len(raw),
                }
            return True, {"action": "subject_review_status", **snapshot}

        target = str(target_path or "").strip()
        if target == "MEMORY.md":
            return False, (
                "MemoryContinuityReviewRequired: MEMORY.md 只通过 "
                "nucleus_memory_continuity_review 读取和治理。"
            )
        if target not in {"SOUL.md", "USER.md"}:
            return False, "target_path 必须是 SOUL.md/USER.md。"
        if not str(reason or "").strip():
            return False, "复盘选择必须填写 reason。"

        try:
            _, actor = _decision_actor(self)
            current_revision = await scheduler.validate_subject_review_context(
                actor_consciousness_instance_id=actor,
                expected_subject_revision=str(expected_subject_revision),
            )
            current_bytes = await scheduler.read_subject_document(
                cast(SubjectDocumentPath, target)
            )
            current_hash = hashlib.sha256(current_bytes).hexdigest()
            if current_hash != str(reviewed_content_sha256 or "").strip().lower():
                return False, (
                    "目标文件内容已变化或尚未按 status 的精确哈希复盘；"
                    "请重新读取 status 和文件原文。"
                )
            occurrence = _subject_review_occurrence(
                self,
                "\0".join(
                    (
                        normalized,
                        target,
                        current_revision,
                        current_hash,
                        hashlib.sha256(
                            str(proposed_content).encode("utf-8")
                        ).hexdigest(),
                    )
                ),
            )
            typed_target = cast(SubjectDocumentPath, target)

            if normalized == "snooze":
                record = await scheduler.record_subject_review_outcome(
                    target_path=typed_target,
                    outcome="snoozed",
                    actor_consciousness_instance_id=actor,
                    subject_revision=current_revision,
                    occurrence_id=occurrence,
                    reason=str(reason),
                    snooze_hours=max(1.0, min(720.0, float(snooze_hours))),
                )
                return True, {
                    "action": "subject_review_snoozed",
                    "target_path": target,
                    "subject_revision": current_revision,
                    "occurrence_id": occurrence,
                    "snooze_until": record.get("snooze_until", ""),
                }

            ledger = getattr(scheduler, "decision_ledger", None)
            if normalized == "propose" and ledger is None:
                return False, (
                    "SubjectAuthorityMigrationRequired: 正式主体存储迁移尚未完成；"
                    "新版本候选已拒绝提交，系统不会退回直接写 SOUL/USER/MEMORY。"
                )

            candidate_content = (
                str(proposed_content).encode("utf-8")
                if normalized == "propose"
                else current_bytes
            )
            if len(candidate_content) > 240 * 1024:
                return False, "完整候选超过单次 240 KiB 安全上限。"
            if (
                normalized == "propose"
                and target == "SOUL.md"
                and not candidate_content.strip()
            ):
                return False, "SOUL.md 候选不能为空。"

            if ledger is None:
                await scheduler.record_subject_review_outcome(
                    target_path=typed_target,
                    outcome="unchanged",
                    actor_consciousness_instance_id=actor,
                    subject_revision=current_revision,
                    occurrence_id=occurrence,
                    reason=str(reason),
                )
                return True, {
                    "action": "subject_review_unchanged",
                    "target_path": target,
                    "subject_revision": current_revision,
                    "content_sha256": current_hash,
                    "occurrence_id": occurrence,
                    "authority_status": "migration_required",
                }

            candidate_hash = hashlib.sha256(candidate_content).hexdigest()
            candidate_id = f"{occurrence.replace(':', '_')}_{candidate_hash[:16]}"
            source_occurrence_id = str(
                getattr(self.trigger_message, "message_id", "") or occurrence
            )
            candidate = LearningCandidate.create(
                candidate_id=candidate_id,
                candidate_revision=1,
                candidate_occurrence_id=f"{occurrence}:candidate",
                candidate_kind=(
                    "subject_document_revision"
                    if normalized == "propose"
                    else "subject_review_no_change"
                ),
                candidate_content_bytes=candidate_content,
                source_occurrence_id=source_occurrence_id,
                source="subject.review.active_consciousness",
                actor_consciousness_instance_id=actor,
                subject_revision=current_revision,
                target_path=typed_target,
                provenance={
                    "surface": "life_engine_tool",
                    "stream_id": self.get_current_stream_id(),
                    "reviewed_content_sha256": current_hash,
                    "review_reason_sha256": hashlib.sha256(
                        str(reason).strip().encode("utf-8")
                    ).hexdigest(),
                    "authority": "candidate_only",
                },
            )
            await ledger.append_candidate(candidate)

            if normalized == "unchanged":
                decision = LearningDecision(
                    decision_occurrence_id=f"{occurrence}:unchanged",
                    decision_kind="kept_open",
                    candidate_id=candidate.candidate_id,
                    candidate_revision=candidate.candidate_revision,
                    candidate_sha256=candidate.candidate_sha256,
                    candidate_occurrence_id=candidate.candidate_occurrence_id,
                    actor_consciousness_instance_id=actor,
                    expected_subject_revision=current_revision,
                    occurred_at=datetime.now(UTC).isoformat(),
                    reason=str(reason),
                    provenance={
                        "surface": "life_engine_tool",
                        "stream_id": self.get_current_stream_id(),
                        "review_outcome": "unchanged",
                    },
                )
                receipt = await ledger.record_decision(decision)
                await scheduler.record_subject_review_outcome(
                    target_path=typed_target,
                    outcome="unchanged",
                    actor_consciousness_instance_id=actor,
                    subject_revision=current_revision,
                    occurrence_id=decision.decision_occurrence_id,
                    reason=str(reason),
                    candidate_id=candidate.candidate_id,
                    candidate_sha256=candidate.candidate_sha256,
                )
                return True, {
                    "action": "subject_review_unchanged",
                    "target_path": target,
                    "subject_revision": current_revision,
                    "content_sha256": current_hash,
                    "candidate_id": candidate.candidate_id,
                    "decision_occurrence_id": receipt.decision_occurrence_id,
                    "authority_status": "selected_ready",
                }

            await scheduler.record_subject_review_outcome(
                target_path=typed_target,
                outcome="candidate_proposed",
                actor_consciousness_instance_id=actor,
                subject_revision=current_revision,
                occurrence_id=candidate.candidate_occurrence_id,
                reason=str(reason),
                candidate_id=candidate.candidate_id,
                candidate_sha256=candidate.candidate_sha256,
            )
            return True, {
                "action": "subject_review_candidate_proposed",
                "target_path": target,
                "candidate_id": candidate.candidate_id,
                "candidate_revision": candidate.candidate_revision,
                "candidate_sha256": candidate.candidate_sha256,
                "candidate_occurrence_id": candidate.candidate_occurrence_id,
                "subject_revision": current_revision,
                "status": "open",
                "note": (
                    "候选仍在主体权威之外。请先重新读取候选；只有另行调用 "
                    "nucleus_decide_subject_candidate accepted 才会尝试提交。"
                ),
            }
        except Exception as exc:  # noqa: BLE001 - explicit tool refusal
            return False, f"主体复盘选择未记录: {type(exc).__name__}"


class LifeListSubjectCandidatesTool(BaseTool):
    """List derived document proposals outside subject authority."""

    tool_name = "nucleus_list_subject_candidates"
    tool_description = (
        "查看学习系统整理出的 SOUL/USER/MEMORY 修改候选。候选不是主体结论，"
        "只有当前活跃意识实例在读完原文和候选后明确决定，才可能进入主体权威。"
        "任何 target_path=MEMORY.md 的历史通用候选都只可审计、不可在这里决定；"
        "MEMORY.md 只能回到 nucleus_memory_continuity_review 读取和决定。"
    )
    chatter_allow = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        status: Annotated[
            str,
            "open|accept_requested|rejected|kept_open|committed|all，默认 open",
        ] = "open",
        limit: Annotated[int, "最多返回多少条，1-50"] = 10,
    ) -> tuple[bool, str | dict]:
        scheduler = _get_scheduler(self.plugin)
        ledger = getattr(scheduler, "decision_ledger", None)
        if ledger is None:
            return False, "主体候选账本仅在统一可选存储就绪后可用。"
        try:
            candidates = await ledger.list_candidates(
                status=str(status),
                limit=max(1, min(50, int(limit or 10))),
            )
        except Exception as exc:  # noqa: BLE001 - explicit tool refusal
            return False, f"主体候选读取失败: {type(exc).__name__}"
        return True, {
            "action": "list_subject_candidates",
            "count": len(candidates),
            "candidates": candidates,
            "note": (
                "所有状态都是可审计流程状态；只有 committed 且带 authority_occurrence_id "
                "才证明主体权威事务已提交；任何 target_path=MEMORY.md 的候选都不能"
                "通过通用决定入口处理。历史通用候选仅供迁移审计。"
            ),
        }


class LifeReadSubjectCandidateTool(BaseTool):
    """Read an exact subject proposal through a bounded byte window."""

    tool_name = "nucleus_read_subject_candidate"
    tool_description = (
        "按 UTF-8 字节窗口读取主体修改候选。若 has_more=true，继续使用 next_offset；"
        "candidate_sha256 用于之后对精确候选作决定。"
    )
    chatter_allow = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        candidate_id: Annotated[str, "主体候选 ID"],
        offset_bytes: Annotated[int, "从哪个 UTF-8 字节偏移开始"] = 0,
        max_bytes: Annotated[int, "本次最多读取 1024-32768 字节"] = 16384,
    ) -> tuple[bool, str | dict]:
        scheduler = _get_scheduler(self.plugin)
        ledger = getattr(scheduler, "decision_ledger", None)
        if ledger is None:
            return False, "主体候选账本仅在统一可选存储就绪后可用。"
        try:
            candidate = await ledger.read_candidate(str(candidate_id))
        except Exception as exc:  # noqa: BLE001 - explicit tool refusal
            return False, f"主体候选读取失败: {type(exc).__name__}"
        if candidate is None:
            return False, f"未找到主体候选 {candidate_id}。"
        is_memory_target = str(candidate.target_path or "").strip() == "MEMORY.md"
        if (
            is_memory_target
            and candidate.candidate_kind == "memory_continuity_document_revision"
        ):
            return False, (
                "MemoryContinuityReviewRequired: 长期记忆连续性候选只能在 "
                "nucleus_memory_continuity_review 中分页读取；通用读取不形成"
                "可供接受使用的精确投递证明。"
            )
        raw = candidate.candidate_content_bytes
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            return False, "该候选不是 UTF-8 主体文档，拒绝通过文本工具呈现。"
        offset = max(0, int(offset_bytes or 0))
        budget = max(1024, min(32768, int(max_bytes or 16384)))
        if offset > len(raw):
            return False, "offset_bytes 超出候选原文长度。"
        try:
            raw[:offset].decode("utf-8")
        except UnicodeDecodeError:
            return False, "offset_bytes 必须位于 UTF-8 字符边界。"
        end = min(len(raw), offset + budget)
        while end > offset:
            try:
                content = raw[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            content = ""
        return True, {
            "action": "read_subject_candidate",
            "candidate_id": candidate.candidate_id,
            "candidate_revision": candidate.candidate_revision,
            "candidate_sha256": candidate.candidate_sha256,
            "candidate_occurrence_id": candidate.candidate_occurrence_id,
            "subject_revision": candidate.subject_revision,
            "target_path": candidate.target_path,
            "offset_bytes": offset,
            "delivered_bytes": end - offset,
            "original_bytes": len(raw),
            "next_offset": end,
            "has_more": end < len(raw),
            "content": content,
            **(
                {
                    "audit_only": True,
                    "migration_required": True,
                    "decision_blocker": "LegacyMemoryCandidateMigrationRequired",
                    "note": (
                        "这是统一连续记忆审查上线前遗留的 MEMORY.md 通用候选；"
                        "本次读取仅用于审计，不构成 exact delivery proof，不能接受、"
                        "拒绝或保持开放，也不会自动转换。"
                    ),
                }
                if is_memory_target
                else {}
            ),
        }


class LifeDecideSubjectCandidateTool(BaseTool):
    """Submit explicit will evidence to the sole SubjectAuthorityPort."""

    tool_name = "nucleus_decide_subject_candidate"
    tool_description = (
        "对精确主体候选作出 accepted/rejected/kept_open 决定。accepted_content 必须是"
        "你最终选择的完整目标文档，不会自动合并；系统会验证活跃意识、候选证据和"
        "当前 SOUL+USER+MEMORY revision，冲突时明确拒绝并要求重读。这个通用入口"
        "只处理 SOUL.md/USER.md；任何 MEMORY.md 候选都必须回到"
        " nucleus_memory_continuity_review。"
    )
    chatter_allow = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        candidate_id: Annotated[str, "主体候选 ID"],
        candidate_revision: Annotated[int, "候选 revision"],
        candidate_sha256: Annotated[str, "候选 SHA-256"],
        expected_subject_revision: Annotated[
            str,
            "读取候选时看到的统一 SOUL+USER+MEMORY revision",
        ],
        decision: Annotated[str, "accepted|rejected|kept_open"],
        reason: Annotated[str, "当前意识作出此决定的理由"],
        accepted_content: Annotated[
            str,
            "仅 accepted 时填写：当前意识最终选择的完整目标文档",
        ] = "",
    ) -> tuple[bool, str | dict]:
        scheduler = _get_scheduler(self.plugin)
        ledger = getattr(scheduler, "decision_ledger", None)
        if ledger is None:
            return False, "主体候选账本仅在统一可选存储就绪后可用。"
        normalized = str(decision or "").strip().lower()
        if normalized not in {"accepted", "rejected", "kept_open"}:
            return False, "decision 必须是 accepted/rejected/kept_open。"
        if not str(reason or "").strip():
            return False, "决定理由不能为空。"
        try:
            candidate = await ledger.read_candidate(str(candidate_id))
            if candidate is None:
                return False, f"未找到主体候选 {candidate_id}。"
            if str(candidate.target_path or "").strip() == "MEMORY.md":
                return False, (
                    "LegacyMemoryCandidateMigrationRequired: 任何 MEMORY.md 目标候选"
                    "都不能通过通用决定入口处理。历史候选保持可读审计但不会自动转换；"
                    "新的长期记忆候选只能在 nucleus_memory_continuity_review 中完整"
                    "读取并决定，以保留 exact full-candidate delivery proof。"
                )
            if candidate.target_path is None:
                return False, "该候选没有主体文档目标，不能走主体接受入口。"
            content_bytes = str(accepted_content).encode("utf-8")
            if normalized == "accepted":
                if not content_bytes:
                    return False, "accepted 必须提供完整 accepted_content。"
                if len(content_bytes) > 240 * 1024:
                    return False, "accepted_content 超过单次 240 KiB 安全上限。"
            elif content_bytes:
                return False, "rejected/kept_open 不能携带 accepted_content。"
            _, actor = _decision_actor(self)
            occurrence = _decision_occurrence(
                self,
                "\0".join(
                    (
                        str(candidate_id),
                        str(candidate_revision),
                        str(candidate_sha256),
                        normalized,
                        str(expected_subject_revision),
                    )
                ),
            )
            decision_record = LearningDecision(
                decision_occurrence_id=occurrence,
                decision_kind=(
                    "accept_requested" if normalized == "accepted" else normalized
                ),
                candidate_id=str(candidate_id),
                candidate_revision=int(candidate_revision),
                candidate_sha256=str(candidate_sha256),
                candidate_occurrence_id=candidate.candidate_occurrence_id,
                actor_consciousness_instance_id=actor,
                expected_subject_revision=str(expected_subject_revision),
                occurred_at=datetime.now(UTC).isoformat(),
                reason=str(reason),
                target_path=(
                    candidate.target_path if normalized == "accepted" else None
                ),
                accepted_content_bytes=(
                    content_bytes if normalized == "accepted" else b""
                ),
                accepted_content_sha256=(
                    hashlib.sha256(content_bytes).hexdigest()
                    if normalized == "accepted"
                    else ""
                ),
                provenance={
                    "surface": "life_engine_tool",
                    "stream_id": self.get_current_stream_id(),
                },
            )
            receipt = (
                await ledger.accept_subject_candidate(decision_record)
                if normalized == "accepted"
                else await ledger.record_decision(decision_record)
            )
        except Exception as exc:  # noqa: BLE001 - explicit tool refusal
            from ..memory.boundary_tools import _recoverable_conflict_payload

            return False, await _recoverable_conflict_payload(
                exc,
                scheduler=scheduler,
                error=type(exc).__name__,
                detail=f"主体候选决定未提交: {type(exc).__name__}: {exc}",
            )
        review_health_warning = ""
        try:
            recorded_revision = (
                await scheduler.current_subject_revision()
                if receipt.status == "committed"
                else str(expected_subject_revision)
            )
            await scheduler.record_subject_review_outcome(
                target_path=candidate.target_path,
                outcome=("committed" if receipt.status == "committed" else normalized),
                actor_consciousness_instance_id=actor,
                subject_revision=recorded_revision,
                occurrence_id=receipt.decision_occurrence_id,
                reason=str(reason),
                candidate_id=receipt.candidate_id,
                candidate_sha256=receipt.candidate_sha256,
                authority_occurrence_id=receipt.authority_occurrence_id,
            )
        except Exception as exc:  # noqa: BLE001 - authority receipt already exists
            review_health_warning = type(exc).__name__
        return True, {
            "action": "decide_subject_candidate",
            "candidate_id": receipt.candidate_id,
            "candidate_revision": receipt.candidate_revision,
            "candidate_sha256": receipt.candidate_sha256,
            "status": receipt.status,
            "decision_occurrence_id": receipt.decision_occurrence_id,
            "authority_occurrence_id": receipt.authority_occurrence_id,
            "committed": receipt.status == "committed",
            **(
                {"review_health_warning": review_health_warning}
                if review_health_warning
                else {}
            ),
        }


class LifeListSkillCandidatesTool(BaseTool):
    """List non-authoritative skill proposals waiting for her decision."""

    tool_name = "nucleus_list_skill_candidates"
    tool_description = (
        "查看后台从经验中整理出的技能候选。候选不会自动成为技能；"
        "你可以继续读取原文，再明确接受、拒绝或保持开放。"
    )
    chatter_allow = ["life_engine_internal"]

    async def execute(
        self,
        status: Annotated[str, "open|accepted|rejected|all，默认 open"] = "open",
        limit: Annotated[int, "最多返回多少条，1-20"] = 10,
    ) -> tuple[bool, str | dict]:
        scheduler = _get_scheduler(self.plugin)
        if scheduler is None:
            return False, "学习系统未初始化。"
        normalized = str(status or "open").strip().lower()
        if normalized not in {"open", "accepted", "rejected", "all"}:
            return False, "status 必须是 open/accepted/rejected/all。"
        candidates = scheduler.skill_store.list_candidates(
            status=None if normalized == "all" else normalized
        )[: max(1, min(20, int(limit or 10)))]
        return True, {
            "action": "list_skill_candidates",
            "count": len(candidates),
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "candidate_revision": item.candidate_revision,
                    "candidate_sha256": item.candidate_sha256,
                    "candidate_occurrence_id": item.candidate_occurrence_id,
                    "subject_revision": item.subject_revision,
                    "target_skill_id": item.target_skill_id,
                    "proposed_skill_id": item.proposed_skill_id,
                    "name": item.name,
                    "status": item.status,
                    "gate_recommended": item.gate_recommended,
                    "source_event_count": len(item.source_event_ids),
                    "insight_count": len(item.insight_ids),
                }
                for item in candidates
            ],
            "note": "gate_recommended 只是独立审查建议，不是你的决定。",
        }


class LifeReadSkillCandidateTool(BaseTool):
    """Read an exact skill candidate through a bounded continuation window."""

    tool_name = "nucleus_read_skill_candidate"
    tool_description = (
        "按字节窗口读取一个技能候选的完整原文。若 has_more=true，使用 next_offset "
        "继续读取；candidate_sha256 用于之后作出精确决定。"
    )
    chatter_allow = ["life_engine_internal"]

    async def execute(
        self,
        candidate_id: Annotated[str, "技能候选 ID"],
        offset_bytes: Annotated[int, "本次从哪个 UTF-8 字节偏移开始"] = 0,
        max_bytes: Annotated[int, "本次最多读取字节数，1024-32768"] = 16384,
    ) -> tuple[bool, str | dict]:
        scheduler = _get_scheduler(self.plugin)
        if scheduler is None:
            return False, "学习系统未初始化。"
        candidate = scheduler.skill_store.get_candidate(candidate_id)
        if candidate is None:
            return False, f"未找到技能候选 {candidate_id}。"
        raw = json.dumps(
            {
                "name": candidate.name,
                "description": candidate.description,
                "instructions": candidate.instructions,
                "insight_ids": candidate.insight_ids,
                "source_event_ids": candidate.source_event_ids,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        offset = max(0, int(offset_bytes or 0))
        budget = max(1024, min(32768, int(max_bytes or 16384)))
        if offset > len(raw):
            return False, "offset_bytes 超出候选原文长度。"
        try:
            raw[:offset].decode("utf-8")
        except UnicodeDecodeError:
            return False, "offset_bytes 必须位于 UTF-8 字符边界。"
        end = min(len(raw), offset + budget)
        while end > offset:
            try:
                text = raw[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        else:
            text = ""
        return True, {
            "action": "read_skill_candidate",
            "candidate_id": candidate.candidate_id,
            "candidate_revision": candidate.candidate_revision,
            "candidate_sha256": candidate.candidate_sha256,
            "subject_revision": candidate.subject_revision,
            "offset_bytes": offset,
            "delivered_bytes": end - offset,
            "original_bytes": len(raw),
            "next_offset": end,
            "has_more": end < len(raw),
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "content": text,
        }


class LifeDecideSkillCandidateTool(BaseTool):
    """Let the current active consciousness instance decide one proposal."""

    tool_name = "nucleus_decide_skill_candidate"
    tool_description = (
        "对精确技能候选作出决定：accepted/rejected/kept_open。接受时可留空改写字段以采用候选原文，"
        "也可给出你自己的完整表述。系统会验证当前意识实例和 SOUL+USER+MEMORY revision。"
    )
    chatter_allow = ["life_engine_internal"]

    async def execute(
        self,
        candidate_id: Annotated[str, "技能候选 ID"],
        candidate_revision: Annotated[int, "候选 revision"],
        candidate_sha256: Annotated[str, "候选 SHA-256"],
        expected_subject_revision: Annotated[
            str, "读取候选时看到的统一 SOUL+USER+MEMORY revision"
        ],
        decision: Annotated[str, "accepted|rejected|kept_open"],
        reason: Annotated[str, "你作出这个决定的理由"],
        accepted_name: Annotated[str, "接受时可选：你最终选择的完整名称"] = "",
        accepted_description: Annotated[str, "接受时可选：你最终选择的完整描述"] = "",
        accepted_instructions: Annotated[
            str, "接受时可选：你最终选择的完整技能正文"
        ] = "",
    ) -> tuple[bool, str | dict]:
        scheduler = _get_scheduler(self.plugin)
        if scheduler is None:
            return False, "学习系统未初始化。"
        normalized = str(decision or "").strip().lower()
        if normalized not in {"accepted", "rejected", "kept_open"}:
            return False, "decision 必须是 accepted/rejected/kept_open。"
        try:
            _, actor = _decision_actor(self)
            occurrence = _decision_occurrence(
                self,
                "\0".join(
                    (
                        candidate_id,
                        str(candidate_revision),
                        candidate_sha256,
                        normalized,
                        expected_subject_revision,
                    )
                ),
            )
            result = await scheduler.decide_skill_candidate(
                candidate_id=str(candidate_id),
                candidate_revision=int(candidate_revision),
                candidate_sha256=str(candidate_sha256),
                decision_occurrence_id=occurrence,
                decision_kind=normalized,
                actor_consciousness_instance_id=actor,
                expected_subject_revision=str(expected_subject_revision),
                reason=str(reason),
                accepted_name=str(accepted_name),
                accepted_description=str(accepted_description),
                accepted_instructions=str(accepted_instructions),
            )
        except Exception as exc:  # noqa: BLE001 - tool returns explicit refusal
            return False, f"技能候选决策未提交: {type(exc).__name__}"
        return True, {"action": "decide_skill_candidate", **result}


class LifeObserveStaleInsightsTool(BaseTool):
    """观察久未验证的洞察（不强制改变）。

    **尊重主体性**：系统只提供观察，你自己决定如何处理：
    - 可以重新验证它们
    - 可以发现它们仍然有效
    - 可以意识到它们已过时
    - 或者保持原样
    """

    tool_name: str = "nucleus_observe_stale_insights"
    tool_description: str = (
        "观察那些很久没有被验证的洞察（默认90天）。"
        "系统不会强制改变它们，只是提醒你关注。"
        "你可以选择重新审视、保持原样、或让它们自然淡化。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        threshold_days: Annotated[int, "陈旧阈值（天），默认90"] = 90,
        max_results: Annotated[int, "最多返回几条，默认10"] = 10,
    ) -> tuple[bool, str | dict]:
        store = _get_store(self.plugin)

        stale_insights = store.get_stale_insights(
            staleness_threshold_days=threshold_days
        )

        if not stale_insights:
            return True, {
                "action": "observe_stale_insights",
                "count": 0,
                "message": f"没有超过 {threshold_days} 天未验证的洞察。",
            }

        # 限制返回数量
        stale_insights = stale_insights[:max_results]

        insights_data = []
        for insight, staleness_days in stale_insights:
            insights_data.append(
                {
                    "insight_id": insight.insight_id,
                    "category": insight.category,
                    "claim": insight.claim,
                    "staleness_days": staleness_days,
                    "confidence": insight.confidence,
                    "evidence_count": len(insight.evidence),
                    "last_validated_at": insight.last_validated_at or insight.born_at,
                }
            )

        return True, {
            "action": "observe_stale_insights",
            "count": len(insights_data),
            "total_stale": len(store.get_stale_insights(threshold_days)),
            "threshold_days": threshold_days,
            "insights": insights_data,
            "note": (
                "这些洞察已经很久没有被新的经历验证了。"
                "你可以选择：重新验证、保持原样、或用 nucleus_challenge_insight 质疑它们。"
            ),
        }


class LifeListValidationExperimentsTool(BaseTool):
    """查看验证实验（预测与现实的对比）。

    这些实验让你能验证自己的洞察是否与现实一致。
    """

    tool_name: str = "nucleus_list_validation_experiments"
    tool_description: str = (
        "查看待验证或已完成的验证实验。"
        "验证实验是将你的洞察转化为可测试的预测，"
        "让你能通过实际交互结果来检验认知是否准确。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        status: Annotated[str, "pending|completed|all，默认pending"] = "pending",
        max_results: Annotated[int, "最多返回几条，默认10"] = 10,
    ) -> tuple[bool, str | dict]:
        store = _get_store(self.plugin)

        if status == "pending":
            experiments = store.list_pending_experiments()
        elif status == "completed":
            experiments = store.list_completed_experiments()
        else:  # all
            experiments = (
                store.list_pending_experiments() + store.list_completed_experiments()
            )

        if not experiments:
            return True, {
                "action": "list_validation_experiments",
                "status": status,
                "count": 0,
                "message": f"没有{status}状态的验证实验。",
            }

        experiments = experiments[:max_results]
        experiments_data = []

        for exp in experiments:
            # 找到关联的洞察
            insight = store.get_insight(exp.insight_id)
            insight_claim = insight.claim if insight else "（洞察已删除）"

            exp_data = {
                "experiment_id": exp.experiment_id,
                "insight_id": exp.insight_id,
                "insight_claim": insight_claim,
                "hypothesis": exp.hypothesis,
                "test_scenario": exp.test_scenario,
                "expected_outcome": exp.expected_outcome,
                "created_at": exp.created_at,
            }

            if exp.is_completed:
                exp_data.update(
                    {
                        "actual_outcome": exp.actual_outcome,
                        "result_type": exp.result_type,
                        "completed_at": exp.completed_at,
                        "notes": exp.notes,
                    }
                )

            experiments_data.append(exp_data)

        return True, {
            "action": "list_validation_experiments",
            "status": status,
            "count": len(experiments_data),
            "total": len(experiments),
            "experiments": experiments_data,
            "note": (
                "这些实验让你能验证洞察是否与现实一致。"
                "对于 pending 的实验，当相关交互发生后，"
                "用 nucleus_complete_validation_experiment 来记录结果。"
            ),
        }


class LifeCompleteValidationExperimentTool(BaseTool):
    """完成一个验证实验的评估。

    **尊重主体性**：由你自己决定何时评估、如何评估。
    """

    tool_name: str = "nucleus_complete_validation_experiment"
    tool_description: str = (
        "完成一个验证实验的评估。"
        "在相关交互发生后，你可以回顾实际结果，"
        "判断预测是否准确（confirmed/contradicted/inconclusive）。"
        "系统会将结果作为高权重证据反馈到原洞察。"
    )
    chatter_allow: list[str] = ["life_engine_internal"]

    async def execute(
        self,
        experiment_id: Annotated[str, "实验ID"],
        actual_outcome: Annotated[str, "实际发生了什么"],
        result_type: Annotated[
            str,
            "confirmed（预测准确）| contradicted（预测错误）| inconclusive（无法判断）",
        ],
        notes: Annotated[str, "补充说明（可选）"] = "",
    ) -> tuple[bool, str | dict]:
        store = _get_store(self.plugin)

        # 验证 result_type
        valid_types = {"confirmed", "contradicted", "inconclusive"}
        if result_type not in valid_types:
            return False, {
                "error": f"result_type 必须是 {valid_types} 之一",
            }

        success = store.complete_experiment(
            experiment_id=experiment_id,
            actual_outcome=actual_outcome,
            result_type=result_type,
            notes=notes,
        )

        if not success:
            return False, {
                "action": "complete_validation_experiment",
                "error": "实验不存在或已完成",
                "experiment_id": experiment_id,
            }
        await _flush_scheduler(self.plugin)

        # 获取完成后的实验
        exp = store.get_experiment(experiment_id)
        insight = store.get_insight(exp.insight_id) if exp else None

        return True, {
            "action": "complete_validation_experiment",
            "experiment_id": experiment_id,
            "result_type": result_type,
            "insight_id": exp.insight_id if exp else None,
            "insight_claim": insight.claim if insight else None,
            "message": (
                f"实验已完成。结果: {result_type}。"
                f"证据已添加到洞察 [{exp.insight_id if exp else ''}]。"
                + (
                    "\n⚠️ 洞察被现实否定，你可以用 nucleus_challenge_insight 重新审视它。"
                    if result_type == "contradicted"
                    else ""
                )
            ),
        }


LEARNING_TOOLS = [
    LifeReflectNowTool,
    LifeListInsightsTool,
    LifeChallengeInsightTool,
    LifeReconsiderInsightTool,
    LifeViewKnowledgeTool,
    LifeReviewSubjectDocumentTool,
    LifeListSubjectCandidatesTool,
    LifeReadSubjectCandidateTool,
    LifeDecideSubjectCandidateTool,
    LifeListSkillCandidatesTool,
    LifeReadSkillCandidateTool,
    LifeDecideSkillCandidateTool,
    LifeObserveStaleInsightsTool,
    LifeListValidationExperimentsTool,
    LifeCompleteValidationExperimentTool,
]
