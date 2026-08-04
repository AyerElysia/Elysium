"""Content-free Seed-VC profile identity and realtime health helpers."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROFILE_SCHEMA_VERSION = 1


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return a stable asset digest without exposing its path or contents."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def validate_runtime_settings(settings: dict[str, float | int]) -> None:
    """Reject unsafe or internally inconsistent realtime settings."""

    for name, value in settings.items():
        if not math.isfinite(float(value)):
            raise ValueError(f"{name} must be finite")
    input_sample_rate = int(settings["input_sample_rate"])
    block_time = float(settings["block_time"])
    crossfade_time = float(settings["crossfade_time"])
    extra_time_ce = float(settings["extra_time_ce"])
    extra_time = float(settings["extra_time"])
    extra_time_right = float(settings["extra_time_right"])
    diffusion_steps = int(settings["diffusion_steps"])
    inference_cfg_rate = float(settings["inference_cfg_rate"])
    max_prompt_length = float(settings["max_prompt_length"])
    output_gain_db = float(settings["output_gain_db"])
    silence_db = float(settings["silence_db"])
    seed = int(settings["seed"])

    if not 8000 <= input_sample_rate <= 192000:
        raise ValueError("input_sample_rate must be between 8000 and 192000")
    if not 0.12 <= block_time <= 2.0:
        raise ValueError("block_time must be between 0.12 and 2.0 seconds")
    if not 0.01 <= crossfade_time < block_time:
        raise ValueError("crossfade_time must be positive and shorter than block_time")
    if not 0.0 <= extra_time_right <= 0.5:
        raise ValueError("extra_time_right must be between 0.0 and 0.5 seconds")
    if extra_time < 0.0 or extra_time_ce < extra_time:
        raise ValueError("extra_time_ce must not be shorter than extra_time")
    if extra_time_ce > 10.0:
        raise ValueError("extra_time_ce must not exceed 10 seconds")
    if not 1 <= diffusion_steps <= 100:
        raise ValueError("diffusion_steps must be between 1 and 100")
    if not 0.0 <= inference_cfg_rate <= 2.0:
        raise ValueError("inference_cfg_rate must be between 0.0 and 2.0")
    if not 0.5 <= max_prompt_length <= 20.0:
        raise ValueError("max_prompt_length must be between 0.5 and 20.0 seconds")
    if not -24.0 <= output_gain_db <= 0.0:
        raise ValueError("output_gain_db must be between -24.0 and 0.0 dB")
    if not -120.0 <= silence_db <= -20.0:
        raise ValueError("silence_db must be between -120.0 and -20.0 dB")
    if not 0 <= seed <= 2**63 - 1:
        raise ValueError("seed must be a non-negative signed 64-bit integer")


def build_profile_manifest(
    *,
    profile_id: str,
    checkpoint_path: Path,
    config_path: Path,
    reference_path: Path,
    settings: dict[str, float | int],
) -> dict[str, Any]:
    """Build one traceable profile manifest from immutable asset digests."""

    if not profile_id.strip():
        raise ValueError("profile_id must not be empty")
    validate_runtime_settings(settings)
    manifest: dict[str, Any] = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": profile_id,
        "assets": {
            "checkpoint_sha256": sha256_file(checkpoint_path),
            "config_sha256": sha256_file(config_path),
            "reference_sha256": sha256_file(reference_path),
        },
        "settings": dict(settings),
    }
    canonical = json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    manifest["revision"] = hashlib.sha256(canonical).hexdigest()
    return manifest


@dataclass
class InferenceTelemetry:
    """Bounded rolling latency telemetry for realtime capacity decisions."""

    window_size: int = 128
    ewma_alpha: float = 0.2
    _samples: deque[float] = field(init=False, repr=False)
    _count: int = field(default=0, init=False, repr=False)
    _ewma_ms: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.window_size < 4:
            raise ValueError("window_size must be at least 4")
        if not 0.0 < self.ewma_alpha <= 1.0:
            raise ValueError("ewma_alpha must be in (0, 1]")
        self._samples = deque(maxlen=self.window_size)

    def record(self, elapsed_ms: float) -> None:
        """Record one completed model block."""

        if not math.isfinite(elapsed_ms) or elapsed_ms < 0.0:
            raise ValueError("elapsed_ms must be finite and non-negative")
        with self._lock:
            self._samples.append(elapsed_ms)
            self._count += 1
            if self._count == 1:
                self._ewma_ms = elapsed_ms
            else:
                alpha = self.ewma_alpha
                self._ewma_ms = alpha * elapsed_ms + (1.0 - alpha) * self._ewma_ms

    def snapshot(self, *, block_time_ms: float) -> dict[str, float | int | str]:
        """Return a content-free bounded health snapshot."""

        if block_time_ms <= 0.0:
            raise ValueError("block_time_ms must be positive")
        with self._lock:
            samples = sorted(self._samples)
            total_block_count = self._count
            ewma_ms = self._ewma_ms
        if not samples:
            return {
                "status": "warming",
                "sample_count": 0,
                "total_block_count": 0,
                "average_ms": 0.0,
                "ewma_ms": 0.0,
                "p95_ms": 0.0,
                "max_ms": 0.0,
                "model_rtf": 0.0,
                "realtime_margin_ms": round(block_time_ms, 3),
            }
        p95_index = max(0, math.ceil(len(samples) * 0.95) - 1)
        p95_ms = samples[p95_index]
        if p95_ms >= block_time_ms:
            status = "overloaded"
        elif p95_ms >= block_time_ms * 0.85:
            status = "degraded"
        else:
            status = "healthy"
        return {
            "status": status,
            "sample_count": len(samples),
            "total_block_count": total_block_count,
            "average_ms": round(sum(samples) / len(samples), 3),
            "ewma_ms": round(ewma_ms, 3),
            "p95_ms": round(p95_ms, 3),
            "max_ms": round(samples[-1], 3),
            "model_rtf": round(ewma_ms / block_time_ms, 4),
            "realtime_margin_ms": round(block_time_ms - p95_ms, 3),
        }


__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "InferenceTelemetry",
    "build_profile_manifest",
    "sha256_file",
    "validate_runtime_settings",
]
