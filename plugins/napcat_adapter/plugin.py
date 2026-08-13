"""NapCat 适配器（全面重写版 v3.0）

核心架构：
- NapCatClient: API 调用层（100+ OneBot API）
- EventRouter: 事件路由层（message/notice/request/meta_event）
- OutgoingSender: 出站消息发送
- CommandHandler: 命令系统（旧式兼容 + 新式透传）

接口兼容：
- 继承 BaseAdapter（from src.core.components.base）
- 实现 from_platform_message(raw) -> MessageEnvelope | None
- 实现 _send_platform_message(envelope) -> None
- 实现 get_bot_info() -> dict
- send_napcat_api(action, params) 向后兼容
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, cast

from mofox_wire import CoreSink, MessageEnvelope, WebSocketAdapterOptions

from src.app.plugin_system.api.log_api import get_logger
from src.core.components.base import BaseAdapter, BasePlugin
from src.core.components.loader import register_plugin
from src.kernel.concurrency import TaskInfo, get_task_manager

from .client import NapCatClient
from .config import NapcatAdapterConfig
from .events import EventRouter
from .outgoing import CommandHandler, OutgoingSender

logger = get_logger("napcat_adapter")


def _validate_bot_identity(config: NapcatAdapterConfig) -> None:
    """校验 Bot 身份配置。"""
    qq_id = str(config.bot.qq_id).strip()
    qq_nickname = str(config.bot.qq_nickname).strip()

    invalid_id_values = {"", "0", "none", "null", "undefined", "pydanticundefined"}
    if qq_id.lower() in invalid_id_values or not qq_id.isdigit():
        raise ValueError("配置项 bot.qq_id 无效：必须为非空数字字符串")

    invalid_nickname_values = {"", "none", "null", "undefined", "pydanticundefined"}
    if qq_nickname.lower() in invalid_nickname_values:
        raise ValueError("配置项 bot.qq_nickname 无效：必须为非空昵称")


class NapcatAdapter(BaseAdapter):
    """NapCat 适配器 v3.0 — 全功能 OneBot 11 适配器。"""

    adapter_name = "napcat_adapter"
    adapter_version = "3.0.0"
    adapter_author = "Elysium Team"
    adapter_description = "全功能 NapCat/OneBot 11 适配器（100+ API，全量事件感知）"
    platform = "qq"

    run_in_subprocess = False

    def __init__(self, core_sink: CoreSink, plugin: "NapcatAdapterPlugin | None" = None, **kwargs):
        """初始化 NapCat 适配器。"""
        # 从配置读取 WebSocket 参数
        if plugin and plugin.config:
            config = cast(NapcatAdapterConfig, plugin.config)
            host = config.napcat_server.host
            port = config.napcat_server.port
            access_token = config.napcat_server.access_token
            mode_str = config.napcat_server.mode
            ws_mode = "client" if mode_str == "direct" else "server"

            ws_url = f"ws://{host}:{port}"
            headers = {}
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"
        else:
            ws_url = "ws://127.0.0.1:8095"
            headers = {}
            ws_mode = "server"

        transport = WebSocketAdapterOptions(
            mode=ws_mode,
            url=ws_url,
            headers=headers if headers else None,
        )

        super().__init__(core_sink, plugin=plugin, transport=transport, **kwargs)

        # 核心组件
        self._client = NapCatClient()
        self._router = EventRouter(self._client, self._get_config)
        self._sender = OutgoingSender(self._client, self._get_config)
        self._command_handler = CommandHandler(self._client, self._get_config, core_sink)

        # 注入重连回调
        self._router.meta_handler.set_reconnect_callback(self.reconnect)

        # Watchdog 状态
        self._last_qq_message_time: float = time.time()
        # 记录每条 CLOSE-WAIT 连接首次被观察到的时间。不能用“多久没收到
        # QQ 消息”代替连接本身的存活时间，否则正常的安静时段也会触发清理。
        self._close_wait_seen_at: dict[tuple[str, int], float] = {}
        self._watchdog_task: TaskInfo | None = None
        # NapCat 连接状态跟踪：用于避免“未连接”稳定状态下重复刷 WARNING。
        self._napcat_present: bool = False

    def _get_config(self) -> NapcatAdapterConfig | None:
        """获取当前插件配置。"""
        if self.plugin and self.plugin.config:
            return cast(NapcatAdapterConfig, self.plugin.config)
        return None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_adapter_loaded(self) -> None:
        """适配器加载。"""
        logger.info("NapCat 适配器 v3.0 正在启动...")

        if not self.plugin or not self.plugin.config:
            raise RuntimeError("NapCat 适配器启动失败：缺少插件配置")

        config = cast(NapcatAdapterConfig, self.plugin.config)
        _validate_bot_identity(config)

        # 重置消息时间戳，启动 watchdog
        self._last_qq_message_time = time.time()
        self._start_watchdog()

        logger.info("NapCat 适配器已加载")

    async def on_adapter_unloaded(self) -> None:
        """适配器卸载。"""
        logger.info("NapCat 适配器正在关闭...")

        # 停止 watchdog
        self._stop_watchdog()

        # 停止 OneBot 元事件心跳检查
        # BaseAdapter 仍负责 WebSocket 连接状态健康检查。
        # 不能用“多久没有业务消息”判断连接僵死：安静连接是正常状态。
        # 传输层 ping/pong 与 OneBot heartbeat 才是连接健康的依据。
        self._router.meta_handler.stop()

        # 关闭 WebSocket 连接
        if self._ws:
            try:
                await self._ws.close()
                logger.info("WebSocket 连接已关闭")
            except Exception as e:
                logger.warning(f"关闭 WebSocket 连接时出错: {e}")
            self._ws = None

        # 关闭 WebSocket 服务器（释放端口）
        if self._ws_server:
            try:
                self._ws_server.close()
                await self._ws_server.wait_closed()
                logger.info("WebSocket 服务器已关闭，端口已释放")
            except Exception as e:
                logger.warning(f"关闭 WebSocket 服务器时出错: {e}")
            self._ws_server = None

        # 解绑 WebSocket
        self._client.unbind_ws()

        logger.info("NapCat 适配器已关闭")

    # ------------------------------------------------------------------
    # WebSocket 连接钩子（由 BaseAdapter 调用）
    # ------------------------------------------------------------------

    async def _start_ws_server(self, options: WebSocketAdapterOptions) -> None:
        """Override mofox_wire default: call on_ws_connected/on_ws_disconnected hooks."""
        from urllib.parse import urlparse

        from websockets.legacy import server as ws_server_lib

        parsed = urlparse(options.url)
        host = parsed.hostname or "0.0.0.0"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"

        async def handler(ws: Any) -> None:
            # path guard（与 mofox_wire 保持一致）
            if options.allowed_paths and ws.path not in options.allowed_paths:
                await ws.close(code=4000, reason="Path not allowed")
                return
            if ws.path != path:
                await ws.close(code=4000, reason="Path mismatch")
                return

            self._ws = ws
            await self.on_ws_connected(ws)   # ← 绑定 NapCatClient._ws
            try:
                await self._ws_listen_loop(options)   # _ws_listen_loop 的 finally 会置 self._ws = None
            finally:
                await self.on_ws_disconnected()   # ← 解绑 NapCatClient._ws

        self._ws_server = await ws_server_lib.serve(
            handler,
            host,
            port,
            extra_headers=options.headers,
            max_size=options.max_message_size,
            ping_interval=20,
            ping_timeout=20,
        )
        logger.info(f"NapCat WebSocket 服务器已在 {host}:{port} 启动，等待连接...")

    async def on_ws_connected(self, ws: Any) -> None:
        """WebSocket 连接建立时调用。"""
        self._client.bind_ws(ws)
        # 检查这是初次连接还是重连
        bot_qq = self.plugin.config.bot.qq_id if self.plugin and self.plugin.config else "未知"
        logger.info(f"NapCat WebSocket 已连接 (Bot {bot_qq})")

    async def on_ws_disconnected(self) -> None:
        """WebSocket 连接断开时调用。"""
        self._client.unbind_ws()
        logger.warning("NapCat WebSocket 已断开")

    # ------------------------------------------------------------------
    # BaseAdapter 接口实现
    # ------------------------------------------------------------------

    async def from_platform_message(self, raw: dict[str, Any]) -> MessageEnvelope | None:  # type: ignore[override]
        """将 OneBot 原始消息转换为 MessageEnvelope。

        这是核心入站方法，由 mofox-wire 的传输层调用。
        所有事件（message/notice/request/meta_event/API响应）都经过这里。
        """
        # 收到真实 QQ 消息（非心跳/元事件）时更新时间戳，供 watchdog 判断是否卡死
        post_type = raw.get("post_type", "")
        if post_type == "message":
            self._last_qq_message_time = time.time()

        return await self._router.dispatch(raw)

    async def _send_platform_message(  # type: ignore[override]
        self,
        envelope: MessageEnvelope,
    ) -> dict[str, Any] | None:
        """将 MessageEnvelope 发送到 NapCat。

        这是核心出站方法，由 mofox-wire 的核心推送调用。
        根据消息段类型分发到 sender 或 command_handler。
        """
        # 检查是否是命令类消息
        segment = envelope.get("message_segment", {})
        if isinstance(segment, list):
            first_seg = segment[0] if segment else {}
        else:
            first_seg = segment

        seg_type = first_seg.get("type") if isinstance(first_seg, dict) else None

        try:
            if seg_type in ("command", "adapter_command", "adapter_response"):
                await self._command_handler.handle(envelope)
                return None
            return await self._sender.send(envelope)
        except Exception as e:
            if getattr(e, "delivery_unknown", False):
                logger.debug(f"NapCat 消息投递状态未知: {e}")
            else:
                logger.error(f"发送消息失败: {e}")
            raise

    async def get_bot_info(self) -> dict[str, Any]:  # type: ignore[override]
        """获取 Bot 信息。"""
        config = self._get_config()
        if not config:
            return {}
        return {
            "bot_id": config.bot.qq_id,
            "bot_name": config.bot.qq_nickname,
            "platform": self.platform,
        }

    async def health_check(self) -> bool:
        """Check the resource this adapter actually owns.

        In reverse-WebSocket mode Elysium owns the listening server while
        NapCat owns reconnecting the client. Restarting the whole adapter just
        because no client is attached races NapCat's reconnect loop and cannot
        repair the QQ session. OneBot heartbeat timeout handles a genuinely
        stale attached client separately.
        """
        config = self._get_config()
        if config and config.napcat_server.mode == "reverse":
            server = self._ws_server
            if server is None:
                return False
            is_serving = getattr(server, "is_serving", None)
            return bool(is_serving()) if callable(is_serving) else True
        return self.is_connected()

    # ------------------------------------------------------------------
    # 向后兼容 API
    # ------------------------------------------------------------------

    async def send_napcat_api(
        self, action: str, params: dict[str, Any], timeout: float = 30.0
    ) -> dict[str, Any]:
        """向后兼容的 API 调用方法。

        旧代码（如 life_engine 的 chat_history_tools）可能直接调用此方法。
        """
        return await self._client.call(action, params, timeout=timeout)

    @property
    def client(self) -> NapCatClient:
        """获取 NapCatClient 实例（供高级用途）。"""
        return self._client

    # ------------------------------------------------------------------
    # NapCat CLOSE-WAIT Watchdog
    # ------------------------------------------------------------------
    # NapCat 与腾讯服务器（198.18.x.x:443）的 TCP 连接有时卡入 CLOSE-WAIT：
    # 腾讯发了 FIN，但 NapCat 没有关闭自己这侧 → socket 死亡，不再收到任何消息。
    # 此时 Elysium↔NapCat 的 WebSocket（8087）仍然 ESTAB，掩盖了真正的问题。
    # Watchdog 每 30s 检查一次，发现 CLOSE-WAIT + 长时间无消息时强制关闭僵尸
    # socket（ss -K），迫使 NapCat 感知到连接断开并自行重连腾讯服务器。
    # ------------------------------------------------------------------

    # 检测间隔（秒）
    _WATCHDOG_CHECK_INTERVAL: int = 30
    # 同一条 CLOSE-WAIT 连接持续多久才允许清理（秒）
    _CLOSE_WAIT_PERSISTENCE_THRESHOLD: int = 90
    # 无消息多久输出一条 WARNING（无 CLOSE-WAIT 时，仅告警不干预）
    _SILENCE_WARN_THRESHOLD: int = 600

    def _start_watchdog(self) -> None:
        """启动 NapCat CLOSE-WAIT 监控 asyncio task。"""
        if self._watchdog_task and not self._watchdog_task.is_done():
            return
        tm = get_task_manager()
        self._watchdog_task = tm.create_task(
            self._watchdog_loop(),
            name="napcat_close_wait_watchdog",
            daemon=True,
        )
        logger.info("[NapCat Watchdog] CLOSE-WAIT 监控任务已启动")

    def _stop_watchdog(self) -> None:
        """取消 watchdog task。"""
        if self._watchdog_task and not self._watchdog_task.is_done():
            tm = get_task_manager()
            if tm.cancel_task(self._watchdog_task.task_id):
                logger.info("[NapCat Watchdog] 监控任务已停止")
        self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        """Watchdog 主循环：每 30s 检查 NapCat 连接健康状态。"""
        while True:
            try:
                await asyncio.sleep(self._WATCHDOG_CHECK_INTERVAL)
                await self._check_napcat_health()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[NapCat Watchdog] 监控循环异常: {exc}", exc_info=True)

    async def _check_napcat_health(self) -> None:
        """单次健康检查：找 PID → 查 CLOSE-WAIT → 必要时 ss -K。"""
        now = time.time()
        silence_secs = now - self._last_qq_message_time

        # 1. 找 NapCat 进程 PID（通过连接到本机 WS 服务端口的客户端进程）
        ws_port = self._ws_port()
        napcat_pid = await self._find_napcat_pid(ws_port)

        if napcat_pid is None:
            # NapCat 未连接（未启动或已退出）。这是一个稳定状态，不应每轮刷
            # WARNING。仅在连接状态由“已连接”变为“未连接”时记录一次 INFO；
            # 长时间无变化时保持安静。真正的僵尸连接（CLOSE-WAIT）检测只在
            # NapCat 已连接时进行，因此此处直接返回即可。
            if self._napcat_present:
                logger.info(
                    f"[NapCat Watchdog] NapCat 已断开（WS port={ws_port}），"
                    f"QQ 消息已静默 {silence_secs:.0f}s"
                )
                self._napcat_present = False
            return

        # NapCat 已连接：首次检测到时记录一次恢复，避免静默无感知。
        if not self._napcat_present:
            logger.info(f"[NapCat Watchdog] NapCat 已连接（pid={napcat_pid}）")
            self._napcat_present = True

        # 2. 查该 PID 的 CLOSE-WAIT 外部连接
        close_wait_sockets = await self._get_close_wait_sockets(napcat_pid)

        # 只有同一条 CLOSE-WAIT 连接持续存在足够长时间才处理。QQ 长时间
        # 没有新消息是正常情况，不能作为强制断连接的依据。
        current_sockets = set(close_wait_sockets)
        self._close_wait_seen_at = {
            socket: self._close_wait_seen_at.get(socket, now)
            for socket in current_sockets
        }
        stale_sockets = [
            socket
            for socket in close_wait_sockets
            if now - self._close_wait_seen_at[socket] >= self._CLOSE_WAIT_PERSISTENCE_THRESHOLD
        ]

        if close_wait_sockets:
            # CLOSE-WAIT 本身是常见的 TCP 收尾状态，普通观察不应污染 INFO 日志。
            logger.debug(
                f"[NapCat Watchdog] 检测到 NapCat(pid={napcat_pid}) "
                f"CLOSE-WAIT 连接 {len(close_wait_sockets)} 条，"
                f"QQ 消息已静默 {silence_secs:.0f}s"
            )
            for sock, fd_num in close_wait_sockets:
                age = now - self._close_wait_seen_at[(sock, fd_num)]
                logger.debug(f"[NapCat Watchdog]   CLOSE-WAIT: {sock} fd={fd_num} 持续 {age:.0f}s")

            if stale_sockets:
                # 只关闭已确认持续存在的 CLOSE-WAIT fd，不能把 NapCat 的其余
                # WebSocket、登录态和 API 连接一并关掉。
                killed = await self._kill_close_wait_sockets(stale_sockets)
                for socket in stale_sockets:
                    self._close_wait_seen_at.pop(socket, None)
                if killed:
                    logger.info(
                        f"[NapCat Watchdog] 已清理 {killed}/{len(stale_sockets)} 条持续 "
                        f"{self._CLOSE_WAIT_PERSISTENCE_THRESHOLD}s 的目标连接，"
                        f"未影响 NapCat WebSocket"
                    )
                else:
                    logger.warning(
                        f"[NapCat Watchdog] CLOSE-WAIT 目标连接清理失败 "
                        f"(目标 {len(stale_sockets)} 条)"
                    )
        else:
            self._close_wait_seen_at.clear()
            # 无 CLOSE-WAIT，但长时间无消息也记录一下（可能是正常的安静时段）
            if silence_secs > self._SILENCE_WARN_THRESHOLD:
                logger.debug(
                    f"[NapCat Watchdog] QQ 消息已静默 {silence_secs:.0f}s，"
                    f"NapCat(pid={napcat_pid}) 无 CLOSE-WAIT，连接状态正常（可能只是没有新消息）"
                )

    def _ws_port(self) -> int:
        """获取当前配置的 WebSocket 端口。"""
        config = self._get_config()
        if config:
            return config.napcat_server.port
        return 8087

    async def _find_napcat_pid(self, ws_port: int) -> int | None:
        """通过连接到 WS 服务端口的客户端进程找到 NapCat PID。

        Elysium 是 WS server，NapCat 是 client。
        `ss -tnp dport = :<port>` 列出目标端口为 <port> 的连接，其中
        包含 NapCat 进程信息。
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ss", "-tnp", "dport", "=", f":{ws_port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            output = stdout.decode(errors="replace")
            for line in output.splitlines():
                if "ESTAB" not in line:
                    continue
                m = re.search(r"pid=(\d+)", line)
                if m:
                    return int(m.group(1))
        except asyncio.TimeoutError:
            logger.debug("[NapCat Watchdog] _find_napcat_pid 超时")
        except Exception as exc:
            logger.debug(f"[NapCat Watchdog] _find_napcat_pid 异常: {exc}")
        return None

    async def _get_close_wait_sockets(self, pid: int) -> list[tuple[str, int]]:
        """获取指定 PID 进程所有处于 CLOSE-WAIT 状态的外部连接地址对。

        返回格式：[("local_addr -> remote_addr", fd), ...]，过滤掉本地回环连接。
        fd 必须从 ss 输出中直接提取，后续 WSL2 清理时只能操作这个 fd。
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ss", "-tnp", "state", "close-wait",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            output = stdout.decode(errors="replace")

            results: list[tuple[str, int]] = []
            for line in output.splitlines():
                pid_match = re.search(r"(?:^|[,=(])pid=(\d+)(?:[,)]|$)", line)
                fd_match = re.search(r"(?:^|[,=(])fd=(\d+)(?:[,)]|$)", line)
                if not pid_match or int(pid_match.group(1)) != pid or not fd_match:
                    continue
                # ss 在带 state 过滤时可能省略 State 列，因此不要依赖固定下标；
                # users: 总在地址列之后，按它反向定位 local/remote。
                parts = line.split()
                process_index = next(
                    (index for index, part in enumerate(parts) if part.startswith("users:")),
                    None,
                )
                if process_index is not None and process_index >= 2:
                    local_addr = parts[process_index - 2]
                    remote_addr = parts[process_index - 1]
                    # 过滤本地回环连接。
                    if local_addr.startswith(("127.", "::1", "[::1]")):
                        continue
                    results.append((f"{local_addr} -> {remote_addr}", int(fd_match.group(1))))
            return results
        except asyncio.TimeoutError:
            logger.debug("[NapCat Watchdog] _get_close_wait_sockets 超时")
        except Exception as exc:
            logger.debug(f"[NapCat Watchdog] _get_close_wait_sockets 异常: {exc}")
        return []

    async def _kill_close_wait_sockets(self, socket_pairs: list[tuple[str, int]]) -> int:
        """对每条 CLOSE-WAIT 连接强制关闭（WSL2 用 pidfd_getfd + shutdown，原生 Linux 用 ss -K）。

        返回成功处理的连接数。
        """
        # WSL2 kernel 不支持 ss -K (SOCK_DESTROY netlink 未实现)
        # 检测 WSL2：uname -r 输出包含 "microsoft" 或 "WSL"
        is_wsl2 = await self._detect_wsl2()

        if is_wsl2:
            return await self._kill_close_wait_wsl2(socket_pairs)
        else:
            return await self._kill_close_wait_native(socket_pairs)

    async def _detect_wsl2(self) -> bool:
        """检测是否运行在 WSL2 环境。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "uname", "-r",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
            kernel = stdout.decode().lower()
            return "microsoft" in kernel or "wsl" in kernel
        except Exception:
            return False

    async def _kill_close_wait_wsl2(self, socket_pairs: list[tuple[str, int]]) -> int:
        """WSL2 环境：通过 pidfd_getfd + shutdown() syscall 关闭 CLOSE-WAIT socket。"""
        killed = 0
        napcat_pid = await self._find_napcat_pid(self._ws_port())
        if napcat_pid is None:
            logger.warning("[NapCat Watchdog] 无法找到 NapCat PID，跳过 socket 清理")
            return 0

        # 只取 ss 明确标记为 CLOSE-WAIT 的 fd，绝不能返回 NapCat 的全部 socket。
        napcat_fds = await self._get_napcat_socket_fds(napcat_pid, socket_pairs)

        for fd_num in napcat_fds:
            try:
                success = await self._shutdown_socket_via_pidfd(napcat_pid, fd_num)
                if success:
                    logger.info(f"[NapCat Watchdog] 已通过 pidfd_getfd 关闭 NapCat(pid={napcat_pid}) fd={fd_num}")
                    killed += 1
                else:
                    logger.warning(f"[NapCat Watchdog] pidfd_getfd 关闭 fd={fd_num} 失败")
            except Exception as exc:
                logger.warning(f"[NapCat Watchdog] 关闭 fd={fd_num} 异常: {exc}")

        return killed

    async def _get_napcat_socket_fds(self, pid: int, socket_pairs: list[tuple[str, int]]) -> list[int]:
        """获取 ss 明确标记的 CLOSE-WAIT fd。

        ``pid`` 保留在签名中用于调用方语义和日志关联；fd 已由
        ``_get_close_wait_sockets`` 按同一个 PID 从 ``ss -tnp`` 提取。
        这里禁止回退到扫描 NapCat 的全部 socket，否则一次清理会误伤
        WebSocket 和其他正常连接。
        """
        del pid
        return sorted({fd_num for _pair, fd_num in socket_pairs})

    async def _shutdown_socket_via_pidfd(self, pid: int, fd: int) -> bool:
        """通过 pidfd_getfd syscall 借用目标进程的 fd，执行 shutdown(SHUT_RDWR)。"""
        script = f"""
import ctypes, os, sys
libc = ctypes.CDLL("libc.so.6", use_errno=True)
SYS_pidfd_open, SYS_pidfd_getfd = 434, 438

pidfd = libc.syscall(ctypes.c_long(SYS_pidfd_open), ctypes.c_int({pid}), ctypes.c_uint(0))
if pidfd < 0:
    sys.exit(1)

borrowed = libc.syscall(ctypes.c_long(SYS_pidfd_getfd), ctypes.c_int(pidfd), ctypes.c_int({fd}), ctypes.c_uint(0))
if borrowed < 0:
    os.close(pidfd)
    sys.exit(2)

ret = libc.shutdown(ctypes.c_int(borrowed), ctypes.c_int(2))
os.close(borrowed)
os.close(pidfd)
sys.exit(0 if ret == 0 else 3)
"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", "-c", script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return proc.returncode == 0
        except asyncio.TimeoutError:
            logger.warning(f"[NapCat Watchdog] pidfd_getfd shutdown timeout for fd={fd}")
            return False
        except Exception as exc:
            logger.warning(f"[NapCat Watchdog] pidfd_getfd shutdown exception: {exc}")
            return False

    async def _kill_close_wait_native(self, socket_pairs: list[tuple[str, int]]) -> int:
        """原生 Linux：使用 ss -K 命令关闭 CLOSE-WAIT socket。"""
        killed = 0
        for pair, _fd_num in socket_pairs:
            # pair 格式: "172.26.x.x:44472 -> 198.18.x.x:443"
            try:
                parts = pair.replace(" -> ", " ").split()
                if len(parts) < 2:
                    continue
                local_addr, remote_addr = parts[0], parts[1]
                proc = await asyncio.create_subprocess_exec(
                    "ss", "-K",
                    "src", local_addr,
                    "dst", remote_addr,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                rc = proc.returncode or 0
                out = stdout.decode(errors="replace").strip()
                err = stderr.decode(errors="replace").strip()
                if rc == 0:
                    logger.info(
                        f"[NapCat Watchdog] ss -K src={local_addr} dst={remote_addr} "
                        f"成功 (rc={rc}){': ' + out if out else ''}"
                    )
                    killed += 1
                else:
                    logger.warning(
                        f"[NapCat Watchdog] ss -K src={local_addr} dst={remote_addr} "
                        f"返回 rc={rc}{': ' + err if err else ''}"
                    )
            except asyncio.TimeoutError:
                logger.warning(f"[NapCat Watchdog] ss -K {pair} 超时")
            except Exception as exc:
                logger.warning(f"[NapCat Watchdog] ss -K {pair} 异常: {exc}")
        return killed


@register_plugin
class NapcatAdapterPlugin(BasePlugin):
    """NapCat 适配器插件。"""

    plugin_name = "napcat_adapter"
    plugin_version = "3.0.0"
    plugin_author = "Elysium Team"
    plugin_description = "全功能 NapCat/OneBot 11 适配器（100+ API，全量事件感知）"
    configs = [NapcatAdapterConfig]

    def get_components(self) -> list[type]:
        """获取插件组件；本机配置停用时不注册适配器。"""
        config = cast(NapcatAdapterConfig | None, self.config)
        if config is None or not config.plugin.enabled:
            logger.info("NapCat 适配器已在配置中停用，不注册 Adapter 组件")
            return []
        return [NapcatAdapter]
