"""Exercise the real TTS client's owned startup, synthesis, and shutdown only.

This does not start Elysium, initialize its life engine, or send platform messages.
The live configuration is read-only; a separate loopback test port is mandatory.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import shlex
import socket
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from plugins.tts_voice_plugin.config import TTSVoiceConfig
from plugins.tts_voice_plugin.services.tts_service import TTSService


async def validate(config_path: Path, output: Path) -> None:
    port = 19880
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    raw_config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    config = TTSVoiceConfig.model_validate(raw_config)
    config.tts.server = f"http://127.0.0.1:{port}"
    config.tts.start_command = shlex.join(
        ["env", f"GPT_SOVITS_PORT={port}"] + shlex.split(config.tts.start_command)
    )
    config.tts.idle_shutdown_seconds = 0
    config.tts_advanced.media_type = "wav"
    service = TTSService(SimpleNamespace(config=config))  # type: ignore[arg-type]
    started = time.monotonic()
    owned = None
    try:
        encoded = await service.generate_voice(
            "这是一段本机语音合成测试，正在确认声音能够正常播放。",
            style_hint="default",
            language_hint="zh",
        )
        owned = service._server_process
        if encoded is None or owned is None or owned.returncode is not None:
            raise RuntimeError("Owned plugin synthesis did not complete")
        raw = base64.b64decode(encoded, validate=True)
        samples, sample_rate = sf.read(io.BytesIO(raw), dtype="float32")
        if samples.size == 0 or not np.isfinite(samples).all() or not np.any(samples):
            raise ValueError("Invalid synthesized audio")
        with output.open("xb") as target:
            target.write(raw)
        output.chmod(0o600)
        print(json.dumps({
            "owner_pid": owned.pid,
            "sample_rate": sample_rate,
            "duration_seconds": round(len(samples) / sample_rate, 3),
            "cold_request_seconds": round(time.monotonic() - started, 3),
        }), flush=True)
    finally:
        await service.stop()
    if owned is None or owned.returncode is None:
        raise RuntimeError("The owned launcher was not reaped")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))
    print("Owned process reaped; test port released; no platform send.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    asyncio.run(validate(args.config, args.output))


if __name__ == "__main__":
    main()
