"""Byte-budget and traceability contracts for learning prompt projections."""

from __future__ import annotations

import hashlib

from plugins.life_engine.learning.knowledge import SelfKnowledgeCompressor
from plugins.life_engine.learning.projection import project_learning_text
from plugins.life_engine.learning.skill_store import SkillPattern, SkillStore
from plugins.life_engine.learning.store import InsightStore


def test_learning_projection_is_utf8_safe_deterministic_and_traceable() -> None:
    source = "开头🌸\n" + ("不同实例共享同一主体，但保留各自视角。" * 1000) + "\n结尾🎀"
    first = project_learning_text(
        source,
        max_bytes=800,
        projection_kind="contract",
    )
    second = project_learning_text(
        source,
        max_bytes=800,
        projection_kind="contract",
    )
    changed = project_learning_text(
        source + "变化",
        max_bytes=800,
        projection_kind="contract",
    )

    assert first == second
    assert len(first.text.encode("utf-8")) <= 800
    assert first.text.encode("utf-8").decode("utf-8") == first.text
    assert first.source_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert first.source_sha256 != changed.source_sha256
    assert first.original_bytes == len(source.encode("utf-8"))
    assert first.delivered_bytes == len(first.text.encode("utf-8"))
    assert first.truncated is True
    assert "开头" in first.text
    assert "结尾" in first.text
    assert "bounded learning projection" in first.text
    assert source == (
        "开头🌸\n" + ("不同实例共享同一主体，但保留各自视角。" * 1000) + "\n结尾🎀"
    )


def test_skill_catalog_projection_does_not_truncate_authoritative_skill(
    tmp_path,
) -> None:
    store = SkillStore(tmp_path)
    for index in range(20):
        store.add_skill(
            SkillPattern.create(
                name=f"skill-{index}",
                description=(f"description-{index}-" + "花" * 200),
                instructions=(f"instruction-{index}-" + "光" * 400),
            )
        )

    catalog = store.get_catalog_text(max_chars=600)
    health = store.catalog_projection_health()
    assert len(catalog.encode("utf-8")) <= 600
    assert health["max_bytes"] == 600
    assert health["truncated"] is True
    assert "instructions" not in str(health)
    assert "instruction-19" in store.get_skill_detail("skill-19")


def test_knowledge_prompt_is_projection_and_full_version_remains_unchanged(
    tmp_path,
) -> None:
    store = InsightStore(tmp_path)
    original = "# self knowledge\n" + "完整来源🌸\n" * 1000
    store.write_knowledge_version(
        content=original,
        version=1,
        insight_ids=[],
        edit_count=1,
        promoted=True,
        reason="fixture",
    )
    compressor = SelfKnowledgeCompressor(store=store, workspace_path=tmp_path)

    projected = compressor.get_knowledge_for_prompt(max_chars=700)
    health = compressor.projection_health()["prompt"]
    assert len(projected.encode("utf-8")) <= 700
    assert health["projection_kind"] == "learning_derived_observations"
    assert health["authority"] == "derived_learning_observation"
    assert health["authoritative"] is False
    assert health["content_source_sha256"] == hashlib.sha256(
        original.encode()
    ).hexdigest()
    assert "非主体权威" in projected
    assert "SOUL.md、USER.md、MEMORY.md" in projected
    assert health["truncated"] is True
    assert store.read_current_knowledge() == original
    assert (store.knowledge_dir / "v1.md").read_text(encoding="utf-8") == original
