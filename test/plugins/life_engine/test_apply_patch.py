from __future__ import annotations

import pytest

from plugins.life_engine.tools.apply_patch import (
    ApplyPatchError,
    apply_ops_to_contents,
    parse_apply_patch,
    strip_read_line_prefixes,
)


def test_parse_add_update_delete_and_move() -> None:
    patch = """*** Begin Patch
*** Add File: notes/new.md
+hello
*** Update File: notes/old.md
*** Move to: notes/renamed.md
@@
 aaa
-bbb
+BBB
 ccc
*** Delete File: notes/gone.md
*** End Patch
"""
    ops = parse_apply_patch(patch)
    assert [op.kind for op in ops] == ["add", "update", "delete"]
    assert ops[0].path == "notes/new.md"
    assert ops[0].add_content == "hello\n"
    assert ops[1].path == "notes/old.md"
    assert ops[1].move_to == "notes/renamed.md"
    assert ops[1].hunks[0].old_lines == ("aaa", "bbb", "ccc")
    assert ops[1].hunks[0].new_lines == ("aaa", "BBB", "ccc")
    assert ops[2].path == "notes/gone.md"


def test_apply_unique_hunks_and_leaves_other_files_untouched() -> None:
    files = {
        "notes/old.md": "aaa\nbbb\nccc\nddd\n",
        "keep.md": "stay\n",
    }
    ops = parse_apply_patch(
        """*** Begin Patch
*** Update File: notes/old.md
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
"""
    )
    snapshot = dict(files)
    planned = apply_ops_to_contents(ops, files)
    assert files == snapshot
    assert planned[0].content == "aaa\nBBB\nccc\nDDD\n"
    assert "keep.md" not in {item.path for item in planned}


def test_hunk_zero_and_duplicate_matches_fail() -> None:
    files = {"a.md": "foo\nfoo\n"}
    missing = parse_apply_patch(
        """*** Begin Patch
*** Update File: a.md
@@
-bar
+baz
*** End Patch
"""
    )
    with pytest.raises(ApplyPatchError, match="未找到"):
        apply_ops_to_contents(missing, files)
    duplicate = parse_apply_patch(
        """*** Begin Patch
*** Update File: a.md
@@
-foo
+bar
*** End Patch
"""
    )
    with pytest.raises(ApplyPatchError, match="2 次"):
        apply_ops_to_contents(duplicate, files)


def test_add_existing_file_fails() -> None:
    ops = parse_apply_patch(
        """*** Begin Patch
*** Add File: a.md
+new
*** End Patch
"""
    )
    with pytest.raises(ApplyPatchError, match="已存在"):
        apply_ops_to_contents(ops, {"a.md": "old\n"})


def test_numbered_read_prefixes_are_stripped_then_matched() -> None:
    files = {"a.md": "hello world\n"}
    ops = parse_apply_patch(
        """*** Begin Patch
*** Update File: a.md
@@
-1\thello world
+2\thello elysia
*** End Patch
"""
    )
    planned = apply_ops_to_contents(ops, files)
    assert planned[0].content == "hello elysia\n"


def test_rename_only_update_and_end_of_file_insert() -> None:
    files = {"a.md": "hello\n"}
    moved = apply_ops_to_contents(
        parse_apply_patch(
            """*** Begin Patch
*** Update File: a.md
*** Move to: b.md
*** End Patch
"""
        ),
        files,
    )
    assert [(item.path, item.action, item.content) for item in moved] == [
        ("b.md", "write", "hello\n"),
        ("a.md", "delete", None),
    ]
    appended = apply_ops_to_contents(
        parse_apply_patch(
            """*** Begin Patch
*** Update File: a.md
@@
+world
*** End of File
*** End Patch
"""
        ),
        files,
    )
    assert appended[0].content == "hello\nworld\n"


def test_missing_begin_end_fails() -> None:
    with pytest.raises(ApplyPatchError, match="Begin Patch"):
        parse_apply_patch("*** Update File: a.md\n@@\n-a\n+b\n")


def test_strip_read_line_prefixes_requires_uniform_prefix() -> None:
    assert strip_read_line_prefixes("1\talpha\n2\tbeta\n") == "alpha\nbeta\n"
    assert strip_read_line_prefixes("alpha\n2\tbeta\n") is None
