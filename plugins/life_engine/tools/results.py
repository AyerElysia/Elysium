"""life_engine 工具返回内容 helper。"""

from __future__ import annotations


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", True
    if len(text) <= limit:
        return text, False
    if limit <= 3:
        return text[:limit], True
    return text[: limit - 3] + "...", True


def decode_output(data: bytes | None, limit: int) -> tuple[str, bool]:
    text = (data or b"").decode("utf-8", errors="replace")
    return truncate_text(text, limit)
