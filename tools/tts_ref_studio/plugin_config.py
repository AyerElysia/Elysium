"""读取 / 回写 tts_voice_plugin 的配置。

读用 tomllib（只读、无依赖），写用 toml_patch.TomlValueEditor（保留注释）。
对外只暴露 studio 需要的几个概念：风格列表、TTS 服务地址、高级参数。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .toml_patch import TomlValueEditor

# 允许 studio 写回的字段白名单，避免界面上的手滑改坏模型路径之类的关键项。
PATCHABLE_KEYS = ("refer_wav_path", "aux_refer_wav_paths", "prompt_text", "prompt_language", "speed_factor")

STR_KEYS = ("refer_wav_path", "prompt_text", "prompt_language")
LIST_KEYS = ("aux_refer_wav_paths",)
FLOAT_KEYS = ("speed_factor",)


@dataclass
class StyleView:
    """一个 [[tts_styles]] 块在界面上的形态。"""

    index: int
    style_name: str
    name: str
    refer_wav_path: str
    aux_refer_wav_paths: list[str] = field(default_factory=list)
    prompt_text: str = ""
    prompt_language: str = "zh"
    gpt_weights: str = ""
    sovits_weights: str = ""
    speed_factor: float = 1.0
    text_language: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "style_name": self.style_name,
            "name": self.name,
            "refer_wav_path": self.refer_wav_path,
            "aux_refer_wav_paths": list(self.aux_refer_wav_paths),
            "prompt_text": self.prompt_text,
            "prompt_language": self.prompt_language,
            "gpt_weights": self.gpt_weights,
            "sovits_weights": self.sovits_weights,
            "speed_factor": self.speed_factor,
            "text_language": self.text_language,
        }


class PluginConfig:
    """插件配置的读写门面。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ---------------- 读 ----------------

    def raw(self) -> dict[str, Any]:
        if not self.path.exists():
            raise FileNotFoundError(f"插件配置不存在: {self.path}")
        with self.path.open("rb") as fp:
            return tomllib.load(fp)

    def styles(self) -> list[StyleView]:
        data = self.raw()
        out: list[StyleView] = []
        for idx, item in enumerate(data.get("tts_styles") or []):
            if not isinstance(item, dict):
                continue
            out.append(
                StyleView(
                    index=idx,
                    style_name=str(item.get("style_name") or f"style_{idx}"),
                    name=str(item.get("name") or item.get("style_name") or f"风格 {idx}"),
                    refer_wav_path=str(item.get("refer_wav_path") or ""),
                    aux_refer_wav_paths=[str(p) for p in (item.get("aux_refer_wav_paths") or []) if str(p).strip()],
                    prompt_text=str(item.get("prompt_text") or ""),
                    prompt_language=str(item.get("prompt_language") or "zh"),
                    gpt_weights=str(item.get("gpt_weights") or ""),
                    sovits_weights=str(item.get("sovits_weights") or ""),
                    speed_factor=float(item.get("speed_factor") or 1.0),
                    text_language=str(item.get("text_language") or "auto"),
                )
            )
        return out

    def find_style(self, style_name: str) -> StyleView:
        for style in self.styles():
            if style.style_name == style_name:
                return style
        raise KeyError(f"没有名为 {style_name} 的风格")

    def server_url(self) -> str:
        tts = self.raw().get("tts") or {}
        return str(tts.get("server") or "http://127.0.0.1:9880").rstrip("/")

    def advanced(self) -> dict[str, Any]:
        adv = self.raw().get("tts_advanced") or {}
        return {k: v for k, v in adv.items() if not isinstance(v, (dict, list))}

    # ---------------- 写 ----------------

    def patch_style(self, style_name: str, updates: dict[str, Any], backup_dir: Path | None = None) -> dict[str, Any]:
        """把 updates 写进指定风格，返回 {"changed": [...], "backup": path|None}。"""
        style = self.find_style(style_name)
        clean: dict[str, Any] = {}
        for key, value in updates.items():
            if key not in PATCHABLE_KEYS:
                continue
            if key in LIST_KEYS:
                if not isinstance(value, (list, tuple)):
                    raise ValueError(f"{key} 需要是数组")
                clean[key] = [str(v).strip() for v in value if str(v).strip()]
            elif key in FLOAT_KEYS:
                clean[key] = float(value)
            else:
                clean[key] = str(value)
        if not clean:
            return {"changed": [], "backup": None}

        editor = TomlValueEditor(self.path)
        for key, value in clean.items():
            editor.set_in_array_table("tts_styles", style.index, key, value)
        backup = editor.save(backup_dir=backup_dir)
        return {"changed": sorted(clean), "backup": str(backup) if backup else None}
