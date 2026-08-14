#!/usr/bin/env python3
"""把历史数据迁移进 text-only 训练数据湖（append-only JSONL）。

三个数据源：

- ``messages`` 表（SQLite）：按 stream 重建 user/assistant 轮次。
- ``life_events.jsonl``：按 heartbeat_run_id 聚合成 agent 轨迹。
- ``llm_metrics.json``：只有 usage/latency，迁成轨迹骨架，避免拆除旧落盘时丢数据。

所有输出统一走 ``ensure_trajectory_record``，因此媒体体、data URL、base64
一律被脱敏为 ``[removed]``，与在线落盘保持同一套规则。

用法::

    python scripts/migrate_training_data.py --all --dry-run
    python scripts/migrate_training_data.py --all
    python scripts/migrate_training_data.py --messages --limit 1000
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.kernel.llm.trajectory_types import (  # noqa: E402
    derive_task_tags,
    ensure_trajectory_record,
    new_trajectory_id,
    sanitize_text_only,
    utc_timestamp,
)

DEFAULT_LAKE = PROJECT_ROOT / "data/training_data_lake"
DEFAULT_DB = PROJECT_ROOT / "data/Elysium.db"
DEFAULT_LIFE_EVENTS = PROJECT_ROOT / "data/life_engine_workspace/life_events.jsonl"
DEFAULT_METRICS = PROJECT_ROOT / "data/json_storage/llm_metrics.json"

BOT_PERSON_IDS = {"bot", "self", "me"}


class ArchiveWriter:
    """把迁移结果写进 ``archive/``，一行一条，可 dry-run。"""

    def __init__(self, path: Path, *, dry_run: bool) -> None:
        self.path = path
        self.dry_run = dry_run
        self.count = 0
        self._handle = None

    def __enter__(self) -> "ArchiveWriter":
        if not self.dry_run:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        return self

    def write(self, record: dict[str, Any]) -> None:
        normalized = ensure_trajectory_record(record)
        self.count += 1
        if self._handle is None:
            return
        self._handle.write(
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        )
        self._handle.write("\n")

    def __exit__(self, *exc: Any) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None


def _iso_from_epoch(value: Any) -> str:
    try:
        return utc_timestamp(datetime.fromtimestamp(float(value), tz=timezone.utc))
    except (TypeError, ValueError, OSError, OverflowError):
        return utc_timestamp()


def _legacy_metadata(
    *,
    source: str,
    original: str,
    completeness: float,
    quality: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """所有迁移记录共用的血缘标记，便于后训练按可信度筛选。"""
    metadata = {
        "source": "legacy_migration",
        "migration_source": source,
        "original_table": original,
        "completeness": completeness,
        "quality": quality,
        "migrated_at": utc_timestamp(),
    }
    if extra:
        metadata.update(extra)
    return metadata


def _flush_message_session(
    *,
    writer: "ArchiveWriter",
    stream_id: str | None,
    session: list[dict[str, Any]],
    platform: str | None,
    min_turns: int,
) -> bool:
    """把一个已切分好的会话片段写成一条轨迹；不合格则跳过。"""
    if len(session) < min_turns:
        return False
    if not any(item["role"] == "assistant" for item in session):
        return False

    messages = [
        {"role": i["role"], "content": i["content"], "message_type": i["message_type"]}
        for i in session
    ]
    last_assistant = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
    request_id = new_trajectory_id("mig-msg")
    writer.write(
        {
            "trace_id": request_id,
            "request_id": request_id,
            "attempt_id": new_trajectory_id("attempt"),
            "timestamp": _iso_from_epoch(session[-1]["time"]),
            "request_name": "legacy_chat_history",
            "task_name": "legacy_chat_history",
            "task_tags": derive_task_tags("legacy_chat_history"),
            "stream_id": stream_id,
            "messages": messages,
            "response": {"content": last_assistant["content"]} if last_assistant else None,
            "success": True,
            "metadata": _legacy_metadata(
                source="messages_table",
                original="messages",
                completeness=0.6,
                quality="approximate",
                extra={
                    "platform": platform,
                    "turn_count": len(messages),
                    "started_at": _iso_from_epoch(session[0]["time"]),
                    "missing_fields": ["system_prompt", "model_identifier", "usage"],
                },
            ),
        }
    )
    return True


def migrate_messages(
    *,
    db_path: Path,
    out_path: Path,
    dry_run: bool,
    limit: int | None = None,
    min_turns: int = 2,
    session_gap_seconds: float = 1800.0,
    max_turns_per_session: int = 40,
) -> int:
    """把 ``messages`` 表重建成 user/assistant 轮次。

    单个 stream 会累积数万条消息，整体聚合只会产出无法训练的巨型样本，
    因此按 ``session_gap_seconds`` 的静默间隔切分会话，并对超长会话再按
    ``max_turns_per_session`` 二次切段。

    历史行缺少 system prompt 与真实模型名，因此 completeness 只给 0.6，
    并标注 quality=approximate，后训练时可据此单独处理。
    """
    if not db_path.exists():
        print(f"[messages] 跳过：数据库不存在 {db_path}")
        return 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, message_id, stream_id, person_id, time, message_type,
                   content, processed_plain_text, platform
            FROM messages
            ORDER BY stream_id, time
            """
        )
        written = 0
        reached_limit = False
        with ArchiveWriter(out_path, dry_run=dry_run) as writer:
            for stream_id, group in groupby(rows, key=lambda row: row["stream_id"]):
                session: list[dict[str, Any]] = []
                platform: str | None = None
                previous_time: float | None = None

                for row in group:
                    text = row["processed_plain_text"] or row["content"] or ""
                    text = sanitize_text_only(str(text))
                    if not text or text == "[removed]":
                        continue

                    try:
                        row_time = float(row["time"])
                    except (TypeError, ValueError):
                        row_time = previous_time or 0.0

                    gap = row_time - previous_time if previous_time is not None else 0.0
                    if session and (
                        gap > session_gap_seconds or len(session) >= max_turns_per_session
                    ):
                        if _flush_message_session(
                            writer=writer,
                            stream_id=stream_id,
                            session=session,
                            platform=platform,
                            min_turns=min_turns,
                        ):
                            written += 1
                            if limit is not None and written >= limit:
                                reached_limit = True
                                break
                        session = []

                    session.append(
                        {
                            "role": "assistant"
                            if str(row["person_id"] or "").lower() in BOT_PERSON_IDS
                            else "user",
                            "content": text,
                            "message_type": row["message_type"],
                            "time": row_time,
                        }
                    )
                    platform = platform or row["platform"]
                    previous_time = row_time

                if not reached_limit and session:
                    if _flush_message_session(
                        writer=writer,
                        stream_id=stream_id,
                        session=session,
                        platform=platform,
                        min_turns=min_turns,
                    ):
                        written += 1
                        if limit is not None and written >= limit:
                            reached_limit = True

                if reached_limit:
                    break
        print(f"[messages] {'预计' if dry_run else '已写入'} {written} 条 -> {out_path}")
        return written
    finally:
        conn.close()


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


