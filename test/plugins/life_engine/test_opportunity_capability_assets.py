"""Built-in opportunity capabilities keep manuals and subject Skills separate."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from plugins.life_engine.opportunity.catalog import (
    CapabilityCatalog,
    CapabilityDescriptor,
    load_capability_package,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CAPABILITIES_ROOT = (
    REPOSITORY_ROOT / "plugins" / "life_engine" / "opportunity" / "capabilities"
)
PACKAGE_FILES = {"manifest.json", "CAPABILITY.md", "DEFAULT_SKILL.md"}
MANIFEST_FIELDS = {
    "schema_version",
    "capability_id",
    "package_version",
    "removability",
    "provider_kind",
    "manual",
    "default_skill",
    "operations",
    "dependencies",
}
FORBIDDEN_MANIFEST_KEYS = {
    "importance",
    "priority",
    "score",
    "audience",
    "platform",
    "prewrittenaction",
    "prewrittenexpression",
}

EXPECTED_PACKAGES = {
    "epistemic_explore": {
        "capability_id": "life.epistemic_explore",
        "provider_kind": "epistemic_explore",
        "operations": (
            "nucleus_search_memory",
            "nucleus_grep_events",
            "nucleus_web_search",
            "nucleus_browser_fetch",
        ),
        "dependencies": (
            "life.epistemic_candidates",
            "life.memory_engine",
            "life.timeline",
            "life.web_access",
        ),
    },
    "file_care": {
        "capability_id": "life.file_care",
        "provider_kind": "file_care",
        "operations": (
            "nucleus_list_files",
            "nucleus_glob_file",
            "nucleus_read_file",
            "nucleus_mkdir",
            "nucleus_write_file",
            "nucleus_edit_file",
            "nucleus_apply_patch",
        ),
        "dependencies": (
            "life.file_policy",
            "life.timeline",
            "life.workspace",
        ),
    },
    "initiative_reencounter": {
        "capability_id": "life.initiative_reencounter",
        "provider_kind": "initiative_reencounter",
        "operations": (
            "nucleus_proactive_query",
            "nucleus_proactive_command",
        ),
        "dependencies": (
            "life.initiative_authority",
            "life.presence",
            "life.proactive_actor_gate",
            "life.timeline",
        ),
    },
    "inner_return": {
        "capability_id": "life.inner_return",
        "provider_kind": "inner_return",
        "operations": (
            "nucleus_proactive_query",
            "nucleus_proactive_command",
        ),
        "dependencies": (
            "life.inner_dialogue_ledger",
            "life.presence",
            "life.proactive_actor_gate",
            "life.timeline",
        ),
    },
    "learning": {
        "capability_id": "life.learning",
        "provider_kind": "learning",
        "operations": ("nucleus_learn",),
        "dependencies": (
            "life.learning_engine",
            "life.presence",
            "life.subject_authority",
            "life.timeline",
        ),
    },
    "memory_review": {
        "capability_id": "life.memory_review",
        "provider_kind": "memory_review",
        "operations": ("nucleus_memory_continuity_review",),
        "dependencies": (
            "life.memory_engine",
            "life.presence",
            "life.subject_authority",
            "life.timeline",
        ),
    },
    "narrative_review": {
        "capability_id": "life.narrative_review",
        "provider_kind": "narrative_review",
        "operations": ("nucleus_write_narrative",),
        "dependencies": (
            "life.narrative_store",
            "life.presence",
            "life.timeline",
            "life.trace",
        ),
    },
    "self_awaken": {
        "capability_id": "life.self_awaken",
        "provider_kind": "self_awaken",
        "operations": ("opportunity.schedule", "opportunity.query"),
        "dependencies": ("life.subconscious", "life.timeline"),
    },
    "todo_reminder": {
        "capability_id": "life.todo_reminder",
        "provider_kind": "todo_reminder",
        "operations": ("nucleus_todo",),
        "dependencies": (
            "life.presence",
            "life.timeline",
            "life.todo_board",
        ),
    },
}


def _normalise_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _all_mapping_keys(value: object) -> tuple[str, ...]:
    if isinstance(value, dict):
        keys = [_normalise_key(key) for key in value]
        for child in value.values():
            keys.extend(_all_mapping_keys(child))
        return tuple(keys)
    if isinstance(value, list):
        keys: list[str] = []
        for child in value:
            keys.extend(_all_mapping_keys(child))
        return tuple(keys)
    return ()


@pytest.mark.parametrize("directory_name", sorted(EXPECTED_PACKAGES))
def test_builtin_capability_package_is_a_strict_loadable_v1_asset(
    directory_name: str,
) -> None:
    package_root = CAPABILITIES_ROOT / directory_name
    expected = EXPECTED_PACKAGES[directory_name]

    assert {path.name for path in package_root.iterdir()} == PACKAGE_FILES
    descriptor = load_capability_package(package_root)

    assert descriptor.capability_id == expected["capability_id"]
    assert descriptor.package_version == "1.0.0"
    assert descriptor.removability == "subject_removable"
    assert descriptor.provider_kind == expected["provider_kind"]
    assert descriptor.operations == expected["operations"]
    assert descriptor.dependencies == expected["dependencies"]
    assert descriptor.package_root.name == directory_name
    assert len(descriptor.package_sha256) == 64
    assert len(descriptor.manifest_sha256) == 64
    assert len(descriptor.manual_sha256) == 64
    assert len(descriptor.default_skill_sha256) == 64

    manifest = json.loads((package_root / "manifest.json").read_text("utf-8"))
    assert set(manifest) == MANIFEST_FIELDS
    assert manifest["schema_version"] == 1
    assert manifest["manual"] == "CAPABILITY.md"
    assert manifest["default_skill"] == "DEFAULT_SKILL.md"
    assert not (set(_all_mapping_keys(manifest)) & FORBIDDEN_MANIFEST_KEYS)


def test_builtin_catalog_discovers_every_package_without_activating_it() -> None:
    catalog = CapabilityCatalog()

    discovered = catalog.discover(CAPABILITIES_ROOT)

    assert tuple(item.capability_id for item in discovered) == tuple(
        sorted(
            str(expected["capability_id"]) for expected in EXPECTED_PACKAGES.values()
        )
    )
    assert catalog.list_descriptors() == discovered
    assert all(isinstance(item, CapabilityDescriptor) for item in discovered)


@pytest.mark.parametrize("directory_name", sorted(EXPECTED_PACKAGES))
def test_default_skill_is_an_editable_suggestion_not_an_authority(
    directory_name: str,
) -> None:
    descriptor = load_capability_package(CAPABILITIES_ROOT / directory_name)
    manual = descriptor.technical_manual.text
    default_skill = descriptor.default_skill_template.text

    assert "文档类型：工程能力手册；不是主体 Skill，也不是主体表达。" in manual
    assert "## 安全边界" in manual
    assert "移除" in manual
    assert (
        "文档类型：工程提供、可由爱莉重写、替换或删除的可选流程建议。" in default_skill
    )
    assert "沉默" in default_skill
    assert "不自动执行" in default_skill
    assert "不增加" in default_skill
    assert "权限" in default_skill
    assert "安装不等于安排，安排不等于执行" in manual
    assert "统一机会管理面" in manual
    assert "`at`" in manual
    assert "`interval`" in manual
    assert "## 使用前提" in default_skill
    assert "三个独立" in default_skill
    if directory_name == "initiative_reencounter":
        assert "唯一已有领域来源桥" in manual
    else:
        assert "不会" in manual and "自动生产机会" in manual


def test_learning_removal_stops_new_cognition_without_erasing_history() -> None:
    descriptor = load_capability_package(CAPABILITIES_ROOT / "learning")
    manual = descriptor.technical_manual.text

    assert "停止产生新的 Learning 机会" in manual
    assert "停止由该能力启动新的 Learning LLM 请求" in manual
    assert "停止其后台认知维护" in manual
    assert "不得回落到第二套隐藏 Learning 实现" in manual
    assert "不删除已经进入不可变历史" in manual


def test_self_awaken_only_wakes_the_subconscious() -> None:
    descriptor = load_capability_package(CAPABILITIES_ROOT / "self_awaken")
    manual = descriptor.technical_manual.text

    assert "只唤醒潜意识" in manual
    assert "不得直接调用领域工具" in manual
    assert "不再产生新的定时自唤醒" in manual
    assert descriptor.removability == "subject_removable"
