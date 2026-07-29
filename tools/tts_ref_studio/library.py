"""音频素材库：目录浏览、时长探测与缓存。

GPT-SoVITS 的主参考音频必须落在 3~10 秒区间，所以浏览素材时"时长"是第一等信息。
逐个文件调用 soundfile/ffprobe 很慢（爱莉希雅的素材有数千条），这里用
(路径, mtime, size) 作为键做持久化缓存，只在文件变动时重新探测。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import soundfile as sf
except Exception:  # pragma: no cover - 环境缺失时退化为纯 ffprobe
    sf = None  # type: ignore[assignment]

_FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

# ogg/opus 的容器 duration 可能是从比特率估算的（ffprobe 警告
# "Estimating duration from bitrate, this may be inaccurate"），
# 实际解码采样数和头里标称的时长最大可差 ~20%。
# 这类格式统一走整解码数采样数，得到精确时长。
_DECODE_EXTS = {".ogg", ".oga", ".opus"}

# 浏览器可直接播放的容器，其余（wma/wmv 等）需要转码后再回放。
AUDIO_EXTS = {
    ".wav",
    ".ogg",
    ".mp3",
    ".flac",
    ".m4a",
    ".aac",
    ".opus",
    ".wma",
    ".oga",
    ".mp4",
}
BROWSER_SAFE_EXTS = {".wav", ".ogg", ".oga", ".mp3", ".flac", ".m4a", ".aac", ".opus"}

# soundfile 读不了的格式交给 ffprobe。
SF_UNSUPPORTED = {".wma", ".m4a", ".aac", ".mp4", ".opus"}

MAIN_REF_MIN_SECONDS = 3.0
MAIN_REF_MAX_SECONDS = 10.0


@dataclass
class AudioInfo:
    """单条音频的元信息。"""

    path: str
    name: str
    size: int
    mtime: float
    duration: float | None = None
    samplerate: int | None = None
    channels: int | None = None
    error: str | None = None

    @property
    def main_ref_ok(self) -> bool:
        """是否可直接用作主参考音频。"""
        if self.duration is None:
            return False
        return MAIN_REF_MIN_SECONDS <= self.duration <= MAIN_REF_MAX_SECONDS

    def to_dict(self) -> dict[str, Any]:
        ext = Path(self.path).suffix.lower()
        return {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "mtime": self.mtime,
            "duration": self.duration,
            "samplerate": self.samplerate,
            "channels": self.channels,
            "error": self.error,
            "main_ref_ok": self.main_ref_ok,
            "needs_transcode": ext not in BROWSER_SAFE_EXTS,
            "ext": ext,
        }


def _probe_with_soundfile(path: str) -> dict[str, Any]:
    if sf is None:
        raise RuntimeError("soundfile 不可用")
    info = sf.info(path)
    if not info.samplerate:
        raise RuntimeError("采样率为 0")
    if not info.frames or info.frames <= 0:
        # 崩三导出的无头 ogg 常见：libsndfile 读不到总帧数，只能交给 ffprobe。
        raise RuntimeError("帧数为 0，交给 ffprobe")
    return {
        "duration": info.frames / float(info.samplerate),
        "samplerate": int(info.samplerate),
        "channels": int(info.channels),
    }


def _probe_with_ffprobe(path: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=duration,sample_rate,channels",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe 失败").strip()[:200])
    payload = json.loads(proc.stdout or "{}")
    streams = payload.get("streams") or [{}]
    stream = streams[0]
    duration = stream.get("duration") or (payload.get("format") or {}).get("duration")
    if duration is None:
        raise RuntimeError("ffprobe 未返回时长")
    return {
        "duration": float(duration),
        "samplerate": int(stream["sample_rate"]) if stream.get("sample_rate") else None,
        "channels": int(stream["channels"]) if stream.get("channels") else None,
    }


def _probe_with_decode(path: str) -> dict[str, Any]:
    """整解码数采样数，用于 ffprobe 可能估算时长的格式（ogg/opus）。

    先用 ffprobe 读取采样率和声道数（这些字段在这类文件里是准确的），
    再把音频流整体 pipe 成 s16le 并数字节，得到精确的采样帧数。
    """
    meta = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels",
            "-of", "json", path,
        ],
        capture_output=True, text=True, timeout=15,
    )
    stream: dict[str, Any] = {}
    if meta.returncode == 0:
        pl = json.loads(meta.stdout or "{}")
        stream = (pl.get("streams") or [{}])[0]
    samplerate = int(stream.get("sample_rate") or 32000)
    channels = int(stream.get("channels") or 1)

    proc = subprocess.run(
        [_FFMPEG, "-nostdin", "-v", "error", "-i", path,
         "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", str(samplerate), "-"],
        capture_output=True, timeout=120,
    )
    # returncode 可能非零但 stdout 有数据（管道被接收方关闭），只在没有数据时报错
    if not proc.stdout:
        raise RuntimeError(
            proc.stderr.decode("utf-8", "replace").strip()[:200] or "ffmpeg 解码失败"
        )
    samples = len(proc.stdout) // 2  # s16le = 2 bytes/sample
    if samples <= 0:
        raise RuntimeError("解码后采样数为 0")
    return {
        "duration": samples / samplerate,
        "samplerate": samplerate,
        "channels": channels,
    }


def probe_audio(path: str) -> dict[str, Any]:
    """探测音频时长/采样率/声道数。

    优先级：soundfile（快）→ _probe_with_decode（ogg/opus 头里时长不可信）→ ffprobe。
    """
    ext = Path(path).suffix.lower()
    errors: list[str] = []

    if ext not in SF_UNSUPPORTED and sf is not None:
        try:
            result = _probe_with_soundfile(path)
            if result["duration"] > 0:
                return result
            errors.append("soundfile: 时长为 0")
        except Exception as e:
            errors.append(f"soundfile: {e}")

    # ogg/opus 的 ffprobe 时长是按比特率估算的，精度差；整解码数采样数更可靠。
    if ext in _DECODE_EXTS:
        try:
            return _probe_with_decode(path)
        except Exception as e:
            errors.append(f"decode-probe: {e}")

    try:
        return _probe_with_ffprobe(path)
    except Exception as e:
        errors.append(f"ffprobe: {e}")
    raise RuntimeError("; ".join(errors))


class DurationCache:
    """(路径 → 时长信息) 的持久化缓存。

    键里带上 mtime 和 size，文件被替换后缓存自动失效。写盘做了节流，
    避免一次浏览触发上千次 IO。
    """

    def __init__(self, cache_path: Path, flush_every: int = 200) -> None:
        self.cache_path = cache_path
        self.flush_every = flush_every
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = 0
        self._load()

    def _load(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except Exception:
            # 缓存坏了不是致命问题，丢掉重建即可。
            self._data = {}

    def flush(self, force: bool = False) -> None:
        with self._lock:
            if not force and self._dirty < self.flush_every:
                return
            if self._dirty == 0 and not force:
                return
            snapshot = dict(self._data)
            self._dirty = 0
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.cache_path)
        except Exception:
            pass

    @staticmethod
    def _key(path: str, mtime: float, size: int) -> str:
        return f"{path}|{int(mtime)}|{size}"

    def get(self, path: str, mtime: float, size: int) -> dict[str, Any] | None:
        with self._lock:
            return self._data.get(self._key(path, mtime, size))

    def put(self, path: str, mtime: float, size: int, value: dict[str, Any]) -> None:
        with self._lock:
            self._data[self._key(path, mtime, size)] = value
            self._dirty += 1
        self.flush()


@dataclass
class LibraryRoot:
    """一个素材库根目录。"""

    key: str
    label: str
    path: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        p = Path(self.path)
        return {
            "key": self.key,
            "label": self.label,
            "path": self.path,
            "note": self.note,
            "exists": p.is_dir(),
        }


@dataclass
class BrowseResult:
    """一次目录浏览的结果。"""

    root_key: str
    rel_path: str
    dirs: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    total_files: int = 0
    probed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_key": self.root_key,
            "rel_path": self.rel_path,
            "dirs": self.dirs,
            "files": self.files,
            "total_files": self.total_files,
            "probed": self.probed,
        }


class AudioLibrary:
    """素材库：负责路径解析、目录浏览与批量时长探测。"""

    def __init__(self, roots: Iterable[LibraryRoot], cache: DurationCache, max_workers: int = 8) -> None:
        self._roots: dict[str, LibraryRoot] = {r.key: r for r in roots}
        self.cache = cache
        self.max_workers = max_workers

    # ---------------- 根目录管理 ----------------

    @property
    def roots(self) -> list[LibraryRoot]:
        return list(self._roots.values())

    def set_roots(self, roots: Iterable[LibraryRoot]) -> None:
        self._roots = {r.key: r for r in roots}

    def get_root(self, key: str) -> LibraryRoot:
        root = self._roots.get(key)
        if root is None:
            raise KeyError(f"未知素材库: {key}")
        return root

    # ---------------- 路径安全 ----------------

    def resolve(self, root_key: str, rel_path: str) -> Path:
        """把 (库, 相对路径) 解析为绝对路径，并阻止越界访问。"""
        root = self.get_root(root_key)
        base = Path(root.path).resolve()
        target = (base / (rel_path or "")).resolve()
        if target != base and base not in target.parents:
            raise ValueError("路径越界")
        return target

    def contains(self, abs_path: str) -> bool:
        """判断绝对路径是否位于任一素材库内（用于放行 /stream）。"""
        try:
            target = Path(abs_path).resolve()
        except Exception:
            return False
        for root in self._roots.values():
            try:
                base = Path(root.path).resolve()
            except Exception:
                continue
            if target == base or base in target.parents:
                return True
        return False

    # ---------------- 探测 ----------------

    def info_for(self, path: Path, probe: bool = True) -> AudioInfo:
        """取单个文件的元信息（优先命中缓存）。"""
        try:
            st = path.stat()
        except OSError as e:
            return AudioInfo(path=str(path), name=path.name, size=0, mtime=0.0, error=str(e))

        info = AudioInfo(path=str(path), name=path.name, size=st.st_size, mtime=st.st_mtime)
        cached = self.cache.get(str(path), st.st_mtime, st.st_size)
        if cached is not None:
            info.duration = cached.get("duration")
            info.samplerate = cached.get("samplerate")
            info.channels = cached.get("channels")
            info.error = cached.get("error")
            return info
        if not probe:
            return info

        try:
            probed = probe_audio(str(path))
            info.duration = probed.get("duration")
            info.samplerate = probed.get("samplerate")
            info.channels = probed.get("channels")
        except Exception as e:
            info.error = str(e)
        self.cache.put(
            str(path),
            st.st_mtime,
            st.st_size,
            {
                "duration": info.duration,
                "samplerate": info.samplerate,
                "channels": info.channels,
                "error": info.error,
            },
        )
        return info

    def probe_many(self, paths: list[Path]) -> list[AudioInfo]:
        """并发探测一批文件。"""
        if not paths:
            return []
        workers = min(self.max_workers, len(paths))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda p: self.info_for(p, probe=True), paths))
        self.cache.flush(force=True)
        return results

    # ---------------- 浏览 ----------------

    def browse(
        self,
        root_key: str,
        rel_path: str = "",
        search: str = "",
        min_duration: float | None = None,
        max_duration: float | None = None,
        offset: int = 0,
        limit: int = 60,
        recursive: bool = False,
    ) -> BrowseResult:
        """列出目录内容。

        只有开启时长筛选时才会整目录探测，否则只探测当前页，保证大目录也能秒开。
        """
        base = self.resolve(root_key, rel_path)
        if not base.is_dir():
            raise NotADirectoryError(f"不是目录: {base}")

        dirs: list[dict[str, Any]] = []
        files: list[Path] = []

        if recursive:
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for fn in filenames:
                    if Path(fn).suffix.lower() in AUDIO_EXTS:
                        files.append(Path(dirpath) / fn)
        else:
            try:
                entries = sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError as e:
                raise NotADirectoryError(str(e)) from e
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    dirs.append(
                        {
                            "name": entry.name,
                            "rel_path": str(Path(rel_path or "") / entry.name).replace("\\", "/"),
                        }
                    )
                elif entry.suffix.lower() in AUDIO_EXTS:
                    files.append(entry)

        keyword = search.strip().lower()
        if keyword:
            files = [f for f in files if keyword in f.name.lower()]
            dirs = [d for d in dirs if keyword in d["name"].lower()] if not recursive else dirs

        files.sort(key=lambda p: p.name.lower())
        need_filter = min_duration is not None or max_duration is not None

        if need_filter:
            infos = self.probe_many(files)
            lo = min_duration if min_duration is not None else 0.0
            hi = max_duration if max_duration is not None else float("inf")
            infos = [i for i in infos if i.duration is not None and lo <= i.duration <= hi]
            total = len(infos)
            page = infos[offset : offset + limit]
        else:
            total = len(files)
            page = self.probe_many(files[offset : offset + limit])

        return BrowseResult(
            root_key=root_key,
            rel_path=rel_path,
            dirs=dirs,
            files=[i.to_dict() for i in page],
            total_files=total,
            probed=True,
        )
