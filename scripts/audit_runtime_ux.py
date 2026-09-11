"""Read-only log and storage metadata audit; never prints conversational bodies."""

from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
from pathlib import Path


LINE = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+)\s*\|\s*(\w+)\s*\|\s*([^|]+)\|\s*(.*)$")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    events: dict[str, list[dict]] = collections.defaultdict(list)
    compactions = []
    failures: dict[str, list[str]] = collections.defaultdict(list)
    components = collections.Counter()
    technical = []
    totals = {}
    for day in ("2026-09-09", "2026-09-10", "2026-09-11"):
        path = args.root / "logs" / f"elysium-{day}.log"
        if not path.is_file():
            continue
        total = 0
        for lineno, line in enumerate(path.open(encoding="utf-8"), 1):
            match = LINE.match(line)
            if not match:
                continue
            when, level, component, message = match.groups()
            component = component.strip()
            total += 1
            components[(day, level, component)] += 1
            for needle in (
                "runtime payload exceeds explicit storage limit",
                "SingletonWriterClaimLost", "RollingContextRecoveryRequired",
                "超时", "请求失败", "发送失败", "context_stewardship_required",
            ):
                if needle in message:
                    failures[needle].append(when)
            if component.endswith(".audit") and message.startswith("{"):
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                event = str(data.get("event", "unknown"))
                safe = {"time": when, "line": lineno, "file": path.name}
                for key, value in data.items():
                    if isinstance(value, (int, float, bool)) or value is None:
                        safe[key] = value
                    elif key in {"event", "kind", "component", "model_task_name", "status", "stop_reason", "stop_stage", "stall_kind", "error_type", "instance_id", "heartbeat_at"}:
                        safe[key] = str(value)[:200]
                safe["keys"] = sorted(data)
                events[event].append(safe)
            if "checkpoint_id=" in message and "released_groups=" in message:
                compactions.append({"time": when, "component": component, "line": lineno, "message": message})
            if any(marker in message for marker in (
                "TTS legacy 调用完成", "TTS服务请求超时", "TTS API调用失败",
                "LLM 请求完成", "LLM调用完成", "上下文压缩", "连续性检查点",
            )) and len(message) < 800:
                if "checkpoint_id=" in message or "chars=" in message or "_ms=" in message or "error_type=" in message:
                    technical.append({"time": when, "component": component, "message": message})
        totals[day] = total
    summary = {}
    for event, rows in events.items():
        summary[event] = {"count": len(rows), "first": rows[0], "last": rows[-1]}
        numeric = {}
        for key in rows[0]:
            if any(part in key for part in ("duration", "elapsed", "latency", "tokens", "bytes", "chars", "released")):
                values = sorted(float(row[key]) for row in rows if isinstance(row.get(key), (int, float)))
                if values:
                    numeric[key] = {"n": len(values), "min": values[0], "median": values[len(values)//2], "p95": values[min(len(values)-1, int(len(values)*.95))], "max": values[-1]}
        if numeric:
            summary[event]["metrics"] = numeric
    schemas = {}
    for relative in ("data/Elysium.db", "data/life_engine_workspace/.memory/memory.db", "data/logs.db", "data/life_storage/local.sqlite3", "data/life_engine_workspace/life_events.sqlite3"):
        db = args.root / relative
        if not db.exists():
            continue
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2) as connection:
            connection.execute("PRAGMA query_only=ON")
            names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            selected = [name for name in names if any(term in name for term in ("runtime", "checkpoint", "trajectory", "llm", "usage", "outbox", "conscious", "event"))]
            schemas[relative] = {name: [row[1] for row in connection.execute('PRAGMA table_info("'+name.replace('"','""')+'")')] for name in selected}
    result = {
        "totals": totals,
        "errors_by_component": [{"day": day, "level": level, "component": component, "count": n} for (day, level, component), n in components.most_common() if level in {"ERROR", "WARNING"}],
        "failures": {key: {"count": len(value), "first": value[0], "last": value[-1]} for key, value in failures.items()},
        "events": summary,
        "compactions": compactions,
        "technical": technical[-30:],
        "schemas": schemas,
        "info_components": [{"day": day, "component": component, "count": n} for (day, level, component), n in components.most_common() if level == "INFO"][:30],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
