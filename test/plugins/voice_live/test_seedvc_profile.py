from __future__ import annotations

from pathlib import Path

import pytest

from plugins.voice_live.seedvc_profile import (
    InferenceTelemetry,
    build_profile_manifest,
    validate_runtime_settings,
)


def _settings() -> dict[str, float | int]:
    return {
        "input_sample_rate": 24000,
        "block_time": 0.24,
        "crossfade_time": 0.04,
        "extra_time_ce": 2.5,
        "extra_time": 0.5,
        "extra_time_right": 0.02,
        "diffusion_steps": 8,
        "inference_cfg_rate": 0.0,
        "max_prompt_length": 3.0,
        "silence_db": -70.0,
        "output_gain_db": -3.0,
        "seed": 42,
    }


def test_seedvc_profile_revision_tracks_assets_and_settings(tmp_path: Path) -> None:
    checkpoint = tmp_path / "model.pth"
    config = tmp_path / "model.yml"
    reference = tmp_path / "reference.wav"
    checkpoint.write_bytes(b"model-a")
    config.write_bytes(b"config-a")
    reference.write_bytes(b"reference-a")

    first = build_profile_manifest(
        profile_id="elysia",
        checkpoint_path=checkpoint,
        config_path=config,
        reference_path=reference,
        settings=_settings(),
    )
    repeated = build_profile_manifest(
        profile_id="elysia",
        checkpoint_path=checkpoint,
        config_path=config,
        reference_path=reference,
        settings=_settings(),
    )
    reference.write_bytes(b"reference-b")
    changed = build_profile_manifest(
        profile_id="elysia",
        checkpoint_path=checkpoint,
        config_path=config,
        reference_path=reference,
        settings=_settings(),
    )

    assert first == repeated
    assert first["revision"] != changed["revision"]
    assert first["assets"]["reference_sha256"] != changed["assets"][
        "reference_sha256"
    ]
    assert "path" not in str(first).lower()


def test_seedvc_settings_reject_non_realtime_geometry() -> None:
    settings = _settings()
    settings["crossfade_time"] = settings["block_time"]
    with pytest.raises(ValueError, match="shorter than block_time"):
        validate_runtime_settings(settings)


def test_seedvc_telemetry_reports_realtime_margin_and_overload() -> None:
    telemetry = InferenceTelemetry(window_size=8, ewma_alpha=0.5)
    assert telemetry.snapshot(block_time_ms=240.0)["status"] == "warming"

    for value in (100.0, 120.0, 140.0, 260.0):
        telemetry.record(value)
    snapshot = telemetry.snapshot(block_time_ms=240.0)

    assert snapshot["status"] == "overloaded"
    assert snapshot["sample_count"] == 4
    assert snapshot["total_block_count"] == 4
    assert snapshot["average_ms"] == 155.0
    assert snapshot["p95_ms"] == 260.0
    assert snapshot["realtime_margin_ms"] == -20.0

    for value in (80.0, 90.0, 100.0, 110.0, 120.0, 130.0, 140.0, 150.0):
        telemetry.record(value)
    recovered = telemetry.snapshot(block_time_ms=240.0)
    assert recovered["status"] == "healthy"
    assert recovered["sample_count"] == 8
    assert recovered["total_block_count"] == 12
    assert recovered["average_ms"] == 115.0
    assert recovered["max_ms"] == 150.0
