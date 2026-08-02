"""证据闭环：复现的观察必须累积成证据，而不是变成重复洞察或被丢弃。

这条链路此前是断的，实跑四天的账本证明了后果：
49 条洞察全卡在 candidate，0 条 validated，其中 6 对 claim 完全逐字相同
却被存成 4 条独立洞察——因为去重在 topic_key 不同时直接跳过比较，
而她每次反思都自由命名 topic（49 条洞察 44 个不同 key）。
同时审计环把 34 条打回 gather_evidence / revise，而 can_review 只认
await_review，这些洞察就永久离开了审计队列。

这里锁住修复后的行为：
- 同一模式的改述 → 并入已有洞察当证据，不新建
- 确实不同的模式 → 照常新建
- 有新证据到达 → 解冻回审计队列；没有新证据 → 不重复消耗审计调用
- 审计留下的建议 → 她看得见（是建议，不是任务）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from plugins.life_engine.learning.models import (
    AuditRecord,
    Evidence,
    EvidenceKind,
    Insight,
    InsightNextAction,
    InsightStatus,
)
from plugins.life_engine.learning.reflection import ReflectionEngine
from plugins.life_engine.learning.store import InsightStore

# 取自真实账本的两条 claim：同一模式的改述，topic_key 不同
_CLAIM_A = (
    "当多人线程并行时，我倾向于把已闭合的轻量收尾与另一人尚未到点的意向窗口"
    "严格分开，不为前者多说一句，也不提前敲后者的门"
)
_CLAIM_A_PARAPHRASE = (
    "当多人线程并行时，我倾向于把已闭合的轻量群聊收尾与另一人尚未到点的私聊"
    "意向窗口严格分开，不为前者多说一句，也不提前敲后者的门"
)
_CLAIM_DISTINCT = (
    "当对方连着说多遍「非常」把一项偏好钉死时，应写进会改写下次选择权重的记忆，"
    "而不是当闲聊带过"
)


def _ev(description: str = "测试证据", *, supports: bool = True) -> Evidence:
    return Evidence.create(
        kind=EvidenceKind.INTERACTION_OUTCOME,
        description=description,
        supports=supports,
    )


def _insight(claim: str, *, topic_key: str = "", with_evidence: bool = True) -> Insight:
    return Insight.create(
        category="behavioral_pattern",
        claim=claim,
        rationale="来自一次实际互动",
        topic_key=topic_key,
        initial_evidence=[_ev()] if with_evidence else [],
    )


def _iso(offset_minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)).isoformat()


class TestParaphraseStaysDistinctWithoutAuthoredRelation:
    """相似文本只是候选线索，不能由代码擅自合并认识。"""

    def test_paraphrase_merges_as_evidence(self, tmp_path):
        store = InsightStore(tmp_path)
        original = _insight(_CLAIM_A, topic_key="线程边界")
        assert store.add_insight(original) is True

        paraphrase = _insight(_CLAIM_A_PARAPHRASE, topic_key="并行收尾")
        assert store.add_insight(paraphrase) is True

        all_insights = store.list_all()
        assert len(all_insights) == 2
        assert [len(item.evidence) for item in all_insights] == [1, 1]

    def test_merged_evidence_records_the_new_wording(self, tmp_path):
        store = InsightStore(tmp_path)
        store.add_insight(_insight(_CLAIM_A, topic_key="线程边界"))
        store.add_insight(_insight(_CLAIM_A_PARAPHRASE, topic_key="并行收尾"))

        assert [item.claim for item in store.list_all()] == [
            _CLAIM_A,
            _CLAIM_A_PARAPHRASE,
        ]

    def test_distinct_claim_still_creates_new_insight(self, tmp_path):
        store = InsightStore(tmp_path)
        store.add_insight(_insight(_CLAIM_A, topic_key="线程边界"))

        assert store.add_insight(_insight(_CLAIM_DISTINCT, topic_key="偏好强度")) is True
        assert len(store.list_all()) == 2

    def test_explicit_reinforce_target_does_not_need_text_similarity(self, tmp_path):
        store = InsightStore(tmp_path)
        target = _insight(_CLAIM_A, topic_key="线程边界")
        store.add_insight(target)

        assert store.reinforce_insight(target.insight_id, _ev("显式关联的证据"))
        assert len(store.get_insight(target.insight_id).evidence) == 2


class TestTerminalTargetKeepsItsVerdict:
    """已 validated 的目标：只附证据，不被新观察改状态。

    她（或独立审计）已经对那条下过判断，一次复现是记录，不是推翻指令。
    """

    def test_validated_target_gains_evidence_but_keeps_status(self, tmp_path):
        store = InsightStore(tmp_path)
        original = _insight(_CLAIM_A, topic_key="线程边界")
        store.add_insight(original)
        store.transition_status(
            original.insight_id,
            InsightStatus.VALIDATED,
            next_action=InsightNextAction.PROMOTE,
            reason="测试",
        )

        assert store.add_insight(_insight(_CLAIM_A_PARAPHRASE)) is True

        kept = store.get_insight(original.insight_id)
        assert kept.status == InsightStatus.VALIDATED.value
        assert kept.next_action == InsightNextAction.PROMOTE.value
        assert len(kept.evidence) == 1
        assert len(store.list_all()) == 2


def _ev_at(offset_minutes: int, description: str = "后到的证据") -> Evidence:
    """时间戳可控的证据——用来表达"这条证据在那次审计之后才到"。"""
    ev = _ev(description)
    ev.timestamp = _iso(offset_minutes)
    return ev


def _audited(insight: Insight, *, suggestions: str, at_minutes: int = 0) -> AuditRecord:
    record = AuditRecord(
        audit_id="audit_test",
        insight_id=insight.insight_id,
        timestamp=_iso(at_minutes),
        verdict="needs_more_evidence",
        reasoning="证据只覆盖单一情境",
        evidence_sufficiency=0.4,
        suggestions=suggestions,
    )
    return record


class TestAuditQueueUnfreezes:
    """被打回的洞察：有新证据就能重新排队，没有就不占用审计调用。

    真实账本里 34 条洞察被打回 gather_evidence / revise，而 can_review
    此前只认 await_review —— 它们永久离开了队列。
    """

    def _audited_insight(self, store: InsightStore, *, next_action: InsightNextAction) -> Insight:
        insight = _insight(_CLAIM_A, topic_key="线程边界")
        store.add_insight(insight)
        store.transition_status(
            insight.insight_id,
            InsightStatus.CANDIDATE,
            next_action=next_action,
            reason="测试",
            audit_record=_audited(insight, suggestions="去找一次反例：有没有哪次你确实提前敲了门", at_minutes=1),
        )
        return store.get_insight(insight.insight_id)

    def test_pushed_back_without_new_evidence_stays_out(self, tmp_path):
        store = InsightStore(tmp_path)
        insight = self._audited_insight(store, next_action=InsightNextAction.GATHER_EVIDENCE)

        # 唯一那条证据比审计更早 → 材料没变，不值得再审一遍
        assert insight.has_new_evidence_since_last_review is False
        assert insight.can_review is False
        assert store.list_candidates_for_review() == []

    def test_new_evidence_puts_it_back_in_the_queue(self, tmp_path):
        store = InsightStore(tmp_path)
        insight = self._audited_insight(store, next_action=InsightNextAction.GATHER_EVIDENCE)

        assert store.reinforce_insight(insight.insight_id, _ev_at(5)) is True

        back = store.get_insight(insight.insight_id)
        assert back.has_new_evidence_since_last_review is True
        assert back.can_review is True
        assert [i.insight_id for i in store.list_candidates_for_review()] == [insight.insight_id]

    def test_revise_also_unfreezes_on_new_evidence(self, tmp_path):
        store = InsightStore(tmp_path)
        insight = self._audited_insight(store, next_action=InsightNextAction.REVISE)
        assert insight.can_review is False

        # revise 不被 reinforce_insight 改回 await_review（那是她的修正权），
        # 但新证据本身足以让它重新可审
        store.add_evidence(insight.insight_id, _ev_at(5))
        back = store.get_insight(insight.insight_id)
        assert back.next_action == InsightNextAction.REVISE.value
        assert back.can_review is True

    def test_archived_stays_out_regardless_of_evidence(self, tmp_path):
        store = InsightStore(tmp_path)
        insight = _insight(_CLAIM_A)
        store.add_insight(insight)
        store.transition_status(
            insight.insight_id,
            InsightStatus.ARCHIVED,
            next_action=InsightNextAction.ARCHIVE,
            reason="测试",
        )
        store.add_evidence(insight.insight_id, _ev_at(5))

        assert store.get_insight(insight.insight_id).can_review is False


def _make_llm(raw_text: str):
    """替掉真实 LLM 调用：固定返回一段 JSON。"""

    async def _fake_call_llm(_user_prompt: str) -> str:
        return raw_text

    return _fake_call_llm


def _engine(tmp_path, raw_text: str = "") -> ReflectionEngine:
    """一个只会吐出固定 JSON 的反思引擎。

    skill_store / memory_service 都留 None —— 两处都有 None 短路，
    所以这里不需要假对象。
    """
    engine = ReflectionEngine(store=InsightStore(tmp_path), workspace_path=tmp_path)
    engine._call_llm = _make_llm(raw_text)  # type: ignore[method-assign]
    return engine


def _llm_payload(claim: str, *, reinforces: str | None = None, topic_key: str = "") -> str:
    item: dict = {
        "category": "behavioral_pattern",
        "claim": claim,
        "rationale": "又一次遇到同样的情形",
        "topic_key": topic_key,
        "initial_evidence": "这次也是这么做的",
    }
    if reinforces is not None:
        item["reinforces"] = reinforces
    return json.dumps({"insights": [item]}, ensure_ascii=False)


class TestSheCanNameTheTarget:
    """`reinforces`：她若知道这次是哪条洞察的又一次印证，可以直接指名。

    指名是可选的建议通道；留空或无效时保留为独立解释。
    """

    async def test_named_target_wins_over_semantic_match(self, tmp_path):
        engine = _engine(tmp_path, "")
        store = engine._store
        target = _insight(_CLAIM_DISTINCT, topic_key="偏好强度")
        store.add_insight(target)

        # claim 与 target 语义上并不重叠，纯靠语义匹配是挂不上去的
        engine._call_llm = _make_llm(_llm_payload(
            "当对方把一个选择的理由讲得很细时，那通常是在给我判断依据，不是在抱怨",
            reinforces=target.insight_id,
        ))
        created = await engine._run_reflection(
            user_prompt="x", source_event_ids=["evt_1"], reflection_type="introspection",
        )

        assert created == []                      # 没新建
        assert len(store.list_all()) == 1
        assert len(store.get_insight(target.insight_id).evidence) == 2

    async def test_bad_id_does_not_fall_back_to_code_authored_match(self, tmp_path):
        engine = _engine(tmp_path, "")
        store = engine._store
        target = _insight(_CLAIM_A, topic_key="线程边界")
        store.add_insight(target)

        engine._call_llm = _make_llm(_llm_payload(
            _CLAIM_A_PARAPHRASE, reinforces="ins_不存在的编号",
        ))
        created = await engine._run_reflection(
            user_prompt="x", source_event_ids=["evt_1"], reflection_type="introspection",
        )

        assert len(created) == 1
        assert len(store.list_all()) == 2
        assert len(store.get_insight(target.insight_id).evidence) == 1

    async def test_no_reinforces_still_creates_when_nothing_matches(self, tmp_path):
        engine = _engine(tmp_path, "")
        engine._store.add_insight(_insight(_CLAIM_A, topic_key="线程边界"))

        engine._call_llm = _make_llm(_llm_payload(_CLAIM_DISTINCT))
        created = await engine._run_reflection(
            user_prompt="x", source_event_ids=["evt_1"], reflection_type="introspection",
        )

        assert len(created) == 1
        assert len(engine._store.list_all()) == 2


class TestAuditSuggestionsBecomeVisible:
    """审计留言此前只写进档案，没人读。她不必照做，但至少该看见。"""

    def test_summary_surfaces_suggestion_from_older_insight(self, tmp_path):
        engine = _engine(tmp_path, "")
        store = engine._store

        old = _insight(_CLAIM_A, topic_key="线程边界")
        store.add_insight(old)
        store.transition_status(
            old.insight_id,
            InsightStatus.CANDIDATE,
            next_action=InsightNextAction.GATHER_EVIDENCE,
            reason="测试",
            audit_record=_audited(old, suggestions="去找一次反例：有没有哪次你确实提前敲了门", at_minutes=1),
        )
        # 再塞 5 条更新的，把 old 挤出"最近 5 条"
        for i in range(5):
            store.add_insight(_insight(f"第 {i} 条无关的观察，讲的是完全另一件事情", topic_key=f"t{i}"))

        summary = engine._build_existing_summary()

        assert "↳ 审计: 去找一次反例" in summary
        assert old.insight_id in summary          # 她能引用它
        assert "【线程边界】" in summary        # 同一模式可以复用同一个 key
        assert "证据×1" in summary                # 看得见这条还很单薄

    def test_summary_without_audits_is_still_well_formed(self, tmp_path):
        engine = _engine(tmp_path, "")
        engine._store.add_insight(_insight(_CLAIM_A, topic_key="线程边界"))

        summary = engine._build_existing_summary()
        assert "↳ 审计" not in summary
        assert "证据×1" in summary

    def test_empty_store_says_so(self, tmp_path):
        assert "暂无已有洞察" in _engine(tmp_path, "")._build_existing_summary()
