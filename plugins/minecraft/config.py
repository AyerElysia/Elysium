"""Independent Minecraft plugin configuration."""
from __future__ import annotations

from typing import ClassVar, Self
from pydantic import field_validator, model_validator
from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class MinecraftConfig(BaseConfig):
    """Own game configuration separately from the shared consciousness substrate."""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "Minecraft 具身体验插件"

    @config_section("settings")
    class SettingsSection(SectionBase):
        """Minecraft 具身体验配置。"""

        enabled: bool = Field(
            default=False,
            description="是否启用 Minecraft 具身体验。",
        )

        evidence_max_result_bytes: int = Field(
            default=8192, ge=2048, le=65536,
            description="Minecraft 单次对话证据投影的 UTF-8 硬字节上限。",
        )

        java_path: str = Field(
            default="java",
            description="Java 可执行文件路径。",
        )

        mc_version: str = Field(
            default="1.21.1",
            description="Minecraft 版本。",
        )

        world_name: str = Field(
            default="Elysian Realm",
            description="专用存档名称。",
        )

        mc_home: str = Field(
            default="/mnt/g/Game/Minecraft/.minecraft",
            description="Exact WSL path of the managed Minecraft home.",
        )

        launch_bat: str = Field(
            default=r"G:\Game\Minecraft\PCL\LaunchElysia.bat",
            description="Exact Windows launch script for the managed client.",
        )

        launch_dir: str = Field(
            default=r"G:\Game\Minecraft\PCL",
            description="Exact Windows working directory for the launch script.",
        )

        window_width: int = Field(
            default=1280,
            ge=640,
            le=3840,
            description="游戏窗口宽度。",
        )

        window_height: int = Field(
            default=720,
            ge=360,
            le=2160,
            description="游戏窗口高度。",
        )

        consciousness_interval_seconds: float = Field(
            default=6.0,
            ge=2.0,
            le=30.0,
            description=(
                "兼容旧配置；专属 Minecraft 意识不使用固定轮询间隔，"
                "每轮由模型在技术上下限内选择复思时间。"
            ),
        )

        consciousness_enabled: bool = Field(
            default=True,
            description="会话就绪后是否启动独立 Minecraft 场景意识。",
        )

        consciousness_task_name: str = Field(
            default="agent",
            min_length=1,
            description="Minecraft 场景意识使用的模型任务名。",
        )

        consciousness_subject_context_max_bytes: int = Field(
            default=8192,
            ge=8192,
            le=65536,
            description="每个 MC episode 固定主体投影的 UTF-8 字节上限。",
        )

        consciousness_observation_max_bytes: int = Field(
            default=8192,
            ge=4096,
            le=65536,
            description="每轮结构化游戏观察投影的 UTF-8 字节上限。",
        )

        consciousness_subconscious_max_bytes: int = Field(
            default=4096,
            ge=1024,
            le=32768,
            description="每轮只读近期潜意识投影的 UTF-8 字节上限。",
        )

        consciousness_subconscious_group_limit: int = Field(
            default=3,
            ge=1,
            le=20,
            description="每轮近期潜意识因果组上限。",
        )

        consciousness_min_wait_seconds: float = Field(
            default=2.0,
            gt=0,
            le=30.0,
            description="模型可选择的最短复思等待时间。",
        )

        consciousness_max_wait_seconds: float = Field(
            default=45.0,
            ge=2.0,
            le=300.0,
            description="模型可选择的最长复思等待时间。",
        )

        consciousness_retry_base_seconds: float = Field(
            default=2.0,
            gt=0,
            le=30.0,
            description="场景意识暂时失败后的基础退避。",
        )

        consciousness_retry_max_seconds: float = Field(
            default=30.0,
            ge=2.0,
            le=300.0,
            description="场景意识暂时失败后的最大退避。",
        )

        consciousness_recent_turn_limit: int = Field(
            default=4,
            ge=1,
            le=24,
            description="下一轮携带的有界 MC 结果摘要数量。",
        )

        consciousness_stop_timeout_seconds: float = Field(
            default=10.0,
            gt=0,
            le=60.0,
            description="关闭场景意识任务的技术宽限时间。",
        )

        vla_model: str = Field(
            default="bytedance-research/UI-TARS-7B-SFT",
            description="VLA 模型名称或路径。",
        )

        vla_fps: int = Field(
            default=6,
            ge=1,
            le=30,
            description="VLA 闭环帧率。",
        )

        vla_timeout_seconds: float = Field(
            default=30.0,
            ge=5.0,
            le=120.0,
            description="单个意图最大执行时间（秒）。",
        )

        max_session_minutes: int = Field(
            default=60,
            ge=5,
            le=240,
            description="最大会话时长（分钟）。",
        )

        reflex_enabled: bool = Field(
            default=True,
            description="是否启用 Reflex 保护层。",
        )

        offline_username: str = Field(
            default="AyerElysia",
            description=(
                "人类玩家离线模式用户名；必须与 agent_shared_username / "
                "bot_username 不同。"
            ),
        )

        default_body: str = Field(
            default="bot",
            pattern=r"^(agent|bot|biomimetic)$",
            description="Explicit Minecraft body selected when start omits body_name.",
        )

        agent_bridge_uri: str = Field(
            default="ws://127.0.0.1:18768/elysium",
            description="Authenticated NeoForge executor bridge URI.",
        )

        agent_bridge_listen_uri: str | None = Field(
            default="ws://127.0.0.1:18768/elysium",
            description="WSL listener for the outbound Windows agent relay.",
        )

        agent_token_file: str = Field(
            default="/mnt/g/Game/Minecraft/ElysiaClient/config/elysium_bridge.json",
            description="NeoForge bridge configuration containing its generated token.",
        )

        biomimetic_bridge_uri: str = Field(
            default="ws://host.docker.internal:8766/elysium",
            description="Authenticated first-person native-input sidecar URI.",
        )

        biomimetic_bridge_listen_uri: str | None = Field(
            default="ws://127.0.0.1:18766/elysium",
            description="WSL listener for the outbound Windows native-body relay.",
        )

        biomimetic_token_file: str = Field(
            default="/mnt/g/Game/Minecraft/.minecraft/config/elysium_native_bridge.json",
            description="Native sidecar configuration containing its generated token.",
        )

        bot_bridge_uri: str = Field(
            default="ws://127.0.0.1:18767/elysium",
            description="Fallback URI for the headless bot body bridge.",
        )

        bot_bridge_listen_uri: str | None = Field(
            default="ws://127.0.0.1:18767/elysium",
            description="WSL listener for the outbound headless bot body relay.",
        )

        bot_token_file: str = Field(
            default="minecraft/bot_bridge_token.json",
            description="Workspace-relative token file generated for the bot body.",
        )

        bot_server_host: str = Field(
            default="auto",
            min_length=1,
            description=(
                'Minecraft server or LAN host the bot body joins; "auto" '
                "resolves the WSL default gateway at launch time."
            ),
        )

        bot_server_port: int = Field(
            default=25565,
            ge=1,
            le=65535,
            description="Minecraft server or LAN port the bot body joins.",
        )

        bot_username: str = Field(
            default="Elysia",
            pattern=r"^[A-Za-z0-9_]{1,16}$",
            description=(
                "In-game account name for the headless bot body; must differ "
                "from the human player's account name."
            ),
        )

        bot_observation_interval_ms: int = Field(
            default=1000,
            gt=0,
            description="Bot observation snapshot cadence in milliseconds.",
        )

        bot_entity_radius_blocks: int = Field(
            default=32,
            gt=0,
            description="Bot entity sensor radius in blocks.",
        )

        shared_world_enabled: bool = Field(
            default=True,
            description=(
                "Her own client window joins the human player's LAN world, "
                "giving her a true first-person view; disabled falls back to "
                "the configured singleplayer world."
            ),
        )

        agent_shared_username: str = Field(
            default="Elysia",
            pattern=r"^[A-Za-z0-9_]{1,16}$",
            description=(
                "In-game account name of her own client in the shared world; "
                "must differ from the human player's account name."
            ),
        )

        agent_game_directory: str = Field(
            default=r"G:\Game\Minecraft\ElysiaClient",
            description="Windows directory exclusively owned by the independent native client.",
        )
        agent_launch_bat: str = Field(
            default=r"G:\Game\Minecraft\ElysiaClient\LaunchElysia.bat",
            description="Prepared isolated launch script; must not contain the human account credentials.",
        )
        agent_expected_bridge_version: str = Field(default="0.3.0")
        agent_bridge_mod_filename: str = Field(default="elysium_bridge-0.3.0.jar")
        agent_expected_bridge_sha256: str = Field(
            default="02765CCD262ADAE3DA2D6EE9AC2323E5B151F20773FF3687D3C73D5DD108AEFE"
        )

        game_turn_interval_seconds: int = Field(
            default=5,
            ge=1,
            description=(
                "兼容旧配置；不再改变核心心跳频率。专属 Minecraft 意识"
                "通过独立任务和模型选择的复思时间维持游玩节奏。"
            ),
        )

        planner_task_name: str = Field(
            default="agent",
            description="Configured Elysium model task used for game execution planning.",
        )

        bridge_ready_timeout_seconds: float = Field(
            default=240.0,
            gt=0,
            description="Technical launch deadline for the selected body endpoint.",
        )

        world_ready_timeout_seconds: float = Field(
            default=120.0,
            gt=0,
            description="Deadline for a playable world and advancing observations.",
        )

        require_quick_play: bool = Field(
            default=True,
            description="Require the launch script to enter the exact configured world.",
        )

        expected_bridge_version: str = Field(
            default="0.2.1",
            min_length=1,
            description="Exact authenticated NeoForge bridge build version.",
        )

        bridge_mod_filename: str = Field(
            default="elysium_bridge-0.2.1.jar",
            min_length=1,
            description="Exact selected NeoForge bridge artifact filename.",
        )

        expected_bridge_sha256: str = Field(
            default=(
                "F6B80E166F8C3EDA683020C8154D817DA3098873AE9ECDF6161F05C8FF8A50DC"
            ),
            pattern=r"^[A-Fa-f0-9]{64}$",
            description="Pinned SHA-256 for the selected NeoForge bridge artifact.",
        )

        baritone_mod_filename: str = Field(
            default="baritone-unoptimized-neoforge-1.11.2.jar",
            min_length=1,
            description="Exact official Baritone NeoForge artifact filename.",
        )

        expected_baritone_sha256: str = Field(
            default=(
                "B413CE0A2754A3C8484AAE39875CF84BE1F999DEE208E86D41B3D0D329D5CA35"
            ),
            pattern=r"^[A-Fa-f0-9]{64}$",
            description="Pinned SHA-256 for the official Baritone artifact.",
        )

        intent_timeout_seconds: float | None = Field(
            default=300.0,
            gt=0,
            description="Optional caller-owned lifetime for one game intention.",
        )

        @field_validator("intent_timeout_seconds", mode="before")
        @classmethod
        def normalize_disabled_intent_timeout(cls, value: object) -> object:
            """Treat TOML's generated zero sentinel as an unset timeout."""

            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and float(value) == 0.0
            ):
                return None
            return value

        @model_validator(mode="after")
        def native_identities_must_not_reuse_human_account(self) -> Self:
            """Fail closed before start when enabled bodies share the human name."""

            if not self.enabled:
                return self
            human = str(self.offline_username or "").strip()
            agent = str(self.agent_shared_username or "").strip()
            bot = str(self.bot_username or "").strip()
            if human and agent and human == agent:
                raise ValueError(
                    "native client must not reuse the human account name"
                )
            if human and bot and human == bot:
                raise ValueError(
                    "bot username must not reuse the human account name"
                )
            return self

    settings: SettingsSection = Field(default_factory=SettingsSection)
