"""Recoverable, non-blocking episode audio archive for future training."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import struct
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .runtime_store import VoiceEpisodeStore

AUDIO_ARCHIVE_SCHEMA_VERSION = 1
_WAV_HEADER_BYTES = 44
_PCM_SAMPLE_BYTES = 2


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_track_name(value: str) -> str:
    clean = "".join(ch for ch in value if ch.isalnum() or ch in "-_")
    if clean != value or not clean:
        raise ValueError("audio track names must contain only safe characters")
    return clean


@dataclass(slots=True, frozen=True)
class AudioTrackSpec:
    """One mono PCM16 track and its provenance role."""

    name: str
    sample_rate: int
    role: str
    stage: str

    def __post_init__(self) -> None:
        _safe_track_name(self.name)
        if self.sample_rate < 8000:
            raise ValueError("audio track sample rate must be at least 8000 Hz")
        if not self.role.strip() or not self.stage.strip():
            raise ValueError("audio track role and stage are required")


@dataclass(slots=True)
class _OpenTrack:
    spec: AudioTrackSpec
    path: Path
    handle: BinaryIO
    digest: Any
    pcm_bytes: int


def _wav_header(sample_rate: int, pcm_bytes: int) -> bytes:
    """Build a canonical mono PCM16 WAV header."""

    if pcm_bytes < 0 or pcm_bytes > 0xFFFFFFFF - 36:
        raise ValueError("audio track exceeds the WAV RIFF size limit")
    block_align = _PCM_SAMPLE_BYTES
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + pcm_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * block_align,
        block_align,
        16,
        b"data",
        pcm_bytes,
    )


def _validate_wav_header(header: bytes, spec: AudioTrackSpec) -> None:
    if len(header) != _WAV_HEADER_BYTES:
        raise RuntimeError(f"audio track header is incomplete: {spec.name}")
    try:
        values = struct.unpack("<4sI4s4sIHHIIHH4sI", header)
    except struct.error as exc:
        raise RuntimeError(f"audio track header is invalid: {spec.name}") from exc
    (
        riff,
        _,
        wave,
        fmt,
        fmt_size,
        audio_format,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        sample_bits,
        data_marker,
        _,
    ) = values
    valid = (
        riff == b"RIFF"
        and wave == b"WAVE"
        and fmt == b"fmt "
        and fmt_size == 16
        and audio_format == 1
        and channels == 1
        and sample_rate == spec.sample_rate
        and byte_rate == spec.sample_rate * _PCM_SAMPLE_BYTES
        and block_align == _PCM_SAMPLE_BYTES
        and sample_bits == 16
        and data_marker == b"data"
    )
    if not valid:
        raise RuntimeError(f"audio track format changed during episode: {spec.name}")


class VoiceAudioArchive:
    """Archive aligned Voice tracks without blocking realtime callbacks.

    Realtime code only performs a bounded ``put_nowait``. One writer thread owns
    every file handle, periodically makes WAV headers durable, and atomically
    updates a content-free manifest. A crash can therefore leave at most a stale
    header; the next resume repairs it from the actual aligned PCM byte count.
    """

    def __init__(
        self,
        store: VoiceEpisodeStore,
        *,
        queue_max_chunks: int = 2048,
        fsync_interval_seconds: float = 1.0,
    ) -> None:
        self.store = store
        self.directory = store.directory / "audio"
        self.manifest_path = self.directory / "manifest.json"
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(128, queue_max_chunks))
        self._fsync_interval_seconds = max(0.1, float(fsync_interval_seconds))
        self._specs: dict[str, AudioTrackSpec] = {}
        self._enqueued_bytes: dict[str, int] = {}
        self._written_bytes: dict[str, int] = {}
        self._dropped_bytes: dict[str, int] = {}
        self._track_hashes: dict[str, str] = {}
        self._metadata: dict[str, Any] = {}
        self._started_at = ""
        self._closed_at = ""
        self._close_reason = ""
        self._writer_error = ""
        self._state = "created"
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._sentinel = object()

    async def start(
        self,
        specs: list[AudioTrackSpec],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Start the writer and recover resumable tracks before returning."""

        if self._thread is not None:
            return
        normalized = {spec.name: spec for spec in specs}
        if not normalized or len(normalized) != len(specs):
            raise ValueError("audio archive requires unique track specifications")
        previous = await asyncio.to_thread(self._load_previous_manifest)
        previous_tracks = dict(previous.get("tracks") or {})
        previous_metadata = dict(previous.get("metadata") or {})
        previous_metadata.update(metadata or {})
        with self._lock:
            self._specs = normalized
            self._enqueued_bytes = {name: 0 for name in normalized}
            self._written_bytes = {name: 0 for name in normalized}
            self._dropped_bytes = {
                name: int(
                    dict(previous_tracks.get(name) or {}).get("dropped_bytes") or 0
                )
                for name in normalized
            }
            self._track_hashes = {name: "" for name in normalized}
            self._metadata = previous_metadata
            self._started_at = str(previous.get("started_at") or "") or _utc_now()
            self._closed_at = ""
            self._close_reason = ""
            self._state = "starting"
        self._thread = threading.Thread(
            target=self._writer_main,
            name=f"voice-audio-{self.store.episode_id}",
            daemon=True,
        )
        self._thread.start()
        ready = await asyncio.to_thread(self._ready.wait, 10.0)
        if not ready:
            raise TimeoutError("voice audio archive writer did not become ready")
        if self._writer_error:
            raise RuntimeError(self._writer_error)

    def append(self, track_name: str, pcm16: bytes, sample_rate: int) -> bool:
        """Queue one PCM16 chunk and report whether it entered the archive."""

        if not pcm16:
            return True
        spec = self._specs.get(track_name)
        if spec is None:
            raise ValueError(f"unknown audio archive track: {track_name}")
        if int(sample_rate) != spec.sample_rate:
            raise ValueError(
                f"audio track sample rate changed: {track_name} "
                f"{sample_rate} != {spec.sample_rate}"
            )
        if len(pcm16) % _PCM_SAMPLE_BYTES:
            raise ValueError("PCM16 chunks must contain complete samples")
        payload = bytes(pcm16)
        with self._lock:
            if self._state != "recording" or self._writer_error:
                self._dropped_bytes[track_name] += len(payload)
                return False
            self._enqueued_bytes[track_name] += len(payload)
        try:
            self._queue.put_nowait((track_name, payload))
            return True
        except queue.Full:
            with self._lock:
                self._enqueued_bytes[track_name] -= len(payload)
                self._dropped_bytes[track_name] += len(payload)
            return False

    def update_metadata(self, **fields: Any) -> None:
        """Attach content-free provenance and request a manifest refresh."""

        with self._lock:
            self._metadata.update(fields)
            active = self._state == "recording"
        if active:
            try:
                self._queue.put_nowait(("__manifest__", b""))
            except queue.Full:
                pass

    def cursor_snapshot(self) -> dict[str, dict[str, int]]:
        """Return sample cursors for transcript and interruption alignment."""

        with self._lock:
            return {
                name: {
                    "sample_rate": spec.sample_rate,
                    "samples_enqueued": self._enqueued_bytes[name] // _PCM_SAMPLE_BYTES,
                    "dropped_samples": self._dropped_bytes[name] // _PCM_SAMPLE_BYTES,
                }
                for name, spec in self._specs.items()
            }

    def snapshot(self) -> dict[str, Any]:
        """Expose bounded health and counters without audio content."""

        with self._lock:
            return {
                "enabled": True,
                "state": self._state,
                "writer_error": self._writer_error,
                "queue_depth": self._queue.qsize(),
                "tracks": {
                    name: {
                        "sample_rate": spec.sample_rate,
                        "enqueued_bytes": self._enqueued_bytes[name],
                        "written_bytes": self._written_bytes[name],
                        "dropped_bytes": self._dropped_bytes[name],
                    }
                    for name, spec in self._specs.items()
                },
            }

    async def close(self, *, reason: str) -> dict[str, Any]:
        """Drain the queue, finalize WAV headers and return the manifest."""

        thread = self._thread
        if thread is None:
            return self.manifest_snapshot()
        with self._lock:
            already_finished = (
                self._state in {"closed", "degraded"} and not thread.is_alive()
            )
            finished_state = self._state
            if not already_finished:
                self._state = "closing"
                self._closed_at = _utc_now()
                self._close_reason = str(reason or "closed")
            failed = bool(self._writer_error) or already_finished
        if already_finished:
            if finished_state == "degraded":
                await asyncio.to_thread(
                    self._write_manifest_atomic,
                    self._manifest_data(),
                )
            return self.manifest_snapshot()
        if thread.is_alive() and not failed:
            await asyncio.to_thread(self._queue.put, self._sentinel)
        await asyncio.to_thread(thread.join, 15.0)
        if thread.is_alive():
            with self._lock:
                self._writer_error = (
                    self._writer_error or "audio archive writer close timed out"
                )
                self._state = "degraded"
            return self._manifest_data()
        return self.manifest_snapshot()

    def manifest_snapshot(self) -> dict[str, Any]:
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return self._manifest_data()
        return value if isinstance(value, dict) else self._manifest_data()

    def _load_previous_manifest(self) -> dict[str, Any]:
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        if value.get("instance_id") != self.store.instance_id:
            raise RuntimeError("audio archive instance identity changed")
        if value.get("episode_id") != self.store.episode_id:
            raise RuntimeError("audio archive episode identity changed")
        return value

    def _writer_main(self) -> None:
        tracks: dict[str, _OpenTrack] = {}
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
            for spec in self._specs.values():
                track = self._open_track(spec)
                tracks[spec.name] = track
                with self._lock:
                    self._written_bytes[spec.name] = track.pcm_bytes
                    self._enqueued_bytes[spec.name] = track.pcm_bytes
            with self._lock:
                self._state = "recording"
            self._write_manifest_atomic(self._manifest_data())
            self._ready.set()
            next_sync = time.monotonic() + self._fsync_interval_seconds
            while True:
                timeout = max(0.0, next_sync - time.monotonic())
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    self._sync_tracks(tracks)
                    self._write_manifest_atomic(self._manifest_data())
                    next_sync = time.monotonic() + self._fsync_interval_seconds
                    continue
                try:
                    if item is self._sentinel:
                        break
                    track_name, payload = item
                    if track_name == "__manifest__":
                        self._write_manifest_atomic(self._manifest_data())
                        continue
                    track = tracks[track_name]
                    track.handle.seek(0, os.SEEK_END)
                    track.handle.write(payload)
                    track.digest.update(payload)
                    track.pcm_bytes += len(payload)
                    with self._lock:
                        self._written_bytes[track_name] = track.pcm_bytes
                finally:
                    self._queue.task_done()
                if time.monotonic() >= next_sync:
                    self._sync_tracks(tracks)
                    self._write_manifest_atomic(self._manifest_data())
                    next_sync = time.monotonic() + self._fsync_interval_seconds
            self._sync_tracks(tracks)
            with self._lock:
                self._track_hashes = {
                    name: track.digest.hexdigest() for name, track in tracks.items()
                }
                self._state = "closed"
                self._closed_at = self._closed_at or _utc_now()
            self._write_manifest_atomic(self._manifest_data())
        except Exception as exc:  # noqa: BLE001 - report gaps without killing Voice
            with self._lock:
                self._writer_error = f"{type(exc).__name__}: {exc}"
                self._state = "degraded"
                self._track_hashes = {
                    name: track.digest.hexdigest() for name, track in tracks.items()
                }
            try:
                self._write_manifest_atomic(self._manifest_data())
            except Exception as manifest_exc:  # noqa: BLE001 - bounded diagnostics
                with self._lock:
                    self._writer_error += (
                        "; manifest_write_failed="
                        f"{type(manifest_exc).__name__}: {manifest_exc}"
                    )
        finally:
            self._ready.set()
            for track in tracks.values():
                try:
                    track.handle.close()
                except OSError:
                    pass

    def _open_track(self, spec: AudioTrackSpec) -> _OpenTrack:
        path = self.directory / f"{spec.name}.wav"
        if not path.exists():
            with path.open("wb") as handle:
                handle.write(_wav_header(spec.sample_rate, 0))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(path, 0o600)
        else:
            os.chmod(path, 0o600)
        handle = path.open("r+b", buffering=0)
        _validate_wav_header(handle.read(_WAV_HEADER_BYTES), spec)
        actual_pcm_bytes = max(0, path.stat().st_size - _WAV_HEADER_BYTES)
        aligned_pcm_bytes = actual_pcm_bytes - (actual_pcm_bytes % _PCM_SAMPLE_BYTES)
        if aligned_pcm_bytes != actual_pcm_bytes:
            handle.truncate(_WAV_HEADER_BYTES + aligned_pcm_bytes)
        digest = hashlib.sha256()
        handle.seek(_WAV_HEADER_BYTES)
        remaining = aligned_pcm_bytes
        while remaining:
            block = handle.read(min(1024 * 1024, remaining))
            if not block:
                raise RuntimeError(f"audio track ended during recovery: {spec.name}")
            digest.update(block)
            remaining -= len(block)
        handle.seek(0)
        handle.write(_wav_header(spec.sample_rate, aligned_pcm_bytes))
        handle.seek(0, os.SEEK_END)
        return _OpenTrack(spec, path, handle, digest, aligned_pcm_bytes)

    @staticmethod
    def _sync_tracks(tracks: dict[str, _OpenTrack]) -> None:
        for track in tracks.values():
            track.handle.seek(0)
            track.handle.write(_wav_header(track.spec.sample_rate, track.pcm_bytes))
            track.handle.flush()
            os.fsync(track.handle.fileno())
            track.handle.seek(0, os.SEEK_END)

    def _manifest_data(self) -> dict[str, Any]:
        with self._lock:
            specs = dict(self._specs)
            enqueued = dict(self._enqueued_bytes)
            written = dict(self._written_bytes)
            dropped = dict(self._dropped_bytes)
            hashes = dict(self._track_hashes)
            metadata = dict(self._metadata)
            state = self._state
            writer_error = self._writer_error
            started_at = self._started_at
            closed_at = self._closed_at
            close_reason = self._close_reason
        tracks: dict[str, dict[str, Any]] = {}
        for name, spec in specs.items():
            pcm_bytes = written.get(name, 0)
            tracks[name] = {
                "path": f"{name}.wav",
                "format": "audio/wav",
                "encoding": "pcm_s16le",
                "channels": 1,
                "sample_rate": spec.sample_rate,
                "role": spec.role,
                "stage": spec.stage,
                "pcm_bytes": pcm_bytes,
                "samples": pcm_bytes // _PCM_SAMPLE_BYTES,
                "duration_ms": round(
                    pcm_bytes / (_PCM_SAMPLE_BYTES * spec.sample_rate) * 1000,
                    3,
                ),
                "enqueued_bytes": enqueued.get(name, 0),
                "unwritten_bytes": max(
                    0,
                    enqueued.get(name, 0) - pcm_bytes,
                ),
                "dropped_bytes": dropped.get(name, 0),
                "sha256_pcm": hashes.get(name, ""),
            }
        return {
            "schema_version": AUDIO_ARCHIVE_SCHEMA_VERSION,
            "instance_id": self.store.instance_id,
            "episode_id": self.store.episode_id,
            "state": state,
            "reason": close_reason,
            "started_at": started_at,
            "closed_at": closed_at,
            "updated_at": _utc_now(),
            "events_path": "../events.jsonl",
            "writer_error": writer_error,
            "queue_max_chunks": self._queue.maxsize,
            "metadata": metadata,
            "tracks": tracks,
        }

    def _write_manifest_atomic(self, data: dict[str, Any]) -> None:
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.manifest_path)
        directory_fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


__all__ = ["AudioTrackSpec", "VoiceAudioArchive"]
