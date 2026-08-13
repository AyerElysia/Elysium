#!/usr/bin/env python3
"""Read-only schema validation used by the deployment doctor.

The parent process deliberately discards this program's output so validation
libraries can never echo a secret-bearing input value into deployment logs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(root: Path = ROOT) -> int:
    previous_cwd = Path.cwd()
    os.chdir(root)
    try:
        from plugins.ayla_adapter.config import AylaAdapterConfig
        from plugins.commands_plugin.config import CommandsPluginConfig
        from plugins.emoji.config import EmojiConfig
        from plugins.feishu_adapter.config import FeishuAdapterConfig
        from plugins.kook_adapter.config import KookAdapterConfig
        from plugins.life_engine.core.config import LifeEngineConfig
        from plugins.livestream.config import LivestreamConfig
        from plugins.napcat_adapter.config import NapcatAdapterConfig
        from plugins.neko_surface.config import NekoSurfaceConfig
        from plugins.skill_manager.config import SkillManagerConfig
        from plugins.tts_voice_plugin.config import TTSVoiceConfig
        from plugins.voice_live.config import VoiceLiveConfig
        from plugins.werewolf_game.config import WerewolfConfig
        from src.core.config.core_config import CoreConfig
        from src.core.config.mcp_config import MCPConfig
        from src.kernel.config.models_loader import (
            PRODUCTION_MODEL_TASKS,
            ModelsConfig,
        )

        CoreConfig.load("config/core.toml", auto_update=False)
        MCPConfig.load("config/mcp.toml", auto_update=False)
        model_config = ModelsConfig("config/models.toml")
        model_config.require_tasks(PRODUCTION_MODEL_TASKS)
        model_config.require_runtime_readiness()

        plugin_configs = (
            (AylaAdapterConfig, "config/plugins/ayla_adapter/config.toml"),
            (CommandsPluginConfig, "config/plugins/commands_plugin/config.toml"),
            (EmojiConfig, "config/plugins/emoji/config.toml"),
            (FeishuAdapterConfig, "config/plugins/feishu_adapter/config.toml"),
            (KookAdapterConfig, "config/plugins/kook_adapter/config.toml"),
            (LifeEngineConfig, "config/plugins/life_engine/config.toml"),
            (LivestreamConfig, "config/plugins/Livestream/config.toml"),
            (NapcatAdapterConfig, "config/plugins/napcat_adapter/config.toml"),
            (NekoSurfaceConfig, "config/plugins/neko_surface/config.toml"),
            (SkillManagerConfig, "config/plugins/skill_manager/config.toml"),
            (TTSVoiceConfig, "config/plugins/tts_voice_plugin/config.toml"),
            (VoiceLiveConfig, "config/plugins/Voice-Live/config.toml"),
            (WerewolfConfig, "config/plugins/werewolf_game/config.toml"),
        )
        for config_type, path in plugin_configs:
            config_type.load(path, auto_update=False)
    except Exception:  # noqa: BLE001 - all schema failures share a redacted exit code
        # Never print exception text: pydantic/tomllib diagnostics may contain
        # the rejected input value. The parent reports only this exit status.
        return 2
    finally:
        os.chdir(previous_cwd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
