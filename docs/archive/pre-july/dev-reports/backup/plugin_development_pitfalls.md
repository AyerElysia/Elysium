# Neo-MoFox 插件开发避坑指南

**文档版本**: 1.0
**创建日期**: 2026-03-18
**作者**: Claude

---

## 概述

本文档总结了在 Neo-MoFox 框架下开发插件时遇到的常见导入和加载问题，以及相应的解决方案。适用于想要在 Neo-MoFox 上开发新插件的开发者。

---

## 问题 1：manifest.json 格式错误

### 症状

插件文件已创建，但运行时插件列表不显示该插件。

### 错误示例

```json
{
  "include": [
    "intent_plugin:service:intent_service",
    "intent_plugin:event_handler:goal_tracker"
  ]
}
```

### 正确格式

Neo-MoFox 使用**对象数组**格式，而非字符串数组：

```json
{
  "name": "intent_plugin",
  "version": "1.0.0",
  "description": "自主意图与短期目标系统",
  "author": "Neo-MoFox Team",
  "dependencies": {
    "plugins": [],
    "components": []
  },
  "include": [
    {
      "component_type": "service",
      "component_name": "intent_service",
      "dependencies": [],
      "enabled": true
    },
    {
      "component_type": "event_handler",
      "component_name": "goal_tracker",
      "dependencies": [],
      "enabled": true
    }
  ],
  "entry_point": "plugin.py",
  "min_core_version": "1.0.0",
  "python_dependencies": [],
  "dependencies_required": true
}
```

### 关键字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | ✅ | 插件唯一标识 |
| `version` | string | ✅ | 语义化版本号 |
| `include` | object[] | ✅ | 组件注册列表 |
| `include[].component_type` | string | ✅ | 组件类型 |
| `include[].component_name` | string | ✅ | 组件名称 |
| `include[].dependencies` | string[] | ✅ | 组件依赖列表 |
| `entry_point` | string | ✅ | 入口文件 |
| `min_core_version` | string | ✅ | 最低核心版本 |

### 组件类型枚举

```python
# 可用的 component_type 值
"service"         # 服务组件
"action"          # 动作组件
"tool"            # 工具组件
"event_handler"   # 事件处理器
"chatter"         # 聊天器
"command"         # 命令处理器
"adapter"         # 适配器
"collection"      # 组件集合
"router"          # 路由组件
"agent"           # Agent 组件
"config"          # 配置组件
```

---

## 问题 2：register_plugin 导入路径错误

### 症状

```
ModuleNotFoundError: No module named 'src.app.plugin_system.decorators'
```

### 错误代码

```python
from src.app.plugin_system.decorators import register_plugin
```

### 正确代码

```python
from src.app.plugin_system.base import BasePlugin, register_plugin
```

### 原因分析

`register_plugin` 定义在 `src.core.components.loader`，但通过 `src.app.plugin_system.base` 导出。

### 导入路径对照表

| 组件 | 正确导入路径 |
|------|-------------|
| `BasePlugin` | `src.app.plugin_system.base` |
| `register_plugin` | `src.app.plugin_system.base` |
| `BaseService` | `src.app.plugin_system.base` |
| `BaseEventHandler` | `src.app.plugin_system.base` |
| `BaseAction` | `src.app.plugin_system.base` |
| `BaseTool` | `src.app.plugin_system.base` |
| `BaseConfig` | `src.app.plugin_system.base` |
| `Field` | `src.app.plugin_system.base` |
| `SectionBase` | `src.app.plugin_system.base` |
| `config_section` | `src.app.plugin_system.base` |

---

## 问题 3：跨模块类型导入错误

### 症状

```
ImportError: cannot import name 'Situation' from 'plugins.intent_plugin.models'
```

### 错误代码

```python
# goal_tracker.py
from .models import Goal, Situation  # Situation 不在 models.py 中！
```

### 正确代码

```python
# goal_tracker.py
from .models import Goal
from .intent_engine import IntentEngine, Situation
```

### 原因分析

`Situation` 类定义在 `intent_engine.py`，而非 `models.py`。跨模块引用时需要找到正确的定义位置。

### 调试方法

```bash
# 查找类定义位置
grep -r "class Situation" plugins/intent_plugin/
# 输出：plugins/intent_plugin/intent_engine.py:class Situation:
```

---

## 问题 4：配置类定义不规范

### 症状

插件加载成功，但配置未生效或使用默认值。

### 正确配置类模板

```python
from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


@config_section("social_intents")
class SocialIntentsSection(SectionBase):
    """社交类意图配置。"""

    social_curiosity: bool = Field(
        default=True,
        description="是否启用「了解用户」意图",
    )
    remember_details: bool = Field(
        default=True,
        description="是否启用「记住细节」意图",
    )


class IntentConfig(BaseConfig):
    """意图插件配置。"""

    config_name = "config"
    config_description = "意图插件配置"

    social_intents: SocialIntentsSection = Field(
        default_factory=SocialIntentsSection
    )
    # ... 其他配置段
```

