"""P3-08 additions to the livestream runtime and ledger."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from plugins.livestream.config import LivestreamConfig
from plugins.livestream.ledger import LivestreamLedger
from plugins.livestream.runtime import LivestreamRuntime


class _Stage:
    primary_client_id = "stage"
    client_count = 1


class _Adapter:
    health = SimpleNamespace(connected=True, last_event_at=None, last_error="")

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.sent: list[str] = []

    async def send_danmaku(self, text: str) -> bool:
        self.sent.append(text)
        return self.result


@pytest.mark.asyncio
async def test_send_danmaku_persists_request_and_receipt(tmp_path) -> None:
    config = LivestreamConfig(
        platform={"room_id": "42"},
        storage={
            "ledger_path": str(tmp_path / "ledger.sqlite3"),
            "audio_artifact_path": str(tmp_path / "audio"),
        },
    )
    runtime = LivestreamRuntime(config, _Stage())
    ledger = LivestreamLedger(config.storage.ledger_path)
    await ledger.start()
    runtime._ledger = ledger
    runtime._session_id = "live-1"
    runtime._running = True
    runtime._adapter = _Adapter()

    result = await runtime.send_danmaku(" hello ")
    records = await ledger.read_since(0, session_id="live-1")
    assert result["confirmed"] is True
    assert [record.kind for record in records] == [
        "platform.danmaku_send_requested",
        "platform.danmaku_sent",
    ]
    assert records[-1].causation_id == f"danmaku-requested:{result['request_id']}"
    await ledger.stop()


@pytest.mark.asyncio
async def test_read_before_is_descending_and_keyset_bounded(tmp_path) -> None:
    ledger = LivestreamLedger(tmp_path / "ledger.sqlite3")
    await ledger.start()
    for number in range(1, 4):
        await ledger.append(
            record_id=f"record-{number}",
            session_id=f"session-{number}",
            kind="session.started",
            source="test",
            payload={"number": number},
        )
    first = await ledger.read_before(None, kinds={"session.started"}, limit=2)
    second = await ledger.read_before(
        first[-1].sequence,
        kinds={"session.started"},
        limit=2,
    )
    assert [record.record_id for record in first] == ["record-3", "record-2"]
    assert [record.record_id for record in second] == ["record-1"]
    await ledger.stop()
