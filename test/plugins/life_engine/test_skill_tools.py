"""life_engine procedural-memory skill tool tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.learning.skill_store import SkillStore
from plugins.life_engine.tools.skill_tools import LifeEngineSkillTool
from plugins.skill_manager.config import SkillManagerConfig


def _make_tool(tmp_path: Path) -> tuple[LifeEngineSkillTool, SkillStore]:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    store = SkillStore(tmp_path)
    plugin = SimpleNamespace(
        config=config,
        service=SimpleNamespace(
            _learning_scheduler=SimpleNamespace(skill_store=store)
        ),
    )
    return LifeEngineSkillTool(plugin=cast(Any, plugin)), store


async def test_manage_skill_draft_publish_and_archive(tmp_path: Path) -> None:
    """Drafted procedural memories should be persisted, listed, and archivable."""
    tool, store = _make_tool(tmp_path)

    ok, payload = await tool.execute(
        action="draft",
        name="Curiosity Notes",
        description="Preserve a stable curiosity-review habit.",
        instructions=(
            "When this pattern repeats, summarize the trigger and its boundary."
        ),
        reason="沉淀好奇心复盘方式",
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["skill"] == "curiosity-notes"
    assert store.get_skill_by_name("curiosity-notes") is not None
    assert store.skills_path.exists()

    ok, listed = await tool.execute(action="list")
    assert ok is True
    assert isinstance(listed, dict)
    assert [item["name"] for item in listed["skills"]] == ["curiosity-notes"]

    ok, archived = await tool.execute(
        action="archive",
        name="curiosity-notes",
        reason="测试归档",
    )
    assert ok is True
    assert isinstance(archived, dict)
    assert store.get_skill_by_name("curiosity-notes") is None


async def test_manage_skill_rejects_script_like_skill(tmp_path: Path) -> None:
    tool, store = _make_tool(tmp_path)

    ok, payload = await tool.execute(
        action="draft",
        name="dangerous-runner",
        description="Shell automation disguised as a skill.",
        instructions="Run this:\n```bash\nrm -rf /tmp/example\n```",
    )

    assert ok is False
    assert "不能包含可执行脚本" in str(payload)
    assert store.get_skill_by_name("dangerous-runner") is None


async def test_manage_skill_validate_reports_frontmatter_errors(
    tmp_path: Path,
) -> None:
    """The current semantic store rejects incomplete drafts before persistence."""
    tool, store = _make_tool(tmp_path)

    ok, payload = await tool.execute(
        action="draft",
        name="bad-skill",
        description="",
        instructions="An incomplete procedural memory.",
    )

    assert ok is False
    assert "description" in str(payload)
    assert store.get_skill_by_name("bad-skill") is None


def test_skill_manager_default_paths_include_life_workspace_skills() -> None:
    config = SkillManagerConfig()

    assert "data/life_engine_workspace/skills" in config.manager.paths
