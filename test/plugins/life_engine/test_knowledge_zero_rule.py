from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.learning.knowledge import SelfKnowledgeCompressor
from plugins.life_engine.learning.models import (
    Insight,
    InsightNextAction,
    InsightStatus,
)
from plugins.life_engine.learning.store import InsightStore
from plugins.life_engine.learning.tools import LifeChallengeInsightTool


def _insight(store: InsightStore, *, claim: str, status: InsightStatus) -> Insight:
    insight = Insight.create(
        category="subject-named-category",
        claim=claim,
        rationale="A concrete experience supplied the reason.",
        constraints="Only in its observed context.",
    )
    insight.status = status.value
    insight.next_action = (
        InsightNextAction.PROMOTE.value
        if status is InsightStatus.VALIDATED
        else InsightNextAction.ARCHIVE.value
    )
    assert store.add_insight(insight) is True
    return insight


@pytest.mark.asyncio
async def test_compressor_exposes_all_rejected_counterexamples(tmp_path) -> None:
    store = InsightStore(tmp_path)
    _insight(store, claim="A validated interpretation.", status=InsightStatus.VALIDATED)
    for index in range(8):
        _insight(
            store,
            claim=f"Rejected counterexample {index}.",
            status=InsightStatus.REJECTED,
        )

    compressor = SelfKnowledgeCompressor(store=store, workspace_path=tmp_path)
    observed_rejected: list[Insight] = []

    async def compress(**kwargs):
        observed_rejected.extend(kwargs["rejected_insights"])
        return "# Revised self knowledge"

    async def reject_gate(**_kwargs):
        return False

    compressor._compress = compress  # type: ignore[method-assign]
    compressor._selection_gate = reject_gate  # type: ignore[method-assign]

    # A rejected background gate still produces an immutable, unpromoted
    # proposal.  The return value means "proposal persisted", never
    # "subject authority accepted".
    assert await compressor.run_compression() is True
    manifest = store.load_knowledge_manifest()
    assert manifest["current_version"] == 0
    assert manifest["versions"][0]["promoted"] is False
    assert manifest["versions"][0]["selection_reason"] == (
        "independent_gate_not_recommended"
    )
    assert [item.claim for item in observed_rejected] == [
        f"Rejected counterexample {index}." for index in range(8)
    ]


def test_change_count_records_actual_diff_regions_without_limiting_them() -> None:
    old = "one\ntwo\nthree\nfour\nfive"
    new = "ONE\ntwo\nTHREE\nfour\nFIVE"

    assert SelfKnowledgeCompressor._count_change_regions(old, new) == 3


@pytest.mark.asyncio
async def test_challenge_records_evidence_without_code_side_confidence_rule(
    tmp_path,
    monkeypatch,
) -> None:
    store = InsightStore(tmp_path)
    insight = _insight(
        store,
        claim="An interpretation that remains open to challenge.",
        status=InsightStatus.VALIDATED,
    )
    insight.confidence = 0.8
    store.update_insight(insight)

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    plugin = SimpleNamespace(config=config)
    monkeypatch.setattr(
        "plugins.life_engine.learning.tools._get_scheduler",
        lambda _plugin: None,
    )
    tool = LifeChallengeInsightTool(plugin=cast(Any, plugin))

    for challenge in ("A first counterexample.", "A second counterexample."):
        ok, _payload = await tool.execute(
            insight_id=insight.insight_id,
            challenge=challenge,
        )
        assert ok is True

    reloaded = InsightStore(tmp_path).get_insight(insight.insight_id)
    assert reloaded.confidence == 0.8
    assert [evidence.weight for evidence in reloaded.evidence] == [1.0, 1.0]


def test_should_compress_enforces_interval_floor_above_trigger_count(
    tmp_path,
) -> None:
    """触发数不能绕过压缩最小间隔。

    validated 洞察数持续高于触发数时，间隔门禁仍然必须生效；否则慢环
    会每个维护周期都重写同一份 base，间隔配置变成死代码。
    """
    from datetime import UTC, datetime, timedelta

    store = InsightStore(tmp_path)
    for index in range(5):
        _insight(store, claim=f"Validated claim {index}.", status=InsightStatus.VALIDATED)

    compressor = SelfKnowledgeCompressor(
        store=store,
        workspace_path=tmp_path,
        trigger_count=5,
        interval_hours=48.0,
    )

    # 无可用材料时永远不压缩。
    empty_store = InsightStore(tmp_path / "empty")
    empty_compressor = SelfKnowledgeCompressor(
        store=empty_store,
        workspace_path=tmp_path / "empty",
        trigger_count=5,
        interval_hours=48.0,
    )
    assert empty_compressor.should_compress() is False

    # 从未压缩过且存在可用材料：允许引导性的第一次压缩（不限触发数）。
    assert compressor.should_compress() is True

    # 刚压缩完成：即使 validated 洞察数仍高于触发数，间隔未到也不压缩。
    state = store.load_state()
    state["last_compress_at"] = datetime.now(UTC).isoformat()
    store.save_state(state)
    assert compressor.should_compress() is False

    # 间隔已过且材料足够：允许再次压缩。
    state = store.load_state()
    state["last_compress_at"] = (
        datetime.now(UTC) - timedelta(hours=49)
    ).isoformat()
    store.save_state(state)
    assert compressor.should_compress() is True

    # last_compress 损坏（无法解析）：显式允许压缩，而不是静默锁死。
    state = store.load_state()
    state["last_compress_at"] = "not-a-timestamp"
    store.save_state(state)
    assert compressor.should_compress() is True
