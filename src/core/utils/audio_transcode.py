"""Audio format conversion helpers shared by adapters and media recognition."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def resolve_ffmpeg() -> str | None:
    """Return an available FFmpeg executable from PATH or imageio-ffmpeg."""
    command = shutil.which("ffmpeg")
    if command:
        return command
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, OSError, RuntimeError):
        return None


def transcode_audio_bytes(
    audio_bytes: bytes,
    *,
    output_suffix: str,
    codec_args: list[str],
) -> bytes:
    """Convert in-memory audio bytes with FFmpeg and return output bytes."""
    if not audio_bytes:
        raise ValueError("音频数据为空")
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "音频转码需要 FFmpeg；请安装项目依赖 imageio-ffmpeg，"
            "或将 ffmpeg 加入 PATH。"
        )

    normalized_suffix = output_suffix if output_suffix.startswith(".") else f".{output_suffix}"
    with tempfile.TemporaryDirectory(prefix="elysium_audio_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        input_path = tmp_path / "input_audio"
        output_path = tmp_path / f"output{normalized_suffix}"
        input_path.write_bytes(audio_bytes)
        result = subprocess.run(
            [ffmpeg, "-y", "-i", str(input_path), *codec_args, str(output_path)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0 or not output_path.is_file():
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"FFmpeg 音频转码失败: {detail[-1000:]}")
        return output_path.read_bytes()


def transcode_audio_to_wav(audio_bytes: bytes) -> bytes:
    """Convert arbitrary supported audio to mono 16 kHz PCM WAV for ASR."""
    return transcode_audio_bytes(
        audio_bytes,
        output_suffix=".wav",
        codec_args=["-acodec", "pcm_s16le", "-ac", "1", "-ar", "16000"],
    )


def transcode_audio_to_opus(audio_bytes: bytes) -> bytes:
    """Convert arbitrary supported audio to mono 16 kHz Opus for Feishu."""
    return transcode_audio_bytes(
        audio_bytes,
        output_suffix=".opus",
        codec_args=["-acodec", "libopus", "-ac", "1", "-ar", "16000"],
    )


def probe_audio_duration_ms(audio_bytes: bytes) -> int:
    """Return audio duration in milliseconds using FFmpeg diagnostics."""
    if not audio_bytes:
        raise ValueError("音频数据为空")
    ffmpeg = resolve_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "读取音频时长需要 FFmpeg；请安装项目依赖 imageio-ffmpeg，"
            "或将 ffmpeg 加入 PATH。"
        )
    with tempfile.TemporaryDirectory(prefix="elysium_audio_probe_") as tmp_dir:
        input_path = Path(tmp_dir) / "audio"
        input_path.write_bytes(audio_bytes)
        result = subprocess.run(
            [ffmpeg, "-i", str(input_path), "-f", "null", "-"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    detail = result.stderr.decode("utf-8", errors="replace")
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", detail)
    if not match:
        return 1
    hours, minutes, seconds = match.groups()
    total_seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return max(1, round(total_seconds * 1000))
