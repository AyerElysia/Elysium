#!/usr/bin/env python3
"""Loopback adapter from the livestream TTS contract to Breeze TTS 2.

The livestream synthesizer posts JSON ``{"text", "speed", "volume"}`` and
expects an audio body. Breeze speaks multipart ``/v1/audio/speech`` and
returns s16le PCM. This process only listens on 127.0.0.1.
"""

from __future__ import annotations

import json
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HOST = "127.0.0.1"
PORT = 18082
BREEZE_URL = "http://127.0.0.1:7862/v1/audio/speech"
REF_WAV = Path(
    "/home/ayerelysia/.local/share/elysium-migration-20260908/"
    "voice/GPT-SoVITS/ref_elysia/ref_gentle.wav"
)
REF_TEXT = "你不需要证明任何事因为你本身就值得被爱"
INSTRUCTION = (
    "始终保持参考说话人的音色、年龄感和身份。像面对熟悉的人自然交谈，"
    "咬字清楚，停顿随语义变化；不要念稿，不要变成另一个人。"
)
MAX_TEXT_CHARS = 1000
BREEZE_TIMEOUT_SECONDS = 55


def _multipart(text: str, audio: bytes) -> tuple[bytes, str]:
    boundary = "----elysium-livestream-breeze"
    chunks = [
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n".encode()
        for name, value in (
            ("text", text),
            ("instruction", INSTRUCTION),
            ("cfg_scale", "3.8"),
            ("ref_text", REF_TEXT),
            ("seed", "1"),
        )
    ]
    chunks.append(
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="ref_audio"; filename="ref_gentle.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n".encode()
        + audio
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    if sample_rate <= 0 or not pcm or len(pcm) % 2:
        raise ValueError("invalid Breeze PCM")
    output = BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return output.getvalue()


def synthesize(text: str) -> bytes:
    body, content_type = _multipart(text, REF_WAV.read_bytes())
    request = Request(
        BREEZE_URL,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urlopen(request, timeout=BREEZE_TIMEOUT_SECONDS) as response:
            sample_rate = int(response.headers.get("X-Sample-Rate", "0"))
            sample_format = response.headers.get("X-Sample-Format", "")
            media_type = response.headers.get("Content-Type", "")
            pcm = response.read()
    except HTTPError as exc:
        detail = exc.read(400).decode("utf-8", "replace")
        raise RuntimeError(f"Breeze HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Breeze unavailable: {exc.reason}") from exc
    if not media_type.startswith("audio/pcm") or sample_format != "s16le":
        raise RuntimeError("Breeze returned an unexpected audio format")
    return _pcm_to_wav(pcm, sample_rate)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/send":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 64 * 1024:
            self._json(400, {"error": "invalid body size"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
            text = str(payload["text"]).strip()
        except (json.JSONDecodeError, KeyError, TypeError, UnicodeError):
            self._json(400, {"error": "text is required"})
            return
        if not text or len(text) > MAX_TEXT_CHARS:
            self._json(400, {"error": "text length is outside the supported range"})
            return
        try:
            wav = synthesize(text)
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"error": str(exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[livestream-tts] {self.address_string()} {fmt % args}", flush=True)

    def _json(self, status: int, payload: dict[str, str]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    if not REF_WAV.is_file():
        raise SystemExit(f"reference audio missing: {REF_WAV}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"livestream TTS bridge listening on http://{HOST}:{PORT}/send", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
