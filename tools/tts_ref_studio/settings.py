"""Studio 自身的设置：素材库根目录、插件配置路径、导入参数。

设置文件放在 data/tts_ref_studio/settings.json，首次启动时按本机实际存在的
目录自动生成，之后可以在界面里增删素材库。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .library import LibraryRoot

# tools/tts_ref_studio/settings.py -> Elysium/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data" / "tts_ref_studio"
SETTINGS_PATH = DATA_DIR / "settings.json"
CACHE_PATH = DATA_DIR / "duration_cache.json"
# 每次写插件配置前先在这里留一份带时间戳的副本。
BACKUP_DIR = DATA_DIR / "config_backups"

DEFAULT_PLUGIN_CONFIG = PROJECT_ROOT / "config" / "plugins" / "tts_voice_plugin" / "config.toml"
DEFAULT_REF_DIR = Path("/root/Elysia/GPT-SoVITS/ref")

# 候选素材库；只有真实存在的才会写进默认设置。
CANDIDATE_ROOTS: list[dict[str, str]] = [
    {
        "key": "ref",
        "label": "当前参考音频库",
        "path": "/root/Elysia/GPT-SoVITS/ref",
        "note": "GPT-SoVITS 直接读取的目录，裁剪导入的片段也落在这里",
    },
    {
        "key": "labeled",
        "label": "已分类语音（爱莉/侵蚀/其余）",
        "path": "/mnt/d/Material/Elysia/音频",
        "note": "人工标注过的角色语音，挑主参考的首选",
    },
    {
        "key": "raw_ogg",
        "label": "崩坏三原始语音包",
        "path": "/mnt/g/BH3_voice_ogg",
        "note": "未分类的原始 ogg 语音，按活动分目录",
    },
    {
        "key": "longform",
        "label": "台词长音频与旧素材",
        "path": "/mnt/d/Material/Elysia/台词音频语录",
        "note": "合集.wav 等长音频，需要裁剪后才能作主参考",
    },
]


@dataclass
class StudioSettings:
    """Studio 运行设置。"""

    host: str = "127.0.0.1"
    port: int = 9881
    plugin_config_path: str = str(DEFAULT_PLUGIN_CONFIG)
    ref_output_dir: str = str(DEFAULT_REF_DIR)
    tts_server: str = ""  # 留空则从插件配置的 [tts].server 读取
    import_samplerate: int = 32000
    roots: list[dict[str, str]] = field(default_factory=list)

    # ---------------- 载入 / 保存 ----------------

    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> "StudioSettings":
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
            known = {f for f in cls.__dataclass_fields__}
            inst = cls(**{k: v for k, v in raw.items() if k in known})
            if not inst.roots:
                inst.roots = cls._default_roots()
            return inst

        inst = cls(roots=cls._default_roots())
        inst.save(path)
        return inst

    @staticmethod
    def _default_roots() -> list[dict[str, str]]:
        return [dict(r) for r in CANDIDATE_ROOTS if Path(r["path"]).is_dir()]

    def save(self, path: Path = SETTINGS_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ---------------- 派生值 ----------------

    def library_roots(self) -> list[LibraryRoot]:
        """素材库列表。参考音频输出目录始终可访问，避免导入后看不到成品。"""
        roots = [
            LibraryRoot(
                key=str(r.get("key") or ""),
                label=str(r.get("label") or r.get("key") or ""),
                path=str(r.get("path") or ""),
                note=str(r.get("note") or ""),
            )
            for r in self.roots
            if r.get("key") and r.get("path")
        ]
        if all(Path(r.path) != Path(self.ref_output_dir) for r in roots):
            roots.insert(
                0,
                LibraryRoot(key="ref", label="参考音频输出目录", path=self.ref_output_dir),
            )
        return roots

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["roots"] = [LibraryRoot(**{k: str(v) for k, v in r.items()}).to_dict() for r in self.roots]
        return data
