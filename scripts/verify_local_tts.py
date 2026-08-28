#!/usr/bin/env python3
"""Verify the configured local message TTS without sending a platform message."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import soundfile as sf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.tts_voice_plugin.config import TTSVoiceConfig  # noqa: E402
from plugins.tts_voice_plugin.services.tts_service import TTSService  # noqa: E402


def _audio_format(data: bytes) -> str:
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    if data.startswith(b"ID3") or data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "mp3"
    return "unknown"


def _audio_metadata(data: bytes) -> dict[str, object]:
    try:
        info = sf.info(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        return {"decode_status": "failed", "decode_error_type": type(exc).__name__}
    duration = info.frames / float(info.samplerate) if info.samplerate else 0.0
    return {
        "decode_status": "ok",
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "duration_seconds": round(duration, 3),
    }


async def _verify(
    config_path: Path,
    text: str,
    style: str,
    language: str | None,
    output_path: Path | None = None,
) -> dict[str, object]:
    config = TTSVoiceConfig.load(config_path, auto_update=False)
    service = TTSService(SimpleNamespace(config=config))  # type: ignore[arg-type]
    cleaned_text = service._clean_text_for_tts(text)
    plan_mode, segments = service._build_synthesis_plan(cleaned_text)
    try:
        encoded = await service.generate_voice(
            text,
            style_hint=style,
            language_hint=language,
        )
        if not isinstance(encoded, str) or not encoded.strip():
            raise RuntimeError("local TTS returned empty audio")
        try:
            audio = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise RuntimeError("local TTS returned invalid Base64 audio") from exc
        if not audio:
            raise RuntimeError("local TTS decoded to empty audio")
        resolved_output: str | None = None
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(audio)
            resolved_output = str(output_path.resolve())
        return {
            "status": "ok",
            "server": config.tts.server,
            "style": style,
            "language": language or "auto",
            "input_chars": len(text),
            "audio_format": _audio_format(audio),
            "audio_bytes": len(audio),
            "audio_sha256": hashlib.sha256(audio).hexdigest(),
            "synthesis_plan_mode": plan_mode,
            "synthesis_segments": len(segments),
            "segment_chars": [len(segment.text) for segment in segments],
            "segment_units": [segment.units for segment in segments],
            "segment_boundaries": [segment.boundary for segment in segments],
            "output_path": resolved_output,
            **_audio_metadata(audio),
        }
    finally:
        await service.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/plugins/tts_voice_plugin/config.toml"),
    )
    parser.add_argument("--text", default="晚安，愿你有个好梦。")
    parser.add_argument("--style", default="default")
    parser.add_argument("--language", default="zh")
    parser.add_argument(
        "--output",
        type=Path,
        help="可选：把生成的单一完整音频写到此路径，便于试听",
    )
    args = parser.parse_args()
    result = asyncio.run(
        _verify(
            args.config,
            str(args.text),
            str(args.style),
            str(args.language).strip() or None,
            args.output,
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
