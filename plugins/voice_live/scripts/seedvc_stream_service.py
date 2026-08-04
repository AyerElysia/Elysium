#!/usr/bin/env python3
"""Headless HTTP adapter for the GPLv3 Seed-VC realtime engine.

Elysium is AGPLv3, so Seed-VC's GPLv3 code is license-compatible with this
project.  The process boundary exists to isolate CUDA/model failures and to
let Seed-VC keep its dedicated Windows Python environment.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import secrets
import sys
import threading
import time
import uuid
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.voice_live.seedvc_profile import (
    InferenceTelemetry,
    build_profile_manifest,
    validate_runtime_settings,
)

os.environ.setdefault("OMP_NUM_THREADS", "4")
os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import librosa
import numpy as np
import torch
from scipy.signal import resample_poly
from torch.nn import functional


def _load_realtime_module(seedvc_root: Path, gpu: int) -> Any:
    source = seedvc_root / "real-time-gui.py"
    spec = importlib.util.spec_from_file_location("seedvc_realtime", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load Seed-VC realtime module: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not torch.cuda.is_available():
        raise RuntimeError("Seed-VC streaming service requires CUDA")
    module.device = torch.device(f"cuda:{gpu}")
    return module


def _resample_exact(
    samples: np.ndarray, source_rate: int, target_rate: int, length: int
) -> np.ndarray:
    converted = resample_poly(samples, target_rate, source_rate).astype(
        np.float32, copy=False
    )
    if converted.size < length:
        converted = np.pad(converted, (0, length - converted.size))
    return np.ascontiguousarray(converted[:length])


def _pcm16(samples: np.ndarray) -> bytes:
    finite = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.round(np.clip(finite, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class SeedVCStream:
    """Stateful block converter matching Seed-VC's realtime GUI algorithm."""

    def __init__(
        self,
        realtime: Any,
        model_set: Any,
        reference_path: Path,
        *,
        input_sample_rate: int,
        block_time: float,
        crossfade_time: float,
        extra_time_ce: float,
        extra_time: float,
        extra_time_right: float,
        diffusion_steps: int,
        inference_cfg_rate: float,
        max_prompt_length: float,
        silence_db: float,
        output_gain_db: float,
        on_inference: Callable[[float], None] | None = None,
    ) -> None:
        self.realtime = realtime
        self.model_set = model_set
        self.device = realtime.device
        self.reference_path = reference_path.resolve()
        self.input_sample_rate = input_sample_rate
        self.model_sample_rate = int(model_set[-1]["sampling_rate"])
        self.diffusion_steps = diffusion_steps
        self.inference_cfg_rate = inference_cfg_rate
        self.max_prompt_length = max_prompt_length
        self.silence_amplitude = 10.0 ** (silence_db / 20.0)
        self.output_gain = 10.0 ** (output_gain_db / 20.0)
        self.on_inference = on_inference
        self.reference_wav, _ = librosa.load(
            str(self.reference_path), sr=self.model_sample_rate, mono=True
        )

        self.zc = self.model_sample_rate // 50
        self.block_frame = (
            round(block_time * self.model_sample_rate / self.zc) * self.zc
        )
        self.input_block_frame = round(
            self.block_frame * input_sample_rate / self.model_sample_rate
        )
        self.block_frame_16k = 320 * self.block_frame // self.zc
        self.crossfade_frame = (
            round(crossfade_time * self.model_sample_rate / self.zc) * self.zc
        )
        self.sola_buffer_frame = min(self.crossfade_frame, 4 * self.zc)
        self.sola_search_frame = self.zc
        self.extra_frame = (
            round(extra_time_ce * self.model_sample_rate / self.zc) * self.zc
        )
        self.extra_frame_right = (
            round(extra_time_right * self.model_sample_rate / self.zc) * self.zc
        )
        self.skip_head = self.extra_frame // self.zc
        self.skip_tail = self.extra_frame_right // self.zc
        self.return_length = (
            self.block_frame + self.sola_buffer_frame + self.sola_search_frame
        ) // self.zc
        self.cd_difference = extra_time_ce - extra_time
        if self.cd_difference < 0:
            raise ValueError(
                "content-encoder context must not be shorter than DiT context"
            )

        total_frames = (
            self.extra_frame
            + self.crossfade_frame
            + self.sola_search_frame
            + self.block_frame
            + self.extra_frame_right
        )
        self.input_wav = torch.zeros(
            total_frames, device=self.device, dtype=torch.float32
        )
        self.input_wav_res = torch.zeros(
            320 * total_frames // self.zc,
            device=self.device,
            dtype=torch.float32,
        )
        self.sola_buffer = torch.zeros(
            self.sola_buffer_frame, device=self.device, dtype=torch.float32
        )
        self.fade_in = (
            torch.sin(
                0.5
                * math.pi
                * torch.linspace(
                    0.0,
                    1.0,
                    steps=self.sola_buffer_frame,
                    device=self.device,
                    dtype=torch.float32,
                )
            )
            ** 2
        )
        self.fade_out = 1.0 - self.fade_in
        self.pending = np.zeros(0, dtype=np.float32)
        self.block_count = 0
        self.inference_ms = 0.0

    def reset(self) -> None:
        self.input_wav.zero_()
        self.input_wav_res.zero_()
        self.sola_buffer.zero_()
        self.pending = np.zeros(0, dtype=np.float32)
        self.block_count = 0
        self.inference_ms = 0.0

    def push_pcm16(self, payload: bytes) -> tuple[bytes, dict[str, float | int]]:
        if len(payload) % 2:
            raise ValueError("PCM16 payload length must be even")
        incoming = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        if incoming.size:
            self.pending = np.concatenate((self.pending, incoming))
        outputs: list[bytes] = []
        request_ms = 0.0
        request_blocks = 0
        while self.pending.size >= self.input_block_frame:
            block = self.pending[: self.input_block_frame]
            self.pending = self.pending[self.input_block_frame :]
            converted, elapsed_ms = self._convert_block(block)
            outputs.append(_pcm16(converted))
            request_ms += elapsed_ms
            request_blocks += 1
        return b"".join(outputs), {
            "block_count": request_blocks,
            "inference_ms": round(request_ms, 3),
            "pending_samples": int(self.pending.size),
        }

    def flush(self) -> tuple[bytes, dict[str, float | int]]:
        if not self.pending.size:
            return b"", {
                "block_count": 0,
                "inference_ms": 0.0,
                "pending_samples": 0,
            }
        valid_input_samples = int(self.pending.size)
        block = np.pad(self.pending, (0, self.input_block_frame - valid_input_samples))
        self.pending = np.zeros(0, dtype=np.float32)
        converted, elapsed_ms = self._convert_block(block)
        valid_output_samples = round(
            valid_input_samples * self.model_sample_rate / self.input_sample_rate
        )
        return _pcm16(converted[:valid_output_samples]), {
            "block_count": 1,
            "inference_ms": round(elapsed_ms, 3),
            "pending_samples": 0,
        }

    @torch.no_grad()
    def _convert_block(self, input_block: np.ndarray) -> tuple[np.ndarray, float]:
        model_block = _resample_exact(
            input_block,
            self.input_sample_rate,
            self.model_sample_rate,
            self.block_frame,
        )
        self.input_wav[: -self.block_frame] = self.input_wav[self.block_frame :].clone()
        self.input_wav[-self.block_frame :] = torch.from_numpy(model_block).to(
            self.device
        )

        self.input_wav_res[: -self.block_frame_16k] = self.input_wav_res[
            self.block_frame_16k :
        ].clone()
        overlap_source = (
            self.input_wav[-self.block_frame - 2 * self.zc :].detach().cpu().numpy()
        )
        overlap_length = 320 * (self.block_frame // self.zc + 1)
        overlap_16k = resample_poly(
            overlap_source, 16000, self.model_sample_rate
        ).astype(np.float32, copy=False)[320:]
        if overlap_16k.size < overlap_length:
            overlap_16k = np.pad(overlap_16k, (0, overlap_length - overlap_16k.size))
        self.input_wav_res[-overlap_length:] = torch.from_numpy(
            np.ascontiguousarray(overlap_16k[:overlap_length])
        ).to(self.device)

        started = time.perf_counter()
        infer_wav = self.realtime.custom_infer(
            self.model_set,
            self.reference_wav,
            str(self.reference_path),
            self.input_wav_res,
            self.block_frame_16k,
            self.skip_head,
            self.skip_tail,
            self.return_length,
            self.diffusion_steps,
            self.inference_cfg_rate,
            self.max_prompt_length,
            self.cd_difference,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if self.on_inference is not None:
            self.on_inference(elapsed_ms)

        search = infer_wav[
            None, None, : self.sola_buffer_frame + self.sola_search_frame
        ]
        numerator = functional.conv1d(search, self.sola_buffer[None, None, :])
        denominator = torch.sqrt(
            functional.conv1d(
                search**2,
                torch.ones(
                    1,
                    1,
                    self.sola_buffer_frame,
                    device=self.device,
                    dtype=torch.float32,
                ),
            )
            + 1e-8
        )
        similarity = numerator[0, 0] / denominator[0, 0]
        offset = int(torch.argmax(similarity).item()) if similarity.numel() > 1 else 0
        infer_wav = infer_wav[offset:]
        infer_wav[: self.sola_buffer_frame] *= self.fade_in
        infer_wav[: self.sola_buffer_frame] += self.sola_buffer * self.fade_out
        self.sola_buffer[:] = infer_wav[
            self.block_frame : self.block_frame + self.sola_buffer_frame
        ]
        output = infer_wav[: self.block_frame]
        rms = float(np.sqrt(np.mean(np.square(input_block), dtype=np.float64)))
        if rms < self.silence_amplitude:
            output = torch.zeros_like(output)
            self.sola_buffer.zero_()
        elif self.output_gain != 1.0:
            output = output * self.output_gain

        self.block_count += 1
        self.inference_ms += elapsed_ms
        return output.detach().float().cpu().numpy(), elapsed_ms


def _read_token_file(configured_path: str) -> str:
    """Read one token value without exposing it in process arguments."""

    if not configured_path:
        return ""
    value = Path(configured_path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("Seed-VC token file is empty")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise RuntimeError("Seed-VC token file must contain exactly one text value")
    return value


class SeedVCRuntime:
    """Own the CUDA model and the bounded set of active conversion streams."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.token = (
            args.token
            or os.environ.get(args.token_env, "")
            or _read_token_file(args.token_file)
        )
        if not self.token:
            raise RuntimeError(
                f"Seed-VC service token is empty; set {args.token_env} "
                "or provide --token-file"
            )
        settings = {
            "input_sample_rate": args.input_sample_rate,
            "block_time": args.block_time,
            "crossfade_time": args.crossfade_time,
            "extra_time_ce": args.extra_time_ce,
            "extra_time": args.extra_time,
            "extra_time_right": args.extra_time_right,
            "diffusion_steps": args.diffusion_steps,
            "inference_cfg_rate": args.inference_cfg_rate,
            "max_prompt_length": args.max_prompt_length,
            "silence_db": args.silence_db,
            "output_gain_db": args.output_gain_db,
            "seed": args.seed,
        }
        validate_runtime_settings(settings)
        self.seedvc_root = Path(args.seedvc_root).resolve()
        if not (self.seedvc_root / "real-time-gui.py").is_file():
            raise FileNotFoundError(
                f"Seed-VC realtime entrypoint is missing: {self.seedvc_root}"
            )
        self.checkpoint_path = Path(args.checkpoint).resolve()
        self.config_path = Path(args.config).resolve()
        self.reference_path = Path(args.reference).resolve()
        self.profile_manifest = build_profile_manifest(
            profile_id=args.profile_id,
            checkpoint_path=self.checkpoint_path,
            config_path=self.config_path,
            reference_path=self.reference_path,
            settings=settings,
        )
        if str(self.seedvc_root) not in sys.path:
            sys.path.insert(0, str(self.seedvc_root))
        os.chdir(self.seedvc_root)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed_all(args.seed)
        self.realtime = _load_realtime_module(self.seedvc_root, args.gpu)
        self.model_set = self.realtime.load_models(
            SimpleNamespace(
                checkpoint_path=str(self.checkpoint_path),
                config_path=str(self.config_path),
                fp16=args.fp16,
            )
        )
        self.model_lock = threading.Lock()
        self.session_lock = threading.Lock()
        self.sessions: dict[str, SeedVCStream] = {}
        self.inference_telemetry = InferenceTelemetry()
        self.started_at = time.time()
        self.warmup_ms = self._warmup()

    @property
    def output_sample_rate(self) -> int:
        return int(self.model_set[-1]["sampling_rate"])

    def _new_stream(self, *, record_telemetry: bool = True) -> SeedVCStream:
        return SeedVCStream(
            self.realtime,
            self.model_set,
            self.reference_path,
            input_sample_rate=self.args.input_sample_rate,
            block_time=self.args.block_time,
            crossfade_time=self.args.crossfade_time,
            extra_time_ce=self.args.extra_time_ce,
            extra_time=self.args.extra_time,
            extra_time_right=self.args.extra_time_right,
            diffusion_steps=self.args.diffusion_steps,
            inference_cfg_rate=self.args.inference_cfg_rate,
            max_prompt_length=self.args.max_prompt_length,
            silence_db=self.args.silence_db,
            output_gain_db=self.args.output_gain_db,
            on_inference=(
                self.inference_telemetry.record if record_telemetry else None
            ),
        )

    def _warmup(self) -> float:
        stream = self._new_stream(record_telemetry=False)
        count = stream.input_block_frame
        source_length = round(
            count * stream.model_sample_rate / stream.input_sample_rate
        )
        source = stream.reference_wav[:source_length]
        warm = _resample_exact(
            source, stream.model_sample_rate, stream.input_sample_rate, count
        )
        started = time.perf_counter()
        with self.model_lock:
            stream._convert_block(warm)
        torch.cuda.synchronize()
        return round((time.perf_counter() - started) * 1000.0, 3)

    def create_session(self, profile_id: str) -> tuple[str, SeedVCStream]:
        if profile_id != self.args.profile_id:
            raise ValueError(f"unknown voice profile: {profile_id}")
        with self.session_lock:
            if self.sessions:
                raise RuntimeError("Seed-VC stream capacity reached")
            torch.manual_seed(self.args.seed)
            torch.cuda.manual_seed_all(self.args.seed)
            session_id = uuid.uuid4().hex[:16]
            stream = self._new_stream()
            self.sessions[session_id] = stream
            return session_id, stream

    def health_snapshot(self) -> dict[str, Any]:
        """Return traceable profile identity and realtime capacity state."""

        block_time_ms = self.args.block_time * 1000.0
        return {
            "status": "ok",
            "protocol_version": 2,
            "profile_id": self.args.profile_id,
            "profile_revision": self.profile_manifest["revision"],
            "asset_fingerprints": self.profile_manifest["assets"],
            "runtime_settings": self.profile_manifest["settings"],
            "input_sample_rate": self.args.input_sample_rate,
            "output_sample_rate": self.output_sample_rate,
            "input_block_samples": round(
                self.args.input_sample_rate * self.args.block_time
            ),
            "block_time_ms": round(block_time_ms),
            "algorithmic_latency_floor_ms": round(
                (2.0 * self.args.block_time + self.args.extra_time_right) * 1000.0
            ),
            "warmup_ms": self.warmup_ms,
            "active_sessions": len(self.sessions),
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "device": str(self.realtime.device),
            "inference": self.inference_telemetry.snapshot(
                block_time_ms=block_time_ms
            ),
        }

    def get_session(self, session_id: str) -> SeedVCStream:
        with self.session_lock:
            stream = self.sessions.get(session_id)
        if stream is None:
            raise KeyError(f"unknown Seed-VC session: {session_id}")
        return stream

    def delete_session(self, session_id: str) -> None:
        with self.session_lock:
            self.sessions.pop(session_id, None)


class SeedVCHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SeedVCStream/1.0"

    @property
    def runtime(self) -> SeedVCRuntime:
        return self.server.runtime  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            f"{self.log_date_time_string()} {self.client_address[0]} {fmt % args}",
            flush=True,
        )

    def _authorized(self) -> bool:
        expected = f"Bearer {self.runtime.token}"
        return secrets.compare_digest(self.headers.get("Authorization", ""), expected)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > self.runtime.args.max_request_bytes:
            raise ValueError("request body exceeds configured limit")
        return self.rfile.read(length)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_audio(self, payload: bytes, metrics: dict[str, float | int]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/L16")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Output-Sample-Rate", str(self.runtime.output_sample_rate))
        self.send_header("X-Block-Count", str(metrics["block_count"]))
        self.send_header("X-Inference-Ms", str(metrics["inference_ms"]))
        self.send_header("X-Pending-Samples", str(metrics["pending_samples"]))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: int, exc: Exception) -> None:
        self._send_json(status, {"error": str(exc) or type(exc).__name__})

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send_json(HTTPStatus.OK, self.runtime.health_snapshot())

    def do_POST(self) -> None:
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid service token"})
            return
        path = urlsplit(self.path).path
        try:
            if path == "/v1/sessions":
                body = self._read_body()
                data = json.loads(body or b"{}")
                profile_id = str(data.get("profile_id") or "")
                session_id, stream = self.runtime.create_session(profile_id)
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "session_id": session_id,
                        "profile_id": profile_id,
                        "profile_revision": self.runtime.profile_manifest["revision"],
                        "input_sample_rate": stream.input_sample_rate,
                        "output_sample_rate": stream.model_sample_rate,
                        "input_block_samples": stream.input_block_frame,
                    },
                )
                return
            parts = [part for part in path.split("/") if part]
            if len(parts) != 4 or parts[:2] != ["v1", "sessions"]:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            session_id, operation = parts[2], parts[3]
            stream = self.runtime.get_session(session_id)
            if operation == "audio":
                payload = self._read_body()
                content_rate = int(
                    self.headers.get(
                        "X-Input-Sample-Rate", str(stream.input_sample_rate)
                    )
                )
                if content_rate != stream.input_sample_rate:
                    raise ValueError(
                        f"input sample rate must be {stream.input_sample_rate}, "
                        f"got {content_rate}"
                    )
                with self.runtime.model_lock:
                    output, metrics = stream.push_pcm16(payload)
                self._send_audio(output, metrics)
                return
            if operation == "flush":
                self._read_body()
                with self.runtime.model_lock:
                    output, metrics = stream.flush()
                self._send_audio(output, metrics)
                return
            if operation == "reset":
                self._read_body()
                with self.runtime.model_lock:
                    stream.reset()
                self._send_json(HTTPStatus.OK, {"status": "reset"})
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except KeyError as exc:
            self._error(HTTPStatus.NOT_FOUND, exc)
        except RuntimeError as exc:
            self._error(HTTPStatus.CONFLICT, exc)
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, exc)
        except Exception as exc:  # noqa: BLE001 - HTTP boundary must return JSON
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, exc)

    def do_DELETE(self) -> None:
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid service token"})
            return
        parts = [part for part in urlsplit(self.path).path.split("/") if part]
        if len(parts) != 3 or parts[:2] != ["v1", "sessions"]:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self.runtime.delete_session(parts[2])
        self._send_json(HTTPStatus.OK, {"status": "deleted"})


class SeedVCHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], runtime: SeedVCRuntime) -> None:
        self.runtime = runtime
        super().__init__(address, SeedVCHandler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seedvc-root", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--profile-id", default="elysia")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=17861)
    parser.add_argument("--token", default="")
    parser.add_argument("--token-env", default="SEEDVC_STREAM_TOKEN")
    parser.add_argument("--token-file", default="")
    parser.add_argument("--gpu", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--fp16", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--input-sample-rate", type=int, default=24000)
    parser.add_argument("--block-time", type=float, default=0.30)
    parser.add_argument("--crossfade-time", type=float, default=0.04)
    parser.add_argument("--extra-time-ce", type=float, default=2.5)
    parser.add_argument("--extra-time", type=float, default=0.5)
    parser.add_argument("--extra-time-right", type=float, default=0.02)
    parser.add_argument("--diffusion-steps", type=int, default=10)
    parser.add_argument("--inference-cfg-rate", type=float, default=0.7)
    parser.add_argument("--max-prompt-length", type=float, default=3.0)
    parser.add_argument("--silence-db", type=float, default=-70.0)
    parser.add_argument("--output-gain-db", type=float, default=-3.0)
    parser.add_argument("--max-request-bytes", type=int, default=2 * 1024 * 1024)
    return parser


def main() -> None:
    args = _parser().parse_args()
    runtime = SeedVCRuntime(args)
    server = SeedVCHTTPServer((args.bind, args.port), runtime)
    print(
        json.dumps(
            {
                "status": "ready",
                "address": f"http://{args.bind}:{args.port}",
                "profile_id": args.profile_id,
                "profile_revision": runtime.profile_manifest["revision"],
                "input_sample_rate": args.input_sample_rate,
                "output_sample_rate": runtime.output_sample_rate,
                "warmup_ms": runtime.warmup_ms,
                "device": str(runtime.realtime.device),
                "diffusion_steps": args.diffusion_steps,
                "block_time_ms": round(args.block_time * 1000),
                "inference_cfg_rate": args.inference_cfg_rate,
                "output_gain_db": args.output_gain_db,
                "seed": args.seed,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
