#!/usr/bin/env python3
"""Stream a WAV through the configured process-isolated SVC service."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import struct
import sys
import time
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.voice_live.audio import resample_pcm16_mono
from plugins.voice_live.secrets import resolve_secret
from plugins.voice_live.voice_conversion import HttpVoiceConverter


def _read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("input WAV must be mono PCM16")
        return source.readframes(source.getnframes()), source.getframerate()


def _write_wav(path: Path, pcm16: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(pcm16)


def _rms(pcm16: bytes) -> int:
    if not pcm16:
        return 0
    samples = [sample[0] for sample in struct.iter_unpack("<h", pcm16)]
    return round(math.sqrt(sum(value * value for value in samples) / len(samples)))


async def _run(args: argparse.Namespace) -> dict[str, object]:
    token = resolve_secret(
        args.token_env,
        args.token_file,
        label="Voice conversion",
    )
    if not token:
        raise RuntimeError("Voice conversion credential is not configured")
    source_pcm, source_rate = _read_wav(args.input)
    converter = HttpVoiceConverter(
        args.url,
        token,
        args.profile_id,
        connect_timeout=args.connect_timeout,
        request_timeout=args.request_timeout,
        activation_timeout=args.activation_timeout,
    )
    started = time.perf_counter()
    first_output_at = 0.0
    output_parts: list[bytes] = []
    total_inference_ms = 0.0
    block_count = 0

    async def consume(frame: bytes) -> None:
        """Submit one frame and accumulate converted output and metrics."""

        nonlocal block_count, first_output_at, total_inference_ms
        result = await converter.process(frame, source_rate)
        if result.data:
            if not first_output_at:
                first_output_at = time.perf_counter()
            output_parts.append(
                resample_pcm16_mono(
                    result.data, result.sample_rate, args.output_sample_rate
                )
            )
        total_inference_ms += float(result.metrics.get("inference_ms", 0.0))
        block_count += int(result.metrics.get("block_count", 0))

    connection = await converter.connect()
    try:
        frame_samples = max(1, round(source_rate * args.frame_ms / 1000))
        frame_bytes = frame_samples * 2
        frames = [
            source_pcm[offset : offset + frame_bytes]
            for offset in range(0, len(source_pcm), frame_bytes)
        ]
        if args.pace_realtime:
            queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=64)

            async def produce() -> None:
                """Capture frames on wall-clock time independently of inference."""

                for frame in frames:
                    await asyncio.sleep(len(frame) / 2 / source_rate)
                    await queue.put(frame)
                await queue.put(None)

            producer = asyncio.create_task(produce(), name="paced-pcm-producer")
            try:
                while True:
                    frame = await queue.get()
                    try:
                        if frame is None:
                            break
                        await consume(frame)
                    finally:
                        queue.task_done()
                await producer
            finally:
                if not producer.done():
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
        else:
            for frame in frames:
                await consume(frame)
        flushed = await converter.flush()
        if flushed.data:
            if not first_output_at:
                first_output_at = time.perf_counter()
            output_parts.append(
                resample_pcm16_mono(
                    flushed.data, flushed.sample_rate, args.output_sample_rate
                )
            )
        total_inference_ms += float(flushed.metrics.get("inference_ms", 0.0))
        block_count += int(flushed.metrics.get("block_count", 0))
    finally:
        await converter.close()

    output_pcm = b"".join(output_parts)
    if len(output_pcm) < args.output_sample_rate:
        raise RuntimeError("voice converter returned less than 500 ms of audio")
    _write_wav(args.output, output_pcm, args.output_sample_rate)
    elapsed = time.perf_counter() - started
    source_seconds = len(source_pcm) / 2 / source_rate
    return {
        "input": str(args.input),
        "output": str(args.output),
        "source_seconds": round(source_seconds, 3),
        "output_seconds": round(len(output_pcm) / 2 / args.output_sample_rate, 3),
        "output_rms": _rms(output_pcm),
        "output_pcm_sha256": hashlib.sha256(output_pcm).hexdigest(),
        "first_output_ms": (
            round((first_output_at - started) * 1000.0, 3) if first_output_at else None
        ),
        "wall_time_seconds": round(elapsed, 3),
        "model_inference_ms": round(total_inference_ms, 3),
        "model_blocks": block_count,
        "wall_realtime_factor": round(elapsed / source_seconds, 3),
        "model_realtime_factor": round(total_inference_ms / 1000.0 / source_seconds, 3),
        "service": connection,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-env", default="SEEDVC_STREAM_TOKEN")
    parser.add_argument("--token-file", default="")
    parser.add_argument("--profile-id", default="elysia")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frame-ms", type=int, default=100)
    parser.add_argument(
        "--pace-realtime",
        action="store_true",
        help="pace input frames like live microphone capture",
    )
    parser.add_argument("--output-sample-rate", type=int, default=24000)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--activation-timeout", type=float, default=120.0)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
