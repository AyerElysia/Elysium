"""Minecraft 启动器：安装、启动、世界管理。

WSL2 环境：通过 cmd.exe 调用 Windows 端的 PCL2 启动脚本。
"""

from __future__ import annotations

import asyncio
import logging
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
    mc_home: Path = field(default_factory=lambda: Path("/mnt/g/Game/Minecraft/.minecraft"))
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
    intent_timeout_seconds: float | None = None


@dataclass(slots=True)
class LaunchResult:
    """启动结果。"""

    success: bool
    pid: int | None = None
    window_title: str = "Minecraft"
    error: str = ""
    reused_existing: bool = False


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
        result: dict[str, Any] = {
            "mc_home": str(mc_home),
            "exists": mc_home.exists(),
            "versions_dir": str(mc_home / "versions"),
            "has_version": False,
            "java_ok": True,  # Windows 端 Java 由 PCL 管理
            "launch_bat": self._cfg.launch_bat,
        }

        # 检查版本
        version_dir = mc_home / "versions" / "neoforge-21.1.219"
        result["has_version"] = version_dir.exists()

        # 检查启动脚本
        bat_path = Path(self._cfg.launch_bat.replace("\\", "/").replace("G:", "/mnt/g"))
        result["bat_exists"] = bat_path.exists()

        # 检查是否有可用的版本
        versions_path = mc_home / "versions"
        if versions_path.exists():
            available = [d.name for d in versions_path.iterdir() if d.is_dir()]
            result["available_versions"] = available

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
            )

        await asyncio.to_thread(self._configure_options)

        # 通过 cmd.exe 启动 bat
        bat_name = PureWindowsPath(self._cfg.launch_bat).name
        launch_dir = self._cfg.launch_dir

        command_line = f'cd /D "{launch_dir}" && start "" "{bat_name}"'
        cmd = ["cmd.exe", "/d", "/s", "/c", command_line]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd="/mnt/c",  # 避免 UNC 路径问题
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode != 0:
                error = stderr.decode(errors="replace") or stdout.decode(errors="replace")
                return LaunchResult(success=False, error=error.strip())
            logger.info(f"Minecraft 启动命令已发送: {bat_name}")
            self._running = True
            return LaunchResult(success=True, window_title="Minecraft NeoForge* 1.21.1")
        except (OSError, TimeoutError) as exc:
            return LaunchResult(success=False, error=str(exc))

    def _configure_options(self) -> None:
        """配置 MC options.txt（pauseOnLostFocus 等）。"""
        options_file = self._cfg.mc_home / "options.txt"
        if not options_file.exists():
            return

        try:
            content = options_file.read_text(encoding="utf-8")
            target = "pauseOnLostFocus:false" if not self._cfg.pause_on_lost_focus else "pauseOnLostFocus:true"

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
                    self._cfg.window_x, self._cfg.window_y,
                    self._cfg.window_width, self._cfg.window_height,
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
