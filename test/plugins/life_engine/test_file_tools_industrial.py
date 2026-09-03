from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.tool_manifests import (
    get_tool_manifest,
    heartbeat_tool_classes,
)
from plugins.life_engine.tools._utils import resolve_registry_tool
from plugins.life_engine.tools.file_tools import (
    FILE_CHATTER_ALLOW,
    LifeEngineApplyPatchTool,
    LifeEngineEditFileTool,
    LifeEngineGlobFileTool,
    LifeEngineReadFileTool,
    LifeEngineWriteFileTool,
)
from plugins.life_engine.tools.grep_tools import LifeEngineGrepFileTool
from plugins.life_engine.trace.store import AsyncLocalLifeTraceStore, LifeTraceStore


def _plugin(tmp_path: Path) -> SimpleNamespace:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = SimpleNamespace(
        _selectable_storage_enabled=False,
        life_trace_store=lambda: AsyncLocalLifeTraceStore(tmp_path),
    )
    return SimpleNamespace(config=config, service=service)


async def test_apply_patch_tool_updates_file_and_records_trace(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    target = tmp_path / "notes" / "page.md"
    target.parent.mkdir()
    target.write_text("aaa\nbbb\nccc\nddd\n", encoding="utf-8")

    ok, payload = await LifeEngineApplyPatchTool(plugin=plugin).execute(
        """*** Begin Patch
*** Update File: notes/page.md
@@
 aaa
-bbb
+BBB
 ccc
@@
 ccc
-ddd
+DDD
*** End Patch
""",
        reason="industrial edit",
    )
    assert ok is True
    assert isinstance(payload, dict)
    assert payload["action"] == "apply_patch"
    assert payload["files"][0]["operation"] == "update"
    assert payload["files"][0]["trace_id"].startswith("trace_")
    assert target.read_text(encoding="utf-8") == "aaa\nBBB\nccc\nDDD\n"
    records = LifeTraceStore(tmp_path).history("notes/page.md")
    assert records[0].operation == "update"
    assert records[0].reason == "industrial edit"


async def test_apply_patch_can_update_standing_prompt_file(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    (tmp_path / "SOUL.md").write_text("original\n", encoding="utf-8")
    ok, payload = await LifeEngineApplyPatchTool(plugin=plugin).execute(
        """*** Begin Patch
*** Update File: SOUL.md
@@
-original
+rewritten
*** End Patch
"""
    )
    assert ok is True
    assert isinstance(payload, dict)
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "rewritten\n"


async def test_write_file_rejects_empty_soul(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    (tmp_path / "SOUL.md").write_text("original\n", encoding="utf-8")
    ok, error = await LifeEngineWriteFileTool(plugin=plugin).execute(
        "SOUL.md",
        "   \n",
        reason="empty soul cannot be assembled",
    )
    assert ok is False
    assert "StandingPromptSoulEmpty" in str(error)
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "original\n"


async def test_apply_patch_rejects_deleting_standing_prompt_file(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    (tmp_path / "MEMORY.md").write_text("keep\n", encoding="utf-8")
    ok, error = await LifeEngineApplyPatchTool(plugin=plugin).execute(
        """*** Begin Patch
*** Delete File: MEMORY.md
*** End Patch
"""
    )
    assert ok is False
    assert "StandingPromptPathProtected" in str(error)
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "keep\n"


async def test_write_standing_prompt_commits_selected_store_before_disk(
    tmp_path: Path,
) -> None:
    commits: list[dict[str, object]] = []

    async def commit_subject_authority_file_write(**kwargs: object) -> dict[str, str]:
        assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "original\n"
        commits.append(kwargs)
        return {"status": "committed"}

    plugin = _plugin(tmp_path)
    plugin.service._selectable_storage_enabled = True
    plugin.service.commit_subject_authority_file_write = (
        commit_subject_authority_file_write
    )
    (tmp_path / "SOUL.md").write_text("original\n", encoding="utf-8")

    ok, payload = await LifeEngineWriteFileTool(plugin=plugin).execute(
        "SOUL.md",
        "next-turn\n",
        reason="prompt must follow the write",
    )

    assert ok is True
    assert isinstance(payload, dict)
    assert len(commits) == 1
    assert commits[0]["workspace_relative_path"] == "SOUL.md"
    assert commits[0]["content_bytes"] == b"next-turn\n"
    assert (tmp_path / "SOUL.md").read_text(encoding="utf-8") == "next-turn\n"


async def test_edit_file_strips_read_line_prefixes(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    path = tmp_path / "notes.md"
    path.write_text("hello world\n", encoding="utf-8")
    ok, payload = await LifeEngineEditFileTool(plugin=plugin).execute(
        "notes.md",
        "1\thello world",
        "2\thello elysia",
        reason="strip prefixes",
    )
    assert ok is True
    assert isinstance(payload, dict)
    assert path.read_text(encoding="utf-8") == "hello elysia\n"


async def test_apply_patch_does_not_write_when_any_hunk_misses(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    first = tmp_path / "a.md"
    second = tmp_path / "b.md"
    first.write_text("keep-a\n", encoding="utf-8")
    second.write_text("keep-b\n", encoding="utf-8")
    ok, error = await LifeEngineApplyPatchTool(plugin=plugin).execute(
        """*** Begin Patch
*** Update File: a.md
@@
-keep-a
+changed-a
*** Update File: b.md
@@
-missing
+nope
*** End Patch
"""
    )
    assert ok is False
    assert "未找到" in str(error)
    assert first.read_text(encoding="utf-8") == "keep-a\n"
    assert second.read_text(encoding="utf-8") == "keep-b\n"


async def test_apply_patch_adds_and_deletes_in_one_call(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    gone = tmp_path / "old.md"
    gone.write_text("bye\n", encoding="utf-8")
    ok, payload = await LifeEngineApplyPatchTool(plugin=plugin).execute(
        """*** Begin Patch
*** Add File: notes/new.md
+hello
*** Delete File: old.md
*** End Patch
"""
    )
    assert ok is True
    assert isinstance(payload, dict)
    operations = {item["path"]: item["operation"] for item in payload["files"]}
    assert operations["notes/new.md"] == "add"
    assert operations["old.md"] == "delete"
    assert (tmp_path / "notes" / "new.md").read_text(encoding="utf-8") == "hello\n"
    assert gone.exists() is False


def test_file_tools_are_visible_to_chatter() -> None:
    for cls in (
        LifeEngineReadFileTool,
        LifeEngineWriteFileTool,
        LifeEngineEditFileTool,
        LifeEngineApplyPatchTool,
        LifeEngineGlobFileTool,
        LifeEngineGrepFileTool,
    ):
        assert "life_chatter" in cls.chatter_allow
        assert cls.chatter_allow == FILE_CHATTER_ALLOW or "life_chatter" in cls.chatter_allow


def test_heartbeat_and_chat_manifests_include_apply_patch() -> None:
    names = {cls.tool_name for cls in heartbeat_tool_classes()}
    assert "nucleus_apply_patch" in names
    assert "nucleus_glob_file" in names
    chat = set(get_tool_manifest("chat"))
    assert "tool-nucleus_apply_patch" in chat
    assert "tool-nucleus_glob_file" in chat
    schema = LifeEngineApplyPatchTool.to_schema()["function"]
    assert schema["name"] == "tool-nucleus_apply_patch"
    assert "input" in schema["parameters"]["required"]


def test_resolve_registry_tool_accepts_bare_and_prefixed_names() -> None:
    class _Registry:
        def __init__(self) -> None:
            self._tools = {"tool-nucleus_apply_patch": LifeEngineApplyPatchTool}

        def get(self, name: str):
            return self._tools.get(name)

    registry = _Registry()
    assert resolve_registry_tool(registry, "nucleus_apply_patch") is LifeEngineApplyPatchTool
    assert (
        resolve_registry_tool(registry, "tool-nucleus_apply_patch")
        is LifeEngineApplyPatchTool
    )
    assert resolve_registry_tool(registry, "missing") is None


async def test_glob_file_finds_markdown_by_mtime(tmp_path: Path) -> None:
    plugin = _plugin(tmp_path)
    older = tmp_path / "a.md"
    newer = tmp_path / "notes" / "b.md"
    newer.parent.mkdir()
    older.write_text("old\n", encoding="utf-8")
    newer.write_text("new\n", encoding="utf-8")
    older_stat = older.stat()
    os.utime(newer, (older_stat.st_atime, older_stat.st_mtime + 5))
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.md").write_text("hidden\n", encoding="utf-8")
    (tmp_path / ".secret.md").write_text("hidden\n", encoding="utf-8")
    ok, payload = await LifeEngineGlobFileTool(plugin=plugin).execute("**/*.md")
    assert ok is True
    assert isinstance(payload, dict)
    paths = [item["path"] for item in payload["items"]]
    assert paths[0] == "notes/b.md"
    assert "a.md" in paths
    assert ".secret.md" not in paths
    assert not any(path.startswith(".git/") for path in paths)
