"""life_engine skill authoring tool tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.tools.skill_tools import LifeEngineSkillTool
from plugins.skill_manager.config import SkillManagerConfig


def _make_plugin(tmp_path: Path) -> SimpleNamespace:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    return SimpleNamespace(config=config)


def _make_tool(tmp_path: Path) -> LifeEngineSkillTool:
    return LifeEngineSkillTool(plugin=cast(Any, _make_plugin(tmp_path)))


class _FakePluginManager:
    def __init__(self, plugin: object | None) -> None:
        self._plugin = plugin

    def get_plugin(self, name: str) -> object | None:
        assert name == "skill_manager"
        return self._plugin


class _FakeSkillManager:
    def __init__(self) -> None:
        self.skills: dict[str, object] = {"existing": object()}
        self.refresh_skill_catalog = AsyncMock(side_effect=self._refresh)

    async def _refresh(self) -> None:
        self.skills["fresh"] = object()


async def test_manage_skill_draft_publish_and_archive(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    skill_manager = _FakeSkillManager()

    ok, payload = await tool.execute(
        action="draft",
        name="Curiosity Notes",
        description="Use when Aili wants to preserve a stable curiosity-review habit.",
        body="When this pattern appears repeatedly, summarize the trigger and boundary.",
        reason="沉淀好奇心复盘方式",
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["name"] == "curiosity-notes"
    draft_path = tmp_path / "skill_drafts" / "curiosity-notes" / "SKILL.md"
    assert draft_path.exists()
    assert "name: curiosity-notes" in draft_path.read_text(encoding="utf-8")

    with patch(
        "plugins.life_engine.tools.skill_tools.get_plugin_manager",
        return_value=_FakePluginManager(skill_manager),
    ):
        ok, payload = await tool.execute(
            action="publish",
            name="curiosity-notes",
            reason="草稿稳定，可以发布",
        )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["refresh"] == {"refreshed": True, "indexed_count": 2}
    published_path = tmp_path / "skills" / "curiosity-notes" / "SKILL.md"
    assert published_path.exists()
    skill_manager.refresh_skill_catalog.assert_awaited_once()

    ok, payload = await tool.execute(action="list")

    assert ok is True
    assert isinstance(payload, dict)
    assert [item["name"] for item in payload["drafts"]] == ["curiosity-notes"]
    assert [item["name"] for item in payload["published"]] == ["curiosity-notes"]

    with patch(
        "plugins.life_engine.tools.skill_tools.get_plugin_manager",
        return_value=_FakePluginManager(skill_manager),
    ):
        ok, payload = await tool.execute(
            action="archive",
            name="curiosity-notes",
            location="published",
            reason="测试归档",
        )

    assert ok is True
    assert isinstance(payload, dict)
    assert not published_path.exists()
    assert "skill_archive" in payload["archived_path"]


async def test_manage_skill_rejects_script_like_skill(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)

    ok, payload = await tool.execute(
        action="draft",
        name="dangerous-runner",
        description="Use when a workflow wants shell automation.",
        body="Run this:\n```bash\nrm -rf /tmp/example\n```",
    )

    assert ok is False
    assert isinstance(payload, dict)
    assert payload["valid"] is False
    assert any("instruction-only" in warning for warning in payload["warnings"])
    assert not (tmp_path / "skill_drafts" / "dangerous-runner" / "SKILL.md").exists()


async def test_manage_skill_validate_reports_frontmatter_errors(tmp_path: Path) -> None:
    tool = _make_tool(tmp_path)
    bad = tmp_path / "skill_drafts" / "bad-skill" / "SKILL.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("No frontmatter\n", encoding="utf-8")

    ok, payload = await tool.execute(
        action="validate",
        name="bad-skill",
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert payload["valid"] is False
    assert "缺少 YAML frontmatter" in payload["warnings"][0]


def test_skill_manager_default_paths_include_life_workspace_skills() -> None:
    config = SkillManagerConfig()

    assert "data/life_engine_workspace/skills" in config.manager.paths
