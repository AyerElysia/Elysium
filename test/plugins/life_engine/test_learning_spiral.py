"""螺旋上升：validated 的认知不等于被钉死的认知。

实跑账本暴露的问题：一条洞察一旦 validated 并被压缩进 knowledge/vN.md，
就永久离开审计队列——can_review 只认 candidate / needs_more_evidence，
is_terminal 又把 validated 算作终态，而 _merge_as_evidence 明确拒绝改
validated 的状态。结果是"信念永远可重新审视"这句承诺在代码里不成立。

这里锁住修复后的行为：
- 她可以把任何一条已判断的认知拿回来重新审视（只由她发起）
- 拿回来 = 重新排队，不是删除：证据、审计历史、knowledge_versions 全留着
- 系统不替她降置信度、不替她改结论、不自动把任何东西打回
- 重新验证后能正确回到压缩队列，且知道自己在旧版本里已有表述
- 反例备忘有了真实来源（rejected 几乎不产生，reconsidered 才是主要形态）

"""

from __future__ import annotations

from plugins.life_engine.learning.models import (
    AuditRecord,
    AuditVerdict,
    Evidence,
    EvidenceKind,
    Insight,
    InsightNextAction,
    InsightStatus,
)
from plugins.life_engine.learning.knowledge import SelfKnowledgeCompressor
from plugins.life_engine.learning.prompts import (
    KNOWLEDGE_COMPRESS_USER,
    format_reconsidered_for_compression,
)
from plugins.life_engine.learning.store import InsightStore

_CLAIM = "我在深夜会主动把话收短，避免把对方留在一个需要回应的句子里"


def test_knowledge_compression_prompt_accepts_reconsidered_insights() -> None:
    """The compression template and compressor must share one field contract."""
    prompt = KNOWLEDGE_COMPRESS_USER.format(
        current_knowledge="old",
        validated_insights="validated",
        rejected_insights="rejected",
        reconsidered_insights="reconsidered",
        max_edits=4,
    )

    assert "<reconsidered>\nreconsidered\n</reconsidered>" in prompt


def _ev(description: str = "一次实际互动", *, supports: bool = True) -> Evidence:
    return Evidence.create(
        kind=EvidenceKind.INTERACTION_OUTCOME,
        description=description,
        supports=supports,
    )


def _audit(insight_id: str) -> AuditRecord:
    return AuditRecord(
        audit_id="audit_test",
        insight_id=insight_id,
        timestamp="2026-07-27T21:00:00+08:00",
        verdict=AuditVerdict.VALIDATED.value,
        reasoning="证据充分",
        evidence_sufficiency=0.9,
    )


def _validated_and_compressed(store: InsightStore, *, version: int = 1) -> Insight:
    """造出真实账本里的那个状态：validated → 写进 vN → next_action=archive。"""
    ins = Insight.create(
        category="behavioral_pattern",
        claim=_CLAIM,
        rationale="连续几天都是这样",
        initial_evidence=[_ev()],
    )
    store.add_insight(ins)
    store.transition_status(
        ins.insight_id,
        InsightStatus.VALIDATED,
        next_action=InsightNextAction.PROMOTE,
        reason="审计通过",
        audit_record=_audit(ins.insight_id),
    )
    store.record_knowledge_version(ins.insight_id, version)
    compressed = store.get_insight(ins.insight_id)
    compressed.next_action = InsightNextAction.ARCHIVE.value
    store.update_insight(compressed)
    return store.get_insight(ins.insight_id)


def _memo_ids(store: InsightStore, workspace) -> list[str]:
    """反例备忘实际会收到哪些洞察。

    这条规则住在压缩器里而不是 store 里：store.list_reconsidered() 老实
    返回她重新想过的全部，"曾经写进认知文档"这层筛选是备忘渠道自己的事。
    只读，不触发压缩，不调模型。
    """
    compressor = SelfKnowledgeCompressor(store=store, workspace_path=workspace)
    return [ins.insight_id for ins in compressor.collect_reconsidered_memo()]


