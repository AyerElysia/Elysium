#!/usr/bin/env python3
"""Pin MiMo chat models to Xiaomi New API channels; strip them from OpenCode.

Idempotent. Does not print keys or request bodies. Does not restart processes.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIMO_CHAT_MODELS = ("mimo-v2.5", "mimo-v2.5-pro")
OPENCODE_URL_MARKERS = ("opencode.ai", "zen/go", "console go")


def _split_models(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def _join_models(names: list[str]) -> str:
    return ",".join(names)


def _is_opencode_url(base_url: str) -> bool:
    lowered = str(base_url or "").strip().lower()
    return any(marker in lowered for marker in OPENCODE_URL_MARKERS)


def _channel_has_mimo(models_raw: str) -> bool:
    names = set(_split_models(models_raw))
    return any(model in names for model in MIMO_CHAT_MODELS)


def inspect(conn: sqlite3.Connection) -> dict[str, object]:
    channels = conn.execute(
        "SELECT id, name, status, priority, weight, base_url, models FROM channels"
    ).fetchall()
    abilities = conn.execute(
        "SELECT channel_id, model, enabled, priority, weight FROM abilities "
        "WHERE model IN (?, ?)",
        MIMO_CHAT_MODELS,
    ).fetchall()
    return {
        "opencode_with_mimo": [
            {
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "priority": row["priority"],
                "mimo_models": [
                    name
                    for name in _split_models(row["models"])
                    if name in MIMO_CHAT_MODELS
                ],
            }
            for row in channels
            if _is_opencode_url(row["base_url"]) and _channel_has_mimo(row["models"])
        ],
        "xiaomi_mimo_abilities": [
            {
                "channel_id": row["channel_id"],
                "model": row["model"],
                "enabled": row["enabled"],
                "priority": row["priority"],
            }
            for row in abilities
            if row["channel_id"]
            not in {
                item["id"]
                for item in channels
                if _is_opencode_url(item["base_url"])
            }
        ],
        "opencode_mimo_abilities": [
            {
                "channel_id": row["channel_id"],
                "model": row["model"],
                "enabled": row["enabled"],
                "priority": row["priority"],
            }
            for row in abilities
            if any(
                item["id"] == row["channel_id"] and _is_opencode_url(item["base_url"])
                for item in channels
            )
        ],
    }


def pin_opencode_only(conn: sqlite3.Connection) -> list[int]:
    changed_ids: list[int] = []
    opencode_ids: list[int] = []
    channels = conn.execute(
        "SELECT id, base_url, models FROM channels"
    ).fetchall()
    for row in channels:
        if not _is_opencode_url(row["base_url"]):
            continue
        opencode_ids.append(int(row["id"]))
        names = _split_models(row["models"])
        kept = [name for name in names if name not in MIMO_CHAT_MODELS]
        if kept == names:
            continue
        conn.execute(
            "UPDATE channels SET models = ? WHERE id = ?",
            (_join_models(kept), row["id"]),
        )
        changed_ids.append(int(row["id"]))
    if opencode_ids:
        placeholders = ",".join("?" for _ in opencode_ids)
        conn.execute(
            f"DELETE FROM abilities WHERE model IN (?, ?) AND channel_id IN ({placeholders})",
            (*MIMO_CHAT_MODELS, *opencode_ids),
        )
    return changed_ids


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Strip MiMo chat models from OpenCode New API channels."
    )
    parser.add_argument(
        "--db",
        default="/root/Elysia/new-api/one-api.db",
        help="Path to New API SQLite database",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Default is inspect-only.",
    )
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.is_file():
        raise SystemExit(f"database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        before = inspect(conn)
        print("before_opencode_with_mimo", before["opencode_with_mimo"])
        print("before_opencode_mimo_abilities", before["opencode_mimo_abilities"])
        print("before_xiaomi_mimo_abilities", before["xiaomi_mimo_abilities"])
        if not args.apply:
            return 0
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = db_path.with_name(f"{db_path.name}.bak-mimo-xiaomi-{stamp}")
        shutil.copy2(db_path, backup)
        print("backup", str(backup))
        changed = pin_opencode_only(conn)
        conn.commit()
        after = inspect(conn)
        print("changed_channel_ids", changed)
        print("after_opencode_with_mimo", after["opencode_with_mimo"])
        print("after_opencode_mimo_abilities", after["opencode_mimo_abilities"])
        print("after_xiaomi_mimo_abilities", after["xiaomi_mimo_abilities"])
        if after["opencode_with_mimo"] or after["opencode_mimo_abilities"]:
            raise SystemExit("OpenCode still advertises MiMo after apply")
        if not after["xiaomi_mimo_abilities"]:
            raise SystemExit("Xiaomi MiMo abilities missing after apply")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
