"""Regression tests for the package identity used by the plugin loader.

2026-08-13 方案 C：``plugin_manager._load_from_folder`` 以 ``plugins.<package_name>``
前缀加载插件（不再向 sys.path 插入 plugins 目录、不再造顶层包身份），与 src/ 侧
``from plugins.xxx import`` 的常规导入完全同一身份。此前顶层身份与 plugins 身份
并存会让同一源码出现两份异常类，导致 except/isinstance 漏捕（memory_witness
ERROR 刷屏根因）。

这些测试在隔离子进程中模拟真实启动顺序：
1. src 侧先以 plugins 身份常规导入（main.py 路径）；
2. plugin_manager 再以 ``plugins.<name>`` 身份 spec 加载插件入口；
3. 断言顶层 ``life_engine.*`` 不可达、关键模块单实例、捕获类 is 抛出类。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# 模拟 _load_from_folder 的 spec 加载逻辑 + 单身份断言，作为子进程脚本运行。
_LOADER_IDENTITY_PROBE = """
import importlib.util
import os
import sys

repo = "{repo}"
sys.path.insert(0, repo)
# 确保 plugins 目录不在 sys.path：仓库根才是唯一解析来源
sys.path = [
    p for p in sys.path
    if os.path.normpath(p) != os.path.normpath(os.path.join(repo, "plugins"))
]

# 1) src 侧先以 plugins 身份导入（main.py 常规 import 路径）
from plugins.life_engine.service import memory_witness as mw
from plugins.life_engine.service.presence_store import PresenceRevisionConflict
from plugins.life_engine.storage.writer_claims import SingletonWriterClaimConflict

# 2) 顶层 life_engine.* 必须不可达（sys.path 无 plugins 目录）
try:
    import life_engine  # noqa: F401
except ModuleNotFoundError:
    pass
else:
    raise SystemExit("FAIL: 顶层 life_engine 仍可达，双身份未消除")

# 3) 模拟 plugin_manager._load_from_folder 的 spec 加载（plugins.<name> 身份）
entry = os.path.join(repo, "plugins", "life_engine", "core", "plugin.py")
spec = importlib.util.spec_from_file_location(
    "plugins.life_engine.core.plugin",
    entry,
    submodule_search_locations=[os.path.join(repo, "plugins", "life_engine")],
)
module = importlib.util.module_from_spec(spec)
module.__package__ = "plugins.life_engine.core"
sys.modules["plugins.life_engine.core.plugin"] = module
spec.loader.exec_module(module)

# 4) sys.modules 中不得出现任何顶层 life_engine.* 副本
dups = [
    name for name in sys.modules
    if name == "life_engine" or name.startswith("life_engine.")
]
if dups:
    raise SystemExit(f"FAIL: 存在顶层身份副本: {{dups}}")

# 5) memory_witness 捕获元组覆盖 plugins 身份类，且单身份下退化为单类
assert PresenceRevisionConflict in mw._PRESENCE_CONFLICT_TYPES
assert SingletonWriterClaimConflict in mw._WRITER_CLAIM_TYPES
assert len({id(x) for x in mw._PRESENCE_CONFLICT_TYPES}) == 1

# 6) 关键模块单实例（重新导入 is 同一对象）
import plugins.life_engine.service.presence_store as ps2
import plugins.life_engine.storage.writer_claims as wc2

assert ps2 is sys.modules["plugins.life_engine.service.presence_store"]
assert wc2 is sys.modules["plugins.life_engine.storage.writer_claims"]
assert "life_engine.storage.writer_claims" not in sys.modules
"""


def test_plugin_loader_identity_is_single_plugins_instance() -> None:
    """真实加载顺序下：顶层不可达、插件入口 plugins 身份、关键模块单实例。"""
    repository_root = Path(__file__).resolve().parents[3]
    environment = dict(os.environ)
    # 只保留仓库根作为解析来源（与真实运行一致），剥离可能残留的 plugins 目录
    environment["PYTHONPATH"] = str(repository_root)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _LOADER_IDENTITY_PROBE.replace("{repo}", str(repository_root)),
        ],
        cwd=repository_root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr or result.stdout
