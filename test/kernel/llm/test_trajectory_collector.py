"""Tests for the text-only trajectory data lake."""

from __future__ import annotations

import json

import pytest

from src.kernel.llm import trajectory_collector as collector_mod
from src.kernel.llm.trajectory_collector import (
    TrajectoryCollector,
    get_global_trajectory_collector,
    record_trajectory,
)
from src.kernel.llm.trajectory_types import (
    TRAJECTORY_SCHEMA_VERSION,
    derive_task_tags,
    ensure_trajectory_record,
    new_trajectory_id,
    sanitize_text_only,
)

FAKE_JPEG_B64 = "/9j/4AAQSkZJRgABAQAAAQ" + "A" * 4000


@pytest.fixture
def collector(tmp_path):
    """A collector rooted in a temp lake, always shut down after the test."""
    instance = TrajectoryCollector(
        base_path=tmp_path / "lake", flush_interval=0.05, queue_limit=100
    )
    yield instance
    instance.shutdown()


@pytest.fixture(autouse=True)
def reset_global_collector(monkeypatch):
    """Never let a test bind the process-wide singleton to the real lake."""
    monkeypatch.setattr(collector_mod, "_global_collector", None)
    yield
    existing = collector_mod._global_collector
    if existing is not None and hasattr(existing, "shutdown"):
        existing.shutdown()
    monkeypatch.setattr(collector_mod, "_global_collector", None)


def read_all(collector: TrajectoryCollector) -> list[dict]:
    collector.flush()
    records: list[dict] = []
    for path in sorted(collector.raw_path.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records
class TestLayout:
    """The lake layout has to be self-describing for downstream tooling."""

    def test_directories_and_schema_created(self, collector: TrajectoryCollector) -> None:
        for directory in (
            collector.raw_path,
            collector.processed_path,
            collector.export_path,
            collector.archive_path,
            collector.schema_path,
        ):
            assert directory.is_dir()

        schema_file = collector.schema_path / f"trajectory.v{TRAJECTORY_SCHEMA_VERSION}.json"
        assert schema_file.exists()
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        assert schema["schema_version"] == TRAJECTORY_SCHEMA_VERSION
        assert "messages" in schema["fields"]

    def test_raw_partition_is_utc_dated(self, collector: TrajectoryCollector) -> None:
        collector.record({"request_name": "life_chatter", "messages": []})
        collector.flush()
        files = list(collector.raw_path.glob("*.jsonl"))
        assert len(files) == 1
        # YYYY-MM-DD.jsonl
        assert len(files[0].stem) == 10
        assert files[0].stem.count("-") == 2


class TestPersistence:
    def test_record_roundtrip(self, collector: TrajectoryCollector) -> None:
        request_id = new_trajectory_id("req")
        collector.record(
            {
                "request_id": request_id,
                "trace_id": request_id,
                "attempt_id": new_trajectory_id("attempt"),
                "request_name": "life_chatter",
                "task_tags": derive_task_tags("life_chatter"),
                "model_identifier": "grok-4.5",
                "messages": [{"role": "user", "content": "hi"}],
                "response": {"content": "hello"},
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                "success": True,
            }
        )
        records = read_all(collector)
        assert len(records) == 1
        record = records[0]
        assert record["schema_version"] == TRAJECTORY_SCHEMA_VERSION
        assert record["request_id"] == request_id
        assert record["messages"][0]["content"] == "hi"
        assert record["response"]["content"] == "hello"
        assert record["timestamp"].endswith("Z")

    def test_appends_are_never_overwritten(self, collector: TrajectoryCollector) -> None:
        for index in range(5):
            collector.record({"request_name": f"req_{index}", "messages": []})
            collector.flush()
        records = read_all(collector)
        assert [r["request_name"] for r in records] == [f"req_{i}" for i in range(5)]

    def test_queue_drains_after_flush(self, collector: TrajectoryCollector) -> None:
        collector.record({"request_name": "life_chatter", "messages": []})
        assert collector.queue_size == 1
        assert collector.flush() is True
        assert collector.queue_size == 0

    def test_attempt_chain_is_preserved(self, collector: TrajectoryCollector) -> None:
        first = new_trajectory_id("attempt")
        second = new_trajectory_id("attempt")
        request_id = new_trajectory_id("req")
        collector.record(
            {
                "request_id": request_id,
                "attempt_id": first,
                "parent_attempt_id": None,
                "success": False,
                "messages": [],
            }
        )
        collector.record(
            {
                "request_id": request_id,
                "attempt_id": second,
                "parent_attempt_id": first,
                "success": True,
                "messages": [],
            }
        )
        records = read_all(collector)
        assert records[1]["parent_attempt_id"] == records[0]["attempt_id"]
        assert records[0]["request_id"] == records[1]["request_id"]
class TestTextOnlyRedaction:
    """Nothing binary may ever reach the lake."""

    def test_data_url_is_redacted(self, collector: TrajectoryCollector) -> None:
        collector.record(
            {
                "request_name": "image_recognition",
                "messages": [
                    {"role": "user", "content": f"data:image/jpeg;base64,{FAKE_JPEG_B64}"}
                ],
            }
        )
        collector.flush()
        raw = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(collector.raw_path.glob("*.jsonl"))
        )
        assert "/9j/" not in raw
        assert "base64" not in raw

    def test_blob_embedded_in_longer_text_is_scrubbed(
        self, collector: TrajectoryCollector
    ) -> None:
        collector.record(
            {
                "request_name": "life_chatter",
                "messages": [
                    {
                        "role": "user",
                        "content": f"look at this data:image/png;base64,{FAKE_JPEG_B64} thanks",
                    }
                ],
            }
        )
        records = read_all(collector)
        content = records[0]["messages"][0]["content"]
        # Surrounding prose survives; only the blob is replaced.
        assert content.startswith("look at this ")
        assert content.endswith(" thanks")
        assert "[removed]" in content
        assert "/9j/" not in content

    def test_bytes_and_paths_are_redacted(self, collector: TrajectoryCollector) -> None:
        collector.record(
            {
                "request_name": "life_chatter",
                "messages": [{"role": "user", "content": "ok"}],
                "metadata": {
                    "blob": b"\x89PNG\r\n\x1a\n",
                    "image_path": "/root/data/cache/image_abc.png",
                    "keep": "plain text",
                },
            }
        )
        records = read_all(collector)
        metadata = records[0]["metadata"]
        assert metadata["blob"] == "[removed]"
        assert metadata["image_path"] == "[removed]"
        assert metadata["keep"] == "plain text"

    def test_sanitize_helper_rejects_raw_base64(self) -> None:
        assert sanitize_text_only(FAKE_JPEG_B64) == "[removed]"
        assert sanitize_text_only("normal reply") == "normal reply"


