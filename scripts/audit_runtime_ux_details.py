"""Read-only audit of timing, ownership and event metadata, without message bodies."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
from pathlib import Path

from audit_runtime_ux_storage import connect, stats


LINE = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+)\s*\|\s*(\w+)\s*\|\s*([^|]+)\|\s*(.*)$")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    result = {}
    durations = collections.defaultdict(list)
    stops = collections.Counter()
    ttserrors = []
    recent = collections.defaultdict(list)
    claim_errors = []
    lease_window = []
    for day in ("2026-09-09", "2026-09-10", "2026-09-11"):
        path = args.root / "logs" / f"elysium-{day}.log"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                match = LINE.match(line)
                if not match:
                    continue
                when, level, component, message = match.groups()
                if "SingletonWriterClaimLost" in message and "pid-820499" in message:
                    claim_errors.append({"time": when, "line": number})
                if "2026-09-10T16:49:30" <= when <= "2026-09-10T16:51:00" and not component.strip().endswith(".audit"):
                    if any(word in message.lower() for word in ("renewal", "authority", "cancel", "storage", "lease", "shut", "停止", "关闭")) and len(message) < 900:
                        lease_window.append({"time": when, "line": number, "component": component.strip(), "message": message})
                if "tts" in component.lower() and level in ("WARNING", "ERROR"):
                    ttserrors.append({"time": when, "component": component.strip(), "line": number, "type": "reference_missing" if "ref_audio" in message or "参考音频" in message else "other"})
                if not component.strip().endswith(".audit") or not message.startswith("{"):
                    continue
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    continue
                event = data.get("event")
                if event == "heartbeat_model_response" and data.get("heartbeat_at"):
                    end = dt.datetime.fromisoformat(when)
                    start = dt.datetime.fromisoformat(data["heartbeat_at"])
                    if start.tzinfo:
                        end = end.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
                    seconds = (end-start).total_seconds()
                    stage = "post_manual_restart" if when >= "2026-09-11T00:34:51" else "after_22_13" if when >= "2026-09-10T22:13:41" else "before_22_13"
                    durations[stage].append(seconds)
                if event == "heartbeat_tool_loop_stopped":
                    stops[(data.get("stop_reason"), data.get("stall_kind"), data.get("stop_stage"))] += 1
                if when >= "2026-09-11T00:34:51":
                    recent[event].append({"time": when, "line": number, **{key: value for key, value in data.items() if isinstance(value, (int, float, bool))}})
    result["heartbeat_duration_seconds"] = {stage: stats(values) for stage, values in durations.items()}
    result["heartbeat_stops"] = [{"reason": key[0], "kind": key[1], "stage": key[2], "count": count} for key, count in stops.most_common()]
    result["tts_errors"] = ttserrors
    result["pid_820499_claim_errors"] = {"count": len(claim_errors), "first": claim_errors[:2], "last": claim_errors[-2:]}
    result["lease_window"] = lease_window
    result["post_manual_restart"] = {event: {"count": len(rows), "first": rows[0], "last": rows[-1]} for event, rows in recent.items()}
    with connect(args.root / "data/life_storage/local.sqlite3") as connection:
        result["writer_transitions"] = [dict(row) for row in connection.execute("SELECT position,namespace,owner_instance_id,lease_epoch,event_kind,occurred_at FROM runtime_singleton_writer_events WHERE namespace='life_engine.runtime_context' AND occurred_at >= '2026-09-09' AND event_kind != 'renewed' ORDER BY position")]
        result["outbox"] = [dict(row) for row in connection.execute("SELECT status,count(*) AS count,min(created_at) AS first,max(updated_at) AS last,max(attempts) AS max_attempts,last_error_type FROM outbox_actions WHERE created_at >= '2026-09-09' GROUP BY status,last_error_type")]
        result["raw_activity_types"] = [dict(row) for row in connection.execute("SELECT json_extract(payload_json,'$.event_type') AS type,count(*) AS count,min(occurred_at) AS first,max(occurred_at) AS last FROM raw_life_events WHERE occurred_at >= '2026-09-09' GROUP BY type")]
        result["raw_shapes"] = []
        for row in connection.execute("SELECT ingest_position,payload_json FROM raw_life_events ORDER BY ingest_position DESC LIMIT 12"):
            data = json.loads(row["payload_json"])
            result["raw_shapes"].append({"position": row["ingest_position"], "keys": sorted(data), "nested_keys": {key: sorted(value) for key, value in data.items() if isinstance(value, dict)}})
        result["consumer_offsets"] = [dict(row) for row in connection.execute("SELECT consumer_id,ingest_position,revision,updated_at FROM raw_event_consumer_offsets")]
        owner_events = [dict(row) for row in connection.execute("SELECT event_kind,occurred_at FROM runtime_singleton_writer_events WHERE namespace='life_engine.runtime_context' AND owner_instance_id LIKE '%pid-820499:%' ORDER BY position")]
        result["pid_820499_writer_events"] = {"count": len(owner_events), "first": owner_events[:3], "last": owner_events[-5:]}
        result["tool_activity"] = [dict(row) for row in connection.execute("SELECT json_extract(payload_json,'$.event_type') AS type,json_extract(payload_json,'$.metadata.tool_name') AS tool,count(*) AS count,min(occurred_at) AS first,max(occurred_at) AS last FROM raw_life_events WHERE occurred_at >= '2026-09-09' AND json_extract(payload_json,'$.metadata.tool_name') IS NOT NULL GROUP BY type,tool")]
        result["tool_outcomes"] = [dict(row) for row in connection.execute("SELECT json_extract(payload_json,'$.metadata.tool_name') AS tool,json_extract(payload_json,'$.metadata.tool_success') AS success,count(*) AS count,max(occurred_at) AS last FROM raw_life_events WHERE occurred_at >= '2026-09-09' AND json_extract(payload_json,'$.event_type') IN ('conscious_activity_tool_result','tool_result') GROUP BY tool,success")]
        result["failed_tool_metadata"] = []
        for row in connection.execute("SELECT occurred_at,payload_json FROM raw_life_events WHERE occurred_at >= '2026-09-09' AND json_extract(payload_json,'$.event_type') IN ('conscious_activity_tool_result','tool_result') AND json_extract(payload_json,'$.metadata.tool_success')=0"):
            data = json.loads(row["payload_json"])
            content = data.get("content", "")
            try:
                parsed = json.loads(content) if isinstance(content, str) else content
            except (json.JSONDecodeError, TypeError):
                parsed = None
            technical_codes = {}
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    if key in ("error_code", "error_type", "status", "code", "technical_outcome") and isinstance(value, (str, int, bool)):
                        technical_codes[key] = str(value)[:100]
                nested = parsed.get("result")
                if isinstance(nested, str):
                    try:
                        nested = json.loads(nested)
                    except json.JSONDecodeError:
                        nested = None
                if isinstance(nested, dict):
                    technical_codes["result_keys"] = sorted(nested)
                    for key in ("error_code", "error_type", "status", "code"):
                        if isinstance(nested.get(key), (str, int, bool)):
                            technical_codes["result."+key] = str(nested[key])[:100]
            known_reasons = [phrase for phrase in ("media_ref", "attachment", "unknown tool", "Unknown tool", "not found", "not allowed", "not available", "context_stewardship_required", "source_manifest_mismatch", "invalid", "未知工具", "未找到", "不存在", "不能为空", "未提供", "参数", "无权限", "缺少", "超时", "TTS", "ref_audio", "maintenance", "失败") if phrase in str(content)]
            result["failed_tool_metadata"].append({"time": row["occurred_at"], "tool": data.get("metadata", {}).get("tool_name"), "content_type": type(content).__name__, "json_keys": sorted(parsed) if isinstance(parsed, dict) else None, "codes": technical_codes, "matched_technical_markers": known_reasons})
        result["delivery_shapes"] = []
        for row in connection.execute("SELECT payload_json FROM raw_life_events WHERE json_extract(payload_json,'$.event_type') IN ('chat.message.delivery_confirmed','conscious_activity_tool_result','tool_result') AND occurred_at >= '2026-09-09' ORDER BY ingest_position DESC LIMIT 8"):
            data = json.loads(row["payload_json"])
            metadata = data.get("metadata", {})
            result["delivery_shapes"].append({"type": data.get("event_type"), "metadata_keys": sorted(metadata), "tool": metadata.get("tool_name"), "status": metadata.get("status"), "success": metadata.get("success")})
    relay = args.root / "data/new_api_relay/one-api.db"
    if relay.exists():
        with connect(relay) as connection:
            result["relay_log_columns"] = [row[1] for row in connection.execute("PRAGMA table_info(logs)")]
            relay_rows = [dict(row) for row in connection.execute("SELECT model_name,type,use_time,prompt_tokens,completion_tokens,created_at FROM logs WHERE created_at >= 1788979200 ORDER BY id")]
            relay_groups = collections.defaultdict(list)
            for row in relay_rows:
                relay_groups[(row["model_name"], row["type"])].append(row)
            result["relay_timings"] = [{"model": key[0], "log_type": key[1], "count": len(rows), "first": rows[0]["created_at"], "last": rows[-1]["created_at"], "use_time": stats([row["use_time"] for row in rows]), "prompt_tokens": stats([row["prompt_tokens"] for row in rows]), "completion_tokens": stats([row["completion_tokens"] for row in rows])} for key, rows in relay_groups.items()]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
