"""Learning integration proposals never impersonate subject acceptance."""

from __future__ import annotations

from plugins.life_engine.learning.knowledge import SelfKnowledgeCompressor
from plugins.life_engine.learning.models import (
    Insight,
    InsightNextAction,
    InsightStatus,
)
from plugins.life_engine.learning.scheduler import LearningScheduler
from plugins.life_engine.learning.store import InsightStore


def _promotable(store: InsightStore) -> Insight:
    insight = Insight.create(
        category="situated observation",
        claim="A candidate understanding",
        rationale="One explicit reflection",
    )
    insight.status = InsightStatus.VALIDATED.value
    insight.next_action = InsightNextAction.PROMOTE.value
    store.add_insight(insight)
    return insight


async def test_compression_gate_creates_proposal_without_promoting(tmp_path) -> None:
    store = InsightStore(tmp_path)
    insight = _promotable(store)
    compressor = SelfKnowledgeCompressor(
        store=store,
        workspace_path=tmp_path,
        trigger_count=2,
    )

    async def compress(**kwargs) -> str:
        del kwargs
        return "# proposed self knowledge\n"

    async def recommend(**kwargs) -> bool:
        del kwargs
        return True

    compressor._compress = compress  # type: ignore[method-assign]
    compressor._selection_gate = recommend  # type: ignore[method-assign]
    assert await compressor.run_compression() is True

    manifest = store.load_knowledge_manifest()
    assert manifest["current_version"] == 0
    assert manifest["versions"][0]["promoted"] is False
    assert manifest["versions"][0]["selection_reason"] == (
        "independent_gate_recommended"
    )
    assert store.read_current_knowledge() == ""
    persisted = store.get_insight(insight.insight_id)
    assert persisted is not None
    assert persisted.next_action == InsightNextAction.PROMOTE.value
    assert persisted.knowledge_versions == []

    assert await compressor.run_compression() is True
    manifest = store.load_knowledge_manifest()
    assert [item["version"] for item in manifest["versions"]] == [1, 2]
    assert manifest["current_version"] == 0


async def test_scheduler_never_mirrors_compression_as_fake_subject_actor(
    tmp_path,
) -> None:
    class _Memory:
        async def version_memory_artifact(self, **kwargs) -> None:
            raise AssertionError(f"unexpected subject-like mirror: {kwargs}")

    scheduler = LearningScheduler(
        workspace_path=tmp_path,
        memory_service=_Memory(),
    )
    scheduler.compressor.should_compress = lambda: True  # type: ignore[method-assign]

    async def propose() -> bool:
        return True

    scheduler.compressor.run_compression = propose  # type: ignore[method-assign]
    scheduler._snapshot_metrics_now = lambda: None  # type: ignore[method-assign]
    await scheduler._maybe_run_compression()
