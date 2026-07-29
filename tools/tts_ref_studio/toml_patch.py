"""对 tts_voice_plugin/config.toml 做"就地改值"的最小编辑器。

Elysium 的插件 TOML 是从 Python schema 渲染出来的，里面每个字段上方都带着
注释说明。用 tomli_w / json 之类整体重写会把这些注释全部抹掉，所以这里走
按行定位、只替换目标 key 值的路线：
  1. 扫描出所有 [[tts_styles]] 块的行区间；
  2. 在目标块内找到 key，把它的值（含跨行数组）整体换掉；
  3. key 不存在时插到块末尾。

只支持本工具需要写回的几个字段（字符串、字符串数组），刻意不做通用 TOML 编辑器。
"""

from __future__ import annotations

import re
import shutil
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any

# 顶层表头 / 数组表头，例如 [tts] 或 [[tts_styles]]
_TABLE_RE = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*(?:#.*)?$")
_ARRAY_TABLE_RE = re.compile(r"^\s*\[\[([^\]]+)\]\]\s*(?:#.*)?$")


def _dump_str(value: str) -> str:
    """按 TOML 基本字符串输出，转义反斜杠和引号。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", "\\n").replace("\t", "\\t")
    return f'"{escaped}"'


def _dump_value(value: Any, indent: str = "") -> list[str]:
    """把 Python 值渲染成 TOML 片段（返回多行）。"""
    if isinstance(value, str):
        return [_dump_str(value)]
    if isinstance(value, bool):
        return ["true" if value else "false"]
    if isinstance(value, (int, float)):
        return [repr(value)]
    if isinstance(value, (list, tuple)):
        items = list(value)
        if not items:
            return ["[]"]
        lines = ["["]
        for item in items:
            rendered = _dump_value(item, indent + "    ")
            if len(rendered) == 1:
                lines.append(f"{indent}    {rendered[0]},")
            else:
                rendered[0] = f"{indent}    {rendered[0]}"
                rendered[-1] = f"{rendered[-1]},"
                lines.extend(rendered)
        lines.append(f"{indent}]")
        return lines
    raise TypeError(f"不支持写回的值类型: {type(value)!r}")


class TomlValueEditor:
    """按行编辑 TOML，保留注释与排版。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lines: list[str] = self.path.read_text(encoding="utf-8").splitlines()

    # ---------------- 结构定位 ----------------

    def _blocks(self, table_name: str) -> list[tuple[int, int]]:
        """返回指定数组表（[[name]]）每个块的 [起始行, 结束行) 区间。"""
        spans: list[tuple[int, int]] = []
        start: int | None = None
        for idx, line in enumerate(self.lines):
            m = _TABLE_RE.match(line)
            if not m:
                continue
            is_target = _ARRAY_TABLE_RE.match(line) is not None and m.group(1).strip() == table_name
            if start is not None:
                spans.append((start, idx))
                start = None
            if is_target:
                start = idx + 1
        if start is not None:
            spans.append((start, len(self.lines)))
        return spans

    def _find_key(self, span: tuple[int, int], key: str) -> tuple[int, int] | None:
        """在区间内定位 key，返回其值所占的 [起始行, 结束行)。"""
        start, end = span
        key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for idx in range(start, min(end, len(self.lines))):
            if not key_re.match(self.lines[idx]):
                continue
            # 值可能是跨行数组：向下找到括号配平的位置。
            depth = 0
            for j in range(idx, min(end, len(self.lines))):
                stripped = re.sub(r"#.*$", "", self.lines[j])
                depth += stripped.count("[") - stripped.count("]")
                if depth <= 0:
                    return (idx, j + 1)
            return (idx, idx + 1)
        return None

    def read_value(self, table_name: str, index: int, key: str) -> str | None:
        """读回某个 key 的原始行（调试用）。"""
        spans = self._blocks(table_name)
        if index >= len(spans):
            return None
        found = self._find_key(spans[index], key)
        if not found:
            return None
        return "\n".join(self.lines[found[0] : found[1]])

    # ---------------- 写入 ----------------

    def set_in_array_table(self, table_name: str, index: int, key: str, value: Any) -> None:
        """把 [[table_name]] 第 index 个块里的 key 设为 value。"""
        spans = self._blocks(table_name)
        if index >= len(spans):
            raise IndexError(f"[[{table_name}]] 只有 {len(spans)} 个块，取不到第 {index} 个")
        span = spans[index]
        rendered = _dump_value(value)
        new_lines = [f"{key} = {rendered[0]}"] + rendered[1:]

        found = self._find_key(span, key)
        if found:
            self.lines[found[0] : found[1]] = new_lines
        else:
            insert_at = span[1]
            # 跳过块尾的空行，让新 key 紧贴内容。
            while insert_at - 1 > span[0] and not self.lines[insert_at - 1].strip():
                insert_at -= 1
            self.lines[insert_at:insert_at] = [""] + new_lines

    def save(self, backup_dir: Path | None = None) -> Path | None:
        """写回磁盘，先备份，再用 tomllib 校验语法。

        校验失败会回滚，避免把损坏的配置留给 bot。
        """
        text = "\n".join(self.lines) + "\n"
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"生成的 TOML 语法非法，已放弃写入: {e}") from e

        backup_path: Path | None = None
        if backup_dir is not None and self.path.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = backup_dir / f"{self.path.stem}.{stamp}.toml"
            shutil.copy2(self.path, backup_path)

        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)
        return backup_path