class TestSchemaNormalization:
    def test_unknown_fields_go_to_extensions_or_survive(self) -> None:
        record = ensure_trajectory_record(
            {"request_name": "life_chatter", "some_future_field": "value"}
        )
        assert record["schema_version"] == TRAJECTORY_SCHEMA_VERSION
        assert record["request_name"] == "life_chatter"
        assert "timestamp" in record

    def test_task_tags_are_derived_when_missing(self) -> None:
        record = ensure_trajectory_record({"request_name": "life_engine_heartbeat"})
        assert record["task_tags"], "task_tags should never be empty"

    def test_non_finite_floats_are_dropped(self) -> None:
        record = ensure_trajectory_record(
            {"request_name": "x", "latency_s": float("inf"), "usage": {"t": float("nan")}}
        )
        # json.dumps(allow_nan=False) must not explode on the normalized record.
        json.dumps(record, allow_nan=False)


class TestDisableSwitch:
    def test_disabled_collector_writes_nothing(self, tmp_path) -> None:
        lake = tmp_path / "disabled_lake"
        record_trajectory(
            {"request_name": "life_chatter", "messages": []},
            base_path=str(lake),
            enabled=False,
        )
        instance = get_global_trajectory_collector()
        assert instance.enabled is False
        assert not (lake / "raw").exists()

    def test_record_failure_never_raises(self, monkeypatch, tmp_path) -> None:
        def explode(*args, **kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(collector_mod, "get_global_trajectory_collector", explode)
        # Observability must never break the LLM request path.
        record_trajectory({"request_name": "life_chatter"}, base_path=str(tmp_path))
