"""Minecraft 日志实时读取器。

追踪 latest.log，解析聊天消息、玩家进出、死亡等事件，
为爱莉提供游戏世界的感知基础。无需安装 Mod。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("life_engine.minecraft.log_reader")

# MC 日志默认路径（WSL 下访问 Windows 分区）
DEFAULT_LOG_PATH = Path("/mnt/g/Game/Minecraft/.minecraft/logs/latest.log")

# ─── 日志行正则 ──────────────────────────────────────────────
# 格式: [DD Mon YYYY HH:MM:SS.mmm] [Thread/LEVEL] [Class/]: message
_LINE_RE = re.compile(
    r"^\[([^\]]+)\]\s+\[([^\]]+)\]\s+\[([^\]]+)\]:\s+(.+)$"
)

# 玩家聊天: <PlayerName> message
_CHAT_RE = re.compile(r"^<([^>]+)>\s+(.+)$")

# 系统聊天: [System] [CHAT] message  OR  just [CHAT] message
_SYSCHAT_RE = re.compile(r"^\[System\]\s+\[CHAT\]\s+(.+)$|^\[CHAT\]\s+(.+)$")

# 玩家进入/离开（单人/多人都会记录）
_JOIN_RE = re.compile(r"^(\S+) joined the game$")
_LEAVE_RE = re.compile(r"^(\S+) left the game$")

# 玩家死亡（各种死亡信息）
_DEATH_KEYWORDS = [
    "was slain", "died", "drowned", "fell", "burned", "was shot",
    "was blown up", "was killed", "suffocated", "starved", "was impaled",
    "was squished", "withered away", "was pummeled", "hit the ground",
    "walked into a cactus", "was fireballed",
]

# 游戏世界加载完成
_WORLD_LOADED_RE = re.compile(r"Loaded \d+ advancements|Preparing spawn area|Client received|joining world")

# 游戏世界关闭
_WORLD_UNLOAD_RE = re.compile(r"Stopping singleplayer server|Disconnected from server")


@dataclass(slots=True)
class LogEvent:
    """一条解析后的游戏事件。"""

    type: str          # chat / system_chat / join / leave / death / world_loaded / world_closed / raw
    timestamp: str     # HH:MM:SS
    player: str        # 相关玩家名（可能为空）
    message: str       # 消息/事件内容
    raw: str           # 原始日志行


class MinecraftLogReader:
    """异步实时日志读取器。

    用法：
        reader = MinecraftLogReader()
        await reader.start()
        ...
        events = reader.drain_events()  # 取走所有新事件
        await reader.stop()
    """

    def __init__(self, log_path: Path | None = None) -> None:
        self._log_path = log_path or DEFAULT_LOG_PATH
        self._events: list[LogEvent] = []
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_size: int = 0
        self._poll_interval: float = 0.8  # 秒

    # ─── 生命周期 ────────────────────────────────────────────

    async def start(self) -> None:
        """启动后台日志追踪任务。"""
        if self._running:
            return
        self._running = True

        # 记录当前文件大小，只读新内容
        try:
            self._last_size = self._log_path.stat().st_size
        except FileNotFoundError:
            self._last_size = 0

        self._task = asyncio.create_task(self._tail_loop(), name="mc_log_reader")
        logger.info(f"开始追踪 MC 日志: {self._log_path}")

    async def stop(self) -> None:
        """停止日志追踪。"""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("MC 日志追踪已停止")

    def drain_events(self) -> list[LogEvent]:
        """取走所有待处理事件（清空队列）。"""
        events, self._events = self._events, []
        return events

    def peek_events(self) -> list[LogEvent]:
        """查看事件但不清空（只读）。"""
        return list(self._events)

    # ─── 内部实现 ────────────────────────────────────────────

    async def _tail_loop(self) -> None:
        """轮询新日志行的后台任务。"""
        while self._running:
            try:
                await self._read_new_lines()
            except Exception as exc:
                logger.debug(f"日志读取异常: {exc}")
            await asyncio.sleep(self._poll_interval)

    async def _read_new_lines(self) -> None:
        """读取文件自上次以来新增的行。"""
        if not self._log_path.exists():
            return

        current_size = self._log_path.stat().st_size

        # 文件被重置（游戏重启）
        if current_size < self._last_size:
            logger.info("MC 日志文件已重置（游戏重新启动）")
            self._last_size = 0

        if current_size <= self._last_size:
            return

        # 读取新增内容，尝试多种编码
        new_text = self._read_bytes(self._last_size, current_size)
        self._last_size = current_size

        if not new_text:
            return

        for line in new_text.splitlines():
            line = line.strip()
            if line:
                event = self._parse_line(line)
                if event:
                    self._events.append(event)

    def _read_bytes(self, start: int, end: int) -> str:
        """读取文件指定字节范围，自动检测编码。"""
        try:
            with open(self._log_path, "rb") as f:
                f.seek(start)
                raw = f.read(end - start)

            # 依次尝试编码
            for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue

            # 最后兜底
            return raw.decode("utf-8", errors="replace")

        except Exception as exc:
            logger.debug(f"读取日志字节失败: {exc}")
            return ""

    def _parse_line(self, line: str) -> LogEvent | None:
        """解析一行日志，返回 LogEvent 或 None。"""
        m = _LINE_RE.match(line)
        if not m:
            return None

        timestamp_raw, thread_level, log_class, message = m.groups()

        # 提取时间（只取 HH:MM:SS 部分）
        ts_parts = timestamp_raw.split()
        timestamp = ts_parts[-1].split(".")[0] if ts_parts else timestamp_raw

        # 只处理 INFO 级别
        if "INFO" not in thread_level:
            return None

        # ── 聊天消息 ─────────────────────────────────────────
        if "ChatComponent" in log_class or "chat" in log_class.lower():
            # 系统聊天 (mod 提示、成就等)
            sys_m = _SYSCHAT_RE.match(message)
            if sys_m:
                content = sys_m.group(1) or sys_m.group(2) or ""
                return LogEvent(
                    type="system_chat",
                    timestamp=timestamp,
                    player="",
                    message=content,
                    raw=line,
                )
            # 玩家聊天: <Name> text
            chat_m = _CHAT_RE.match(message)
            if chat_m:
                return LogEvent(
                    type="chat",
                    timestamp=timestamp,
                    player=chat_m.group(1),
                    message=chat_m.group(2),
                    raw=line,
                )
            # 其他聊天行（直接显示）
            if message and not message.startswith("["):
                return LogEvent(
                    type="chat",
                    timestamp=timestamp,
                    player="",
                    message=message,
                    raw=line,
                )

        # ── 玩家进出（单人/局域网/服务器）───────────────────────
        if "MinecraftServer" in log_class or "ServerGamePacket" in log_class or "network" in log_class.lower():
            join_m = _JOIN_RE.match(message)
            if join_m:
                return LogEvent(
                    type="join",
                    timestamp=timestamp,
                    player=join_m.group(1),
                    message=f"{join_m.group(1)} 进入了游戏",
                    raw=line,
                )
            leave_m = _LEAVE_RE.match(message)
            if leave_m:
                return LogEvent(
                    type="leave",
                    timestamp=timestamp,
                    player=leave_m.group(1),
                    message=f"{leave_m.group(1)} 离开了游戏",
                    raw=line,
                )
            # 死亡消息
            for kw in _DEATH_KEYWORDS:
                if kw in message.lower():
                    # 提取玩家名（消息开头的单词）
                    player = message.split()[0] if message else ""
                    return LogEvent(
                        type="death",
                        timestamp=timestamp,
                        player=player,
                        message=message,
                        raw=line,
                    )

        # ── 世界加载/关闭 ─────────────────────────────────────
        if _WORLD_LOADED_RE.search(message):
            return LogEvent(
                type="world_loaded",
                timestamp=timestamp,
                player="",
                message="世界已加载",
                raw=line,
            )
        if _WORLD_UNLOAD_RE.search(message):
            return LogEvent(
                type="world_closed",
                timestamp=timestamp,
                player="",
                message="世界正在关闭",
                raw=line,
            )

        return None

    # ─── 便捷查询 ─────────────────────────────────────────────

    def get_recent_chat_events(self, limit: int = 10) -> list[LogEvent]:
        """获取最近的聊天事件（不清空队列）。"""
        chat = [e for e in self._events if e.type in ("chat", "system_chat")]
        return chat[-limit:]

    @property
    def is_running(self) -> bool:
        return self._running


def create_log_reader(log_path: Path | None = None) -> MinecraftLogReader:
    """创建日志读取器实例。"""
    return MinecraftLogReader(log_path)
