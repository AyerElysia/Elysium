# Elysium 基座 v2 兼容迁移说明

> **状态：部分落地的渐进式重构蓝图，不是当前最终架构总图。**
> Kernel、统一配置、DI、LLM API、Registry 与插件上下文已经进入代码；旧 Manager 和兼容入口仍并行存在。当前运行事实请先读 [当前架构](./architecture/current_architecture.md)。

本文保留当时的基础设施迁移目标。项目定位已经进一步明确：Elysium 是爱莉一个人的专用系统，不以成为通用 Agent 框架为目标；其中“可替换、可插拔”描述的是技术边界，不意味着主体可以被替换。

## 设计原则

1. **协议驱动**：模块间通过 Protocol 通信，实现可替换
2. **最小核心**：kernel 只做不可能在外部做的事
3. **不定死**：框架不预设"生命应该有什么"，一切可插拔
4. **配置即声明**：一个 TOML 描述全部运行时行为
5. **性能底线**：全链路 async，零阻塞 I/O

## 新模块总览

```
src/kernel/
├── __init__.py          # 统一导出：container, get_config, Protocols
├── bootstrap.py         # 启动引导：加载配置 → 注册服务 → 就绪
├── container.py         # DI 容器：register / resolve / scoped
├── protocols.py         # 服务协议：EventBus, LogStore, LLM, Tool, DB, Vector, Scheduler
├── runtime.py           # 异步运行时：生命周期钩子 + 信号处理 + 后台任务
├── config/
│   ├── schema.py        # 统一配置 Schema（Pydantic）
│   └── unified.py       # 配置加载器：TOML + env 插值 + ELYSIUM_* 覆盖
├── llm/
│   └── api.py           # 快捷 API：chat() / stream()
└── mcp/
    ├── protocol.py      # 工具协议（对齐 MCP spec）
    └── registry.py      # 统一工具注册表（本地 + MCP 远程）

src/core/
├── registry.py          # 统一组件注册表（替代 12 个 managers）
├── plugin_protocol.py   # 插件协议 v2：Plugin + Context
└── pipeline.py          # 消息管线：middleware 链
```

## 使用指南

### 统一配置

```toml
# config/elysium.toml（新格式，可选；不存在时自动兼容老文件）
[runtime]
log_level = "INFO"
db_path = "data/elysium.db"

[llm.providers.openai]
base_url = "https://api.openai.com/v1"
api_key = "${OPENAI_API_KEY}"

[llm.models.main]
provider = "openai"
model = "gpt-4o"
max_tokens = 8192

[llm.routing]
default = "main"
```

```python
from src.kernel import get_config

cfg = get_config()
print(cfg.runtime.log_level)          # "INFO"
print(cfg.llm.routing["default"])     # "main"
```

环境变量覆盖：`ELYSIUM_RUNTIME_LOG_LEVEL=DEBUG`

### DI 容器

```python
from src.kernel import container
from src.kernel.protocols import EventBusProtocol

# 启动时注册
container.register(EventBusProtocol, my_event_bus)

# 任何地方解析
bus = container.resolve(EventBusProtocol)
await bus.publish("hello", {"data": 1})
```

### LLM 快捷 API

```python
from src.kernel.llm.api import chat, stream

# 单轮
resp = await chat("你好")
print(resp.text)

# 指定路由
resp = await chat("快速回答", model="fast")

# 流式
async for chunk in stream("写一首诗"):
    print(chunk.delta, end="")
```

### 工具注册

```python
from src.kernel.mcp import tool_registry

@tool_registry.tool(name="weather", description="查天气")
async def weather(city: str) -> str:
    return f"{city}: 晴 25°C"

# 执行
result = await tool_registry.execute("weather", {"city": "北京"})

# 列出 schema（给 LLM）
schemas = tool_registry.list_schemas()
```

### 组件注册

```python
from src.core.registry import Component, component_registry

class MyService(Component):
    name = "my_service"
    priority = 10

    async def on_start(self):
        print("服务启动")

component_registry.register(MyService())
await component_registry.start_all()
```

### 插件 v2

```python
from src.core.plugin_protocol import Plugin, Context

class HelloPlugin(Plugin):
    name = "hello"

    async def on_load(self, ctx: Context) -> None:
        ctx.on_message(self.handle)

    async def handle(self, msg, ctx: Context) -> None:
        await ctx.reply("你好！")
```

### 消息管线

```python
from src.core.pipeline import MessagePipeline, filter_middleware, tap_middleware

pipeline = MessagePipeline()
pipeline.use(tap_middleware(lambda m: print(f"收到: {m}")))
pipeline.use(filter_middleware(lambda m: m.type != "system"))

result = await pipeline.process_incoming(message)
```

## 迁移路径

| 阶段 | 状态 | 说明 |
|------|------|------|
| Phase 1: 基础设施 | 已完成 | 配置 + DI + Runtime |
| Phase 2: LLM 层 | 已完成 | 快捷 API + MCP 注册表 |
| Phase 3: 服务层 | 已完成 | Component + Registry |
| Phase 4: 插件层 | 已完成 | Plugin + Context |
| Phase 5: 传输层 | 已完成 | Pipeline + Middleware |
| 渐进迁移 | 进行中 | 老代码通过桥接继续工作，逐模块切换到新接口 |

## 兼容性

- `core.toml` 继续兼容；生产模型路由只读取 `models.toml`，旧 `model.toml` 仅供显式迁移
- 老插件（BasePlugin）继续运行，不受影响
- 老 managers 继续工作，新 Registry 并行存在
- 2725 个测试保持绿色
