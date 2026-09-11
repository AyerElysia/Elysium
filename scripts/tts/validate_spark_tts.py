"""Synthesize explicit engineering test text over loopback, without platform sends."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--port", type=int, default=19880)
    parser.add_argument("--style", default="default")
    args = parser.parse_args()
    config = tomllib.loads(args.config.read_text(encoding="utf-8"))
    styles = [item for item in config["tts_styles"] if item["style_name"] == args.style]
    if len(styles) != 1:
        parser.error("expected exactly one configured style")
    style = styles[0]
    default_style = next(item for item in config["tts_styles"] if item["style_name"] == "default")
    for kind in ("gpt", "sovits"):
        # Match the plugin's explicit default-style contract inheritance.
        weights = Path(style[f"{kind}_weights"] or default_style[f"{kind}_weights"])
        expected = style[f"{kind}_weights_sha256"] or default_style[f"{kind}_weights_sha256"]
        with weights.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != expected:
            raise ValueError(f"Unapproved {kind} checkpoint")
    payload = dict(config.get("tts_advanced", {}))
    payload.pop("text_normalization", None)
    payload.update(
        text="这是一段本机语音合成测试，正在确认声音能够正常播放。",
        text_lang="zh",
        ref_audio_path=style["refer_wav_path"],
        prompt_text=style["prompt_text"],
        prompt_lang=style["prompt_language"],
        speed_factor=style["speed_factor"],
        media_type="wav",
        streaming_mode=False,
        batch_size=1,
        seed=20260910,
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{args.port}/tts",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(error.read().decode("utf-8", errors="replace")) from error
    elapsed = time.monotonic() - started
    samples, sample_rate = sf.read(io.BytesIO(raw), dtype="float32")
    if samples.size == 0 or not np.isfinite(samples).all():
        raise ValueError("Invalid audio samples")
    rms = float(np.sqrt(np.mean(samples**2)))
    if rms == 0:
        raise ValueError("Silent audio")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(raw)
    args.output.chmod(0o600)
    print(json.dumps({
        "style": args.style,
        "path": str(args.output),
        "sample_rate": sample_rate,
        "duration_seconds": round(len(samples) / sample_rate, 3),
        "request_seconds": round(elapsed, 3),
        "rms": round(rms, 6),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
