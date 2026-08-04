"""Minecraft 启动器：安装、启动、世界管理。

WSL2 环境：通过 cmd.exe 调用 Windows 端的 PCL2 启动脚本。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

from .win_bridge import WinBridge, get_bridge

logger = logging.getLogger("life_engine.minecraft.launcher")


@dataclass(slots=True)
class MCConfig:
    """MC 启动配置。"""

    java_path: str = "java"
    mc_version: str = "1.21.1"
    world_name: str = "Elysian Realm"
    window_width: int = 854
    window_height: int = 480
    # 窗口位置（右下角小窗，不干扰用户）
    window_x: int = 1700
    window_y: int = 1050
    offline_username: str = "AyerElysia"
    mc_home: Path = field(
        default_factory=lambda: Path("/mnt/g/Game/Minecraft/.minecraft")
    )
    # Windows 端启动脚本路径
    launch_bat: str = r"G:\Game\Minecraft\PCL\LaunchElysia.bat"
    launch_dir: str = r"G:\Game\Minecraft\PCL"
    extra_jvm_args: list[str] = field(default_factory=list)
    # 是否失焦时暂停渲染（False = 后台继续渲染）
    pause_on_lost_focus: bool = False
    default_body: str = "agent"
    agent_bridge_uri: str = "ws://host.docker.internal:8765/elysium"
    agent_bridge_listen_uri: str | None = "ws://127.0.0.1:18765/elysium"
    agent_token_file: Path = field(
        default_factory=lambda: Path(
            "/mnt/g/Game/Minecraft/.minecraft/config/elysium_bridge.json"
        )
    )
    biomimetic_bridge_uri: str = "ws://host.docker.internal:8766/elysium"
    biomimetic_bridge_listen_uri: str | None = "ws://127.0.0.1:18766/elysium"
    biomimetic_token_file: Path = field(
        default_factory=lambda: Path(
            "/mnt/g/Game/Minecraft/.minecraft/config/elysium_native_bridge.json"
        )
    )
    planner_task_name: str = "agent"
    bridge_ready_timeout_seconds: float = 240.0
    world_ready_timeout_seconds: float = 120.0
    intent_timeout_seconds: float | None = 300.0
    require_quick_play: bool = True
    expected_bridge_version: str = "0.2.1"
    bridge_mod_filename: str = "elysium_bridge-0.2.1.jar"
    expected_bridge_sha256: str = (
        "F6B80E166F8C3EDA683020C8154D817DA3098873AE9ECDF6161F05C8FF8A50DC"
    )
    baritone_mod_filename: str = "baritone-unoptimized-neoforge-1.11.2.jar"
    expected_baritone_sha256: str = (
        "B413CE0A2754A3C8484AAE39875CF84BE1F999DEE208E86D41B3D0D329D5CA35"
    )


@dataclass(slots=True)
class LaunchResult:
    """启动结果。"""

    success: bool
    pid: int | None = None
    window_title: str = "Minecraft"
    error: str = ""
    reused_existing: bool = False
    window: dict[str, Any] | None = None


class MinecraftLauncher:
    """Minecraft 启动与世界管理（WSL2 + Windows）。"""

    def __init__(self, config: MCConfig | None = None) -> None:
        self._cfg = config or MCConfig()
        self._bridge: WinBridge = get_bridge()
        self._running = False

    @property
    def is_running(self) -> bool:
        """MC 进程是否在运行。"""
        return self._running

    async def check_installation(self) -> dict[str, Any]:
        """检查 MC 安装状态。"""
        mc_home = self._cfg.mc_home
        launch_script = self._launch_script_path()
        result: dict[str, Any] = {
            "mc_home": str(mc_home),
            "exists": mc_home.exists(),
            "versions_dir": str(mc_home / "versions"),
            "has_version": False,
            "java_ok": True,  # Windows 端 Java 由 PCL 管理
            "launch_bat": self._cfg.launch_bat,
            "launch_script_path": str(launch_script),
            "world_path": str(self.get_world_path()),
            "world_exists": self.world_exists(),
            "quick_play_required": self._cfg.require_quick_play,
            "quick_play_configured": False,
            "bridge_mod_ready": False,
            "baritone_mod_ready": False,
        }

        # 检查版本
        version_dir = mc_home / "versions" / "neoforge-21.1.219"
        result["has_version"] = version_dir.exists()

        # Production startup must name the exact world.  A bridge at the title
        # screen is connectivity, not readiness.
        result["bat_exists"] = launch_script.exists()
        if launch_script.exists():
            try:
                content = launch_script.read_text(
                    encoding="utf-8-sig", errors="replace"
                )
            except OSError as exc:
                result["launch_script_error"] = str(exc)
            else:
                quick_play = re.search(
                    r"--quickPlaySingleplayer(?:=|\s+)(?:\"([^\"]+)\"|'([^']+)'|([^\s]+))",
                    content,
                    flags=re.IGNORECASE,
                )
                configured_world = (
                    next((part for part in quick_play.groups() if part), "")
                    if quick_play
                    else ""
                )
                result["quick_play_world"] = configured_world
                result["quick_play_configured"] = (
                    configured_world.casefold() == self._cfg.world_name.casefold()
                )

        # 检查是否有可用的版本
        versions_path = mc_home / "versions"
        if versions_path.exists():
            available = [d.name for d in versions_path.iterdir() if d.is_dir()]
            result["available_versions"] = available

        mods_path = mc_home / "mods"
        bridge_candidates = (
            sorted(mods_path.glob("elysium_bridge-*.jar")) if mods_path.is_dir() else []
        )
        baritone_candidates = (
            sorted(mods_path.glob("baritone*.jar")) if mods_path.is_dir() else []
        )
        result["bridge_mod_candidates"] = [item.name for item in bridge_candidates]
        result["baritone_mod_candidates"] = [item.name for item in baritone_candidates]
        if (
            len(bridge_candidates) == 1
            and bridge_candidates[0].name == self._cfg.bridge_mod_filename
        ):
            bridge_hash = await asyncio.to_thread(self._sha256, bridge_candidates[0])
            result["bridge_mod_sha256"] = bridge_hash
            result["bridge_mod_ready"] = (
                bridge_hash.casefold() == self._cfg.expected_bridge_sha256.casefold()
            )
        if (
            len(baritone_candidates) == 1
            and baritone_candidates[0].name == self._cfg.baritone_mod_filename
        ):
            baritone_hash = await asyncio.to_thread(
                self._sha256, baritone_candidates[0]
            )
            result["baritone_mod_sha256"] = baritone_hash
            result["baritone_mod_ready"] = (
                baritone_hash.casefold()
                == self._cfg.expected_baritone_sha256.casefold()
            )

        return result

    async def launch(self) -> LaunchResult:
        """启动 Minecraft（通过 cmd.exe 调用 Windows 端 bat）。"""
        # 确保 pauseOnLostFocus 设置正确
        existing = await self._bridge.find_window()
        if existing is not None:
            self._running = True
            raw_pid = existing.get("pid")
            pid = int(raw_pid) if raw_pid is not None else None
            return LaunchResult(
                success=True,
                pid=pid,
                window_title=str(existing.get("title") or "Minecraft"),
                reused_existing=True,
                window=dict(existing),
            )

        await asyncio.to_thread(self._configure_options)

        # Dispatch through an exact Windows-side helper to avoid WSL quoting ambiguity.
        try:
            dispatch_pid = await self._bridge.launch_minecraft(
                self._cfg.launch_bat,
                self._cfg.launch_dir,
            )
            logger.info(
                "Minecraft launch dispatched through Windows helper pid=%s",
                dispatch_pid,
            )
            self._running = True
            return LaunchResult(success=True, window_title="Minecraft NeoForge* 1.21.1")
        except (OSError, RuntimeError, TimeoutError) as exc:
            return LaunchResult(success=False, error=str(exc))

    def _configure_options(self) -> None:
        """配置 MC options.txt（pauseOnLostFocus 等）。"""
        options_file = self._cfg.mc_home / "options.txt"
        if not options_file.exists():
            return

        try:
            content = options_file.read_text(encoding="utf-8")
            target = (
                "pauseOnLostFocus:false"
                if not self._cfg.pause_on_lost_focus
                else "pauseOnLostFocus:true"
            )

            if "pauseOnLostFocus:" in content:
                # 替换现有设置
                import re

                content = re.sub(r"pauseOnLostFocus:\w+", target, content)
            else:
                # 添加设置
                content += f"\n{target}\n"

            options_file.write_text(content, encoding="utf-8")
            logger.info(f"MC options 已配置: {target}")
        except (OSError, UnicodeError) as exc:
            logger.warning(f"配置 MC options 失败: {exc}")

    async def wait_for_window(self, timeout: float = 120.0) -> dict[str, Any] | None:
        """等待 MC 窗口出现，并定位到指定位置。"""
        import time

        start = time.time()
        while time.time() - start < timeout:
            win = await self._bridge.find_window()
            if win:
                logger.info(f"MC 窗口已出现: hwnd={win['hwnd']}, {win['w']}x{win['h']}")
                # 定位窗口到指定位置（右下角小窗）
                await self._bridge.position_window(
                    self._cfg.window_x,
                    self._cfg.window_y,
                    self._cfg.window_width,
                    self._cfg.window_height,
                )
                # 更新窗口信息
                win = await self._bridge.find_window()
                return win
            await asyncio.sleep(3)
        return None

    async def stop(self) -> None:
        """Refuse to kill unrelated Java processes without an owned process id."""

        raise RuntimeError(
            "launcher does not own the Minecraft java process; close the exact client "
            "window or leave it running"
        )

    async def find_window(self) -> dict[str, Any] | None:
        """查找 MC 窗口。"""
        return await self._bridge.find_window()

    def get_world_path(self) -> Path:
        """获取世界存档路径。"""
        return self._cfg.mc_home / "saves" / self._cfg.world_name

    def world_exists(self) -> bool:
        """检查世界是否存在。"""
        return self.get_world_path().exists()

    def _launch_script_path(self) -> Path:
        """Translate one explicit Windows launch script into its WSL path."""

        windows_path = PureWindowsPath(self._cfg.launch_bat)
        drive = windows_path.drive.rstrip(":").casefold()
        if len(drive) != 1 or not drive.isalpha():
            raise ValueError(
                f"launch_bat must be an absolute drive path: {self._cfg.launch_bat}"
            )
        return Path(f"/mnt/{drive}", *windows_path.parts[1:])

    @staticmethod
    def _sha256(path: Path) -> str:
        """Hash one pinned deployment artifact without loading it all at once."""

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().upper()
