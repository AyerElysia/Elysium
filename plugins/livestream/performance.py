"""Durable speech synthesis and acknowledged stage playback."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from .domain import PerformancePlan, PlaybackReceipt
from .ledger import LivestreamLedger


class TTSProtocolError(RuntimeError):
    """Raised when the speech service returns an invalid audio response."""


class AudioArtifactMissingError(RuntimeError):
    """Raised when durable ledger metadata points to missing/corrupt audio."""


@dataclass(frozen=True, slots=True)
class AudioPacket:
    """One bounded synthesized audio artifact."""

    content: bytes
    mime_type: str

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("audio content must not be empty")
        if not self.mime_type.strip():
            raise ValueError("audio mime_type must not be empty")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class SpeechSynthesizer(Protocol):
    """Technical speech synthesis boundary."""

    async def synthesize(self, text: str) -> AudioPacket:
        """Synthesize one non-empty text chunk or raise explicitly."""


class StageTransport(Protocol):
    """Idempotent output transport controlled by playback identities."""

    async def play(
        self,
        *,
        playback_id: str,
        utterance_id: str,
        chunk_id: str,
        text: str,
        audio: AudioPacket,
        cues: dict[str, str],
        timeout_seconds: float,
    ) -> PlaybackReceipt:
        """Play or recover one stable playback and return actual outcome."""

    async def interrupt(self, utterance_id: str, reason: str) -> None:
        """Request cancellation of the active utterance."""


@dataclass(frozen=True, slots=True)
class PerformanceSettings:
    """Technical bounds for the performance consumer."""

    consumer_name: str = "livestream.performance.v1"
    sentence_delimiters: str = "。！？；\n"
    max_chunk_chars: int = 80
    playback_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not self.consumer_name.strip():
            raise ValueError("consumer_name must not be empty")
        if not self.sentence_delimiters:
            raise ValueError("sentence_delimiters must not be empty")
        if self.max_chunk_chars <= 0:
            raise ValueError("max_chunk_chars must be positive")
        if self.playback_timeout_seconds <= 0:
            raise ValueError("playback_timeout_seconds must be positive")


class AudioArtifactStore:
    """Content-addressed audio files referenced by immutable ledger records."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    async def put(self, packet: AudioPacket) -> str:
        digest = packet.sha256
        path = self._path(digest)
        await asyncio.to_thread(self._put_sync, path, packet.content)
        return digest

    async def get(self, digest: str, mime_type: str) -> AudioPacket:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AudioArtifactMissingError("invalid audio artifact digest")
        path = self._path(digest)
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise AudioArtifactMissingError(
                f"audio artifact is missing: {digest}"
            ) from exc
        packet = AudioPacket(content=content, mime_type=mime_type)
        if packet.sha256 != digest:
            raise AudioArtifactMissingError(f"audio artifact is corrupt: {digest}")
        return packet

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / f"{digest}.audio"

    @staticmethod
    def _put_sync(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if hashlib.sha256(existing).digest() != hashlib.sha256(content).digest():
                raise RuntimeError(f"audio artifact collision at {path}")
            return
        temporary = path.with_suffix(f".tmp-{os.getpid()}")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_fd = os.open(path.parent, directory_flags)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)


class HttpTTSClient:
    """Bounded local TTS client with explicit response validation."""

    def __init__(
        self,
        endpoint: str,
        *,
        speed: float = 1.0,
        volume: float = 1.0,
        timeout_seconds: float = 30.0,
        retry_count: int = 1,
        max_audio_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        if not endpoint.strip():
            raise ValueError("TTS endpoint must not be empty")
        if timeout_seconds <= 0 or retry_count < 0 or max_audio_bytes <= 0:
            raise ValueError("invalid TTS resource limits")
        self.endpoint = endpoint
        self.speed = speed
        self.volume = volume
        self.timeout_seconds = timeout_seconds
        self.retry_count = retry_count
        self.max_audio_bytes = max_audio_bytes
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
            )

    async def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def synthesize(self, text: str) -> AudioPacket:
        if not text.strip():
            raise ValueError("TTS text must not be empty")
        if self._client is None:
            raise RuntimeError("TTS client is not started")

        last_error: BaseException | None = None
        for attempt in range(self.retry_count + 1):
            try:
                async with self._client.stream(
                    "POST",
                    self.endpoint,
                    json={
                        "text": text,
                        "speed": self.speed,
                        "volume": self.volume,
                    },
                ) as response:
                    response.raise_for_status()
                    body = await self._read_response_bounded(response)
                    return self._decode_response(
                        response.headers.get("content-type", ""),
                        body,
                    )
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, TTSProtocolError) as exc:
                last_error = exc
                retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                    exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )
                if attempt >= self.retry_count or not retryable:
                    break
                await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
        raise TTSProtocolError(f"TTS synthesis failed: {last_error}") from last_error

    async def _read_response_bounded(self, response: httpx.Response) -> bytes:
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        limit = (
            self.max_audio_bytes
            if content_type.startswith("audio/")
            or content_type == "application/octet-stream"
            else self.max_audio_bytes * 2 + 65536
        )
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > limit:
                raise TTSProtocolError(f"TTS response exceeds {limit} bytes")
            body.extend(chunk)
        return bytes(body)

    def _decode_response(self, content_type: str, body: bytes) -> AudioPacket:
        content_type = content_type.split(";", 1)[0]
        if content_type.startswith("audio/") or content_type == "application/octet-stream":
            content = body
            mime_type = content_type or "application/octet-stream"
        else:
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise TypeError("TTS JSON response must be an object")
                encoded = payload.get("audio", payload.get("data", ""))
                mime_type = str(payload.get("mime_type", "audio/wav"))
                content = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise TTSProtocolError("TTS returned neither audio nor valid JSON") from exc
        if not content:
            raise TTSProtocolError("TTS returned empty audio")
        if len(content) > self.max_audio_bytes:
            raise TTSProtocolError(
                f"TTS audio exceeds {self.max_audio_bytes} bytes"
            )
        return AudioPacket(content=content, mime_type=mime_type)


