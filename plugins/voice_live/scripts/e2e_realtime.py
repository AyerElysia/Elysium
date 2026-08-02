#!/usr/bin/env python3
"""Run a repeatable speech-in/speech-out acceptance test against a provider."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import struct
import sys
import time
import wave
from pathlib import Path
from typing import Any

# Keep direct execution (``./e2e_realtime.py``) equivalent to ``python -m``.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.voice_live.protocol import ProviderState  # noqa: E402
from plugins.voice_live.providers.base import AudioDelta, TranscriptEvent  # noqa: E402
from plugins.voice_live.providers.minicpm_omni import MiniCPMOmniProvider  # noqa: E402
from plugins.voice_live.providers.qwen_realtime import QwenRealtimeProvider  # noqa: E402


def _read_pcm16_mono_16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as source:
        if (
            source.getnchannels(),
            source.getsampwidth(),
            source.getframerate(),
        ) != (1, 2, 16000):
            raise ValueError("input WAV must be 16 kHz, mono, PCM16")
        return source.readframes(source.getnframes())


def _write_pcm16_mono(path: Path, pcm: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(pcm)


def _pcm16_rms(pcm: bytes) -> int:
    if not pcm:
        return 0
    samples = struct.iter_unpack("<h", pcm)
    square_sum = sum(sample[0] * sample[0] for sample in samples)
    return round(math.sqrt(square_sum / (len(pcm) // 2)))


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.provider == "qwen_realtime":
        api_key = os.environ.get(args.api_key_env, "")
        if not api_key:
            raise RuntimeError(f"environment variable {args.api_key_env} is empty")
        provider = QwenRealtimeProvider(
            args.url,
            api_key,
            model=args.model,
            voice=args.voice,
            connect_timeout=args.connect_timeout,
            event_timeout=args.event_timeout,
        )
    else:
        if args.text:
            raise RuntimeError("MiniCPM-o acceptance requires --input audio")
        provider = MiniCPMOmniProvider(
            args.url,
            mode=args.mode,
            reference_audio_path=args.reference_audio,
            tts_reference_audio_path=args.tts_reference_audio,
            input_chunk_ms=args.upstream_chunk_ms,
            connect_timeout=args.connect_timeout,
            event_timeout=args.event_timeout,
        )
    output = bytearray()
    transcripts: list[dict[str, Any]] = []
    states: list[str] = []
    errors: list[str] = []
    response_finished = asyncio.Event()
    first_audio_at = 0.0
    input_complete_at = 0.0
    started_at = time.monotonic()
    saw_speaking = False

    async def on_audio(event: AudioDelta) -> None:
        nonlocal first_audio_at
        if not first_audio_at:
            first_audio_at = time.monotonic()
        output.extend(event.data)

    async def on_transcript(event: TranscriptEvent) -> None:
        transcripts.append(
            {
                "role": event.role,
                "text": event.text,
                "is_final": event.is_final,
                "event_id": event.event_id,
            }
        )

    async def on_state(state: ProviderState) -> None:
        nonlocal saw_speaking
        states.append(state.value)
        if state is ProviderState.SPEAKING:
            saw_speaking = True
        elif state is ProviderState.LISTENING and saw_speaking:
            response_finished.set()

    async def on_error(message: str) -> None:
        errors.append(message)
        response_finished.set()

    provider.on_audio_delta(on_audio)
    provider.on_transcript(on_transcript)
    provider.on_state_change(on_state)
    provider.on_error(on_error)
    try:
        await provider.connect(
            {
                "instructions": args.instructions,
                "tools": [],
            }
        )
        if args.text:
            await provider.send_text(args.text)
            input_complete_at = time.monotonic()
        else:
            source = _read_pcm16_mono_16k(args.input)
            if args.provider == "minicpm_omni" and args.mode == "turn_based":
                await provider.send_turn_audio(source)
                input_complete_at = time.monotonic()
                await asyncio.wait_for(response_finished.wait(), timeout=args.response_timeout)
                source = b""
            frame_bytes = 16000 * 2 * args.frame_ms // 1000
            silence = b"\x00" * frame_bytes
            if source:
                for _ in range(1000 // args.frame_ms):
                    await provider.send_audio(silence)
                    await asyncio.sleep(args.frame_ms / 1000)
                for offset in range(0, len(source), frame_bytes):
                    frame = source[offset : offset + frame_bytes]
                    if len(frame) < frame_bytes:
                        frame += b"\x00" * (frame_bytes - len(frame))
                    await provider.send_audio(frame)
                    await asyncio.sleep(args.frame_ms / 1000)
                for _ in range(args.trailing_silence_ms // args.frame_ms):
                    await provider.send_audio(silence)
                    await asyncio.sleep(args.frame_ms / 1000)
                input_complete_at = time.monotonic()
                await asyncio.wait_for(response_finished.wait(), timeout=args.response_timeout)
    finally:
        await provider.disconnect()

    pcm = bytes(output)
    if errors:
        raise RuntimeError("; ".join(errors))
    if not pcm:
        raise RuntimeError("provider returned no audio")
    _write_pcm16_mono(args.output, pcm, provider.output_sample_rate)
    duration = len(pcm) / (provider.output_sample_rate * 2)
    result = {
        "provider": provider.provider_name,
        "model": args.model,
        "input_mode": "text" if args.text else "audio",
        "states": states,
        "transcripts": transcripts,
        "output_path": str(args.output),
        "output_bytes": len(pcm),
        "output_seconds": round(duration, 3),
        "output_rms": _pcm16_rms(pcm),
        "output_sha256": hashlib.sha256(pcm).hexdigest(),
        "first_audio_latency_ms": (
            round((first_audio_at - started_at) * 1000, 1) if first_audio_at else None
        ),
        "first_audio_after_input_ms": (
            round((first_audio_at - input_complete_at) * 1000, 1)
            if first_audio_at and input_complete_at
            else None
        ),
    }
    if result["output_rms"] <= 0 or result["output_seconds"] <= 0.1:
        raise RuntimeError(f"provider returned invalid audio: {result}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--provider",
        choices=("qwen_realtime", "minicpm_omni"),
        default="qwen_realtime",
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--model", default="qwen3.5-omni-plus-realtime")
    parser.add_argument("--voice", default="Tina")
    parser.add_argument("--api-key-env", default="VOICE_LIVE_API_KEY")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--text", default="")
    parser.add_argument("--instructions", default="请自然、简洁地用中文回答。")
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--mode", choices=("full_duplex", "turn_based"), default="full_duplex")
    parser.add_argument("--reference-audio", default="")
    parser.add_argument("--tts-reference-audio", default="")
    parser.add_argument("--upstream-chunk-ms", type=int, default=1000)
    parser.add_argument("--trailing-silence-ms", type=int, default=1800)
    parser.add_argument("--connect-timeout", type=float, default=20.0)
    parser.add_argument("--event-timeout", type=float, default=30.0)
    parser.add_argument("--response-timeout", type=float, default=45.0)
    args = parser.parse_args()
    if not args.text and args.input is None:
        parser.error("--input is required unless --text is used")
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
