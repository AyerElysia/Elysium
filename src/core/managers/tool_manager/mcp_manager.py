"""MCP Manager implementation.

本模块提供 MCPManager 类，负责管理 MCP 服务器连接、工具发现、
server metadata 缓存和工具调用。
"""

import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Coroutine

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from src.core.config.mcp_config import MCPConfig, is_mcp_server_defer_loading
from src.core.managers.tool_manager.mcp_adapter import MCPToolAdapter
from src.kernel.logger import get_logger

logger = get_logger("mcp_manager")
_MCP_CONNECT_TIMEOUT_SECONDS = 30.0
_MCP_INITIALIZE_TIMEOUT_SECONDS = 35.0
_MCP_CLEANUP_TIMEOUT_SECONDS = 10.0
_MCP_PARTIAL_CLEANUP_TIMEOUT_SECONDS = 5.0
_MCP_TOOL_TIMEOUT_SECONDS = 60.0


def _extract_configured_instructions(params: Any) -> str:
    """从 MCP 服务配置中提取手动 instructions。"""
    if not isinstance(params, dict):
        return ""

    for key in ("instructions", "instruction"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


@dataclass(frozen=True, slots=True)
class MCPServerMetadata:
    """已连接 MCP 服务器的元数据快照。"""

    server_name: str
    instructions: str
    server_label: str
    defer_loading: bool


class MCPManager:
    """MCP 管理器。"""

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}
        self._exit_stack = AsyncExitStack()
        self._adapters: dict[str, MCPToolAdapter] = {}
        self._tool_signatures: set[str] = set()
        self._server_metadata: dict[str, MCPServerMetadata] = {}
        self._tool_classes_by_server: dict[str, list[type[Any]]] = {}
        self._lifecycle_lock = asyncio.Lock()
        logger.info("MCP 管理器初始化")

    async def initialize(self) -> None:
        """Initialize all configured servers under one lifecycle lock."""

        async with self._lifecycle_lock:
            await self._initialize_unlocked()

    async def _initialize_unlocked(self) -> None:
        """初始化 MCP 管理器。"""
        if self._sessions or self._adapters or self._tool_signatures:
            await self._cleanup_unlocked()

        try:
            from src.core.config import get_mcp_config

            config = get_mcp_config()
        except Exception:
            logger.warning("MCP 配置尚未初始化，尝试使用默认配置")
            config = MCPConfig()

        if not config.mcp.enabled:
            logger.info("MCP 功能未启用")
            return

        connection_tasks: list[Coroutine[Any, Any, bool]] = []
        if config.mcp.stdio_servers:
            logger.info(f"开始连接 Stdio MCP 服务器: {list(config.mcp.stdio_servers.keys())}")
            for name, params in config.mcp.stdio_servers.items():
                command = params.get("command")
                args = params.get("args", [])
                env = params.get("env")

                if command:
                    connection_tasks.append(
                        self._run_connection_task(
                            name,
                            self.connect_stdio_server(name, command, args, env),
                        )
                    )
                else:
                    logger.error(f"MCP 服务器 {name} 配置缺少 command")

        if config.mcp.sse_servers:
            logger.info(f"开始连接 SSE MCP 服务器: {list(config.mcp.sse_servers.keys())}")
            for name, params in config.mcp.sse_servers.items():
                connection_tasks.append(
                    self._run_connection_task(
                        name,
                        self.connect_sse_server_from_config(name, params),
                    )
                )

        if config.mcp.streamable_http_servers:
            logger.info(
                "开始连接 Streamable HTTP MCP 服务器: "
                f"{list(config.mcp.streamable_http_servers.keys())}"
            )
            for name, params in config.mcp.streamable_http_servers.items():
                connection_tasks.append(
                    self._run_connection_task(
                        name,
                        self.connect_streamable_http_server_from_config(name, params),
                    )
                )

        if connection_tasks:
            try:
                async with asyncio.timeout(_MCP_INITIALIZE_TIMEOUT_SECONDS):
                    await asyncio.gather(*connection_tasks)
            except TimeoutError:
                logger.error(
                    "MCP 初始化超过总时限，未完成的服务器连接已取消"
                )

    async def _run_connection_task(
        self,
        name: str,
        coro: Coroutine[Any, Any, bool],
    ) -> bool:
        """在隔离任务中执行一次 MCP 连接。"""
        from src.kernel.concurrency import get_task_manager

        task_info = get_task_manager().create_task(
            coro,
            name=f"mcp_connect_{name}",
            daemon=True,
        )
        if task_info.task is None:
            logger.error(f"MCP 服务器连接任务创建失败: {name}")
            return False

        try:
            return bool(await task_info.task)
        except asyncio.CancelledError:
            logger.info(f"MCP 服务器连接任务被取消: {name}")
            raise
        except BaseExceptionGroup as e:
            logger.error(f"MCP 服务器连接任务失败 {name}: {e}")
            return False
        except Exception as e:
            logger.error(f"MCP 服务器连接任务失败 {name}: {e}")
            return False

    async def _close_connection_stack(
        self,
        connection_stack: AsyncExitStack,
        name: str,
    ) -> None:
        """Bound cleanup of resources from a failed server connection."""

        try:
            async with asyncio.timeout(_MCP_PARTIAL_CLEANUP_TIMEOUT_SECONDS):
                await connection_stack.aclose()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"清理 MCP 服务器部分连接失败 {name}: {exc}")

    async def connect_stdio_server(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> bool:
        """连接 Stdio MCP 服务器。"""
        connection_stack = AsyncExitStack()
        try:
            async with asyncio.timeout(_MCP_CONNECT_TIMEOUT_SECONDS):
                server_params = StdioServerParameters(
                    command=command,
                    args=args,
                    env={**os.environ, **(env or {})},
                )
                stdio_transport = await connection_stack.enter_async_context(
                    stdio_client(server_params)
                )
                await self._connect_session(
                    name,
                    stdio_transport,
                    connection_stack,
                )
            self._adopt_connection_stack(connection_stack)
            return True

        except asyncio.CancelledError:
            await self._close_connection_stack(connection_stack, name)
            raise
        except BaseExceptionGroup as e:
            await self._close_connection_stack(connection_stack, name)
            logger.error(f"连接 MCP 服务器失败 {name}: {e}")
            return False
        except Exception as e:
            await self._close_connection_stack(connection_stack, name)
            logger.error(f"连接 MCP 服务器失败 {name}: {e}")
            return False

    async def connect_sse_server_from_config(
        self,
        name: str,
        params: str | dict[str, Any],
    ) -> bool:
        """根据配置连接 SSE MCP 服务器。"""
        if isinstance(params, str):
            return await self.connect_sse_server(name, params)

        url = params.get("url")
        if not isinstance(url, str) or not url:
            logger.error(f"SSE MCP 服务器 {name} 配置缺少 url")
            return False

        return await self.connect_sse_server(
            name=name,
            url=url,
            headers=params.get("headers"),
            timeout=float(params.get("timeout", 5)),
            sse_read_timeout=float(params.get("sse_read_timeout", 300)),
        )

    async def connect_sse_server(
        self,
        name: str,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: float = 5,
        sse_read_timeout: float = 300,
    ) -> bool:
        """连接 SSE MCP 服务器。"""
        connection_stack = AsyncExitStack()
        try:
            async with asyncio.timeout(_MCP_CONNECT_TIMEOUT_SECONDS):
                sse_transport = await connection_stack.enter_async_context(
                    sse_client(
                        url=url,
                        headers=headers,
                        timeout=timeout,
                        sse_read_timeout=sse_read_timeout,
                    )
                )
                await self._connect_session(
                    name,
                    sse_transport,
                    connection_stack,
                )
            self._adopt_connection_stack(connection_stack)
            return True
        except asyncio.CancelledError:
            await self._close_connection_stack(connection_stack, name)
            raise
        except BaseExceptionGroup as e:
            await self._close_connection_stack(connection_stack, name)
            logger.error(f"连接 SSE MCP 服务器失败 {name}: {e}")
            return False
        except Exception as e:
            await self._close_connection_stack(connection_stack, name)
            logger.error(f"连接 SSE MCP 服务器失败 {name}: {e}")
            return False

    async def connect_streamable_http_server_from_config(
        self,
        name: str,
        params: str | dict[str, Any],
    ) -> bool:
        """根据配置连接 Streamable HTTP MCP 服务器。"""
        if isinstance(params, str):
            return await self.connect_streamable_http_server(name, params)

        url = params.get("url")
        if not isinstance(url, str) or not url:
            logger.error(f"Streamable HTTP MCP 服务器 {name} 配置缺少 url")
            return False

        return await self.connect_streamable_http_server(
            name=name,
            url=url,
            headers=params.get("headers"),
            timeout=float(params.get("timeout", 30)),
        )

    async def connect_streamable_http_server(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> bool:
        """连接 Streamable HTTP MCP 服务器。"""
        connection_stack = AsyncExitStack()
        try:
            async with asyncio.timeout(_MCP_CONNECT_TIMEOUT_SECONDS):
                http_client: httpx.AsyncClient | None = None
                if headers or timeout:
                    http_client = await connection_stack.enter_async_context(
                        httpx.AsyncClient(headers=headers, timeout=timeout)
                    )

                http_transport = await connection_stack.enter_async_context(
                    streamable_http_client(url, http_client=http_client)
                )
                await self._connect_session(
                    name,
                    http_transport,
                    connection_stack,
                )
            self._adopt_connection_stack(connection_stack)
            return True
        except asyncio.CancelledError:
            await self._close_connection_stack(connection_stack, name)
            raise
        except BaseExceptionGroup as e:
            await self._close_connection_stack(connection_stack, name)
            logger.error(f"连接 Streamable HTTP MCP 服务器失败 {name}: {e}")
            return False
        except Exception as e:
            await self._close_connection_stack(connection_stack, name)
            logger.error(f"连接 Streamable HTTP MCP 服务器失败 {name}: {e}")
            return False

    def _adopt_connection_stack(self, connection_stack: AsyncExitStack) -> None:
        """把已成功建立的单服务资源转交给管理器总生命周期。"""
        adopted_stack = connection_stack.pop_all()
        self._exit_stack.push_async_callback(adopted_stack.aclose)

    def _cache_server_metadata(self, name: str, initialize_result: Any) -> None:
        """缓存已连接 MCP 服务器的元数据。"""
        raw_instructions = getattr(initialize_result, "instructions", None)
        instructions = raw_instructions.strip() if isinstance(raw_instructions, str) else ""
        defer_loading = True

        try:
            from src.core.config import get_mcp_config

            config = get_mcp_config().mcp
            configured_params = (
                config.stdio_servers.get(name)
                or config.sse_servers.get(name)
                or config.streamable_http_servers.get(name)
            )
            configured_instructions = _extract_configured_instructions(configured_params)
            if configured_instructions:
                instructions = configured_instructions
            defer_loading = is_mcp_server_defer_loading(configured_params)
        except Exception:
            pass

        server_info = getattr(initialize_result, "serverInfo", None)
        server_label = name
        info_name = getattr(server_info, "name", None)
        info_version = getattr(server_info, "version", None)
        if isinstance(info_name, str) and info_name.strip():
            server_label = info_name.strip()
            if isinstance(info_version, str) and info_version.strip():
                server_label = f"{server_label} {info_version.strip()}"

        self._server_metadata[name] = MCPServerMetadata(
            server_name=name,
            instructions=instructions,
            server_label=server_label,
            defer_loading=defer_loading,
        )

    async def _connect_session(
        self,
        name: str,
        transport: tuple[Any, ...],
        connection_stack: AsyncExitStack,
    ) -> None:
        """从 MCP 传输对象创建会话并发现工具。"""
        if name in self._sessions:
            raise RuntimeError(f"MCP 服务器已连接: {name}")
        read, write = transport[0], transport[1]
        session = await connection_stack.enter_async_context(ClientSession(read, write))

        initialize_result = await session.initialize()
        await self._discover_tools(name, session)
        self._sessions[name] = session
        self._cache_server_metadata(name, initialize_result)
        logger.info(f"已连接 MCP 服务器: {name}")

    async def _discover_tools(self, server_name: str, session: ClientSession) -> None:
        """发现并注册工具。"""
        registered_signatures: list[str] = []
        registered_adapter_names: list[str] = []
        try:
            from src.core.components.base.tool import BaseTool
            from src.core.components.registry import get_global_registry
            from src.core.components.state_manager import get_global_state_manager
            from src.core.components.types import ComponentState, ComponentType

            result = await session.list_tools()
            registry = get_global_registry()
            state_manager = get_global_state_manager()

            for tool in result.tools:
                adapter = MCPToolAdapter(server_name, tool, self)
                logger.debug(f"发现 MCP 工具: {adapter.tool_name}")

                class DynamicMCPTool(BaseTool):
                    """动态生成的 MCP 工具代理类。"""

                    plugin_name = "mcp_provider"
                    tool_name = adapter.tool_name
                    tool_description = adapter.description
                    _adapter = adapter

                    async def execute(self, **kwargs: Any) -> tuple[bool, str | dict[str, Any]]:
                        result = await self._adapter.execute(kwargs)

                        is_error = result.get("is_error", False)
                        content = result.get("content", "")

                        return not is_error, content

                    @classmethod
                    def to_schema(cls) -> dict[str, Any]:
                        return cls._adapter.get_schema()

                DynamicMCPTool.__name__ = f"MCPTool_{adapter.tool_name}"

                signature = f"mcp_provider:{ComponentType.TOOL.value}:{adapter.tool_name}"
                DynamicMCPTool._plugin_ = "mcp_provider"
                DynamicMCPTool._signature_ = signature

                try:
                    registry.register(DynamicMCPTool, signature)
                    registered_signatures.append(signature)
                    state_manager.set_state(signature, ComponentState.ACTIVE)
                    self._tool_signatures.add(signature)
                    self._adapters[adapter.tool_name] = adapter
                    registered_adapter_names.append(adapter.tool_name)
                    self._tool_classes_by_server.setdefault(server_name, []).append(
                        DynamicMCPTool
                    )
                    logger.info(f"已动态注册 MCP 工具: {signature}")
                except ValueError as e:
                    logger.warning(f"注册 MCP 工具失败 ({signature}): {e}")

        except Exception as e:
            from src.core.components.registry import get_global_registry
            from src.core.components.state_manager import get_global_state_manager

            registry = get_global_registry()
            state_manager = get_global_state_manager()
            for signature in registered_signatures:
                registry.unregister(signature)
                state_manager.remove_state(signature)
                self._tool_signatures.discard(signature)
            for tool_name in registered_adapter_names:
                self._adapters.pop(tool_name, None)
            self._tool_classes_by_server.pop(server_name, None)
            logger.error(f"从 {server_name} 获取工具列表失败: {e}")
            raise RuntimeError(f"MCP 工具发现失败: {server_name}") from e

    def get_connected_server_metadata(self) -> list[MCPServerMetadata]:
        """返回当前已连接 MCP 服务器的元数据列表。"""
        return [
            self._server_metadata[name]
            for name in sorted(self._sessions)
            if name in self._server_metadata
        ]

    def get_tool_classes_for_servers(
        self,
        server_names: list[str] | None = None,
    ) -> list[type[Any]]:
        """返回指定 MCP 服务器暴露出的动态工具类。"""
        selected_server_names = server_names or list(self._tool_classes_by_server)
        selected_tools: list[type[Any]] = []
        seen_classes: set[type[Any]] = set()

        for server_name in selected_server_names:
            for tool_cls in self._tool_classes_by_server.get(server_name, []):
                if tool_cls in seen_classes:
                    continue
                seen_classes.add(tool_cls)
                selected_tools.append(tool_cls)

        return selected_tools

    def get_deferred_tool_classes(self) -> list[type[Any]]:
        """返回配置为 defer_loading 的 MCP 动态工具类。"""
        deferred_server_names = [
            metadata.server_name
            for metadata in self.get_connected_server_metadata()
            if metadata.defer_loading
        ]
        return self.get_tool_classes_for_servers(deferred_server_names)

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float = _MCP_TOOL_TIMEOUT_SECONDS,
    ) -> Any:
        """调用 MCP 工具 (底层调用)。"""
        session = self._sessions.get(server_name)
        if not session:
            raise RuntimeError(f"MCP 服务器未连接: {server_name}")

        return await asyncio.wait_for(
            session.call_tool(tool_name, arguments),
            timeout=timeout,
        )

    async def cleanup(self) -> None:
        """Clean up all resources under the lifecycle lock."""

        async with self._lifecycle_lock:
            await self._cleanup_unlocked()

    async def _cleanup_unlocked(self) -> None:
        """清理资源。"""
        from src.core.components.registry import get_global_registry
        from src.core.components.state_manager import get_global_state_manager

        registry = get_global_registry()
        state_manager = get_global_state_manager()
        for signature in list(self._tool_signatures):
            registry.unregister(signature)
            state_manager.remove_state(signature)

        try:
            async with asyncio.timeout(_MCP_CLEANUP_TIMEOUT_SECONDS):
                await self._exit_stack.aclose()
        except asyncio.CancelledError as e:
            logger.warning(f"MCP 管理器关闭连接时被取消: {e}")
            raise
        except TimeoutError:
            logger.warning(
                f"MCP 管理器关闭连接超过 {_MCP_CLEANUP_TIMEOUT_SECONDS:.0f}s 时限"
            )
        except BaseExceptionGroup as e:
            logger.warning(f"MCP 管理器关闭连接时出现异常，已忽略: {e}")
        except Exception as e:
            logger.warning(f"MCP 管理器关闭连接时出现异常，已忽略: {e}")
        finally:
            self._exit_stack = AsyncExitStack()
            self._sessions.clear()
            self._adapters.clear()
            self._tool_signatures.clear()
            self._server_metadata.clear()
            self._tool_classes_by_server.clear()
        logger.info("MCP 管理器资源已清理")


_mcp_manager = MCPManager()


def get_mcp_manager() -> MCPManager:
    """获取全局 MCP 管理器实例。"""
    return _mcp_manager
