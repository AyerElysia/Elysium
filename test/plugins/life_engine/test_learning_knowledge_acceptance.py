"""知识候选的接受链与压缩节流。

生产账本暴露的问题：压缩器只产出 promoted=False 的候选，全代码库没有任
何接受路径——1629 个候选无人接受，生效文档冻结三周，而同一批洞察被反复
压缩、每几分钟产出一个只差几行的新候选。

这里锁住修复后的行为：
- 接受/拒绝是账本的显式操作：幂等、留审计、不可作用于被取代的候选
- 接受把候选提升为当前自我认知（manifest + self_knowledge.md），并把
  来源洞察移出可压缩池（next_action → ARCHIVE），打破同一批材料的重压
- 接受最新候选时，更早的未决候选被标记被取代（文件保留）
- 拒绝不动生效文档、材料留在池里
- 压缩节流：与生效文档差异过小的产出不再写候选，并标记"无候选"状态，
  在新材料进池之前 should_compress 不再放行
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.learning.knowledge import SelfKnowledgeCompressor
from plugins.life_engine.learning.models import (
    AuditRecord,
    AuditVerdict,
    Evidence,
    EvidenceKind,
    Insight,
    InsightNextAction,
    InsightStatus,
)
from plugins.life_engine.learning.store import InsightStore

_BASE_KNOWLEDGE = "# 自我认知\n\n## 模式\n\n- 旧内容一行。\n"


def _ev(description: str = "一次实际互动") -> Evidence:
    return Evidence.create(
        kind=EvidenceKind.INTERACTION_OUTCOME,
        description=description,
        supports=True,
    )


def _audit(insight_id: str) -> AuditRecord:
    return AuditRecord(
        audit_id="audit_test",
        insight_id=insight_id,
        timestamp="2026-08-25T10:00:00+08:00",
        verdict=AuditVerdict.VALIDATED.value,
        reasoning="证据充分",
        evidence_sufficiency=0.9,
    )


def _promote_ready(store: InsightStore, *, claim: str) -> Insight:
    """一条已通过审计、等待进入压缩池的洞察。"""
    ins = Insight.create(
        category="behavioral_pattern",
        claim=claim,
        rationale="连续多天观察到",
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
    return store.get_insight(ins.insight_id)


def _store_with_candidates(tmp_path) -> InsightStore:
    """v1 生效 + v2/v3 两个未决候选，各自带一条来源洞察。"""
    store = InsightStore(tmp_path)
    ins_a = _promote_ready(store, claim="模式 A")
    ins_b = _promote_ready(store, claim="模式 B")
    store.write_knowledge_version(
        content=_BASE_KNOWLEDGE,
        version=1,
        insight_ids=[ins_a.insight_id],
        edit_count=5,
        promoted=True,
        reason="seed",
    )
    store.write_knowledge_version(
        content=_BASE_KNOWLEDGE + "\n- 候选二新增一行。\n",
        version=2,
        insight_ids=[ins_a.insight_id],
        edit_count=1,
        promoted=False,
        reason="independent_gate_recommended",
    )
    store.write_knowledge_version(
        content=_BASE_KNOWLEDGE + "\n- 候选三新增两行。\n- 另一行。\n",
        version=3,
        insight_ids=[ins_b.insight_id],
        edit_count=2,
        promoted=False,
        reason="independent_gate_recommended",
    )
    return store


class TestAcceptKnowledgeCandidate:
    def test_accept_promotes_candidate_and_drains_pool(self, tmp_path) -> None:
        store = _store_with_candidates(tmp_path)
        manifest = store.load_knowledge_manifest()
        entry_v3 = next(e for e in manifest["versions"] if e["version"] == 3)
        source_id = entry_v3["insight_ids"][0]

        outcome = store.accept_knowledge_candidate(3, actor="ci_test_actor")

        assert outcome["idempotent"] is False
        assert outcome["archived_insights"] == 1
        assert outcome["superseded_candidates"] == 1

        manifest = store.load_knowledge_manifest()
        assert manifest["current_version"] == 3
        by_version = {e["version"]: e for e in manifest["versions"]}
        assert by_version[3]["promoted"] is True
        assert by_version[3]["accepted_by"] == "ci_test_actor"
        assert by_version[2]["superseded"] is True
        assert by_version[2]["superseded_by"] == 3
        assert by_version[1]["promoted"] is True  # 历史生效版本不受影响
        source_id_b = by_version[3]["insight_ids"][0]
        source_id_a = by_version[2]["insight_ids"][0]

        # 生效文档回写为候选内容
        current = store.read_current_knowledge()
        assert "候选三新增两行" in current

        # 被接受候选的来源洞察离开可压缩池；被取代候选（v2）的来源洞察留在池里，
        # 以后伴随新材料重新压缩——取代不丢材料。
        source = store.get_insight(source_id)
        assert source.next_action == InsightNextAction.ARCHIVE.value
        assert 3 in source.knowledge_versions
        remaining = [ins.insight_id for ins in store.list_for_compression()]
        assert remaining == [source_id_a]

    def test_accept_writes_audit_events(self, tmp_path) -> None:
        store = _store_with_candidates(tmp_path)
        store.accept_knowledge_candidate(3, actor="ci_test_actor", occurrence_id="occ_1")

        events = [
            json.loads(line)
            for line in (tmp_path / ".life_learning" / "insights_audit.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        accepted = [e for e in events if e.get("action") == "knowledge_version_accepted"]
        assert len(accepted) == 1
        assert accepted[0]["version"] == 3
        assert accepted[0]["actor"] == "ci_test_actor"
        assert accepted[0]["occurrence_id"] == "occ_1"
        transitions = [
            e for e in events
            if e.get("action") == "status_transition"
            and e.get("reason") == "knowledge_candidate_accepted:v3"
        ]
        assert len(transitions) == 1

    def test_accept_is_idempotent(self, tmp_path) -> None:
        store = _store_with_candidates(tmp_path)
        first = store.accept_knowledge_candidate(3, actor="ci_test_actor")
        second = store.accept_knowledge_candidate(3, actor="ci_test_actor")

        assert first["idempotent"] is False
        assert second["idempotent"] is True

    def test_accept_unknown_version_raises(self, tmp_path) -> None:
        store = _store_with_candidates(tmp_path)
        with pytest.raises(ValueError, match="KnowledgeVersionNotFound"):
            store.accept_knowledge_candidate(99, actor="ci_test_actor")

    def test_accept_superseded_candidate_raises(self, tmp_path) -> None:
        store = _store_with_candidates(tmp_path)
        store.accept_knowledge_candidate(3, actor="ci_test_actor")
        with pytest.raises(ValueError, match="KnowledgeCandidateSuperseded"):
            store.accept_knowledge_candidate(2, actor="ci_test_actor")


class TestDeclineKnowledgeCandidate:
    def test_decline_keeps_pool_and_effective_document(self, tmp_path) -> None:
        store = _store_with_candidates(tmp_path)
        manifest = store.load_knowledge_manifest()
        entry_v3 = next(e for e in manifest["versions"] if e["version"] == 3)
        source_id = entry_v3["insight_ids"][0]

        outcome = store.decline_knowledge_candidate(
            3, actor="ci_test_actor", reason="表述太绝对了"
        )

        assert outcome["declined"] is True
        manifest = store.load_knowledge_manifest()
        by_version = {e["version"]: e for e in manifest["versions"]}
        assert by_version[3]["declined"] is True
        assert by_version[3]["declined_reason"] == "表述太绝对了"
        assert manifest["current_version"] == 1
        assert "候选三" not in store.read_current_knowledge()

        # 材料留在压缩池，且候选文件保留
        source = store.get_insight(source_id)
        assert source.next_action == InsightNextAction.PROMOTE.value
        assert store.read_knowledge_version(3)

    def test_decline_is_idempotent(self, tmp_path) -> None:
        store = _store_with_candidates(tmp_path)
        first = store.decline_knowledge_candidate(2, actor="ci_test_actor")
        second = store.decline_knowledge_candidate(2, actor="ci_test_actor")
        assert first["idempotent"] is False
        assert second["idempotent"] is True

    def test_cross_decisions_are_rejected(self, tmp_path) -> None:
        store = _store_with_candidates(tmp_path)
        store.decline_knowledge_candidate(2, actor="ci_test_actor")
        with pytest.raises(ValueError, match="KnowledgeCandidateAlreadyDeclined"):
            store.accept_knowledge_candidate(2, actor="ci_test_actor")

        store.accept_knowledge_candidate(3, actor="ci_test_actor")
        with pytest.raises(ValueError, match="KnowledgeCandidateAlreadyAccepted"):
            store.decline_knowledge_candidate(3, actor="ci_test_actor")


class TestCompressionThrottle:
    """近重复产出不再写候选；无候选标记挡住重复压缩。"""

    def _compressor(self, store: InsightStore, tmp_path) -> SelfKnowledgeCompressor:
        compressor = SelfKnowledgeCompressor(
            store=store, workspace_path=tmp_path, interval_hours=6.0
        )
        return compressor

    def _seed_promotable(self, store: InsightStore) -> None:
        _promote_ready(store, claim="一条待压缩的模式")

    @pytest.mark.asyncio
    async def test_near_duplicate_output_skips_candidate(self, tmp_path) -> None:
        store = InsightStore(tmp_path)
        store.get_current_knowledge_path().parent.mkdir(parents=True, exist_ok=True)
        store.get_current_knowledge_path().write_text(_BASE_KNOWLEDGE, encoding="utf-8")
        store.save_knowledge_manifest({"versions": [], "current_version": 0})
        self._seed_promotable(store)

        compressor = self._compressor(store, tmp_path)
        # 只改一行：低于默认阈值（3 处变更区域）
        near_duplicate = _BASE_KNOWLEDGE.replace("旧内容一行", "改写后的一行")
        compressor._compress = AsyncMock(return_value=near_duplicate)
        compressor._selection_gate = AsyncMock(return_value=True)

        assert await compressor.run_compression() is False

        assert compressor._selection_gate.await_count == 0  # 门禁评估也被跳过
        assert store.load_knowledge_manifest()["versions"] == []
        state = store.load_state()
        assert state["last_compress_no_candidate"] is True

    @pytest.mark.asyncio
    async def test_meaningful_diff_still_produces_candidate(self, tmp_path) -> None:
        store = InsightStore(tmp_path)
        store.get_current_knowledge_path().parent.mkdir(parents=True, exist_ok=True)
        multi_section_base = (
            "# 自我认知\n\n## 社交模式\n\n- 旧内容一。\n- 旧内容二。\n\n"
            "## 行为边界\n\n- 旧边界一。\n\n## 情感模式\n\n- 旧情感一。\n"
        )
        store.get_current_knowledge_path().write_text(
            multi_section_base, encoding="utf-8"
        )
        store.save_knowledge_manifest({"versions": [], "current_version": 0})
        self._seed_promotable(store)

        compressor = self._compressor(store, tmp_path)
        # 四处离散变更（标题、社交、边界、新章节），超过默认阈值（3 处变更区域）
        new_content = (
            "# 自我认知（整理后）\n\n## 社交模式\n\n- 新内容一。\n- 旧内容二。\n\n"
            "## 行为边界\n\n- 新边界一。\n- 新边界二。\n\n## 情感模式\n\n- 旧情感一。\n\n"
            "## 成长方向\n\n- 新增长段一。\n- 新增长段二。\n"
        )
        compressor._compress = AsyncMock(return_value=new_content)
        compressor._selection_gate = AsyncMock(return_value=True)

        assert await compressor.run_compression() is True

        assert compressor._selection_gate.await_count == 1

        manifest = store.load_knowledge_manifest()
        assert len(manifest["versions"]) == 1
        assert manifest["versions"][0]["promoted"] is False
        assert manifest["current_version"] == 0  # 候选绝不自动生效
        assert store.load_state()["last_compress_no_candidate"] is False

    def test_no_candidate_flag_blocks_recompression(self, tmp_path) -> None:
        store = InsightStore(tmp_path)
        self._seed_promotable(store)
        compressor = self._compressor(store, tmp_path)

        # 间隔已过但没有"无候选"标记 → 放行
        store.save_state({"last_compress_at": "2026-08-01T00:00:00+08:00"})
        assert compressor.should_compress() is True

        # 上一轮已确认产不出候选 → 拦住，直到池子变化
        store.save_state({
            "last_compress_at": "2026-08-01T00:00:00+08:00",
            "last_compress_no_candidate": True,
        })
        assert compressor.should_compress() is False
