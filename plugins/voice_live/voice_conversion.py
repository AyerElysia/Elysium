"""Process-isolated streaming voice conversion client.

The client implements only a small HTTP/PCM contract.  GPL voice-conversion
engines and model code remain in their own service process and distribution.
"""

from __future__ import annotations

import os
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .audio import resample_pcm16_mono
from .secrets import resolve_secret


def _default_gateway_from_route_table(route_table: str) -> str:
    """Extract the IPv4 default gateway from Linux ``/proc/net/route``."""

    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            gateway = int(fields[2], 16)
        except ValueError:
            continue
        if flags & 0x2 and gateway:
            return socket.inet_ntoa(struct.pack("<I", gateway))
    raise RuntimeError("WSL Windows host gateway is unavailable")


def resolve_service_url(service_url: str) -> str:
    """Resolve the stable ``wsl-host`` alias without relying on DNS proxies."""

    raw = str(service_url or "").strip()
    parts = urlsplit(raw)
    if parts.hostname != "wsl-host":
        return raw
    if parts.username or parts.password:
        raise ValueError("voice-conversion service_url must not contain credentials")
    if os.name == "nt":
        host = "127.0.0.1"
    else:
        route_table = Path("/proc/net/route").read_text(encoding="ascii")
        host = _default_gateway_from_route_table(route_table)
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass(slots=True, frozen=True)
class ConvertedAudio:
    """One converted PCM response plus service-side timing counters."""

    data: bytes
    sample_rate: int
    metrics: dict[str, float | int]


class VoiceConverter(Protocol):
    """Contract owned by a single realtime call session."""

    @property
    def input_sample_rate(self) -> int:
        """Return the PCM sample rate accepted by the converter."""

        ...

    @property
    def output_sample_rate(self) -> int:
        """Return the converter's native PCM sample rate."""

        ...

    async def connect(self) -> dict[str, Any]:
        """Allocate and validate one converter session."""

        ...

    async def process(self, pcm16: bytes, sample_rate: int) -> ConvertedAudio:
        """Consume one mono PCM16 chunk and return any completed output blocks."""

        ...

    async def flush(self) -> ConvertedAudio:
        """Convert the final partial block."""

        ...

    async def reset(self) -> None:
        """Discard pending audio and overlap state after an interruption."""

        ...

    async def close(self) -> None:
        """Release the remote session and local HTTP resources."""

        ...