class TestCompressedKnowledgeIsFrozenWithoutReconsider:
    """先锁住问题本身：不用 reconsider，压缩过的认知确实出不来。"""

    def test_compressed_insight_leaves_the_audit_queue(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)

        assert ins.is_terminal is True
        assert ins.can_review is False
        assert store.list_candidates_for_review() == []
        assert store.list_for_compression() == []

    def test_new_counter_evidence_alone_does_not_reopen_it(self, tmp_path):
        """新反例会被记录，但不会自动推翻结论——那是她的判断，不是系统的。"""
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)

        store.add_evidence(ins.insight_id, _ev("昨天我没有收短", supports=False))

        after = store.get_insight(ins.insight_id)
        assert after.negative_evidence_count == 1
        assert after.contradiction_count == 1       # 记录下来了
        assert after.status == InsightStatus.VALIDATED.value  # 但状态没被系统改
        assert after.can_review is False


class TestReconsiderReopensTheSpiral:
    """她主动拿回来重想：回到队列，什么都不丢。"""

    def test_reconsider_returns_it_to_the_queue(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)

        assert store.reconsider_insight(ins.insight_id, reason="这条太绝对了") is True

        after = store.get_insight(ins.insight_id)
        assert after.status == InsightStatus.CANDIDATE.value
        assert after.next_action == InsightNextAction.AWAIT_REVIEW.value
        assert after.can_review is True
        assert [i.insight_id for i in store.list_candidates_for_review()] == [ins.insight_id]

    def test_reconsider_keeps_everything(self, tmp_path):
        """重新审视不是删除：证据、审计历史、知识版本、置信度都留着。"""
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        evidence_before = len(ins.evidence)
        audits_before = len(ins.audit_history)
        confidence_before = ins.confidence

        store.reconsider_insight(ins.insight_id, reason="现在的我不这么想了")

        after = store.get_insight(ins.insight_id)
        assert len(after.evidence) == evidence_before
        assert len(after.audit_history) == audits_before
        assert after.knowledge_versions == [1]
        assert after.confidence == confidence_before   # 不自动降低
        assert after.revision_note == "现在的我不这么想了"

    def test_published_knowledge_version_is_untouched(self, tmp_path):
        """已发布的 vN.md 是不可变历史，修正体现在下一版。"""
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        store.write_knowledge_version(
            content="# 自我认知\n\n## 社交模式\n- 我在深夜会主动把话收短\n",
            version=1,
            insight_ids=[ins.insight_id],
            edit_count=1,
            promoted=True,
            reason="test",
        )
        v1_before = (store.knowledge_dir / "v1.md").read_text(encoding="utf-8")

        store.reconsider_insight(ins.insight_id, reason="想重新检验")

        assert (store.knowledge_dir / "v1.md").read_text(encoding="utf-8") == v1_before

    def test_reconsider_writes_an_audit_event(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        store.reconsider_insight(ins.insight_id, reason="太绝对")

        events = [
            __import__("json").loads(line)
            for line in store.audit_log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        reconsidered = [e for e in events if e.get("action") == "insight_reconsidered"]
        assert len(reconsidered) == 1
        assert reconsidered[0]["from_status"] == InsightStatus.VALIDATED.value
        assert reconsidered[0]["to_status"] == InsightStatus.CANDIDATE.value
        assert reconsidered[0]["reason"] == "太绝对"
        assert reconsidered[0]["knowledge_versions"] == [1]

    def test_reconsider_survives_reload(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        store.reconsider_insight(ins.insight_id, reason="重新想")

        fresh = InsightStore(tmp_path)
        reloaded = fresh.get_insight(ins.insight_id)
        assert reloaded.status == InsightStatus.CANDIDATE.value
        assert reloaded.revision_note == "重新想"
        assert reloaded.knowledge_versions == [1]

    def test_reconsider_unknown_id_is_noop(self, tmp_path):
        store = InsightStore(tmp_path)
        assert store.reconsider_insight("ins_nonexistent", reason="x") is False

    def test_reconsider_without_reason_still_works(self, tmp_path):
        """她不一定说得清为什么。说不清也允许重新想。"""
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)

        assert store.reconsider_insight(ins.insight_id) is True
        assert store.get_insight(ins.insight_id).can_review is True


class TestRevalidationUpdatesInsteadOfDuplicating:
    """重新验证之后：回到压缩队列，且压缩器知道它已在 v1 里。"""

    def test_revalidated_insight_returns_to_compression_queue(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        store.reconsider_insight(ins.insight_id, reason="重新检验")

        store.transition_status(
            ins.insight_id,
            InsightStatus.VALIDATED,
            next_action=InsightNextAction.PROMOTE,
            reason="重新审计后仍然成立，但边界要收窄",
        )

        queued = store.list_for_compression()
        assert [i.insight_id for i in queued] == [ins.insight_id]
        assert queued[0].knowledge_versions == [1]   # 压缩器据此更新而非重复写入

    def test_knowledge_version_is_recorded_once_per_version(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)

        store.record_knowledge_version(ins.insight_id, 1)   # 重复记录
        store.record_knowledge_version(ins.insight_id, 2)

        assert store.get_insight(ins.insight_id).knowledge_versions == [1, 2]


class TestCounterExampleChannelIsAlive:
    """反例备忘此前是结构性死的：只读 rejected，而账本里 rejected 恒为 0。"""

    def test_reconsidered_insights_are_a_counter_example_source(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        store.reconsider_insight(ins.insight_id, reason="最近几次我其实没那么克制")

        assert [i.insight_id for i in store.list_reconsidered()] == [ins.insight_id]

    def test_reconsider_in_candidate_phase_is_just_thinking(self, tmp_path):
        """候选期就改主意，只是想法在流动，不算"我曾经以为"。

        list_reconsidered 照实返回（她确实重新想过），
        但反例备忘只收曾经写进认知文档的那些——过滤在压缩器这层。
        """
        store = InsightStore(tmp_path)
        ins = Insight.create(category="x", claim=_CLAIM, rationale="r")
        store.add_insight(ins)
        store.reconsider_insight(ins.insight_id, reason="随手改主意")

        assert [i.insight_id for i in store.list_reconsidered()] == [ins.insight_id]
        assert _memo_ids(store, tmp_path) == []

    def test_published_insight_reaches_the_memo(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        store.reconsider_insight(ins.insight_id, reason="最近几次我其实没那么克制")

        assert _memo_ids(store, tmp_path) == [ins.insight_id]

    def test_reconsidered_renders_as_a_memo(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        store.reconsider_insight(ins.insight_id, reason="这条太绝对了")

        rendered = format_reconsidered_for_compression(
            [i.to_dict() for i in store.list_reconsidered()]
        )
        assert "v1" in rendered
        assert _CLAIM in rendered
        assert "这条太绝对了" in rendered

    def test_empty_reconsidered_renders_placeholder(self):
        assert "暂无" in format_reconsidered_for_compression([])


class TestNothingIsImposedOnHer:
    """主体性边界：系统只负责管路，不替她改主意。"""

    def test_counter_evidence_never_auto_demotes(self, tmp_path):
        """哪怕反例堆到 5 条，状态也只由她或审计来改。"""
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        for i in range(5):
            store.add_evidence(ins.insight_id, _ev(f"反例 {i}", supports=False))

        after = store.get_insight(ins.insight_id)
        assert after.contradiction_count == 5
        assert after.status == InsightStatus.VALIDATED.value
        assert after.revision_note == ""

    def test_reconsider_does_not_lower_confidence(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        ins.confidence = 0.85
        store.update_insight(ins)

        store.reconsider_insight(ins.insight_id, reason="重新想想")

        assert store.get_insight(ins.insight_id).confidence == 0.85

    def test_supporting_evidence_does_not_count_as_contradiction(self, tmp_path):
        store = InsightStore(tmp_path)
        ins = _validated_and_compressed(store)
        store.add_evidence(ins.insight_id, _ev("又一次印证", supports=True))

        assert store.get_insight(ins.insight_id).contradiction_count == 0


