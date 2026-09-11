"""Read recent LLM timing and runtime snapshot metadata without returning text."""

from __future__ import annotations

import argparse
import collections
import json
import sqlite3
from pathlib import Path


def connect(path):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    return connection


def stats(values):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return {}
    return {"n": len(values), "min": values[0], "median": values[len(values)//2], "p95": values[min(len(values)-1,int(len(values)*.95))], "max": values[-1], "sum": sum(values)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    output = {}
    with connect(args.root / "data/Elysium.db") as connection:
        rows = [dict(row) for row in connection.execute("SELECT model_name,model_assign_name,request_type,time_cost,status,timestamp,prompt_tokens,completion_tokens FROM llm_usage WHERE timestamp >= '2026-09-09' ORDER BY id")]
        groups = collections.defaultdict(list)
        for row in rows:
            groups[(row["model_assign_name"], row["model_name"], row["status"])].append(row)
        output["llm_usage"] = [{"task": key[0], "model": key[1], "status": key[2], "count": len(items), "first": items[0]["timestamp"], "last": items[-1]["timestamp"], "time_cost": stats([row["time_cost"] for row in items]), "input_tokens": stats([row["prompt_tokens"] for row in items]), "output_tokens": stats([row["completion_tokens"] for row in items])} for key, items in groups.items()]
        output["recent_llm"] = rows[-8:]
    with connect(args.root / "data/life_storage/local.sqlite3") as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        output["runtime_tables"] = {table: [row[1] for row in connection.execute('PRAGMA table_info("'+table+'")')] for table in tables if any(word in table for word in ("runtime", "authority", "outbox", "trajectory"))}
        if "runtime_states" in tables:
            output["runtime_namespaces"] = [dict(row) for row in connection.execute("SELECT namespace,count(*) AS count,max(length(CAST(payload_json AS BLOB))) AS max_bytes,max(updated_at) AS last_update FROM runtime_states GROUP BY namespace")]
            output["runtime_states"] = [dict(row) for row in connection.execute("SELECT namespace,state_key,revision,schema_version,updated_at,length(CAST(payload_json AS BLOB)) AS payload_bytes FROM runtime_states WHERE namespace IN ('life_engine.runtime_context','life_heartbeat.rolling_context','life_chatter.rolling_context')")]
            details = {}
            for row in connection.execute("SELECT namespace,state_key,payload_json FROM runtime_states WHERE namespace IN ('life_engine.runtime_context','life_heartbeat.rolling_context','life_chatter.rolling_context')"):
                data = json.loads(row["payload_json"])
                details[f'{row["namespace"]}:{row["state_key"]}'] = {key: {"type": type(value).__name__, "length": len(value) if isinstance(value, (str,list,dict)) else None, "json_bytes": len(json.dumps(value,ensure_ascii=False,separators=(',',':')).encode())} for key,value in data.items()}
            output["state_fields"] = details
        if "runtime_singleton_writer_claims" in tables:
            output["writer_claims"] = [dict(row) for row in connection.execute("SELECT namespace,state_key,owner_instance_id,lease_epoch,acquired_at,renewed_at,lease_until,released_at FROM runtime_singleton_writer_claims")]
    print(json.dumps(output,ensure_ascii=False))


if __name__ == "__main__":
    main()