class HttpVoiceConverter:
    """One remote SVC session with explicit failure semantics."""

    def __init__(
        self,
        service_url: str,
        token: str,
        profile_id: str,
        *,
        connect_timeout: float,
        request_timeout: float,
    ) -> None:
        if not service_url.strip():
            raise ValueError("voice-conversion service_url is required")
        if not token:
            raise RuntimeError("voice-conversion service token is empty")
        if not profile_id.strip():
            raise ValueError("voice-conversion profile_id is required")
        self._service_url = resolve_service_url(service_url).rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._profile_id = profile_id
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._http: aiohttp.ClientSession | None = None
        self._session_id = ""
        self._input_sample_rate = 0
        self._output_sample_rate = 0
        self._input_block_bytes = 0
        self._pending_input = bytearray()
        self._profile_revision = ""

    @property
    def is_connected(self) -> bool:
        return bool(self._http and not self._http.closed and self._session_id)

    @property
    def input_sample_rate(self) -> int:
        return self._input_sample_rate

    @property
    def output_sample_rate(self) -> int:
        return self._output_sample_rate

    @property
    def profile_revision(self) -> str:
        return self._profile_revision

    async def connect(self) -> dict[str, Any]:
        """Validate the profile and allocate one remote conversion session."""
        if self.is_connected:
            raise RuntimeError("voice-conversion session is already connected")
        timeout = aiohttp.ClientTimeout(
            total=self._connect_timeout,
            connect=self._connect_timeout,
        )
        self._http = aiohttp.ClientSession(timeout=timeout)
        try:
            async with self._http.get(f"{self._service_url}/health") as response:
                health = await self._json_response(response, expected_status=200)
            if health.get("status") != "ok":
                raise RuntimeError(f"voice-conversion service is not ready: {health}")
            if str(health.get("profile_id") or "") != self._profile_id:
                raise RuntimeError(
                    "voice-conversion profile mismatch: "
                    f"expected {self._profile_id!r}, got {health.get('profile_id')!r}"
                )
            if int(health.get("protocol_version") or 0) < 2:
                raise RuntimeError(
                    "voice-conversion service protocol is obsolete; "
                    "restart the traceable Seed-VC service manually"
                )
            health_revision = str(health.get("profile_revision") or "")
            if not health_revision:
                raise RuntimeError("voice-conversion profile revision is missing")
            async with self._http.post(
                f"{self._service_url}/v1/sessions",
                headers=self._headers,
                json={"profile_id": self._profile_id},
            ) as response:
                created = await self._json_response(response, expected_status=201)
            self._session_id = str(created.get("session_id") or "")
            self._input_sample_rate = int(created.get("input_sample_rate") or 0)
            self._output_sample_rate = int(created.get("output_sample_rate") or 0)
            input_block_samples = int(created.get("input_block_samples") or 0)
            session_revision = str(created.get("profile_revision") or "")
            if (
                not self._session_id
                or self._input_sample_rate <= 0
                or self._output_sample_rate <= 0
                or input_block_samples <= 0
            ):
                raise RuntimeError(
                    f"invalid voice-conversion session response: {created}"
                )
            if session_revision != health_revision:
                raise RuntimeError(
                    "voice-conversion profile changed while allocating the session"
                )
            self._input_block_bytes = input_block_samples * 2
            self._profile_revision = health_revision
            self._pending_input.clear()
            return {"health": health, "session": created}
        except Exception:
            await self.close()
            raise

    async def process(self, pcm16: bytes, sample_rate: int) -> ConvertedAudio:
        """Resample and submit one mono PCM16 chunk."""
        if not self.is_connected or self._http is None:
            raise RuntimeError("voice-conversion session is not connected")
        converted_input = resample_pcm16_mono(
            pcm16, sample_rate, self._input_sample_rate
        )
        self._pending_input.extend(converted_input)
        complete_bytes = (
            len(self._pending_input) // self._input_block_bytes
        ) * self._input_block_bytes
        if complete_bytes <= 0:
            return ConvertedAudio(
                b"",
                self._output_sample_rate,
                {
                    "block_count": 0,
                    "inference_ms": 0.0,
                    "pending_samples": len(self._pending_input) // 2,
                },
            )
        payload = bytes(self._pending_input[:complete_bytes])
        del self._pending_input[:complete_bytes]
        converted = await self._audio_request("audio", payload)
        metrics = dict(converted.metrics)
        metrics["pending_samples"] = (
            int(converted.metrics.get("pending_samples", 0))
            + len(self._pending_input) // 2
        )
        return ConvertedAudio(converted.data, converted.sample_rate, metrics)

    async def flush(self) -> ConvertedAudio:
        """Convert the remote session's remaining partial block."""
        if not self.is_connected:
            return ConvertedAudio(b"", self._output_sample_rate, {})
        parts: list[bytes] = []
        block_count = 0
        inference_ms = 0.0
        if self._pending_input:
            pending = bytes(self._pending_input)
            self._pending_input.clear()
            submitted = await self._audio_request("audio", pending)
            parts.append(submitted.data)
            block_count += int(submitted.metrics.get("block_count", 0))
            inference_ms += float(submitted.metrics.get("inference_ms", 0.0))
        flushed = await self._audio_request("flush", b"")
        parts.append(flushed.data)
        block_count += int(flushed.metrics.get("block_count", 0))
        inference_ms += float(flushed.metrics.get("inference_ms", 0.0))
        return ConvertedAudio(
            b"".join(parts),
            flushed.sample_rate,
            {
                "block_count": block_count,
                "inference_ms": round(inference_ms, 3),
                "pending_samples": 0,
            },
        )

    async def reset(self) -> None:
        """Clear remote streaming context after playback interruption."""
        if not self.is_connected or self._http is None:
            return
        self._pending_input.clear()
        async with self._http.post(
            self._session_url("reset"),
            headers=self._headers,
            data=b"",
            timeout=self._request_timeout,
        ) as response:
            await self._json_response(response, expected_status=200)

    async def close(self) -> None:
        """Delete the remote session and close the HTTP client."""
        http = self._http
        session_id = self._session_id
        self._http = None
        self._session_id = ""
        self._input_block_bytes = 0
        self._pending_input.clear()
        self._profile_revision = ""
        if http is None:
            return
        if session_id and not http.closed:
            try:
                async with http.delete(
                    f"{self._service_url}/v1/sessions/{session_id}",
                    headers=self._headers,
                    timeout=self._connect_timeout,
                ) as response:
                    await response.read()
            except (aiohttp.ClientError, TimeoutError):
                # Session deletion is best-effort after transport ownership ends.
                pass
        await http.close()

    async def _audio_request(self, operation: str, payload: bytes) -> ConvertedAudio:
        assert self._http is not None
        headers = {
            **self._headers,
            "Content-Type": "audio/L16",
            "X-Input-Sample-Rate": str(self._input_sample_rate),
        }
        async with self._http.post(
            self._session_url(operation),
            headers=headers,
            data=payload,
            timeout=self._request_timeout,
        ) as response:
            body = await response.read()
            if response.status != 200:
                message = body.decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"voice-conversion {operation} failed ({response.status}): {message}"
                )
            output_rate = int(
                response.headers.get("X-Output-Sample-Rate", self._output_sample_rate)
            )
            metrics: dict[str, float | int] = {
                "block_count": int(response.headers.get("X-Block-Count", "0")),
                "inference_ms": float(response.headers.get("X-Inference-Ms", "0")),
                "pending_samples": int(response.headers.get("X-Pending-Samples", "0")),
            }
            return ConvertedAudio(body, output_rate, metrics)

    def _session_url(self, operation: str) -> str:
        return f"{self._service_url}/v1/sessions/{self._session_id}/{operation}"

    @staticmethod
    async def _json_response(
        response: aiohttp.ClientResponse, *, expected_status: int
    ) -> dict[str, Any]:
        body = await response.text()
        if response.status != expected_status:
            raise RuntimeError(
                f"voice-conversion service returned {response.status}: {body}"
            )
        try:
            value = await response.json(content_type=None)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"voice-conversion service returned invalid JSON: {body}"
            ) from exc
        if not isinstance(value, dict):
            raise TypeError("voice-conversion response must be a JSON object")
        return value


def create_voice_converter(config: object) -> HttpVoiceConverter | None:
    """Build the configured converter without embedding its bearer token."""
    section = config.voice_conversion
    if not section.enabled:
        return None
    token = resolve_secret(
        section.token_env,
        section.token_file,
        label="Voice conversion",
    )
    return HttpVoiceConverter(
        section.service_url,
        token,
        section.profile_id,
        connect_timeout=section.connect_timeout_seconds,
        request_timeout=section.request_timeout_seconds,
    )


__all__ = [
    "ConvertedAudio",
    "HttpVoiceConverter",
    "VoiceConverter",
    "create_voice_converter",
    "resolve_service_url",
]
