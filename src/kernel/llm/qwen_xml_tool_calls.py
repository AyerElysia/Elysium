"""Recover Qwen XML tool calls that landed in assistant text instead of tool_calls."""

from __future__ import annotations

import json
import re
from typing import Any

_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)
_TOOL_CALL_BLOCK = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL | re.IGNORECASE)
_FUNCTION_BLOCK = re.compile(
    r"<function=([^>\s]+)>(.*?)</function>",
    re.DOTALL | re.IGNORECASE,
)
_PARAMETER_BLOCK = re.compile(
    r"<parameter=([^>\s]+)>(.*?)</parameter>",
    re.DOTALL | re.IGNORECASE,
)
_LOOSE_FUNCTION = re.compile(r"<function=", re.IGNORECASE)
_LOOSE_TOOL_CALL = re.compile(r"<tool_call>", re.IGNORECASE)


def looks_like_qwen_xml_tool_markup(text: str) -> bool:
    raw = str(text or "")
    return bool(_LOOSE_TOOL_CALL.search(raw) or _LOOSE_FUNCTION.search(raw))


def split_qwen_think_tags(text: str) -> tuple[str, str]:
    """Move ``<think>`` bodies into reasoning and drop leftover think tags."""

    raw = str(text or "")
    chunks: list[str] = []

    def _keep(match: re.Match[str]) -> str:
        body = str(match.group(1) or "").strip()
        if body:
            chunks.append(body)
        return ""

    rest = _THINK_BLOCK.sub(_keep, raw)
    rest = re.sub(r"</think>", "", rest, flags=re.IGNORECASE)
    rest = re.sub(r"<think>", "", rest, flags=re.IGNORECASE)
    return "\n".join(chunks).strip(), rest.strip()


def parse_qwen_xml_tool_calls(raw_text: str) -> tuple[str, list[dict[str, Any]]]:
    """Lift Qwen XML markup into native tool-call dicts and strip it from text."""

    message = str(raw_text or "")
    if not looks_like_qwen_xml_tool_markup(message):
        return message, []

    calls: list[dict[str, Any]] = []
    for index, match in enumerate(_TOOL_CALL_BLOCK.finditer(message)):
        inner = str(match.group(1) or "")
        calls.extend(_parse_tool_call_inner(inner, index=index))

    if not calls and _LOOSE_FUNCTION.search(message):
        calls.extend(_parse_function_blocks(message, index_base=0))

    if not calls:
        return message, []

    cleaned = _TOOL_CALL_BLOCK.sub("", message)
    if _LOOSE_FUNCTION.search(cleaned):
        cleaned = _FUNCTION_BLOCK.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, calls


def recover_qwen_xml_tool_markup(
    raw_text: str,
    *,
    existing_calls: list[Any] | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Return ``(message, extra_reasoning, calls)``. Skip when native calls exist."""

    if existing_calls:
        reasoning, rest = split_qwen_think_tags(raw_text)
        return rest, reasoning, []
    reasoning, rest = split_qwen_think_tags(raw_text)
    message, calls = parse_qwen_xml_tool_calls(rest)
    return message, reasoning, calls


def _parse_tool_call_inner(inner: str, *, index: int) -> list[dict[str, Any]]:
    functions = _parse_function_blocks(inner, index_base=index)
    if functions:
        return functions
    payload = inner.strip()
    if not payload:
        return []
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if isinstance(decoded, list):
        return [
            call
            for item_index, item in enumerate(decoded)
            if isinstance(item, dict)
            for call in [_normalize_json_call(item, index=index + item_index)]
            if call is not None
        ]
    if isinstance(decoded, dict):
        call = _normalize_json_call(decoded, index=index)
        return [call] if call is not None else []
    return []


def _parse_function_blocks(text: str, *, index_base: int) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for offset, match in enumerate(_FUNCTION_BLOCK.finditer(text)):
        name = str(match.group(1) or "").strip()
        if not name:
            continue
        args: dict[str, Any] = {}
        for param in _PARAMETER_BLOCK.finditer(str(match.group(2) or "")):
            key = str(param.group(1) or "").strip()
            if not key:
                continue
            args[key] = str(param.group(2) or "").strip()
        calls.append(
            {
                "id": f"qwen_xml_{index_base + offset + 1}",
                "name": name,
                "args": args,
            }
        )
    return calls


def _normalize_json_call(item: dict[str, Any], *, index: int) -> dict[str, Any] | None:
    name = item.get("name")
    args = item.get("arguments", item.get("args"))
    function_obj = item.get("function")
    if isinstance(function_obj, dict):
        name = function_obj.get("name", name)
        if args is None:
            args = function_obj.get("arguments")
    if not isinstance(name, str) or not name.strip():
        return None
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            parsed = {"value": args}
        args = parsed
    if args is None:
        args = {}
    if not isinstance(args, dict):
        args = {"value": args}
    call_id = item.get("id")
    return {
        "id": call_id if isinstance(call_id, str) and call_id else f"qwen_xml_{index + 1}",
        "name": name,
        "args": args,
    }