### 配置文件路径

```
config/plugins/{plugin_name}/config.toml
```

### TOML 配置示例

```toml
[social_intents]
social_curiosity = true
remember_details = true

[settings]
max_active_intents = 3
min_priority_threshold = 4
```

---

## 问题 5：BasePlugin 构造函数签名错误

### 症状

```
plugin_manager | ERROR | 插件 'intent_plugin' 加载失败：插件实例化失败：IntentPlugin.__init__()
```

### 错误代码

```python
@register_plugin
class IntentPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__()
```

### 正确代码

`BasePlugin.__init__` 接受一个 `config` 参数，子类必须兼容：

```python
from src.app.plugin_system.base import BasePlugin, BaseConfig

@register_plugin
class IntentPlugin(BasePlugin):
    def __init__(self, config: "BaseConfig | None" = None) -> None:
        super().__init__(config)
```

### 原因分析

`BasePlugin` 的构造函数定义为：
```python
def __init__(self, config: "BaseConfig | None" = None) -> None:
    self.config = config
```

子类重写时必须保持兼容的签名，否则在插件管理器调用 `PluginClass(config)` 时会失败。

---

## 问题 6：组件类定义不规范

### 症状

组件无法被插件系统识别或注册失败。

### 正确组件模板

#### Service 组件

```python
from src.app.plugin_system.base import BaseService


class IntentService(BaseService):
    """意图服务。"""

    service_name: str = "intent_service"
    service_description: str = "意图状态管理"

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
```

#### EventHandler 组件

```python
from src.core.components.types import EventType
from src.app.plugin_system.base import BaseEventHandler
from src.kernel.event import EventDecision


class GoalTracker(BaseEventHandler):
    """目标追踪器。"""

    handler_name: str = "goal_tracker"
    handler_description: str = "追踪目标进度"
    weight: int = 10  # 优先级

    init_subscribe: list[EventType | str] = [
        EventType.ON_CHATTER_STEP,
    ]

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        # 实现逻辑
        return EventDecision.SUCCESS, params
```

### 必填类属性

| 组件类型 | 必填属性 |
|----------|---------|
| Service | `service_name`, `service_description` |
| EventHandler | `handler_name`, `handler_description`, `init_subscribe` |
| Action | `action_name`, `action_description` |
| Tool | `tool_name`, `tool_description` |
| Command | `command_name`, `command_description` |

---

## 问题 7：循环导入

### 症状

```
ImportError: cannot import name 'X' from 'module_a' (partial circular import)
```

### 错误示例

```python
# module_a.py
from .module_b import B

class A:
    pass

# module_b.py
from .module_a import A

class B:
    pass
```

### 解决方案

**方案 1**: 使用类型注解字符串

```python
# module_b.py
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .module_a import A

class B:
    def use_a(self, a: "A") -> None:
        pass
```

**方案 2**: 将共同依赖提取到第三模块

```python
# models.py - 共享数据结构
@dataclass
class Goal:
    pass

# module_a.py
from .models import Goal

# module_b.py
from .models import Goal
```

**方案 3**: 延迟导入

```python
class B:
    def use_a(self, a):
        from .module_a import A  # 函数内部导入
```

---

## 问题 8：__init__.py 缺失或为空

### 症状

```
ModuleNotFoundError: No module named 'plugins.intent_plugin'
```

### 解决方案

确保插件目录下存在 `__init__.py` 文件：

```python
"""意图插件 - 自主意图与短期目标系统。

功能特性：
- 基于情境的意图生成
- 目标自动分解
- 进度追踪
- System Reminder 注入
"""
```

可以只包含 docstring，但文件必须存在。

---

## 问题 9：在 on_plugin_loaded 中访问不存在的属性

### 症状

```
plugin_manager | ERROR | 调用插件 'intent_plugin' 的 on_plugin_loaded 钩子时出错:
'IntentPlugin' object has no attribute 'event_handlers'
```

### 错误代码

```python
async def on_plugin_loaded(self) -> None:
    # 错误：BasePlugin 没有 event_handlers 或 services 属性
    for handler in self.event_handlers:
        if isinstance(handler, GoalTracker):
            self.goal_tracker = handler
```

### 正确代码

```python
async def on_plugin_loaded(self) -> None:
    """插件加载时的回调"""
    logger.info("插件已加载")
    # 不要在 on_plugin_loaded 中访问 event_handlers 或 services
    # 这些属性不由插件管理器注入
```

### 原因分析

