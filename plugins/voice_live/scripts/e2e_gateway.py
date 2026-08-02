#!/usr/bin/env python3
"""Exercise the deployed Voice Live gateway from ticket to output WAV."""

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
from typing import Any

import aiohttp

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.voice_live.protocol import pack_audio_frame, unpack_audio_frame  # noqa: E402


def _read_input(path: Path) -> bytes:
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError("input WAV must be 16 kHz, mono, PCM16")
        return source.readframes(source.getnframes())


def _write_output(path: Path, pcm16: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(pcm16)


def _rms(pcm16: bytes) -> int:
    if not pcm16:
        return 0
    count = len(pcm16) // 2
    total = sum(value[0] * value[0] for value in struct.iter_unpack("<h", pcm16))
    return round(math.sqrt(total / count))


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    base = args.base_url.rstrip("/")
    origin = args.origin or base.split("/voice-live", 1)[0]
    output = bytearray()
    events: list[dict[str, Any]] = []
    ready = asyncio.Event()
    response_finished = asyncio.Event()
    ended = asyncio.Event()
    errors: list[str] = []
    sample_rate = 24000
    episode_id = ""
    saw_speaking = False
    first_audio_at = 0.0
    input_complete_at = 0.0
    started_at = time.monotonic()

    timeout = aiohttp.ClientTimeout(total=None, connect=args.connect_timeout)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.get(f"{base}/health") as response:
            health_before = await response.json()
            response.raise_for_status()
        async with client.post(f"{base}/ticket", headers={"Origin": origin}) as response:
            ticket_body = await response.json()
            response.raise_for_status()
        ws_url = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        ws = await client.ws_connect(
            f"{ws_url}/ws?ticket={ticket_body['ticket']}",
            headers={"Origin": origin},
            heartbeat=20,
        )

        async def receive() -> None:
            nonlocal episode_id, first_audio_at, sample_rate, saw_speaking
            async for message in ws:
                if message.type is aiohttp.WSMsgType.BINARY:
                    frame = unpack_audio_frame(message.data)
                    if not first_audio_at:
                        first_audio_at = time.monotonic()
                    sample_rate = frame.sample_rate
                    output.extend(frame.pcm16)
                    continue
                if message.type is not aiohttp.WSMsgType.TEXT:
                    if message.type is aiohttp.WSMsgType.ERROR:
                        errors.append(str(ws.exception() or "gateway websocket error"))
                    break
                event = json.loads(message.data)
                events.append(event)
                event_type = str(event.get("type") or "")
                if event_type == "ready":
                    episode_id = str(event.get("episode_id") or "")
                    ready.set()
                elif event_type == "state":
                    state = str(event.get("state") or "")
                    if state == "speaking":
                        saw_speaking = True
                    elif state == "listening" and saw_speaking:
                        response_finished.set()
                elif event_type == "error":
                    errors.append(str(event.get("message") or "gateway error"))
                    if event.get("fatal"):
                        ready.set()
                        response_finished.set()
                elif event_type == "ended":
                    ended.set()

        receive_task = asyncio.create_task(receive())
        try:
            await ws.send_json({"type": "start", "mode": "full_duplex"})
            await asyncio.wait_for(ready.wait(), timeout=args.connect_timeout)
            if errors:
                raise RuntimeError("; ".join(errors))

            if args.text:
                await ws.send_json({"type": "text", "text": args.text})
            else:
                source = _read_input(args.input)
                frame_bytes = 16000 * 2 * args.frame_ms // 1000
                silence = b"\x00" * frame_bytes
                sequence = 0

                async def send_frame(frame: bytes) -> None:
                    nonlocal sequence
                    sequence += 1
                    await ws.send_bytes(pack_audio_frame(sequence, 16000, frame))
                    await asyncio.sleep(args.frame_ms / 1000)

                for _ in range(args.leading_silence_ms // args.frame_ms):
                    await send_frame(silence)
                for offset in range(0, len(source), frame_bytes):
                    frame = source[offset : offset + frame_bytes]
                    await send_frame(frame.ljust(frame_bytes, b"\x00"))
                for _ in range(args.trailing_silence_ms // args.frame_ms):
                    await send_frame(silence)
            input_complete_at = time.monotonic()
            await asyncio.wait_for(response_finished.wait(), timeout=args.response_timeout)
            await ws.send_json({"type": "stop"})
            await asyncio.wait_for(ended.wait(), timeout=15)
        finally:
            await ws.close()
            await asyncio.wait_for(receive_task, timeout=5)

        async with client.get(f"{base}/health") as response:
            health_after = await response.json()
            response.raise_for_status()

    pcm16 = bytes(output)
    if errors:
        raise RuntimeError("; ".join(errors))
    if not pcm16:
        raise RuntimeError("gateway returned no audio")
    _write_output(args.output, pcm16, sample_rate)
    result = {
        "episode_id": episode_id,
        "provider": health_before.get("provider"),
        "model": health_before.get("model"),
        "input_mode": "text" if args.text else "audio",
        "event_types": [str(event.get("type") or "") for event in events],
        "transcripts": [event for event in events if event.get("type") == "transcript"],
        "health_before": health_before,
        "health_after": health_after,
        "output_path": str(args.output),
        "output_bytes": len(pcm16),
        "output_seconds": round(len(pcm16) / (sample_rate * 2), 3),
        "output_rms": _rms(pcm16),
        "output_sha256": hashlib.sha256(pcm16).hexdigest(),
        "first_audio_latency_ms": round((first_audio_at - started_at) * 1000, 1),
        "first_audio_after_input_ms": round((first_audio_at - input_complete_at) * 1000, 1),
    }
    if result["output_rms"] <= 0 or result["output_seconds"] <= 0.1:
        raise RuntimeError(f"gateway returned invalid audio: {result}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18000/voice-live")
    parser.add_argument("--origin", default="")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--text", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frame-ms", type=int, default=20)
    parser.add_argument("--leading-silence-ms", type=int, default=800)
    parser.add_argument("--trailing-silence-ms", type=int, default=1800)
    parser.add_argument("--connect-timeout", type=float, default=45)
    parser.add_argument("--response-timeout", type=float, default=60)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
