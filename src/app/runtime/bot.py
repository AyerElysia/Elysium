"""Bot 主类

Elysium 框架的核心协调器，负责系统初始化、插件加载和生命周期管理。
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.core.config import CORE_VERSION

from .console_ui import ConsoleUIManager, UILevel
from .exceptions import BotInitializationError, BotRuntimeError, BotShutdownError
from .signal_handler import SignalHandler

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

if TYPE_CHECKING:
    from src.app.api.v1.mount import APIV1Mount
    from src.core.components import PluginLoader, PluginManifest
    from src.core.config import CoreConfig
    from src.core.managers import MCPManager, PluginManager
    from src.core.transport import HTTPServer, MessageReceiver, SinkManager
    from src.kernel.concurrency import TaskManager, WatchDog
    from src.kernel.event import EventBus
    from src.kernel.logger import Logger
    from src.kernel.scheduler import UnifiedScheduler
    from src.kernel.storage import JSONStore
    from src.kernel.vector_db import VectorDBBase


class Bot:
    """Elysium Bot 主类

    管理完整的 Bot 生命周期，包括：
    - Kernel 层初始化
    - Core 层组件加载
    - 插件发现和加载
    - 运行时管理
    - 优雅关闭

    Attributes:
        bot_name: Bot 名称
        bot_version: Bot 版本
        config_path: 配置文件路径
        plugins_dir: 插件目录
        log_dir: 日志目录
        ui_level: UI 详细程度
    """

    bot_name: str = "Elysium"
    bot_version: str = CORE_VERSION

    def __init__(
        self,
        config_path: str = "config/core.toml",
        plugins_dir: str = "plugins",
        log_dir: str = "logs",
        ui_level: UILevel = UILevel.STANDARD,
    ) -> None:
        """初始化 Bot

        Args:
            config_path: 配置文件路径
            plugins_dir: 插件目录
            log_dir: 日志目录
            ui_level: UI 详细程度
        """
        self.config_path = config_path
        self.plugins_dir = plugins_dir
        self.log_dir = log_dir

        # UI 管理器
        self.ui = ConsoleUIManager(level=ui_level)

        # 状态标志
        self._initialized = False
        self._running = False
        self._shutdown_requested = False
        self._network_loop: asyncio.AbstractEventLoop | None = None
        self._dns_executor: ThreadPoolExecutor | None = None
        self._original_getaddrinfo: Any | None = None
        self._original_getnameinfo: Any | None = None
        self._patched_getaddrinfo: Any | None = None
        self._patched_getnameinfo: Any | None = None

        # Kernel 层组件（延迟初始化）
        self.config: CoreConfig | None = None
        self.logger: Logger | None = None
        self.event_bus: EventBus | None = None
        self.task_manager: TaskManager | None = None
        self.watchdog: WatchDog | None = None
        self.vector_db: VectorDBBase | None = None
        self.scheduler: UnifiedScheduler | None = None
        self.storage: JSONStore | None = None

        # Core 层组件（延迟初始化）
        self.message_receiver: MessageReceiver | None = None
        self.sink_manager: SinkManager | None = None
        self.plugin_loader: PluginLoader | None = None
        self.plugin_manager: PluginManager | None = None
        self.mcp_manager: MCPManager | None = None
        self.http_server: HTTPServer | None = None
        self.app_api_mount: APIV1Mount | None = None
        self.load_order: list[str] = []
        self.manifests: dict[str, PluginManifest] = {}
        self.load_results: dict[str, bool] = {}

        # 统计数据
        self._stats: dict[str, int | bool | dict] = {
            "plugins_loaded": 0,
            "plugins_failed": 0,
            "components_by_type": {},
        }

    async def initialize(self) -> None:
        """完整初始化流程

        按顺序初始化：
        1. Kernel 层（9 步）
        2. Core 层组件初始化
        3. 插件发现
        4. 插件加载

        Raises:
            BotInitializationError: 初始化失败
        """
        try:
            # 显示启动横幅（在进度条之前）
            self.ui.show_banner(self.bot_version, self.bot_name)

            # 单一总体进度条贯穿全部初始化阶段
            with self.ui.startup_progress(total_steps=15):
                # Phase 1: Kernel 初始化
                await self._initialize_kernel()

                # Plugin startup hooks restore persisted schedules. Scheduler
                # readiness is therefore a kernel-initialization invariant,
                # not something deferred until the interactive run loop.
                assert self.scheduler is not None
                await self.scheduler.start()
                self._stats["scheduler_running"] = True

                # Phase 2: Core 组件初始化
                await self._initialize_core()

                # Phase 3: 插件发现
                await self._discover_plugins()

                # Phase 3.5: 安装插件 Python 依赖
                await self._install_plugin_deps()

                # Phase 4: 插件加载（进度条追加插件子任务）
                self.ui.begin_plugin_loading(len(self.load_order))
                await self._load_plugins()

            self._initialized = True

            # 显示成功消息
            loaded = len([r for r in self.load_results.values() if r])
            total = len(self.load_results)
            failed = total - loaded

            if failed > 0:
                self.ui.display_warning(
                    f"Elysium 已苏醒，加载了 {loaded}/{total} 个插件（{failed} 个失败）"
                )
            else:
                self.ui.display_success(f"Elysium 已苏醒 ({total} 个插件)")

        except Exception as e:
            self.ui.display_error(f"Initialization failed: {e}", e)
            await self.shutdown(raise_on_error=False)
            raise BotInitializationError(str(e), "unknown") from e

    async def _optimize_async_network_runtime(self) -> None:
        """优化异步网络运行时：线程池与 DNS 预解析。"""
        if self._dns_executor is not None:
            return

        loop = asyncio.get_running_loop()

        # DNS 专用线程池：避免 getaddrinfo 被通用任务挤占
        dns_executor = ThreadPoolExecutor(
            max_workers=16,
            thread_name_prefix="elysium-dns",
        )
        original_getaddrinfo = loop.getaddrinfo
        original_getnameinfo = loop.getnameinfo

        async def _patched_getaddrinfo(host, port, *args, **kwargs):
            func = partial(socket.getaddrinfo, host, port, *args, **kwargs)
            return await loop.run_in_executor(dns_executor, func)

        async def _patched_getnameinfo(sockaddr, flags=0):
            func = partial(socket.getnameinfo, sockaddr, flags)
            return await loop.run_in_executor(dns_executor, func)

        loop.getaddrinfo = _patched_getaddrinfo  # type: ignore[method-assign]
        loop.getnameinfo = _patched_getnameinfo  # type: ignore[method-assign]
        self._network_loop = loop
        self._dns_executor = dns_executor
        self._original_getaddrinfo = original_getaddrinfo
        self._original_getnameinfo = original_getnameinfo
        self._patched_getaddrinfo = _patched_getaddrinfo
        self._patched_getnameinfo = _patched_getnameinfo

        # 只预解析权威快照实际启用的 provider，减少首包抖动。
        from src.kernel.config.models_loader import get_models_config

        registry = get_models_config()
        targets = self._extract_active_provider_hosts(
            registry.providers,
            registry.snapshot.active_providers,
        )
        if not targets:
            return

        async def _resolve(host: str, port: int) -> None:
            try:
                await asyncio.wait_for(
                    loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
                    timeout=5.0,
                )
            except Exception:
                return

        await asyncio.gather(
            *(_resolve(host, port) for host, port in targets),
            return_exceptions=True,
        )

    def _shutdown_async_network_runtime(self) -> None:
        """恢复事件循环 DNS 方法并关闭专用解析线程池。"""
        loop = getattr(self, "_network_loop", None)
        if loop is not None:
            patched_getaddrinfo = getattr(self, "_patched_getaddrinfo", None)
            patched_getnameinfo = getattr(self, "_patched_getnameinfo", None)
            if loop.getaddrinfo is patched_getaddrinfo:
                loop.getaddrinfo = getattr(self, "_original_getaddrinfo", None)  # type: ignore[method-assign]
            if loop.getnameinfo is patched_getnameinfo:
                loop.getnameinfo = getattr(self, "_original_getnameinfo", None)  # type: ignore[method-assign]

        executor = getattr(self, "_dns_executor", None)
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)

        self._network_loop = None
        self._dns_executor = None
        self._original_getaddrinfo = None
        self._original_getnameinfo = None
        self._patched_getaddrinfo = None
        self._patched_getnameinfo = None

    @staticmethod
    def _extract_active_provider_hosts(
        providers: Mapping[str, Mapping[str, object]],
        active_providers: tuple[str, ...],
    ) -> list[tuple[str, int]]:
        """从已验证快照提取活动 provider 的 (host, port) 列表。"""
        out: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for provider_name in active_providers:
            item = providers.get(provider_name)
            if item is None:
                continue
            base_url = item.get("base_url")
            if not isinstance(base_url, str) or not base_url:
                continue

            parsed = urlparse(base_url)
            host = parsed.hostname
            if not host:
                continue

            if parsed.port is not None:
                port = parsed.port
            elif parsed.scheme == "https":
                port = 443
            else:
                port = 80

            key = (host, int(port))
            if key not in seen:
                seen.add(key)
                out.append(key)

        return out

    async def _initialize_kernel(self) -> None:
        """初始化 Kernel 层（9 步）

        1. Config
        2. Logger
        3. Event Bus
        4. Task Manager
        5. Scheduler
        6. WatchDog
        7. Database
        8. VectorDB
        9. Storage

        同时将所有服务注册到 DI 容器（架构 v2）。
        """
        self.ui.update_phase_status("内核", "初始化中...")

        # Step 1: Config
        from src.core.config import init_core_config, init_mcp_config
        from src.kernel.config.unified import init_config as init_unified_config
        from src.kernel.container import container
        from src.kernel.protocols import (
            EventBusProtocol,
            SchedulerProtocol,
            VectorStoreProtocol,
        )

        self.config = init_core_config(self.config_path)
        init_mcp_config("config/mcp.toml")
        # 架构 v2：统一配置（兼容模式，从老文件构建）
        init_unified_config("config/elysium.toml")
        # Step 2: Logger
        from src.kernel.logger import COLOR, get_logger, initialize_logger_system
        from src.kernel.protocols import LogStoreProtocol

        initialize_logger_system(log_level=self.config.bot.log_level)
        self.logger = get_logger(name="console", display="控制台", color=COLOR.BLUE)

        # Automatic task routing has one authoritative source.  Load it only
        # after logging is ready so the immutable route manifest and any
        # validation failure are always visible during startup.
        from src.kernel.config.models_loader import init_models_config

        init_models_config("config/models.toml")
        await self._optimize_async_network_runtime()

        # v2: 注册 LogStore
        from src.kernel.logger.logger import _global_log_store

        if _global_log_store is not None:
            container.register(LogStoreProtocol, _global_log_store)

        await self._preflight_llm_providers()

        # Step 3: Event Bus
        from src.kernel.event import get_event_bus

        self.event_bus = get_event_bus()
        container.register(EventBusProtocol, self.event_bus)  # v2

        # Step 4: Task Manager
        from src.kernel.concurrency import get_task_manager, get_watchdog

        self.task_manager = get_task_manager(
            process_workers=self.config.advanced.process_workers
        )
        container.register(type(self.task_manager), self.task_manager)  # v2

        # 仅在启用时启动 WatchDog
        if self.config.bot.enable_watchdog:
            get_watchdog().start()
        else:
            self.logger.warning("WatchDog 已禁用 (调试模式)")

        # Step 5: Scheduler
        from src.kernel.scheduler import get_unified_scheduler

        self.scheduler = get_unified_scheduler()
        container.register(SchedulerProtocol, self.scheduler)  # v2

        # Step 6: WatchDog（复用 Step 4 已启动的全局单例）
        self.watchdog = get_watchdog()

        # Step 7: Database
        from src.kernel.db import init_database_from_config

        db_cfg = self.config.database
        global_storage_backend = self.config.storage.backend
        use_mysql = global_storage_backend == "mysql"
        core_database_type = "mysql" if use_mysql else "sqlite"
        await init_database_from_config(
            database_type=core_database_type,
            sqlite_path=db_cfg.sqlite_path,
            mysql_host=db_cfg.mysql_host,
            mysql_port=db_cfg.mysql_port,
            mysql_database=db_cfg.mysql_database,
            mysql_user=db_cfg.mysql_user,
            mysql_password=db_cfg.mysql_password,
            mysql_charset=db_cfg.mysql_charset,
            mysql_ssl_mode=db_cfg.mysql_ssl_mode,
            mysql_ssl_ca=db_cfg.mysql_ssl_ca,
            mysql_ssl_cert=db_cfg.mysql_ssl_cert,
            mysql_ssl_key=db_cfg.mysql_ssl_key,
            connection_pool_size=db_cfg.connection_pool_size,
            connection_timeout=db_cfg.connection_timeout,
            echo=db_cfg.echo,
        )

        from src.core.utils.schema_sync import enforce_database_schema_consistency

        sync_stats = await enforce_database_schema_consistency()
        if self.logger:
            self.logger.debug(
                "数据库结构已对齐: "
                f"tables={sync_stats.tables_checked}, "
                f"add={sync_stats.columns_added}, "
                f"drop={sync_stats.columns_removed}, "
                f"preserve={sync_stats.columns_preserved}, "
                f"type={sync_stats.columns_type_altered}, "
                f"nullable={sync_stats.columns_nullability_altered}, "
                f"type_drift={sync_stats.type_mismatches}, "
                f"nullable_drift={sync_stats.nullability_mismatches}"
            )
            if sync_stats.requires_migration:
                self.logger.warning(
                    "检测到需要显式迁移的数据库结构差异；启动阶段已保留现有数据"
                )

        self._stats["db_connected"] = True

        # Step 8: VectorDB
        from src.kernel.vector_db import get_vector_db_service

        # 确保 data 目录存在
        Path("data/chroma_db").mkdir(parents=True, exist_ok=True)
        self.vector_db = get_vector_db_service("data/chroma_db")
        container.register(VectorStoreProtocol, self.vector_db)  # v2

        # Step 9: Storage
        from src.kernel.storage import JSONStore

        # 确保 data 目录存在
        Path("data/json_storage").mkdir(parents=True, exist_ok=True)
        self.storage = JSONStore("data/json_storage")
        self.ui.update_phase_status("内核", "就绪")

    async def _preflight_llm_providers(self) -> None:
        assert self.config is not None
        if not self.config.bot.llm_preflight_check:
            self.ui.update_phase_status("LLM 预检", "已跳过")
            return

        assert self.logger is not None

        import time

        import httpx

        from src.kernel.config.models_loader import get_models_config

        registry = get_models_config()
        providers = {
            name: registry.providers[name]
            for name in registry.snapshot.active_providers
        }
        if not providers:
            self.ui.update_phase_status("LLM 预检", "无配置")
            return

        timeout = float(self.config.bot.llm_preflight_timeout or 5.0)
        self.ui.update_phase_status("LLM 预检", "进行中...")

        results: list[tuple[str, bool, float]] = []
        start_all = time.perf_counter()

        async with httpx.AsyncClient(timeout=timeout) as client:

            async def _check_provider(name: str, provider) -> None:
                base_url = str(provider["base_url"]).rstrip("/")
                url = f"{base_url}/models"
                headers: dict[str, str] = {}
                api_key = provider.get("api_key", "")
                if isinstance(api_key, (list, tuple)):
                    api_key = api_key[0] if api_key else ""
                if api_key:
                    if provider.get("client_type", "openai") == "anthropic":
                        headers.update(
                            {
                                "x-api-key": str(api_key),
                                "anthropic-version": "2023-06-01",
                            }
                        )
                    else:
                        headers["Authorization"] = f"Bearer {api_key}"

                start = time.perf_counter()
                try:
                    resp = await client.get(url, headers=headers)
                    elapsed = time.perf_counter() - start
                    results.append((name, resp.status_code == 200, elapsed))
                except Exception:
                    elapsed = time.perf_counter() - start
                    results.append((name, False, elapsed))

            await asyncio.gather(
                *(
                    _check_provider(name, provider)
                    for name, provider in providers.items()
                ),
                return_exceptions=True,
            )

        total_elapsed = time.perf_counter() - start_all
        ok_providers = [name for name, ok, _ in results if ok]
        failed_providers = [name for name, ok, _ in results if not ok]

        # 只打一行摘要
        if ok_providers:
            summary = "  ".join(f"{name} OK" for name in ok_providers)
            self.logger.info(f"LLM: {summary}  ({total_elapsed:.1f}s)")
        if failed_providers and not ok_providers:
            self.logger.warning(
                f"LLM 预检: 所有提供商不可用 ({', '.join(failed_providers)})"
            )
        elif failed_providers:
            self.logger.debug(f"LLM 预检: {', '.join(failed_providers)} 不可用")

        self.ui.update_phase_status("LLM 预检", "已完成")

    async def _check_http_security(self, host: str, api_keys: list[str]) -> None:
        """检查 HTTP 服务器安全配置

        检测以下不安全的配置组合并发出警告：
        1. 监听地址为 0.0.0.0（对外开放）
        2. 未配置有效的 API 密钥或使用示例密钥

        Args:
            host: HTTP 服务器监听地址
            api_keys: API 密钥列表

        Warnings:
            当检测到不安全配置时，在终端输出警告信息
        """
        assert self.logger is not None

        # 不安全的示例密钥列表
        INSECURE_KEYS = {
            "secret-key-1",
            "test-key",
            "example-key",
            "demo-key",
            "default-key",
            "changeme",
            "password",
            "123456",
        }

        # 检查是否对外开放
        is_public = host == "0.0.0.0"

        # 检查密钥是否不安全（空或包含示例密钥）
        has_insecure_keys = not api_keys or any(
            key.lower() in INSECURE_KEYS for key in api_keys
        )

        if is_public and has_insecure_keys:
            self.logger.warning(f"HTTP 对外开放但无安全密钥 ({host})")
            self.logger.warning("建议设置 api_keys 或改为 127.0.0.1")
            self.ui.update_phase_status("HTTP服务器", "⚠️ 不安全配置")

    async def _initialize_core(self) -> None:
        """初始化 Core 层组件

        包括插件管理器、Action 管理器、Chatter 管理器、Command 管理器等。
        """
        assert self.config is not None

        # Step 1: 初始化 MessageReceiver 和 SinkManager
        from src.core.transport import MessageReceiver, SinkManager
        from src.core.transport.sink import set_sink_manager

        self.message_receiver = MessageReceiver()
        self.sink_manager = SinkManager(self.message_receiver)
        set_sink_manager(self.sink_manager)
        self.ui.update_phase_status("消息接收器", "已初始化")

        # Step 2: 导入其他manager以初始化
        from src.core.managers import (
            initialize_adapter_manager,
            initialize_distribution,
            initialize_event_manager,
            initialize_router_manager,
        )

        initialize_adapter_manager()
        initialize_router_manager()
        initialize_event_manager()
        initialize_distribution()

        self.ui.update_phase_status("核心管理器", "已初始化")

        # Step 3: 初始化 MCP 客户端工具接入
        from src.core.managers import get_mcp_manager

        self.mcp_manager = get_mcp_manager()
        await self.mcp_manager.initialize()
        self.ui.update_phase_status("MCP", "已初始化")

        # Step 4: 启动 HTTP 服务器
        from src.core.transport.router.http_server import get_http_server

        if self.config.http_router.enable_http_router:
            host = self.config.http_router.http_router_host
            port = self.config.http_router.http_router_port
            api_keys = self.config.http_router.api_keys

            # 端口预检：避免绑定失败触发 C 扩展层 segfault
            import socket

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if (
                    probe.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port))
                    == 0
                ):
                    self.logger.error(
                        f"HTTP 端口 {port} 已被占用，跳过 HTTP 服务器启动。"
                        "请检查是否有残留进程（ss -tlnp | grep {port}）。"
                    )
                    self.ui.update_phase_status("HTTP服务器", f"⚠️ 端口 {port} 被占用")
                    self.http_server = None
                    return

            # 安全检查：检测对外开放且无有效密钥的情况
            await self._check_http_security(host, api_keys)

            self.http_server = get_http_server(host=host, port=port)
            if self.config.http_router.enable_app_api_v1:
                from src.app.api.v1.chat_runtime import create_chat_command_service
                from src.app.api.v1.events import event_store_from_bot
                from src.app.api.v1.foundation import (
                    FoundationProjection,
                    snapshot_from_bot,
                )
                from src.app.api.v1.livestream_runtime import MountedLivestreamProvider
                from src.app.api.v1.mount import mount_api_v1
                from src.app.api.v1.p312_runtime import create_runtime_p312_providers
                from src.app.api.v1.tabletop_runtime import MountedTabletopProvider
                from src.app.api.v1.voice_runtime import MountedVoiceCallProvider

                foundation = FoundationProjection(
                    node_id=self.bot_name,
                    snapshot_provider=lambda: snapshot_from_bot(self),
                )
                self.app_api_mount = mount_api_v1(
                    self.http_server.app,
                    workspace_root=_PROJECT_ROOT,
                    database_path=self.config.http_router.app_api_v1_database_path,
                    allowed_origins=tuple(
                        self.config.http_router.app_api_v1_allowed_origins
                    ),
                    max_concurrency=(
                        self.config.http_router.app_api_v1_max_concurrency
                    ),
                    max_websocket_connections=(
                        self.config.http_router.app_api_v1_max_websocket_connections
                    ),
                    foundation=foundation,
                    event_store_provider=lambda: event_store_from_bot(self),
                    chat_command_service_factory=create_chat_command_service,
                    livestream_provider=MountedLivestreamProvider(),
                    voice_call_provider=MountedVoiceCallProvider(),
                    tabletop_provider=MountedTabletopProvider(),
                    p312_providers=create_runtime_p312_providers(),
                    task_manager=self.task_manager,
                )
                await self.app_api_mount.start()
            await self.http_server.start()

            # 挂载 LLM 请求体检视器（调试用 WebUI）
            try:
                from src.kernel.llm.request_inspector import get_inspector

                get_inspector().mount(self.http_server.app)
            except Exception:
                pass

            self.ui.update_phase_status("HTTP服务器", "已启动")

    async def _discover_plugins(self) -> None:
        """发现插件并解析依赖"""
        self.ui.update_phase_status("发现插件", "扫描中...")

        from src.core.components.loader import PluginLoader

        self.plugin_loader = PluginLoader()
        self.load_order, self.manifests = await self.plugin_loader.plan_plugins(
            self.plugins_dir
        )

        # 显示插件加载计划
        self.ui.display_plugin_plan(self.load_order, self.manifests)
        self.ui.update_phase_status("发现插件", f"已发现 {len(self.load_order)} 个插件")

    async def _install_plugin_deps(self) -> None:
        """Phase 3.5：批量安装所有插件声明的 Python 包依赖。

        在插件发现完成、插件加载开始之前执行：
        1. 读取全局开关 plugin_deps.enabled，若为 False 则跳过整个流程。
        2. 收集 load_order 中每个插件的 python_dependencies，构建 PluginDepSpec 列表。
        3. 调用 DependencyInstaller.install_for_plugins() 批量安装（去重，可选跳过已满足包）。
        4. 对安装失败且 dependencies_required=True 的插件，将其从 load_order / manifests 中移除。
        5. 对安装失败且 dependencies_required=False 的插件，仅记录 WARNING，保留加载队列。
        """
        assert self.config is not None

        cfg = self.config.plugin_deps

        if not cfg.enabled:
            self.ui.update_phase_status("依赖安装", "已跳过（已禁用）")
            return

        # 构建规格列表（忽略无依赖的插件）
        from src.core.components.utils import DependencyInstaller, PluginDepSpec

        specs = [
            PluginDepSpec(
                plugin_name=name,
                packages=list(self.manifests[name].python_dependencies),
                required=self.manifests[name].dependencies_required,
            )
            for name in self.load_order
            if self.manifests[name].python_dependencies
        ]

        if not specs:
            self.ui.update_phase_status("依赖安装", "无需安装")
            return

        total_pkgs = sum(len(s.packages) for s in specs)
        self.ui.update_phase_status("依赖安装", f"检查 {total_pkgs} 个依赖...")

        installer = DependencyInstaller()
        results = await installer.install_for_plugins(
            specs,
            command=cfg.install_command,
            skip_if_satisfied=cfg.skip_if_satisfied,
        )

        # 根据结果决定是否将插件从加载队列移除
        removed: list[str] = []
        for plugin_name, success in results.items():
            if not success:
                manifest = self.manifests[plugin_name]
                if manifest.dependencies_required:
                    removed.append(plugin_name)
                    if self.logger:
                        self.logger.warning(
                            f"插件 '{plugin_name}' 依赖安装失败且标记为必需，已从加载队列移除。"
                        )
                else:
                    if self.logger:
                        self.logger.warning(
                            f"插件 '{plugin_name}' 依赖安装失败但标记为非必需，仍尝试加载。"
                        )

        for name in removed:
            self.load_order.remove(name)
            self.load_results[name] = False

        status_parts: list[str] = []
        installed_count = sum(1 for ok in results.values() if ok)
        if installed_count:
            status_parts.append(f"{installed_count} 个插件依赖已就绪")
        if removed:
            status_parts.append(f"{len(removed)} 个插件因依赖失败被移除")
        self.ui.update_phase_status("依赖安装", "、".join(status_parts) or "完成")

    async def _load_plugins(self) -> dict[str, bool]:
        """加载插件"""
        self.ui.update_phase_status("加载插件", "启动中...")

        from src.core.managers import get_plugin_manager

        self.plugin_manager = get_plugin_manager()

        for plugin_name in self.load_order:
            manifest = self.manifests[plugin_name]
            try:
                success = await self.plugin_manager.load_plugin_from_manifest(
                    manifest._source_path, manifest
                )
                self.load_results[plugin_name] = success
                version = str(getattr(manifest, "version", "") or "")
                self.ui.update_plugin_progress(plugin_name, success, version=version)

            except Exception as e:
                self.load_results[plugin_name] = False
                self.ui.update_plugin_progress(plugin_name, False, version="")
                if self.logger:
                    self.logger.error(f"插件 '{plugin_name}' 加载失败: {e}")
                # 继续加载其他插件（容错策略）

        # 更新统计
        self._stats["plugins_loaded"] = len(
            [r for r in self.load_results.values() if r]
        )
        self._stats["plugins_failed"] = len(
            [r for r in self.load_results.values() if not r]
        )

        # 发布插件加载完成事件
        assert self.event_bus is not None

        from src.core.components.types import EventType

        await self.event_bus.publish(EventType.ON_ALL_PLUGIN_LOADED, {})

        return self.load_results

    async def run(self) -> None:
        """主运行循环

        Raises:
            BotRuntimeError: Bot 未初始化
        """
        if not self._initialized:
            raise BotRuntimeError("Bot 未初始化。请先调用 initialize()。")

        # 断言核心组件已初始化（由于_initialized=True，这些不应该为None）
        assert self.logger is not None
        assert self.scheduler is not None
        assert self.task_manager is not None

        self._running = True

        # 启动调度器
        if not self.scheduler.is_running:
            raise BotRuntimeError("Scheduler stopped before runtime startup completed")

        # 触发 ON_START 事件（所有初始化完成，系统即将进入运行状态）
        try:
            from src.core.components.types import EventType

            assert self.event_bus is not None
            await self.event_bus.publish(EventType.ON_START, {})
            if self.logger:
                self.logger.info("已触发 ON_START 事件")
        except Exception as e:
            if self.logger:
                self.logger.warning(f"触发 ON_START 事件失败: {e}")

        # 启动实时仪表盘（如果 UI 级别为 VERBOSE）
        if self.ui.level == UILevel.VERBOSE:
            self.ui.start_live_dashboard()

        # 启动信号处理器
        signal_handler = SignalHandler(self)
        signal_handler.register_signals()

        # 创建交互式命令解析器
        from .command_parser import CommandParser

        command_parser = CommandParser(self)

        self.logger.info("Elysium 已苏醒。输入 /help 查看命令。")

        # 主循环
        try:
            while self._running:
                try:
                    # 读取并执行命令（内部使用短超时轮询）
                    should_continue = await command_parser.read_and_execute()

                    if not should_continue:
                        break

                    # 更新仪表盘统计
                    if self.ui.level == UILevel.VERBOSE:
                        await self._update_runtime_stats()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.logger.error(f"主循环错误: {e}", exc_info=e)

        finally:
            command_parser.close()

            # 停止实时仪表盘
            if self.ui.level == UILevel.VERBOSE:
                self.ui.stop_live_dashboard()

            # 恢复信号处理器
            signal_handler.restore_handlers()

    async def _update_runtime_stats(self) -> None:
        """更新运行时统计数据（用于仪表盘）"""
        assert self.task_manager is not None

        stats = {
            "plugins_loaded": self._stats["plugins_loaded"],
            "plugins_failed": self._stats["plugins_failed"],
            "components_by_type": self._stats["components_by_type"],
            "tasks_active": len(self.task_manager.get_all_tasks()),
            "db_connected": self._stats["db_connected"],
            "scheduler_running": self._stats["scheduler_running"],
        }

        self.ui.update_dashboard_stats(stats)

    async def reload_plugin(self, plugin_name: str | None = None) -> dict[str, bool]:
        """重新加载插件

        Args:
            plugin_name: 插件名，None 表示重新加载所有插件

        Returns:
            加载结果字典 {plugin_name: success}
        """
        results = {}

        assert self.plugin_manager is not None
        try:
            if plugin_name:
                # 单插件重载
                if plugin_name not in self.manifests:
                    self.ui.display_error(f"未知插件: {plugin_name}")
                    return {plugin_name: False}

                # 卸载
                await self.plugin_manager.unload_plugin(plugin_name)

                # 重新加载
                manifest = self.manifests[plugin_name]
                success = await self.plugin_manager.load_plugin_from_manifest(
                    manifest._source_path, manifest
                )
                results[plugin_name] = success

            else:
                # 全部重载
                await self._unload_all_plugins()
                results = await self._load_plugins()

        except Exception as e:
            if self.logger:
                self.logger.error(f"插件重载失败: {e}", exc_info=e)
            if plugin_name:
                results[plugin_name] = False

        return results

    async def _unload_all_plugins(self) -> None:
        """卸载所有插件"""
        if not self.plugin_manager:
            return

        # 按相反顺序卸载
        for plugin_name in reversed(self.load_order):
            try:
                await self.plugin_manager.unload_plugin(plugin_name)
                if self.logger:
                    self.logger.info(f"插件已卸载: {plugin_name}")
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"插件 '{plugin_name}' 卸载失败: {e}")

        # 清空统计
        self.load_results.clear()
        self._stats["plugins_loaded"] = 0
        self._stats["plugins_failed"] = 0

    async def shutdown(
        self,
        timeout: float = 30.0,
        *,
        raise_on_error: bool = True,
    ) -> None:
        """优雅关闭 Bot

        Args:
            timeout: 关闭超时时间（秒）

        Raises:
            BotShutdownError: 关闭失败
        """
        if self._shutdown_requested:
            return

        self._shutdown_requested = True
        self._running = False

        if self.logger:
            self.logger.info("正在关闭 Elysium...")
        else:
            print("正在关闭 Elysium...")

        if self.logger:
            self.logger.info("停止接受新任务...")

        errors: list[tuple[str, Exception]] = []
        loop = asyncio.get_running_loop()
        shutdown_deadline = loop.time() + max(0.0, timeout)

        def _consume_step_result(task: asyncio.Task[None]) -> None:
            """Consume a detached shutdown step's eventual exception."""

            if task.cancelled():
                return
            try:
                task.exception()
            except asyncio.CancelledError:
                return

        async def _run_step(name: str, operation) -> None:
            remaining = shutdown_deadline - loop.time()
            if remaining <= 0:
                exc = TimeoutError(f"shutdown deadline exhausted before step '{name}'")
                errors.append((name, exc))
                return

            step_task = asyncio.create_task(
                operation(),
                name=f"shutdown:{name}",
            )
            try:
                done, _ = await asyncio.wait({step_task}, timeout=remaining)
                if not done:
                    step_task.cancel()
                    step_task.add_done_callback(_consume_step_result)
                    raise TimeoutError(
                        f"shutdown step '{name}' exceeded the remaining "
                        f"{remaining:.2f}s deadline"
                    )
                await step_task
            except asyncio.CancelledError:
                step_task.cancel()
                raise
            except Exception as exc:
                errors.append((name, exc))
                if self.logger:
                    self.logger.error(
                        f"关闭步骤失败 [{name}]: {exc}",
                        exc_info=exc,
                    )
                else:
                    print(f"关闭步骤失败 [{name}]: {exc}")

        async def _publish_stop() -> None:
            if self.event_bus is None:
                return
            from src.core.components.types import EventType

            await self.event_bus.publish(EventType.ON_STOP, {})
            if self.logger:
                self.logger.info("已触发 ON_STOP 事件")

        async def _stop_stream_loops() -> None:
            from src.core.transport.distribution.stream_loop_manager import (
                get_stream_loop_manager,
            )

            await get_stream_loop_manager().stop()

        async def _stop_adapters() -> None:
            from src.core.managers.adapter_manager import get_adapter_manager

            results = await get_adapter_manager().stop_all_adapters()
            failures = [
                signature for signature, success in results.items() if not success
            ]
            if failures:
                message = "适配器首次关闭未完成，将在插件卸载后重试: " + ", ".join(
                    sorted(failures)
                )
                if self.logger:
                    self.logger.warning(message)
                else:
                    print(message)

        async def _verify_adapters_stopped() -> None:
            from src.core.managers.adapter_manager import get_adapter_manager

            manager = get_adapter_manager()
            remaining = manager.list_active_adapters()
            if not remaining:
                return

            await manager.stop_all_adapters()
            remaining = manager.list_active_adapters()
            if remaining:
                raise RuntimeError(
                    "adapter shutdown failed after retry: "
                    + ", ".join(sorted(remaining))
                )

            if self.logger:
                self.logger.info("适配器已在插件卸载后重试关闭完成")

        async def _stop_scheduler() -> None:
            if self.scheduler is not None:
                await self.scheduler.stop()
                self._stats["scheduler_running"] = False

        async def _stop_http_server() -> None:
            if self.http_server and self.http_server.is_running():
                if self.logger:
                    self.logger.info("停止 HTTP 服务器...")
                await self.http_server.stop()

        async def _close_app_api_mount() -> None:
            mount = getattr(self, "app_api_mount", None)
            if mount is None:
                return
            await mount.aclose()
            self.app_api_mount = None

        async def _cleanup_mcp() -> None:
            if self.mcp_manager is not None:
                if self.logger:
                    self.logger.info("关闭 MCP 客户端连接...")
                await self.mcp_manager.cleanup()

        async def _stop_watchdog() -> None:
            if self.watchdog is not None:
                self.watchdog.stop()

        async def _stop_tasks() -> None:
            if self.task_manager is None:
                return
            try:
                for task_info in self.task_manager.get_active_tasks():
                    self.task_manager.cancel_task(task_info.task_id)
                await asyncio.wait_for(
                    self.task_manager.wait_all_tasks(),
                    timeout=timeout,
                )
            finally:
                self.task_manager.cleanup_tasks()
                self.task_manager.shutdown_process_pool(wait=False)

        async def _close_database() -> None:
            from src.kernel.db import close_engine

            await close_engine()
            self._stats["db_connected"] = False

        async def _close_model_clients() -> None:
            from src.kernel.llm.model_client import close_default_model_clients

            await close_default_model_clients()

        async def _close_vector_databases() -> None:
            from src.kernel.vector_db import close_all_vector_db_services

            await close_all_vector_db_services()

        async def _close_network_runtime() -> None:
            self._shutdown_async_network_runtime()

        async def _close_logger() -> None:
            from src.kernel.logger import shutdown_logger_system_async

            await shutdown_logger_system_async()

        steps = (
            ("on_stop", _publish_stop),
            ("stream_loops", _stop_stream_loops),
            ("adapters", _stop_adapters),
            ("http_server", _stop_http_server),
            ("app_api_mount", _close_app_api_mount),
            ("plugins", self._unload_all_plugins),
            ("adapters_verify", _verify_adapters_stopped),
            ("scheduler", _stop_scheduler),
            ("mcp", _cleanup_mcp),
            ("watchdog", _stop_watchdog),
            ("tasks", _stop_tasks),
            ("llm_clients", _close_model_clients),
            ("database", _close_database),
            ("vector_database", _close_vector_databases),
            ("network_runtime", _close_network_runtime),
            ("logger", _close_logger),
        )
        for name, operation in steps:
            await _run_step(name, operation)

        self._initialized = False
        if not errors:
            self.ui.display_success("关闭完成")
            return

        summary = "; ".join(f"{name}: {exc}" for name, exc in errors)
        if raise_on_error:
            raise BotShutdownError(f"关闭失败: {summary}") from errors[0][1]
        self.ui.display_warning(f"关闭完成，但有 {len(errors)} 个步骤失败")

    async def start(self) -> None:
        """完整的启动流程（初始化 + 运行 + 关闭）

        这是推荐的启动方式。
        """
        primary_error: Exception | None = None
        try:
            await self.initialize()
            await self.run()

        except KeyboardInterrupt:
            # 用户中断
            if self.logger:
                self.logger.info("用户中断")
            else:
                print("\n[用户中断]")
        except Exception as e:
            primary_error = e
            if self.logger:
                self.logger.error(f"Bot 错误: {e}", exc_info=e)
            else:
                print(f"\n[致命错误: {e}]")
            raise
        finally:
            try:
                await self.shutdown()
            except BotShutdownError as shutdown_error:
                if primary_error is None:
                    raise
                if self.logger:
                    self.logger.error(
                        f"主错误后的关闭过程仍有失败: {shutdown_error}",
                        exc_info=shutdown_error,
                    )


__all__ = ["Bot"]