`BasePlugin` 类只定义了以下属性：
- `config`: 配置实例
- `plugin_name`: 插件名称
- `plugin_description`: 插件描述
- `plugin_version`: 插件版本
- `configs`: 配置类列表
- `dependent_components`: 依赖组件列表

**没有** 以下属性：
- `event_handlers`
- `services`
- `actions`
- `tools`

组件注册是由 `_register_components` 方法负责，注册到**全局注册表**，而非插件实例的属性。

### 解决方案

如果需要访问组件实例，可以通过全局注册表获取：

```python
from src.core.components.registry import get_global_registry

registry = get_global_registry()
# 通过签名获取组件类
goal_tracker_cls = registry.get("intent_plugin:event_handler:goal_tracker")
```

或者在组件内部通过 `self.plugin` 反向引用插件实例。

---

## 调试检查清单

### 创建新插件后

```bash
# 1. 检查 manifest.json 格式
cat plugins/{plugin_name}/manifest.json | jq

# 2. 检查入口文件是否存在
ls -la plugins/{plugin_name}/plugin.py

# 3. 检查配置文件是否存在
ls -la config/plugins/{plugin_name}/config.toml

# 4. 测试导入
python -c "from plugins.{plugin_name}.plugin import {PluginClass}; print('OK')"

# 5. 运行 lint
uv run ruff check plugins/{plugin_name}/
uv run ruff format plugins/{plugin_name}/

# 6. 检查组件注册
grep -r "class.*BaseService" plugins/{plugin_name}/
grep -r "class.*BaseEventHandler" plugins/{plugin_name}/
```

### manifest.json 快速验证

```python
import json

with open("plugins/{plugin_name}/manifest.json") as f:
    manifest = json.load(f)

# 检查必需字段
required_fields = ["name", "version", "include", "entry_point"]
for field in required_fields:
    assert field in manifest, f"缺少必需字段：{field}"

# 检查 include 格式
for item in manifest["include"]:
    assert isinstance(item, dict), "include 必须是对象数组"
    assert "component_type" in item, "缺少 component_type"
    assert "component_name" in item, "缺少 component_name"
```

---

## 完整插件目录结构

```
plugins/{plugin_name}/
├── __init__.py           # 模块文档
├── plugin.py             # 插件入口类
├── manifest.json         # 插件清单
├── config.py             # 配置类定义
├── service.py            # 服务组件
├── action.py             # 动作组件（可选）
├── tool.py               # 工具组件（可选）
├── event_handler.py      # 事件处理器（可选）
└── models.py             # 数据结构（可选）

config/plugins/{plugin_name}/
└── config.toml           # 配置文件
```

---

## 参考资源

- 插件基类：`src/app/plugin_system/base/__init__.py`
- 组件加载器：`src/core/components/loader.py`
- 插件管理器：`src/core/managers/plugin_manager.py`
- 已有插件示例：
  - `plugins/diary_plugin/` - 完整插件示例
  - `plugins/emoji_sender/` - 简单插件示例

---

## 快速开始模板

复制以下模板开始新插件开发：

### manifest.json

```json
{
  "name": "my_plugin",
  "version": "1.0.0",
  "description": "我的插件描述",
  "author": "Your Name",
  "dependencies": {
    "plugins": [],
    "components": []
  },
  "include": [
    {
      "component_type": "service",
      "component_name": "my_service",
      "dependencies": [],
      "enabled": true
    }
  ],
  "entry_point": "plugin.py",
  "min_core_version": "1.0.0",
  "python_dependencies": [],
  "dependencies_required": true
}
```

### plugin.py

```python
"""我的插件。"""

from __future__ import annotations

from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.logger import get_logger

from .config import MyConfig
from .service import MyService


logger = get_logger("my_plugin")


@register_plugin
class MyPlugin(BasePlugin):
    """我的插件入口类。"""

    plugin_name: str = "my_plugin"
    plugin_description: str = "我的插件描述"
    configs: list[type] = [MyConfig]

    def get_components(self) -> list[type]:
        return [MyService]

    async def on_plugin_loaded(self) -> None:
        logger.info("我的插件已加载")
```

### config.py

```python
from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


@config_section("general")
class GeneralSection(SectionBase):
    """主配置。"""

    enabled: bool = Field(default=True, description="是否启用")


class MyConfig(BaseConfig):
    """我的插件配置。"""

    config_name = "config"
    config_description = "我的插件配置"
    general: GeneralSection = Field(default_factory=GeneralSection)
```

### service.py

```python
from typing import Any

from src.app.plugin_system.base import BaseService


class MyService(BaseService):
    """我的服务。"""

    service_name: str = "my_service"
    service_description: str = "我的服务描述"

    def __init__(self, plugin: Any) -> None:
        super().__init__(plugin)
```

---

**最后更新**: 2026-03-18
