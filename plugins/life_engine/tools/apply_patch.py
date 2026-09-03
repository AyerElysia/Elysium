"""Codex-style apply_patch parser and in-memory applier.

The format matches Codex CLI / OpenClaw:

```
*** Begin Patch
*** Add File: path/to/new.txt
+line
*** Update File: path/to/existing.txt
*** Move to: path/to/renamed.txt
@@
 context
-old
+new
*** Delete File: obsolete.txt
*** End Patch
```

Matching is exact and unique. The only non-exact step is stripping the
``N<TAB>`` prefixes that ``nucleus_read_file`` injects, and only when every
line of a search block has that shape.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

_READ_LINE_PREFIX = re.compile(r"^\d+\t")

PatchKind = Literal["add", "update", "delete"]
PlanAction = Literal["write", "delete"]


class ApplyPatchError(ValueError):
    """A malformed patch or a hunk that does not uniquely apply."""


@dataclass(frozen=True, slots=True)
class PatchHunk:
    header: str
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    end_of_file: bool = False


@dataclass(frozen=True, slots=True)
class PatchOp:
    kind: PatchKind
    path: str
    move_to: str | None = None
    hunks: tuple[PatchHunk, ...] = ()
    add_content: str = ""


@dataclass(frozen=True, slots=True)
class PlannedFile:
    path: str
    action: PlanAction
    content: str | None
    operation: str
    source_path: str = ""


def strip_read_line_prefixes(text: str) -> str | None:
    """Return text with ``N<TAB>`` prefixes removed, or None if not uniform."""

    if not text:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    stripped: list[str] = []
    for line in lines:
        match = _READ_LINE_PREFIX.match(line)
        if match is None:
            return None
        stripped.append(line[match.end() :])
    result = "\n".join(stripped)
    if text.endswith("\n"):
        result += "\n"
    return result


def _normalize_relpath(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        raise ApplyPatchError("patch 路径不能为空")
    parsed = PurePosixPath(raw)
    if parsed.is_absolute() or parsed.anchor:
        raise ApplyPatchError(f"patch 路径必须相对 workspace: {path}")
    if ".." in parsed.parts:
        raise ApplyPatchError(f"patch 路径不能包含 '..': {path}")
    normalized = parsed.as_posix().lstrip("./")
    if not normalized or normalized == ".":
        raise ApplyPatchError(f"patch 路径非法: {path}")
    return normalized


def _join_lines(lines: tuple[str, ...] | list[str]) -> str:
    return "\n".join(lines)


def _search_variants(block: str) -> list[str]:
    variants = [block]
    stripped = strip_read_line_prefixes(block)
    if stripped is not None and stripped != block:
        variants.append(stripped)
    return variants


def _replacement_text(old_original: str, matched: str, new_block: str) -> str:
    if matched == old_original:
        return new_block
    stripped_new = strip_read_line_prefixes(new_block)
    return stripped_new if stripped_new is not None else new_block


def _unique_replace(content: str, old_block: str, new_block: str, *, path: str) -> str:
    last_count = 0
    for candidate in _search_variants(old_block):
        if candidate == "":
            continue
        last_count = content.count(candidate)
        if last_count == 1:
            return content.replace(
                candidate,
                _replacement_text(old_block, candidate, new_block),
                1,
            )
        if last_count > 1:
            raise ApplyPatchError(
                f"`{path}` 的 hunk 匹配了 {last_count} 次，请增加上下文使其唯一"
            )
    raise ApplyPatchError(f"`{path}` 中未找到要替换的 hunk（命中 {last_count} 次）")


def apply_hunk(content: str, hunk: PatchHunk, *, path: str) -> str:
    old_block = _join_lines(hunk.old_lines)
    new_block = _join_lines(hunk.new_lines)
    if old_block:
        return _unique_replace(content, old_block, new_block, path=path)

    insertion = new_block
    if hunk.end_of_file:
        if not insertion:
            return content
        prefix = "" if not content or content.endswith("\n") else "\n"
        suffix = "" if insertion.endswith("\n") else "\n"
        return f"{content}{prefix}{insertion}{suffix}"

    header = str(hunk.header or "").strip()
    if not header:
        raise ApplyPatchError(
            f"`{path}` 纯插入 hunk 需要 @@ 定位文本或 *** End of File"
        )
    located = None
    last_count = 0
    for candidate in _search_variants(header):
        last_count = content.count(candidate)
        if last_count == 1:
            located = candidate
            break
        if last_count > 1:
            raise ApplyPatchError(
                f"`{path}` 的 @@ 定位 `{header}` 出现 {last_count} 次，无法插入"
            )
    if located is None:
        raise ApplyPatchError(f"`{path}` 未找到 @@ 定位 `{header}`（命中 {last_count} 次）")
    index = content.find(located)
    newline_at = content.find("\n", index)
    if newline_at == -1:
        prefix = "" if content.endswith("\n") or not content else "\n"
        suffix = "" if insertion.endswith("\n") else "\n"
        return f"{content}{prefix}{insertion}{suffix}"
    inserted = insertion if insertion.endswith("\n") else f"{insertion}\n"
    return content[: newline_at + 1] + inserted + content[newline_at + 1 :]


def parse_apply_patch(text: str) -> tuple[PatchOp, ...]:
    raw = str(text or "").replace("\r\n", "\n")
    begin = raw.find("*** Begin Patch")
    end = raw.find("*** End Patch", begin + 1 if begin >= 0 else 0)
    if begin < 0 or end < 0 or end < begin:
        raise ApplyPatchError("patch 必须包含 *** Begin Patch 和 *** End Patch")
    body = raw[begin + len("*** Begin Patch") : end].removeprefix("\n")

    ops: list[PatchOp] = []
    kind: PatchKind | None = None
    path = ""
    move_to: str | None = None
    add_lines: list[str] = []
    hunks: list[PatchHunk] = []
    hunk_header = ""
    hunk_old: list[str] = []
    hunk_new: list[str] = []
    hunk_end = False
    in_hunk = False

    def flush_hunk() -> None:
        nonlocal in_hunk, hunk_header, hunk_old, hunk_new, hunk_end
        if not in_hunk:
            return
        hunks.append(
            PatchHunk(
                header=hunk_header,
                old_lines=tuple(hunk_old),
                new_lines=tuple(hunk_new),
                end_of_file=hunk_end,
            )
        )
        in_hunk = False
        hunk_header = ""
        hunk_old = []
        hunk_new = []
        hunk_end = False

    def flush_op() -> None:
        nonlocal kind, path, move_to, add_lines, hunks
        flush_hunk()
        if kind is None:
            return
        if kind == "add":
            content = _join_lines(add_lines)
            if add_lines:
                content += "\n"
            ops.append(PatchOp(kind="add", path=path, add_content=content))
        elif kind == "delete":
            ops.append(PatchOp(kind="delete", path=path))
        else:
            if not hunks and not move_to:
                raise ApplyPatchError(f"`{path}` 的 Update File 缺少 hunk 或 Move to")
            ops.append(
                PatchOp(
                    kind="update",
                    path=path,
                    move_to=move_to,
                    hunks=tuple(hunks),
                )
            )
        kind = None
        path = ""
        move_to = None
        add_lines = []
        hunks = []

    for line in body.split("\n"):
        if line.startswith("*** Add File:"):
            flush_op()
            kind = "add"
            path = _normalize_relpath(line[len("*** Add File:") :])
            continue
        if line.startswith("*** Update File:"):
            flush_op()
            kind = "update"
            path = _normalize_relpath(line[len("*** Update File:") :])
            continue
        if line.startswith("*** Delete File:"):
            flush_op()
            kind = "delete"
            path = _normalize_relpath(line[len("*** Delete File:") :])
            continue
        if line.startswith("*** Move to:"):
            if kind != "update":
                raise ApplyPatchError("*** Move to: 只能出现在 Update File 之后")
            move_to = _normalize_relpath(line[len("*** Move to:") :])
            continue
        if line.startswith("*** End of File"):
            if kind != "update" or not in_hunk:
                raise ApplyPatchError("*** End of File 只能出现在 Update hunk 内")
            hunk_end = True
            continue
        if line.startswith("@@"):
            if kind != "update":
                raise ApplyPatchError("@@ hunk 只能出现在 Update File 中")
            flush_hunk()
            in_hunk = True
            hunk_header = line[2:].strip()
            continue
        if kind is None:
            if not line.strip():
                continue
            raise ApplyPatchError(f"无法解析的 patch 行: {line[:80]}")
        if kind == "add":
            if line.startswith("+"):
                add_lines.append(line[1:])
                continue
            if not line.strip():
                continue
            raise ApplyPatchError(f"`{path}` 的 Add File 内容行必须以 + 开头")
        if kind == "delete":
            if not line.strip():
                continue
            raise ApplyPatchError(f"`{path}` 的 Delete File 不能包含内容行")
        if not in_hunk:
            if not line.strip():
                continue
            raise ApplyPatchError(f"`{path}` 的 Update 内容必须写在 @@ hunk 里")
        if line.startswith("+"):
            hunk_new.append(line[1:])
        elif line.startswith("-"):
            hunk_old.append(line[1:])
        elif line.startswith(" "):
            hunk_old.append(line[1:])
            hunk_new.append(line[1:])
        else:
            hunk_old.append(line)
            hunk_new.append(line)

    flush_op()
    if not ops:
        raise ApplyPatchError("patch 不包含任何文件操作")
    return tuple(ops)


def apply_ops_to_contents(
    ops: tuple[PatchOp, ...] | list[PatchOp],
    files: dict[str, str | None],
) -> tuple[PlannedFile, ...]:
    """Apply ops in memory. ``files`` is not mutated."""

    working = dict(files)
    planned: list[PlannedFile] = []
    for op in ops:
        if op.kind == "add":
            if working.get(op.path) is not None:
                raise ApplyPatchError(
                    f"Add File 失败：`{op.path}` 已存在，请改用 Update File"
                )
            working[op.path] = op.add_content
            planned.append(
                PlannedFile(
                    path=op.path,
                    action="write",
                    content=op.add_content,
                    operation="add",
                )
            )
            continue
        if op.kind == "delete":
            if working.get(op.path) is None:
                raise ApplyPatchError(f"Delete File 失败：`{op.path}` 不存在")
            working[op.path] = None
            planned.append(
                PlannedFile(
                    path=op.path,
                    action="delete",
                    content=None,
                    operation="delete",
                )
            )
            continue

        current = working.get(op.path)
        if current is None:
            raise ApplyPatchError(f"Update File 失败：`{op.path}` 不存在")
        updated = current
        for hunk in op.hunks:
            updated = apply_hunk(updated, hunk, path=op.path)
        dest = op.move_to or op.path
        if op.move_to:
            if dest == op.path:
                raise ApplyPatchError(f"`{op.path}` 的 Move to 与源路径相同")
            if working.get(dest) is not None:
                raise ApplyPatchError(f"Move to 失败：`{dest}` 已存在")
            working[op.path] = None
            working[dest] = updated
            planned.append(
                PlannedFile(
                    path=dest,
                    action="write",
                    content=updated,
                    operation="move",
                    source_path=op.path,
                )
            )
            planned.append(
                PlannedFile(
                    path=op.path,
                    action="delete",
                    content=None,
                    operation="move",
                    source_path=op.path,
                )
            )
            continue
        working[op.path] = updated
        planned.append(
            PlannedFile(
                path=op.path,
                action="write",
                content=updated,
                operation="update",
            )
        )
    return tuple(planned)