_USER_EVENT_TYPES = {
    "text",
    "image",
    "emoji",
    "voice",
    "file",
    "video",
    "dfc_message",
    "direct_message",
}
_ASSISTANT_EVENT_TYPES = {"heartbeat_reply", "chatter_inner_monologue"}


def _run_key(event: dict[str, Any]) -> str | None:
    """life_events 的运行标识：优先 run_id，其次心跳序号。"""
    metadata = event.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    run_id = metadata.get("heartbeat_run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    index = metadata.get("heartbeat_index")
    if isinstance(index, int):
        return f"heartbeat-index-{index}"
    return None


def _event_message(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(event.get("event_type") or "")
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    content = sanitize_text_only(str(event.get("content") or ""))
    if not content or content == "[removed]":
        if event_type not in {"tool_call", "tool_result"}:
            return None

    if event_type in _USER_EVENT_TYPES:
        return {"role": "user", "content": content, "event_type": event_type}
    if event_type in _ASSISTANT_EVENT_TYPES:
        return {"role": "assistant", "content": content, "event_type": event_type}
    if event_type == "tool_call":
        return {
            "role": "assistant",
            "content": content,
            "event_type": event_type,
            "tool_calls": [
                {
                    "name": sanitize_text_only(metadata.get("tool_name")),
                    "arguments": sanitize_text_only(metadata.get("tool_args") or {}),
                }
            ],
        }
    if event_type == "tool_result":
        return {
            "role": "tool",
            "content": content,
            "event_type": event_type,
            "name": sanitize_text_only(metadata.get("tool_name")),
            "success": metadata.get("tool_success"),
        }
    return None
def migrate_life_events(
    *,
    life_events_path: Path,
    out_path: Path,
    dry_run: bool,
    limit: int | None = None,
    min_messages: int = 2,
) -> int:
    """按 heartbeat_run_id 把 life_events.jsonl 聚合成 agent 轨迹。

    每个 run 包含 user/tool_call/tool_result/heartbeat_reply 四类事件，
    重组为一条多轮轨迹，stream_id 来自运行内的第一条 text/chatter 事件。
    completeness=0.7（有 tool chain，但 system prompt 与 LLM usage 缺失）。
    """
    if not life_events_path.exists():
        print(f"[life_events] 跳过：文件不存在 {life_events_path}")
        return 0

    runs: dict[str, list[dict[str, Any]]] = {}
    no_run: list[dict[str, Any]] = []
    for event in _iter_jsonl(life_events_path):
        rk = _run_key(event)
        if rk:
            runs.setdefault(rk, []).append(event)
        else:
            no_run.append(event)

    written = 0
    with ArchiveWriter(out_path, dry_run=dry_run) as writer:
        for run_id, events in runs.items():
            events.sort(key=lambda e: e.get("sequence", 0))
            messages: list[dict[str, Any]] = []
            stream_id: str | None = None
            ts_start: str | None = None
            ts_end: str | None = None
            for event in events:
                msg = _event_message(event)
                if msg:
                    messages.append(msg)
                if not stream_id:
                    stream_id = event.get("stream_id") or None
                if ts_start is None:
                    ts_start = event.get("timestamp") or utc_timestamp()
                ts_end = event.get("timestamp") or ts_end

            if len(messages) < min_messages:
                continue

            # Prefer the last assistant message as the "response".
            last_assistant = next(
                (m for m in reversed(messages) if m.get("role") == "assistant"), None
            )
            request_id = new_trajectory_id("mig-hb")
            writer.write(
                {
                    "trace_id": request_id,
                    "request_id": request_id,
                    "attempt_id": new_trajectory_id("attempt"),
                    "timestamp": ts_end or utc_timestamp(),
                    "request_name": "life_engine_heartbeat",
                    "task_name": "life_engine_heartbeat",
                    "task_tags": derive_task_tags("life_engine_heartbeat"),
                    "stream_id": stream_id,
                    "heartbeat_run_id": run_id,
                    "messages": messages,
                    "response": {"content": last_assistant["content"]}
                    if last_assistant
                    else None,
                    "success": True,
                    "metadata": _legacy_metadata(
                        source="life_events_jsonl",
                        original="life_events.jsonl",
                        completeness=0.7,
                        quality="agent_trajectory",
                        extra={
                            "run_id": run_id,
                            "turn_count": len(messages),
                            "started_at": ts_start,
                            "missing_fields": ["system_prompt", "model_identifier", "usage"],
                        },
                    ),
                }
            )
            written += 1
            if limit is not None and written >= limit:
                break

    print(f"[life_events] {'预计' if dry_run else '已写入'} {written} 条 -> {out_path}")
    return written


def _recover_metrics_history(path: Path) -> list[dict[str, Any]]:
    """从（可能损坏的）metrics JSON 里尽量恢复 history。

    旧的落盘方式是整文件重写，中断会留下 ``Extra data`` 型损坏文件：
    前半段仍是合法 JSON。用 ``raw_decode`` 逐文档解析，能把这些
    ``.corrupt.*`` 备份里的历史指标救回来，而不是直接丢掉。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"[llm_metrics] 无法读取 {path.name}: {exc}")
        return []

    decoder = json.JSONDecoder()
    recovered: list[dict[str, Any]] = []
    index = 0
    length = len(text)
    while index < length:
        while index < length and text[index] in " \t\r\n":
            index += 1
        if index >= length:
            break
        try:
            document, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            break
        if isinstance(document, dict):
            history = document.get("history")
            if isinstance(history, list):
                recovered.extend(item for item in history if isinstance(item, dict))
        index = end
    return recovered


def _metrics_dedup_key(entry: dict[str, Any]) -> tuple[Any, ...]:
    return (
        entry.get("timestamp"),
        entry.get("model_name"),
        entry.get("request_name"),
        entry.get("latency"),
        entry.get("tokens_in"),
        entry.get("tokens_out"),
    )


def migrate_llm_metrics(
    *,
    metrics_path: Path,
    out_path: Path,
    dry_run: bool,
    include_corrupt: bool = True,
) -> int:
    """把 llm_metrics.json（含 ``.corrupt.*`` 备份）迁成 usage-only 轨迹骨架。

    这些记录没有 prompt/response，只有 model/tokens/latency/success，
    completeness=0.2，不会进入 SFT 主训练集，但保留在 archive/ 以便对账。
    跨文件按 (timestamp, model, request_name, latency, tokens) 去重。
    """
    sources: list[Path] = []
    if metrics_path.exists():
        sources.append(metrics_path)
    if include_corrupt:
        sources.extend(sorted(metrics_path.parent.glob(f"{metrics_path.name}.corrupt.*")))

    if not sources:
        print(f"[llm_metrics] 跳过：文件不存在 {metrics_path}")
        return 0

    history: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for source in sources:
        recovered = _recover_metrics_history(source)
        added = 0
        for entry in recovered:
            key = _metrics_dedup_key(entry)
            if key in seen:
                continue
            seen.add(key)
            history.append(entry)
            added += 1
        print(f"[llm_metrics] {source.name}: 恢复 {len(recovered)} 条，去重后新增 {added} 条")

    written = 0
    with ArchiveWriter(out_path, dry_run=dry_run) as writer:
        for entry in history:
            if not isinstance(entry, dict):
                continue
            request_id = new_trajectory_id("mig-met")
            model_name = sanitize_text_only(str(entry.get("model_name") or ""))
            ts_raw = entry.get("timestamp")
            timestamp: str
            if isinstance(ts_raw, (int, float)):
                timestamp = _iso_from_epoch(ts_raw)
            elif isinstance(ts_raw, str):
                timestamp = ts_raw
            else:
                timestamp = utc_timestamp()
            writer.write(
                {
                    "trace_id": request_id,
                    "request_id": request_id,
                    "attempt_id": new_trajectory_id("attempt"),
                    "timestamp": timestamp,
                    "request_name": sanitize_text_only(str(entry.get("request_name") or "")),
                    "task_name": sanitize_text_only(str(entry.get("request_name") or "")),
                    "task_tags": derive_task_tags(entry.get("request_name")),
                    "model": model_name,
                    "model_identifier": model_name,
                    "api_provider": None,
                    "messages": [],
                    "response": None,
                    "usage": {
                        "prompt_tokens": entry.get("tokens_in"),
                        "completion_tokens": entry.get("tokens_out"),
                    },
                    "latency_s": entry.get("latency"),
                    "success": bool(entry.get("success", True)),
                    "error": sanitize_text_only(str(entry.get("error") or "")) or None,
                    "metadata": _legacy_metadata(
                        source="llm_metrics_json",
                        original="llm_metrics.json",
                        completeness=0.2,
                        quality="metrics_only",
                        extra={
                            "stream": entry.get("stream"),
                            "retry_count": entry.get("retry_count"),
                            "model_index": entry.get("model_index"),
                        },
                    ),
                }
            )
            written += 1

    print(f"[llm_metrics] {'预计' if dry_run else '已写入'} {written} 条 -> {out_path}")
    return written
def main() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(
        description="迁移历史数据进 text-only 训练数据湖。默认 dry-run，需要 --run 才真正写入。"
    )
    parser.add_argument("--run", action="store_true", help="真正写入（默认 dry-run）")
    parser.add_argument("--all", action="store_true", help="执行全部三个迁移")
    parser.add_argument("--messages", action="store_true", help="迁移 messages 表")
    parser.add_argument("--life-events", action="store_true", help="迁移 life_events.jsonl")
    parser.add_argument("--metrics", action="store_true", help="迁移 llm_metrics.json")
    parser.add_argument("--limit", type=int, default=None, help="每个来源最多输出 N 条")
    parser.add_argument("--lake", default=str(DEFAULT_LAKE), help="数据湖根目录")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Elysium.db 路径")
    parser.add_argument(
        "--life-events-file", default=str(DEFAULT_LIFE_EVENTS), help="life_events.jsonl 路径"
    )
    parser.add_argument(
        "--metrics-file", default=str(DEFAULT_METRICS), help="llm_metrics.json 路径"
    )
    args = parser.parse_args()

    dry_run = not args.run
    if dry_run:
        print("=== DRY-RUN 模式，使用 --run 真正写入 ===\n")

    lake = Path(args.lake)
    archive = lake / "archive"

    do_messages = args.all or args.messages
    do_life = args.all or args.life_events
    do_metrics = args.all or args.metrics

    if not (do_messages or do_life or do_metrics):
        parser.print_help()
        sys.exit(0)

    total = 0
    if do_messages:
        total += migrate_messages(
            db_path=Path(args.db),
            out_path=archive / f"messages_migration_{today}.jsonl",
            dry_run=dry_run,
            limit=args.limit,
        )

    if do_life:
        total += migrate_life_events(
            life_events_path=Path(args.life_events_file),
            out_path=archive / f"life_events_migration_{today}.jsonl",
            dry_run=dry_run,
            limit=args.limit,
        )

    if do_metrics:
        total += migrate_llm_metrics(
            metrics_path=Path(args.metrics_file),
            out_path=archive / f"llm_metrics_migration_{today}.jsonl",
            dry_run=dry_run,
        )

    print(f"\n总计: {'预计' if dry_run else '已写入'} {total} 条轨迹记录")
    if dry_run:
        print("重新加 --run 执行实际写入。")


if __name__ == "__main__":
    main()
