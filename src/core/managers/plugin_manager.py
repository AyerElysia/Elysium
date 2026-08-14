"""插件管理器。

本模块提供插件管理器，负责“单个插件”的导入执行、组件注册与生命周期钩子调用。

宏观层面的插件发现、manifest 读取、依赖/版本检查与加载顺序计算由
src.core.components.loader.PluginLoader 负责。
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.core.components.types import ComponentState, ComponentType
from src.kernel.logger import get_logger

if TYPE_CHECKING:
    from src.core.components.base.plugin import BasePlugin
    from src.core.components.loader import PluginManifest


logger = get_logger("plugin_manager")


class PluginManager:
    """插件管理器。

    负责单个插件的导入、组件注册、卸载和生命周期管理。

    Attributes:
        _loaded_plugins: 已加载的插件实例字典
        _manifests: 插件清单字典
        _plugin_paths: 插件路径字典

    Examples:
        >>> manager = PluginManager()
        >>> await manager.load_all_plugins("plugins")
        >>> plugin = manager.get_plugin("my_plugin")
        >>> await manager.unload_plugin("my_plugin")
    """

    def __init__(self) -> None:
        """初始化插件管理器。"""
        self._loaded_plugins: dict[str, "BasePlugin"] = {}
        self._manifests: dict[str, PluginManifest] = {}
        self._plugin_paths: dict[str, str] = {}
        self._failed_plugins: dict[str, str] = {}
        self._archive_tmpdirs: dict[str, str] = {}  # 压缩包插件解压临时目录
        self._lifecycle_lock = asyncio.Lock()

        logger.info("插件管理器初始化完成")

    async def load_plugin_from_manifest(
        self, plugin_path: str, manifest: PluginManifest
    ) -> bool:
        """加载单个插件（manifest 已由 loader 宏观层校验并提供）。"""
        async with self._lifecycle_lock:
            return await self._load_plugin_from_manifest_unlocked(
                plugin_path,
                manifest,
            )

    async def _load_plugin_from_manifest_unlocked(
        self,
        plugin_path: str,
        manifest: PluginManifest,
    ) -> bool:
        """在生命周期锁内执行插件加载事务。"""
        plugin_name = manifest.name

        # 1. 检查是否已加载
        if plugin_name in self._loaded_plugins:
            logger.warning(f"插件 '{plugin_name}' 已经加载")
            return True

        # 2. 加载插件模块（导入会触发 @register_plugin 执行）
        if plugin_path.endswith((".zip", ".mfp")):
            plugin_module = await self._load_from_archive(plugin_path, manifest)
        else:
            plugin_module = await self._load_from_folder(plugin_path, manifest)

        if not plugin_module:
            error_msg = "插件模块加载失败"
            self._failed_plugins[plugin_name] = error_msg
            await self._rollback_failed_load(plugin_name, plugin_path)
            return False

        # 3. 查找 @register_plugin 注册的插件类
        from src.core.components.loader import get_plugin_class

        plugin_class = get_plugin_class(plugin_name)
        if not plugin_class:
            error_msg = "插件类未注册（未使用 @register_plugin 装饰器）"
            self._failed_plugins[plugin_name] = error_msg
            logger.error(f"插件 '{plugin_name}' 加载失败: {error_msg}")
            await self._rollback_failed_load(plugin_name, plugin_path)
            return False

        plugin_instance: BasePlugin | None = None
        try:
            # 4. 通过插件类属性 configs 加载配置
            from src.core.components.base.config import BaseConfig
            from src.core.managers.config_manager import get_config_manager

            config_instance = None
            has_config = False
            config_classes = plugin_class.configs  # type: ignore[attr-defined]

            if not isinstance(config_classes, list):
                logger.warning(
                    f"插件 '{plugin_name}' 的 configs 不是 list 类型，将忽略并继续兼容旧逻辑"
                )
                config_classes = []

            for config_cls in config_classes:
                if isinstance(config_cls, type) and issubclass(config_cls, BaseConfig):
                    config_instance = get_config_manager().load_config(
                        plugin_name,
                        config_cls,
                    )
                    has_config = True
                    break

            # 5. 实例化插件（注入已加载配置）
            plugin_instance = plugin_class(config=config_instance)  # type: ignore

            if not has_config:
                logger.debug(
                    f"插件 '{plugin_name}' 未通过类属性 configs 声明配置，使用空配置"
                )

            # 6. 注册组件到全局注册表
            await self._register_components(plugin_instance)

            # 7. 生命周期钩子失败意味着插件没有成功启动，必须回滚。
            await plugin_instance.on_plugin_loaded()

            from src.core.managers.event_manager import get_event_manager

            await get_event_manager().register_plugin_handlers(
                plugin_name,
                plugin_instance=plugin_instance,
            )

            from src.core.components.state_manager import get_global_state_manager
            from src.core.components.types import build_signature

            await get_global_state_manager().set_state_async(
                build_signature(plugin_name, ComponentType.PLUGIN, plugin_name),
                ComponentState.ACTIVE,
            )
        except Exception as exc:
            error_msg = f"插件启动事务失败: {exc}"
            self._failed_plugins[plugin_name] = error_msg
            logger.error(f"插件 '{plugin_name}' 加载失败: {error_msg}")
            await self._rollback_failed_load(
                plugin_name,
                plugin_path,
                plugin_instance,
            )
            return False

        # 8. 仅在所有启动步骤成功后发布可见状态。
        self._loaded_plugins[plugin_name] = plugin_instance
        self._manifests[plugin_name] = manifest
        self._plugin_paths[plugin_name] = plugin_path
        self._failed_plugins.pop(plugin_name, None)

        logger.info(f"✅ 插件加载成功: {plugin_name} v{manifest.version}")
        return True

    async def load_plugin(self, plugin_path: str) -> bool:
        """兼容入口：仅用于直接按路径加载单插件。

        宏观校验/依赖检查请使用 loader.PluginLoader。
        """
        from src.core.components.loader import load_manifest

        manifest = await load_manifest(plugin_path)
        if not manifest:
            self._failed_plugins[plugin_path] = "无法加载 manifest.json"
            return False
        return await self.load_plugin_from_manifest(plugin_path, manifest)

    async def unload_plugin(self, plugin_name: str) -> bool:
        """卸载插件。

        卸载指定插件,调用生命周期钩子并清理资源。

        Args:
            plugin_name: 插件名称

        Returns:
            bool: 是否卸载成功

        Examples:
            >>> success = await manager.unload_plugin("my_plugin")
            >>> True
        """
        async with self._lifecycle_lock:
            return await self._unload_plugin_unlocked(plugin_name)

    async def _unload_plugin_unlocked(self, plugin_name: str) -> bool:
        """在生命周期锁内执行尽力而为的完整卸载。"""
        if plugin_name not in self._loaded_plugins:
            logger.warning(f"插件 '{plugin_name}' 未加载")
            return False

        plugin = self._loaded_plugins[plugin_name]
        manifest = self._manifests.get(plugin_name)
        plugin_path = self._plugin_paths.get(plugin_name)
        cleanup_errors: list[tuple[str, Exception]] = []

        async def _cleanup_step(name: str, operation) -> None:
            try:
                await operation()
            except Exception as exc:
                cleanup_errors.append((name, exc))
                logger.error(
                    f"卸载插件 '{plugin_name}' 的 {name} 失败: {exc}"
                )

        async def _stop_owned_adapters() -> None:
            from src.core.managers.adapter_manager import get_adapter_manager

            results = await get_adapter_manager().stop_plugin_adapters(plugin_name)
            failures = [signature for signature, success in results.items() if not success]
            if failures:
                raise RuntimeError(
                    "adapter shutdown failed: " + ", ".join(sorted(failures))
                )

        await _cleanup_step("adapters", _stop_owned_adapters)
        await _cleanup_step("卸载钩子", plugin.on_plugin_unloaded)

        from src.core.components.types import EventType, build_signature
        from src.kernel.event import get_event_bus

        try:
            await get_event_bus().publish(
                EventType.ON_PLUGIN_UNLOADED,
                {"plugin_name": plugin_name, "manifest": manifest},
            )
        except Exception as event_error:
            logger.warning(
                f"触发 ON_PLUGIN_UNLOADED 事件失败 '{plugin_name}': {event_error}"
            )

        from src.core.components.state_manager import get_global_state_manager

        state_manager = get_global_state_manager()
        await _cleanup_step(
            "插件状态",
            lambda: state_manager.set_state_async(
                build_signature(plugin_name, ComponentType.PLUGIN, plugin_name),
                ComponentState.UNLOADED,
            ),
        )

        from src.core.managers.event_manager import get_event_manager

        await _cleanup_step(
            "事件处理器",
            lambda: get_event_manager().unregister_plugin_handlers(plugin_name),
        )
        await _cleanup_step(
            "组件注册",
            lambda: self._unregister_plugin_components(plugin_name),
        )

        from src.core.components.loader import unregister_plugin
        from src.core.managers.config_manager import get_config_manager

        unregister_plugin(plugin_name)
        if plugin_path:
            self._cleanup_sys_modules(plugin_name, plugin_path)
            self._cleanup_plugin_import_paths(plugin_name, plugin_path)

        self._loaded_plugins.pop(plugin_name, None)
        self._manifests.pop(plugin_name, None)
        self._plugin_paths.pop(plugin_name, None)
        get_config_manager().remove_config(plugin_name)

        if cleanup_errors:
            logger.error(
                f"❌ 插件卸载完成但有 {len(cleanup_errors)} 个清理步骤失败: "
                f"{plugin_name}"
            )
            return False

        logger.info(f"✅ 插件卸载成功: {plugin_name}")
        return True

    async def _rollback_failed_load(
        self,
        plugin_name: str,
        plugin_path: str,
        plugin_instance: "BasePlugin | None" = None,
    ) -> None:
        """回滚插件启动事务留下的全部可见状态和资源。"""
        from src.core.components.loader import unregister_plugin
        from src.core.components.state_manager import get_global_state_manager
        from src.core.components.types import build_signature
        from src.core.managers.config_manager import get_config_manager
        from src.core.managers.event_manager import get_event_manager

        async def _cleanup_async(name: str, operation) -> None:
            try:
                await operation()
            except Exception as exc:
                logger.warning(
                    f"回滚插件 '{plugin_name}' 的 {name} 失败: {exc}"
                )

        if plugin_instance is not None:
            await _cleanup_async("卸载钩子", plugin_instance.on_plugin_unloaded)

        await _cleanup_async(
            "事件处理器",
            lambda: get_event_manager().unregister_plugin_handlers(plugin_name),
        )
        await _cleanup_async(
            "组件注册",
            lambda: self._unregister_plugin_components(plugin_name),
        )

        plugin_signature = build_signature(
            plugin_name,
            ComponentType.PLUGIN,
            plugin_name,
        )
        state_manager = get_global_state_manager()
        state_manager.remove_state(plugin_signature)
        state_manager.remove_runtime_data(plugin_signature)

        get_config_manager().remove_config(plugin_name)
        unregister_plugin(plugin_name)
        self._cleanup_sys_modules(plugin_name, plugin_path)
        self._cleanup_plugin_import_paths(plugin_name, plugin_path)

        self._loaded_plugins.pop(plugin_name, None)
        self._manifests.pop(plugin_name, None)
        self._plugin_paths.pop(plugin_name, None)

    def _cleanup_plugin_import_paths(
        self,
        plugin_name: str,
        plugin_path: str,
    ) -> None:
        """移除插件追加的导入路径并清理压缩包临时目录。"""
        paths_to_remove: set[str] = set()
        if plugin_path.endswith((".zip", ".mfp")):
            tmpdir = self._archive_tmpdirs.pop(plugin_name, None)
            if tmpdir:
                tmp_path = Path(tmpdir).resolve()
                for raw_path in sys.path:
                    try:
                        Path(raw_path).resolve().relative_to(tmp_path)
                    except (OSError, ValueError):
                        continue
                    paths_to_remove.add(raw_path)
                shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            paths_to_remove.add(str(Path(plugin_path).parent))

        if paths_to_remove:
            sys.path[:] = [path for path in sys.path if path not in paths_to_remove]

    def _cleanup_sys_modules(self, plugin_name: str, plugin_path: str) -> None:
        """从 sys.modules 中清理插件相关的所有模块。

        Args:
            plugin_name: 插件名称
            plugin_path: 插件路径
        """
        try:
            # 获取插件文件夹名（作为包名）
            folder = Path(plugin_path)
            if plugin_path.endswith((".zip", ".mfp")):
                # 压缩包插件的包名就是插件名（_load_from_archive 仍以顶层身份加载）
                package_prefix = plugin_name
            else:
                # 文件夹插件以 plugins.<包名> 前缀加载（与 _load_from_folder 一致）
                package_prefix = f"plugins.{folder.name}"

            # 清理所有以该包名开头的模块
            modules_to_remove = [
                mod_name
                for mod_name in list(sys.modules.keys())
                if mod_name == package_prefix or mod_name.startswith(f"{package_prefix}.")
            ]

            for mod_name in modules_to_remove:
                del sys.modules[mod_name]
                logger.debug(f"清理模块: {mod_name}")

        except Exception as e:
            logger.warning(f"清理 sys.modules 失败: {e}")

    async def _unregister_plugin_components(self, plugin_name: str) -> None:
        """从全局注册表中注销某插件的所有组件，并更新状态。"""
        from src.core.components.registry import get_global_registry
        from src.core.components.state_manager import get_global_state_manager

        registry = get_global_registry()
        state_manager = get_global_state_manager()

        components = registry.get_by_plugin(plugin_name)
        if not components:
            return

        for signature in list(components.keys()):
            try:
                registry.unregister(signature)
            except Exception as e:
                logger.warning(f"注销组件失败 '{signature}': {e}")
                continue

            # 触发组件卸载事件
            from src.core.components.types import EventType
            from src.kernel.event import get_event_bus

            try:
                await get_event_bus().publish(
                    EventType.ON_COMPONENT_UNLOADED,
                    {
                        "signature": signature,
                        "plugin_name": plugin_name,
                    },
                )
            except Exception as event_error:
                logger.warning(
                    f"触发 ON_COMPONENT_UNLOADED 事件失败 '{signature}': {event_error}"
                )

            try:
                await state_manager.set_state_async(signature, ComponentState.UNLOADED)
                state_manager.remove_runtime_data(signature)
            except Exception as e:
                logger.warning(f"更新组件状态失败 '{signature}': {e}")

    async def reload_plugin(self, plugin_name: str) -> bool:
        """重载插件。

        先卸载插件，然后重新加载。

        Args:
            plugin_name: 插件名称

        Returns:
            bool: 是否重载成功

        Examples:
            >>> success = await manager.reload_plugin("my_plugin")
            >>> True
        """
        async with self._lifecycle_lock:
            if plugin_name not in self._loaded_plugins:
                logger.warning(f"插件 '{plugin_name}' 未加载，无法重载")
                return False

            plugin_path = self._plugin_paths.get(plugin_name)
            if not plugin_path:
                logger.error(f"未找到插件 '{plugin_name}' 的路径")
                return False

            from src.core.components.loader import load_manifest

            manifest = await load_manifest(plugin_path)
            if manifest is None:
                logger.error(f"插件 '{plugin_name}' 的 manifest 校验失败，取消重载")
                return False

            unload_success = await self._unload_plugin_unlocked(plugin_name)
            load_success = await self._load_plugin_from_manifest_unlocked(
                plugin_path,
                manifest,
            )
            return unload_success and load_success

    def get_plugin(self, plugin_name: str) -> "BasePlugin | None":
        """获取插件实例。

        Args:
            plugin_name: 插件名称

        Returns:
            BasePlugin | None: 插件实例，如果未找到则返回 None

        Examples:
            >>> plugin = manager.get_plugin("my_plugin")
        """
        return self._loaded_plugins.get(plugin_name)

    def get_all_plugins(self) -> dict[str, "BasePlugin"]:
        """获取所有已加载插件。

        Returns:
            dict[str, BasePlugin]: 插件名到插件实例的字典

        Examples:
            >>> plugins = manager.get_all_plugins()
        """
        return self._loaded_plugins.copy()

    def list_loaded_plugins(self) -> list[str]:
        """列出所有已加载的插件名称。

        Returns:
            list[str]: 已加载插件名称列表

        Examples:
            >>> names = manager.list_loaded_plugins()
            >>> ['my_plugin', 'other_plugin']
        """
        return list(self._loaded_plugins.keys())

    def get_manifest(self, plugin_name: str) -> PluginManifest | None:
        """获取插件清单。

        Args:
            plugin_name: 插件名称

        Returns:
            PluginManifest | None: 插件清单，如果未找到则返回 None

        Examples:
            >>> manifest = manager.get_manifest("my_plugin")
        """
        return self._manifests.get(plugin_name)

    def is_plugin_loaded(self, plugin_name: str) -> bool:
        """检查插件是否已加载。

        Args:
            plugin_name: 插件名称

        Returns:
            bool: 插件是否已加载

        Examples:
            >>> if manager.is_plugin_loaded("my_plugin"):
            ...     print("插件已加载")
        """
        return plugin_name in self._loaded_plugins

    def get_plugin_path(self, plugin_name: str) -> str | None:
        """获取插件路径，未加载或未知时返回 None。"""

        return self._plugin_paths.get(plugin_name)

    async def get_unloaded_plugins_info(self) -> dict[str, dict[str, Any]]:
        """扫描 plugins 目录，返回当前未加载或加载失败的插件信息。"""

        from src.core.components.loader import PluginLoader, load_manifest

        loader = PluginLoader()
        discovered_paths = await loader.discover_plugins("plugins")

        unloaded_info: dict[str, dict[str, Any]] = {}
        for plugin_path in discovered_paths:
            manifest = await load_manifest(plugin_path)
            if manifest is None:
                unloaded_info[plugin_path] = {
                    "name": Path(plugin_path).stem,
                    "version": "unknown",
                    "description": "无法读取插件信息",
                    "author": "unknown",
                    "path": plugin_path,
                    "status": "failed",
                    "reason": "无法加载 manifest.json",
                }
                continue

            plugin_name = manifest.name
            if plugin_name in self._loaded_plugins:
                continue

            unloaded_info[plugin_name] = {
                "name": manifest.name,
                "version": manifest.version,
                "description": manifest.description,
                "author": manifest.author,
                "path": plugin_path,
                "status": "failed" if plugin_name in self._failed_plugins else "not_loaded",
                "reason": self._failed_plugins.get(plugin_name),
            }

        logger.debug(f"发现 {len(unloaded_info)} 个未加载插件")
        return unloaded_info

    # === 私有方法 ===

    # manifest 读取 / 版本校验 / 依赖解析：已迁移至 loader.PluginLoader

    async def _load_from_archive(
        self, archive_path: str, manifest: PluginManifest
    ) -> Any | None:
        """从 ZIP/MFP 加载插件模块。

        支持两种打包格式：
        1. manifest.json 直接在 zip 根级
        2. 带一层子目录前缀（如 plugin_name/manifest.json）

        提取后的临时目录不会被立即删除，以保证插件运行时子模块导入正常。

        Args:
            archive_path: 压缩包路径
            manifest: 插件清单

        Returns:
            加载的模块对象，失败返回 None
        """
        try:
            # 创建持久化临时目录（不使用 with 块，避免提前删除）
            tmpdir = tempfile.mkdtemp(prefix=f"elysium_plugin_{manifest.name}_")
            self._archive_tmpdirs[manifest.name] = tmpdir

            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(tmpdir)

                # 确定插件根目录：可能是 tmpdir 本身或其中的子目录
                plugin_root = Path(tmpdir)
                entry_point = plugin_root / manifest.entry_point

                if not entry_point.exists():
                    # zip 内有一层子目录前缀，在子目录中查找入口点
                    for sub in plugin_root.iterdir():
                        if sub.is_dir():
                            candidate = sub / manifest.entry_point
                            if candidate.exists():
                                plugin_root = sub
                                entry_point = candidate
                                break

                if not entry_point.exists():
                    logger.error(
                        f"入口点不存在: {manifest.entry_point} (archive: {archive_path})"
                    )
                    return None

            # 将插件根的父目录添加到 sys.path（使包导入正常工作）
            # 注意：archive 插件保持顶层包身份（{package_name}.xxx），与
            # _load_from_folder 的 plugins.<name> 身份不同——ZIP 解压目录不在仓库根，
            # plugins.<name> 的父包无法从 sys.path 解析；archive 插件不与 src/ 互引时
            # 顶层身份内部自洽，无双类风险。若未来启用与 src 互引的 archive 插件，
            # 需专门设计解压目录到 plugins 结构的映射。
            parent_dir = str(plugin_root.parent)
            sys.path.insert(0, parent_dir)

            try:
                # 构建模块名
                package_name = plugin_root.name
                entry_relative = entry_point.relative_to(plugin_root)
                module_parts = list(entry_relative.parts[:-1]) + [entry_relative.stem]
                module_name = f"{package_name}.{'.'.join(module_parts)}"

                spec = importlib.util.spec_from_file_location(
                    module_name,
                    str(entry_point),
                    submodule_search_locations=[str(plugin_root)],
                )
                if spec is None or spec.loader is None:
                    logger.error(f"无法创建模块规范: {entry_point}")
                    return None

                module = importlib.util.module_from_spec(spec)

                # 设置 __package__ 以支持相对导入
                if "." in module_name:
                    module.__package__ = module_name.rsplit(".", 1)[0]
                else:
                    module.__package__ = package_name

                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                return module
            except Exception:
                # 加载失败时从 sys.path 移除并记录
                if parent_dir in sys.path:
                    sys.path.remove(parent_dir)
                raise

        except Exception as e:
            logger.error(f"从压缩包加载插件模块失败 ({archive_path}): {e}")
            return None

    async def _load_from_folder(
        self, folder_path: str, manifest: PluginManifest
    ) -> Any | None:
        """从文件夹加载插件模块。

        Args:
            folder_path: 文件夹路径
            manifest: 插件清单

        Returns:
            加载的模块对象，失败返回 None
        """
        try:
            folder = Path(folder_path)

            # 统一插件加载身份：插件以 plugins.<package_name> 前缀加载（与 src/ 侧
            # `from plugins.xxx import` 的常规导入完全同一身份）。仓库根本就在
            # sys.path（main.py 运行环境 / pytest 根），plugins namespace 包可直接
            # 解析，因此不再把 plugins 目录插入 sys.path——此前插入会让同一源码
            # 以顶层包身份（life_engine.*）再加载一份，产生两份异常类导致
            # except/isinstance 漏捕（2026-08-13 双身份分裂问题根因）。
            entry_point = folder / manifest.entry_point
            if not entry_point.exists():
                logger.error(f"入口点不存在: {manifest.entry_point}")
                return None

            # 构建包名（使用插件文件夹名作为包名）
            package_name = folder.name

            # 计算入口点相对于插件文件夹的模块路径
            try:
                entry_relative = entry_point.relative_to(folder)
                # 将路径转换为模块名 (例如: plugin.py -> plugin, src/main.py -> src.main)
                module_parts = list(entry_relative.parts[:-1]) + [
                    entry_relative.stem
                ]
                module_name = (
                    f"plugins.{package_name}.{'.'.join(module_parts)}"
                    if module_parts[0] != entry_relative.stem
                    else f"plugins.{package_name}.{entry_relative.stem}"
                )
            except ValueError:
                logger.error(f"入口点不在插件文件夹内: {entry_point}")
                return None

            # 使用 spec_from_file_location 并设置正确的包信息
            spec = importlib.util.spec_from_file_location(
                module_name,
                str(entry_point),
                submodule_search_locations=[str(folder)],
            )
            if spec is None or spec.loader is None:
                logger.error(f"无法创建模块规范: {entry_point}")
                return None

            module = importlib.util.module_from_spec(spec)

            # 设置 __package__ 以支持相对导入（必须带 plugins. 前缀，
            # 否则根级入口点会解析回顶层 life_engine.* 身份，双类问题复发）
            if "." in module_name:
                module.__package__ = module_name.rsplit(".", 1)[0]
            else:
                module.__package__ = f"plugins.{package_name}"

            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            return module

        except Exception as e:
            logger.error(f"从文件夹加载插件模块失败 ({folder_path}): {e}")
            return None

    async def _register_components(self, plugin_instance: "BasePlugin") -> None:
        """注册插件的所有组件到全局注册表。

        通过 get_components() 获取插件的所有组件类，推断组件类型，
        构建签名，注册到全局注册表。

        Args:
            plugin_instance: 插件实例
        """
        from src.core.components.registry import get_global_registry
        from src.core.components.state_manager import get_global_state_manager
        from src.core.components.types import build_signature

        registry = get_global_registry()
        state_manager = get_global_state_manager()

        from src.core.components.base.config import BaseConfig

        # 获取插件的所有组件（Config 仅允许通过类属性 configs 声明）
        components = plugin_instance.get_components()

        normalized_components: list[type] = []
        for component_cls in components:
            if (
                isinstance(component_cls, type)
                and issubclass(component_cls, BaseConfig)
            ):
                logger.warning(
                    f"插件 '{plugin_instance.plugin_name}' 在 get_components() 中声明了 Config 组件 "
                    f"{component_cls.__name__}，该路径已弃用并将被忽略，请改用类属性 configs"
                )
                continue
            normalized_components.append(component_cls)

        config_components = plugin_instance.__class__.configs  # type: ignore[attr-defined]
        if isinstance(config_components, list):
            for config_cls in config_components:
                if config_cls not in normalized_components:
                    normalized_components.append(config_cls)

        plugin_name = plugin_instance.plugin_name
        registration_errors: list[tuple[str, Exception]] = []

        logger.debug(f"开始注册插件 '{plugin_name}' 的 {len(normalized_components)} 个组件")

        for component_cls in normalized_components:
            # 推断组件类型和名称
            component_type, component_name, dependencies = self._identify_component(
                component_cls
            )

            if not component_type or not component_name:
                logger.warning(
                    f"跳过无法识别的组件: {component_cls.__name__} "
                    f"(缺少类型标识或名称属性)"
                )
                continue

            # 构建组件签名
            signature = build_signature(plugin_name, component_type, component_name)

            # 检查是否已注册
            if signature in registry:
                logger.warning(f"组件 '{signature}' 已经注册，跳过")
                continue

            try:
                # 注册到全局注册表
                registry.register(component_cls, signature, dependencies)
                logger.debug(f"注册组件: {signature}")

                # 设置组件元数据属性，供其他管理器反向查找
                component_cls._signature_ = signature
                component_cls._plugin_ = plugin_name

                # 触发组件加载事件
                from src.core.components.types import EventType
                from src.kernel.event import get_event_bus

                try:
                    await get_event_bus().publish(
                        EventType.ON_COMPONENT_LOADED,
                        {
                            "signature": signature,
                            "plugin_name": plugin_name,
                            "component_type": component_type.value,
                            "component_name": component_name,
                            "component_class": component_cls,
                        },
                    )
                except Exception as event_error:
                    logger.warning(
                        f"触发 ON_COMPONENT_LOADED 事件失败 '{signature}': {event_error}"
                    )

                # 设置组件状态
                await state_manager.set_state_async(signature, ComponentState.ACTIVE)

            except Exception as e:
                logger.error(f"注册组件 '{signature}' 失败: {e}")
                if signature in registry:
                    registry.unregister(signature)
                state_manager.remove_state(signature)
                state_manager.remove_runtime_data(signature)
                registration_errors.append((signature, e))
                continue

        if registration_errors:
            failed = ", ".join(signature for signature, _ in registration_errors)
            raise RuntimeError(f"组件注册事务失败: {failed}")

        logger.info(f"✅ 插件 '{plugin_name}' 的组件注册完成")

    def _identify_component(
        self, component_cls: type
    ) -> tuple[ComponentType | None, str | None, list[str]]:
        """识别组件的类型、名称和依赖。

        通过检查组件类的基类推断组件类型，并获取对应的名称属性。
        动态导入基类以避免循环导入问题。

        Args:
            component_cls: 组件类

        Returns:
            tuple[ComponentType | None, str | None, list[str]]:
                (组件类型, 组件名称, 依赖列表)
        """
        # 动态导入基类以避免循环导入
        from src.core.components.base.action import BaseAction
        from src.core.components.base.adapter import BaseAdapter
        from src.core.components.base.agent import BaseAgent
        from src.core.components.base.chatter import BaseChatter
        from src.core.components.base.command import BaseCommand
        from src.core.components.base.config import BaseConfig
        from src.core.components.base.event_handler import BaseEventHandler
        from src.core.components.base.router import BaseRouter
        from src.core.components.base.service import BaseService
        from src.core.components.base.tool import BaseTool
        from src.core.components.types import ComponentType

        # 组件类型到名称属性和基类的映射
        type_mapping: dict[
            ComponentType,
            tuple[type, str],
        ] = {
            ComponentType.ACTION: (BaseAction, "action_name"),
            ComponentType.AGENT: (BaseAgent, "agent_name"),
            ComponentType.TOOL: (BaseTool, "tool_name"),
            ComponentType.ADAPTER: (BaseAdapter, "adapter_name"),
            ComponentType.CHATTER: (BaseChatter, "chatter_name"),
            ComponentType.COMMAND: (BaseCommand, "command_name"),
            ComponentType.CONFIG: (BaseConfig, "config_name"),
            ComponentType.EVENT_HANDLER: (BaseEventHandler, "handler_name"),
            ComponentType.SERVICE: (BaseService, "service_name"),
            ComponentType.ROUTER: (BaseRouter, "router_name"),
        }

        # 检查组件类型
        for comp_type, (base_cls, name_attr) in type_mapping.items():
            try:
                if inspect.isclass(component_cls) and issubclass(
                    component_cls, base_cls
                ):
                    component_name = getattr(component_cls, name_attr, None)
                    dependencies = getattr(component_cls, "dependencies", [])
                    return comp_type, component_name, dependencies
            except TypeError:
                # component_cls 不是类
                continue

        return None, None, []


# 全局插件管理器实例
_global_plugin_manager: PluginManager | None = None


def get_plugin_manager() -> PluginManager:
    """获取全局插件管理器实例。

    Returns:
        PluginManager: 全局插件管理器单例

    Examples:
        >>> manager = get_plugin_manager()
        >>> await manager.load_all_plugins("plugins")
    """
    global _global_plugin_manager
    if _global_plugin_manager is None:
        _global_plugin_manager = PluginManager()
    return _global_plugin_manager
