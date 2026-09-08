"""MiMo chat models must not remain on OpenCode New API channels."""

from __future__ import annotations

import sqlite3

from scripts.pin_mimo_to_xiaomi_channel import inspect, pin_opencode_only


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY,
            name TEXT,
            status INTEGER,
            priority INTEGER,
            weight INTEGER,
            base_url TEXT,
            models TEXT
        );
        CREATE TABLE abilities (
            "group" TEXT,
            model TEXT,
            channel_id INTEGER,
            enabled INTEGER,
            priority INTEGER,
            weight INTEGER
        );
        """
    )


def test_pin_opencode_only_leaves_xiaomi_mimo_abilities() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _schema(conn)
    conn.execute(
        "INSERT INTO channels VALUES (34, 'MiMoCN-2', 1, 100, 10, "
        "'https://token-plan-sgp.xiaomimimo.com', "
        "'mimo-v2.5,mimo-v2.5-pro')"
    )
    conn.execute(
        "INSERT INTO channels VALUES (48, 'OpenCode-Go-New-1', 1, -1, 10, "
        "'https://opencode.ai/zen/go', "
        "'qwen3.8-flash,mimo-v2.5-pro,mimo-v2.5,hy3')"
    )
    for channel_id, priority in ((34, 100), (48, -1)):
        for model in ("mimo-v2.5", "mimo-v2.5-pro"):
            conn.execute(
                "INSERT INTO abilities VALUES ('default', ?, ?, 1, ?, 10)",
                (model, channel_id, priority),
            )

    changed = pin_opencode_only(conn)
    after = inspect(conn)
    models_48 = conn.execute(
        "SELECT models FROM channels WHERE id = 48"
    ).fetchone()[0]

    assert changed == [48]
    assert models_48 == "qwen3.8-flash,hy3"
    assert after["opencode_with_mimo"] == []
    assert after["opencode_mimo_abilities"] == []
    assert {
        (row["channel_id"], row["model"])
        for row in after["xiaomi_mimo_abilities"]
    } == {(34, "mimo-v2.5"), (34, "mimo-v2.5-pro")}
