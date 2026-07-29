"""音频转码与裁剪导入。

两件事：
  1. 浏览器放不了的格式（wma/m4a/aac…）转成 ogg 临时流，前端才能试听；
  2. 把选中的片段按起止秒数裁出来，转成 GPT-SoVITS 喜欢的单声道 wav，
     落到 ref 目录，并把参考文本写到同名 .txt 旁边。

统一走 ffmpeg 子进程，参数都用列表传，不拼 shell 字符串。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

# 导入文件名只保留安全字符，避免路径穿越和奇怪的 shell 字符。
_UNSAFE_NAME = re.compile(r"[^\w一-鿿.\-]+")

# 这些容器帧长固定、时间戳和采样数对齐，可以直接按秒裁（快，不用整解码）。
SEEKABLE_EXTS = {".wav", ".flac", ".aiff", ".aif", ".w64", ".caf"}


class AudioToolError(RuntimeError):
    """ffmpeg 调用失败。"""


def _run(args: list[str], timeout: int = 300) -> None:
    proc = subprocess.run(args, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise AudioToolError("; ".join(tail[-3:]) or f"ffmpeg 退出码 {proc.returncode}")


def safe_stem(name: str, fallback: str = "clip") -> str:
    """清洗用户填的文件名，只留下文件名本身。"""
    stem = Path(name.strip()).stem
    stem = _UNSAFE_NAME.sub("_", stem).strip("._")
    return stem or fallback


def _cut_args(start: float, duration: float | None) -> tuple[list[str], list[str]]:
    """输入定位的 -ss / -t 参数，-ss 放在 -i 之前。"""
    pre = ["-ss", f"{start:.3f}"] if start > 0 else []
    post = ["-t", f"{duration:.3f}"] if duration is not None and duration > 0 else []
    return pre, post


def _extract(
    src: Path,
    out: Path,
    start: float,
    duration: float | None,
    encode_args: list[str],
) -> None:
    """从 src 裁出 [start, start+duration) 写到 out。

    崩三导出的 ogg 时间戳和实际采样数不一致（头里写 11.25s，整解码只有 9.37s），
    直接按秒裁会连带这个偏差，5.5s 能裁出 5.68s；顶到 10s 上限时就会超界。
    所以除了 wav/flac 这类时间戳可靠的容器，一律先整文件解码成临时 wav，
    再在临时 wav 上按秒裁 —— 两步都是采样精确的，结果误差为 0。
    这些素材单文件都是几秒到几分钟，多解一遍的代价可以忽略。
    """
    if src.suffix.lower() in SEEKABLE_EXTS:
        pre, post = _cut_args(start, duration)
        _run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *pre, "-i", str(src), *post,
              "-vn", *encode_args, str(out)])
        return

    fd, tmp_name = tempfile.mkstemp(prefix="ref_studio_dec_", suffix=".wav")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        # 第一步：整文件解码，不重采样也不并声道，保持原始质量。
        _run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
              "-vn", "-c:a", "pcm_s16le", str(tmp)])
        # 第二步：在时间戳可靠的 wav 上按秒裁。
        pre, post = _cut_args(start, duration)
        _run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *pre, "-i", str(tmp), *post,
              "-vn", *encode_args, str(out)])
    finally:
        tmp.unlink(missing_ok=True)


def transcode_to_ogg(src: Path, start: float | None = None, duration: float | None = None) -> Path:
    """转成 ogg 临时文件，供前端 <audio> 试听。调用方负责删除。"""
    src = Path(src)
    fd, tmp_name = tempfile.mkstemp(prefix="ref_studio_", suffix=".ogg")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        _extract(src, tmp, max(0.0, float(start or 0.0)), duration,
                 ["-c:a", "libvorbis", "-q:a", "4"])
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


@dataclass
class ImportResult:
    """裁剪导入的结果。"""

    path: str
    duration: float
    prompt_text: str
    sidecar: str | None


def _unique_path(directory: Path, stem: str, suffix: str = ".wav") -> Path:
    """避免覆盖已有参考音频，重名时自动加序号。"""
    candidate = directory / f"{stem}{suffix}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{n}{suffix}"
        n += 1
    return candidate


def import_clip(
    src: Path,
    dest_dir: Path,
    stem: str,
    start: float = 0.0,
    end: float | None = None,
    samplerate: int = 32000,
    prompt_text: str = "",
) -> ImportResult:
    """裁剪并导入一段参考音频。

    输出固定为单声道 16bit PCM wav —— GPT-SoVITS 内部也是这么重采样的，
    提前转好可以少一次隐式转换，也方便肉眼确认时长。
    """
    src = Path(src)
    if not src.is_file():
        raise AudioToolError(f"源文件不存在: {src}")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    start = max(0.0, float(start))
    duration: float | None = None
    if end is not None:
        duration = float(end) - start
        if duration <= 0:
            raise AudioToolError("结束时间必须大于开始时间")

    out = _unique_path(dest_dir, safe_stem(stem))
    try:
        _extract(src, out, start, duration,
                 ["-ac", "1", "-ar", str(int(samplerate)), "-c:a", "pcm_s16le"])
    except Exception:
        out.unlink(missing_ok=True)
        raise

    if not out.is_file() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise AudioToolError("裁剪结果为空，请检查起止时间")

    from .library import probe_audio

    try:
        actual = float(probe_audio(str(out)).get("duration") or 0.0)
    except Exception:
        actual = duration or 0.0

    sidecar: Path | None = None
    text = (prompt_text or "").strip()
    if text:
        sidecar = out.with_suffix(".txt")
        sidecar.write_text(text + "\n", encoding="utf-8")

    return ImportResult(
        path=str(out),
        duration=round(actual, 3),
        prompt_text=text,
        sidecar=str(sidecar) if sidecar else None,
    )


def read_sidecar_text(path: Path) -> str:
    """读取参考音频旁边的文本。

    优先同名 .txt；再退回目录里的 ref_text.txt / <stem>_text.txt 这类惯例命名。
    """
    path = Path(path)
    candidates = [
        path.with_suffix(".txt"),
        path.parent / f"{path.stem}_text.txt",
    ]
    for cand in candidates:
        if cand.is_file():
            try:
                return cand.read_text(encoding="utf-8").strip()
            except Exception:
                continue
    return ""