def split_speech_text(text: str, settings: PerformanceSettings) -> list[str]:
    """Split transport chunks without assigning semantic importance."""

    normalized = text.strip()
    if not normalized:
        return []
    pattern = re.compile(f"([{re.escape(settings.sentence_delimiters)}])")
    parts = pattern.split(normalized)
    sentences: list[str] = []
    current = ""
    for part in parts:
        current += part
        if pattern.fullmatch(part) and current.strip():
            sentences.append(current.strip())
            current = ""
    if current.strip():
        sentences.append(current.strip())

    chunks: list[str] = []
    for sentence in sentences:
        for start in range(0, len(sentence), settings.max_chunk_chars):
            chunk = sentence[start : start + settings.max_chunk_chars].strip()
            if chunk:
                chunks.append(chunk)
    return chunks


class PerformanceRuntime:
    """Execute planned speech and persist only acknowledged spoken output."""

    def __init__(
        self,
        ledger: LivestreamLedger,
        synthesizer: SpeechSynthesizer,
        stage: StageTransport,
        artifact_store: AudioArtifactStore,
        *,
        session_id: str,
        settings: PerformanceSettings | None = None,
    ) -> None:
        self.ledger = ledger
        self.synthesizer = synthesizer
        self.stage = stage
        self.artifact_store = artifact_store
        self.session_id = session_id
        self.settings = settings or PerformanceSettings()
        self.current_utterance_id: str | None = None
        self.current_interruptible = False

    async def run_once(self) -> str | None:
        try:
            return await self._run_once()
        finally:
            self.current_utterance_id = None
            self.current_interruptible = False

    async def _run_once(self) -> str | None:
        cursor = await self.ledger.get_cursor(
            self.session_id,
            self.settings.consumer_name,
        )
        records = await self.ledger.read_since(
            cursor,
            session_id=self.session_id,
            kinds={"performance.planned"},
            limit=1,
        )
        if not records:
            return None
        source = records[0]
        utterance_id = str(source.payload["utterance_id"])
        self.current_utterance_id = utterance_id
        plan = PerformancePlan.model_validate(source.payload["plan"])
        self.current_interruptible = plan.interruptible
        terminal = await self._existing_terminal(utterance_id)
        if terminal is not None:
            await self.ledger.commit_cursor(
                self.session_id,
                self.settings.consumer_name,
                source.sequence,
            )
            return terminal

        await self.ledger.append(
            record_id=f"performance-started:{utterance_id}",
            session_id=self.session_id,
            kind="performance.started",
            source="livestream.performance",
            payload={
                "utterance_id": utterance_id,
                "decision_id": source.payload["decision_id"],
            },
            correlation_id=utterance_id,
            causation_id=source.record_id,
        )

        chunks = split_speech_text(plan.speech_text, self.settings)
        spoken_chunks: list[str] = []
        terminal_kind: str
        try:
            for index, text in enumerate(chunks):
                receipt = await self._perform_chunk(
                    utterance_id=utterance_id,
                    plan=plan,
                    index=index,
                    text=text,
                )
                if receipt.outcome == "completed":
                    spoken_chunks.append(text)
                    continue
                terminal_kind = (
                    "performance.interrupted"
                    if receipt.outcome == "interrupted"
                    else "performance.failed"
                )
                await self._append_terminal(
                    utterance_id,
                    terminal_kind,
                    spoken_chunks,
                    detail=f"playback outcome: {receipt.outcome}",
                    partial_chunk_text=(
                        text
                        if receipt.outcome == "interrupted" and receipt.played_ms > 0
                        else ""
                    ),
                    partial_played_ms=receipt.played_ms,
                    partial_duration_ms=receipt.duration_ms,
                )
                break
            else:
                terminal_kind = "performance.completed"
                await self._append_terminal(
                    utterance_id,
                    terminal_kind,
                    spoken_chunks,
                )

        except asyncio.CancelledError:
            await self.stage.interrupt(utterance_id, "runtime cancellation")
            raise
        # This is the terminal boundary for arbitrary synthesizer and stage
        # implementations; every failure is converted into a durable record.
        except Exception as exc:  # noqa: BLE001
            terminal_kind = "performance.failed"
            await self._append_terminal(
                utterance_id,
                terminal_kind,
                spoken_chunks,
                detail=f"{type(exc).__name__}: {exc}",
            )

        # Cursor persistence is deliberately outside the performance failure
        # handler.  A cursor-store outage must replay the already terminal
        # record, never manufacture a contradictory performance.failed record.
        await self.ledger.commit_cursor(
            self.session_id,
            self.settings.consumer_name,
            source.sequence,
        )
        return terminal_kind

    async def _perform_chunk(
        self,
        *,
        utterance_id: str,
        plan: PerformancePlan,
        index: int,
        text: str,
    ) -> PlaybackReceipt:
        chunk_id = f"{utterance_id}:{index}"
        playback_id = hashlib.sha256(
            f"{chunk_id}:playback".encode()
        ).hexdigest()[:24]
        receipt_id = f"playback-receipt:{playback_id}"
        existing_receipt = await self.ledger.get_record(receipt_id)
        if existing_receipt is not None:
            return PlaybackReceipt.model_validate(existing_receipt.payload)

        artifact_id = f"tts-artifact:{chunk_id}"
        artifact_record = await self.ledger.get_record(artifact_id)
        if artifact_record is None:
            packet = await self.synthesizer.synthesize(text)
            digest = await self.artifact_store.put(packet)
            await self.ledger.append(
                record_id=artifact_id,
                session_id=self.session_id,
                kind="tts.synthesized",
                source="livestream.tts",
                payload={
                    "utterance_id": utterance_id,
                    "chunk_id": chunk_id,
                    "text": text,
                    "audio_sha256": digest,
                    "mime_type": packet.mime_type,
                    "size_bytes": len(packet.content),
                },
                correlation_id=utterance_id,
            )
        else:
            payload = artifact_record.payload
            if payload.get("text") != text:
                raise RuntimeError(f"TTS artifact text conflict for {chunk_id}")
            packet = await self.artifact_store.get(
                str(payload["audio_sha256"]),
                str(payload["mime_type"]),
            )

        await self.ledger.append(
            record_id=f"playback-dispatched:{playback_id}",
            session_id=self.session_id,
            kind="playback.dispatched",
            source="livestream.performance",
            payload={
                "playback_id": playback_id,
                "utterance_id": utterance_id,
                "chunk_id": chunk_id,
                "audio_sha256": packet.sha256,
            },
            correlation_id=utterance_id,
            causation_id=artifact_id,
        )
        receipt = await self.stage.play(
            playback_id=playback_id,
            utterance_id=utterance_id,
            chunk_id=chunk_id,
            text=text,
            audio=packet,
            cues={
                "expression": plan.expression_hint,
                "motion": plan.motion_hint,
                "scene": plan.scene_cue,
            },
            timeout_seconds=self.settings.playback_timeout_seconds,
        )
        if (
            receipt.playback_id != playback_id
            or receipt.utterance_id != utterance_id
            or receipt.chunk_id != chunk_id
        ):
            raise RuntimeError("stage returned a receipt for another playback")
        await self.ledger.append(
            record_id=receipt_id,
            session_id=self.session_id,
            kind="playback.receipt",
            source="livestream.stage",
            payload=receipt.model_dump(mode="json"),
            occurred_at=receipt.ended_at,
            correlation_id=utterance_id,
            causation_id=f"playback-dispatched:{playback_id}",
        )
        return receipt

    async def _append_terminal(
        self,
        utterance_id: str,
        kind: str,
        spoken_chunks: list[str],
        *,
        detail: str = "",
        partial_chunk_text: str = "",
        partial_played_ms: int = 0,
        partial_duration_ms: int | None = None,
    ) -> None:
        await self.ledger.append(
            record_id=f"{kind}:{utterance_id}",
            session_id=self.session_id,
            kind=kind,
            source="livestream.performance",
            payload={
                "utterance_id": utterance_id,
                "spoken_text": "".join(spoken_chunks),
                "completed_chunk_count": len(spoken_chunks),
                "detail": detail,
                "partial_chunk_text": partial_chunk_text,
                "partial_played_ms": partial_played_ms,
                "partial_duration_ms": partial_duration_ms,
            },
            correlation_id=utterance_id,
        )

    async def _existing_terminal(self, utterance_id: str) -> str | None:
        for kind in (
            "performance.completed",
            "performance.interrupted",
            "performance.failed",
        ):
            if await self.ledger.get_record(f"{kind}:{utterance_id}") is not None:
                return kind
        return None
