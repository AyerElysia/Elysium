#!/usr/bin/env python3
"""从训练数据湖导出后训练数据集。

读取 ``raw/`` 与 ``archive/`` 里的 append-only JSONL，做质量过滤后按格式
输出到 ``export/``。原始数据永不改写，导出可以反复重跑。

三种格式：

- ``sft_chat``：OpenAI messages 格式的对话样本（闲聊/回复类）。
- ``agent``：带 tool_call / tool_result 链路的 agent 轨迹。
- ``stats``：只统计不落盘，用来先看清数据分布。

用法::

    python scripts/export_training_data.py --stats
    python scripts/export_training_data.py --format sft_chat
    python scripts/export_training_data.py --format agent --include-archive
    python scripts/export_training_data.py --format sft_chat --request-name life_chatter
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_LAKE = PROJECT_ROOT / "data/training_data_lake"

# 面向"说话方式"训练的请求名：产出自然语言回复。
CHAT_REQUEST_NAMES = {
    "life_chatter",
    "default_chatter",
    "neko_chat",
    "sister_bridge_chat",
    "legacy_chat_history",
}
# 面向"工具使用/决策"训练的请求名：产出 tool 链路与路由判断。
AGENT_REQUEST_NAMES = {
    "life_engine_heartbeat",
    "router",
    "life_curiosity",
    "life_learning_reflection",
}
# 工具输出往往是大段文件/命令回显，超长就截断，避免污染训练集。
VERBOSE_TOOL_NAMES = {
    "nucleus_read_file",
    "tool-nucleus_read_file",
    "nucleus_bash",
    "tool-nucleus_bash",
}

MIN_TOTAL_TOKENS = 10
MAX_TOTAL_TOKENS = 100_000
MAX_TOOL_OUTPUT_CHARS = 5_000


def iter_records(lake: Path, *, include_archive: bool) -> Iterator[dict[str, Any]]:
    """按文件名顺序读出湖里的所有轨迹记录。"""
    sources = [lake / "raw"]
    if include_archive:
        sources.append(lake / "archive")
    for directory in sources:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.jsonl")):
            with path.open("r", encoding="utf-8") as file:
                for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        record.setdefault("_source_file", path.name)
                        yield record


def _usage_total(record: dict[str, Any]) -> int | None:
    usage = record.get("usage") or {}
    if not isinstance(usage, dict):
        return None
    values = [usage.get("prompt_tokens"), usage.get("completion_tokens")]
    numbers = [int(v) for v in values if isinstance(v, (int, float))]
    return sum(numbers) if numbers else None


def should_include_in_training(record: dict[str, Any], *, require_response: bool) -> tuple[bool, str]:
    """质量门：返回 (是否保留, 拒绝原因)。"""
    if record.get("success") is False:
        return False, "failed_attempt"

    messages = record.get("messages")
    if not isinstance(messages, list) or not messages:
        return False, "no_messages"

    if require_response:
        response = record.get("response")
        content = response.get("content") if isinstance(response, dict) else response
        if not isinstance(content, str) or not content.strip():
            return False, "empty_response"

    total = _usage_total(record)
    if total is not None:
        if total < MIN_TOTAL_TOKENS:
            return False, "too_few_tokens"
        if total > MAX_TOTAL_TOKENS:
            return False, "too_many_tokens"

    return True, ""


def _truncate_tool_output(message: dict[str, Any]) -> dict[str, Any]:
    """截断啰嗦工具的巨型输出，保留结构但压掉体积。"""
    name = str(message.get("name") or "")
    content = message.get("content")
    if (
        name in VERBOSE_TOOL_NAMES
        and isinstance(content, str)
        and len(content) > MAX_TOOL_OUTPUT_CHARS
    ):
        clipped = dict(message)
        clipped["content"] = (
            content[:MAX_TOOL_OUTPUT_CHARS] + f"\n...[truncated {len(content) - MAX_TOOL_OUTPUT_CHARS} chars]"
        )
        clipped["truncated"] = True
        return clipped
    return message


def to_sft_chat(record: dict[str, Any]) -> dict[str, Any] | None:
    """转成 OpenAI messages 格式的 SFT 样本。"""
    messages: list[dict[str, Any]] = []
    for item in record.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        messages.append({"role": role, "content": content})

    if not messages:
        return None

    response = record.get("response")
    reply = response.get("content") if isinstance(response, dict) else response
    if isinstance(reply, str) and reply.strip():
        if not messages or messages[-1]["role"] != "assistant":
            messages.append({"role": "assistant", "content": reply})

    if not any(m["role"] == "assistant" for m in messages):
        return None

    return {
        "messages": messages,
        "meta": {
            "request_id": record.get("request_id"),
            "request_name": record.get("request_name"),
            "task_tags": record.get("task_tags"),
            "model": record.get("model_identifier"),
            "timestamp": record.get("timestamp"),
            "source_file": record.get("_source_file"),
            "completeness": (record.get("metadata") or {}).get("completeness"),
        },
    }


def to_agent_trajectory(record: dict[str, Any]) -> dict[str, Any] | None:
    """保留 tool 链路的 agent 轨迹样本。"""
    messages: list[dict[str, Any]] = []
    tool_calls = 0
    for item in record.get("messages") or []:
        if not isinstance(item, dict):
            continue
        cleaned = _truncate_tool_output(item)
        if cleaned.get("tool_calls"):
            tool_calls += 1
        messages.append(cleaned)

    if not messages:
        return None

    response = record.get("response")
    return {
        "messages": messages,
        "response": response,
        "tool_results": record.get("tool_results") or [],
        "meta": {
            "request_id": record.get("request_id"),
            "attempt_id": record.get("attempt_id"),
            "request_name": record.get("request_name"),
            "task_tags": record.get("task_tags"),
            "heartbeat_run_id": record.get("heartbeat_run_id"),
            "model": record.get("model_identifier"),
            "timestamp": record.get("timestamp"),
            "tool_call_turns": tool_calls,
            "source_file": record.get("_source_file"),
            "completeness": (record.get("metadata") or {}).get("completeness"),
        },
    }
def print_stats(records: list[dict[str, Any]]) -> None:
    """先看清分布，再决定导什么。"""
    names = Counter(r.get("request_name") or "<none>" for r in records)
    models = Counter(r.get("model_identifier") or "<none>" for r in records)
    success = Counter(str(r.get("success")) for r in records)
    completeness = Counter(str((r.get("metadata") or {}).get("completeness")) for r in records)
    sources = Counter(r.get("_source_file") or "<none>" for r in records)

    print(f"总记录数: {len(records)}")
    print("\nrequest_name 分布:")
    for name, count in names.most_common(20):
        print(f"  {name:32s} {count:7d}")
    print("\n模型分布:")
    for name, count in models.most_common(10):
        print(f"  {name:32s} {count:7d}")
    print(f"\nsuccess: {dict(success)}")
    print(f"completeness: {dict(completeness)}")
    print("\n来源文件:")
    for name, count in sources.most_common(10):
        print(f"  {name:40s} {count:7d}")


def main() -> None:
    parser = argparse.ArgumentParser(description="从训练数据湖导出后训练数据集")
    parser.add_argument(
        "--format",
        choices=["sft_chat", "agent"],
        help="导出格式；不传则只统计",
    )
    parser.add_argument("--stats", action="store_true", help="只打印统计，不导出")
    parser.add_argument("--lake", default=str(DEFAULT_LAKE), help="数据湖根目录")
    parser.add_argument(
        "--include-archive",
        action="store_true",
        help="同时读取 archive/（迁移来的历史数据）",
    )
    parser.add_argument(
        "--request-name",
        action="append",
        default=None,
        help="只导出指定 request_name，可重复传",
    )
    parser.add_argument(
        "--min-completeness",
        type=float,
        default=0.0,
        help="过滤掉 completeness 低于该值的迁移数据",
    )
    parser.add_argument("--out", default=None, help="输出文件路径（默认写进 export/）")
    args = parser.parse_args()

    lake = Path(args.lake)
    if not lake.is_dir():
        print(f"数据湖不存在: {lake}")
        sys.exit(1)

    records = list(iter_records(lake, include_archive=args.include_archive))
    if not records:
        print("湖里没有任何记录")
        sys.exit(0)

    if args.stats or not args.format:
        print_stats(records)
        if not args.format:
            return

    if args.request_name:
        allowed = set(args.request_name)
    elif args.format == "sft_chat":
        allowed = CHAT_REQUEST_NAMES
    else:
        allowed = AGENT_REQUEST_NAMES

    require_response = args.format == "sft_chat"
    converter = to_sft_chat if args.format == "sft_chat" else to_agent_trajectory

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = Path(args.out) if args.out else lake / "export" / f"{args.format}_{today}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rejected: Counter[str] = Counter()
    written = 0
    with out_path.open("w", encoding="utf-8", newline="\n") as file:
        for record in records:
            if (record.get("request_name") or "") not in allowed:
                rejected["request_name_not_allowed"] += 1
                continue

            completeness = (record.get("metadata") or {}).get("completeness")
            if isinstance(completeness, (int, float)) and completeness < args.min_completeness:
                rejected["below_min_completeness"] += 1
                continue

            keep, reason = should_include_in_training(record, require_response=require_response)
            if not keep:
                rejected[reason] += 1
                continue

            sample = converter(record)
            if sample is None:
                rejected["conversion_failed"] += 1
                continue

            file.write(json.dumps(sample, ensure_ascii=False, separators=(",", ":")))
            file.write("\n")
            written += 1

    print(f"\n已导出 {written} 条 -> {out_path}")
    if rejected:
        print("过滤统计:")
        for reason, count in rejected.most_common():
            print(f"  {reason:28s} {count:7d}")


if __name__ == "__main__":
    main()
