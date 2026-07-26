# Core 层领域逻辑详解：插件组件生态与智能实现

> **核心定位**：Core层实现Neo-MoFox的领域智能——记忆、对话、行为，使用kernel提供的基础设施构建完整的插件组件系统。

---

## 一、Core 层的设计哲学

### 1.1 领域逻辑层

**Core层的本质**：使用kernel的技术基础设施，实现AI聊天的领域概念：

```
Kernel层提供：数据库、LLM接口、配置、日志、并发...
          ↓
          ↓ 使用这些基础设施
          ↓
Core层实现：记忆、对话、消息、插件组件、管理器...
```

**关键区别**：

| 维度 | Kernel层 | Core层 |
|-----|---------|--------|
| **概念层次** | 技术基础设施 | 领域概念 |
| **抽象级别** | 工具库 | 业务逻辑 |
| **平台依赖** | 完全无关 | 抽象（不含具体平台） |
| **复用性** | 可独立使用 | Neo-MoFox专属 |

### 1.2 插件组件化架构

**所有功能通过插件组件实现**：

```
Core层不直接实现功能，而是提供组件基类：
┌──────────────────────────────────┐
│   BasePlugin（插件容器）          │
│   ├─ BaseAction（动作）           │
│   ├─ BaseTool（查询）             │
│   ├─ BaseChatter（对话控制）      │
│   ├─ BaseCommand（命令）          │
│   ├─ BaseService（服务）          │
│   ├─ BaseEventHandler（事件）     │
│   ├─ BaseAdapter（平台桥接）      │
│   └─ BaseRouter（HTTP路由）       │
└──────────────────────────────────┘
```

**具体实现在 plugins/ 目录中**：

```
plugins/
├── default_chatter/  → BaseChatter实现（对话流控制器）
├── life_engine/      → BaseService + BaseTool实现（生命中枢）
├── diary_plugin/     → BaseAction实现（日记系统）
├── napcat_adapter/   → BaseAdapter实现（QQ平台适配）
├── emoji_sender/     → BaseAction实现（表情包发送）
├── command_dispatch_plugin/ → BaseCommand实现
├── thinking_plugin/  → BaseAction实现
└── ...（共10个插件）
```

---

## 二、插件组件系统详解

### 2.1 组件基类矩阵

| 组件类型 | 基类 | 职责 | 构造注入 | 必须实现 | 返回约定 |
|---------|-----|------|---------|---------|---------|
| **Plugin** | BasePlugin | 组件容器 | config | get_components() | list[type] |
| **Action** | BaseAction | 执行动作（副作用） | chat_stream, plugin | execute() | (bool, str) |
| **Tool** | BaseTool | 查询信息（无副作用） | plugin | execute() | (bool, str \| dict) |
| **Chatter** | BaseChatter | 对话主流程 | stream_id, plugin | execute() | AsyncGenerator |
| **Command** | BaseCommand | 命令处理 | plugin, stream_id | @cmd_route方法 | (bool, str) |
| **Service** | BaseService | 插件间通信 | plugin | 自定义方法 | 自定义 |
| **EventHandler** | BaseEventHandler | 事件订阅 | plugin | execute(event, params) | (EventDecision, dict) |
| **Adapter** | BaseAdapter | 平台桥接 | core_sink, plugin | from_platform_message() | MessageEnvelope |
| **Router** | BaseRouter | HTTP端点 | plugin | register_endpoints() | FastAPI |
| **Config** | BaseConfig | 配置管理 | 无 | 配置节定义 | 配置模型 |

### 2.2 组件签名与注册

**签名格式**：`plugin_name:component_type:component_name`

```python
# core/components/registry.py
class ComponentRegistry:
    """组件注册表（全局唯一）。"""
    
    def register_component(self, component_class: type, plugin_class: type):
        """注册组件到全局表。"""
        # 自动注入 _plugin_ 和 _signature_
        component_class._plugin_ = plugin_class
        
        # 根据继承关系识别类型
        if issubclass(component_class, BaseAction):
            comp_type = "action"
            comp_name = component_class.action_name
        elif issubclass(component_class, BaseTool):
            comp_type = "tool"
            comp_name = component_class.tool_name
        elif issubclass(component_class, BaseChatter):
            comp_type = "chatter"
            comp_name = component_class.chatter_name
        # ... 其他类型
        
        # 构建签名
        signature = f"{plugin_class.plugin_name}:{comp_type}:{comp_name}"
        component_class._signature_ = signature
        
        # 存入注册表
        self._registry[comp_type][comp_name] = component_class
```

**查询示例**：

```python
# 获取组件
action_class = registry.get("action", "send_emoji")
# 返回：emoji_sender:action:send_emoji_meme 类

tool_class = registry.get("tool", "nucleus_search_memory")
# 返回：life_engine:tool:nucleus_search_memory 类
```

### 2.3 组件生命周期

**插件加载流程**：

```python
# core/managers/plugin_manager.py
async def load_plugin(self, path: str) -> PluginInfo:
    """加载插件完整流程。"""
    # 1. 读取manifest
    manifest = self._read_manifest(path)
    
    # 2. 导入模块
    module = import_module(f"{path}.plugin")
    
    # 3. 找到插件类
    plugin_class = self._find_registered_plugin(module, manifest.name)
    
    # 4. 加载配置
    if plugin_class.configs:
        config = plugin_class.configs[0].load_for_plugin(manifest.name)
    
    # 5. 实例化插件
    plugin_instance = plugin_class(config=config)
    
    # 6. 注册组件
    components = plugin_instance.get_components()
    for comp in components:
        registry.register_component(comp, plugin_class)
    
    # 7. 调用生命周期钩子
    await plugin_instance.on_plugin_loaded()
    
    return PluginInfo(
        plugin_name=manifest.name,
        plugin_class=plugin_class,
        plugin_instance=plugin_instance,
        components=components,
    )
```

**生命周期钩子**：

```python
class BasePlugin:
    async def on_plugin_loaded(self) -> None:
        """插件加载完成后的初始化。"""
        # 示例：启动后台任务、初始化服务
    
    async def on_plugin_unloaded(self) -> None:
        """插件卸载前的清理。"""
        # 示例：停止后台任务、清理资源
```

---

## 三、管理器详解

### 3.1 PluginManager

**职责**：插件加载、生命周期管理、依赖解析。

```python
# core/managers/plugin_manager.py
class PluginManager:
    """插件管理器。"""
    
    async def load_all(self, plugin_dir: str):
        """加载所有插件（支持folder/zip/.mfp）。"""
        plugins = []
        
        # 扫描插件目录
        for path in pathlib.Path(plugin_dir).iterdir():
            if path.is_dir():
                info = await self.load_plugin(path)
                plugins.append(info)
            elif path.suffix in (".zip", ".mfp"):
                info = await self.load_plugin_from_archive(path)
                plugins.append(info)
        
        # 依赖排序（拓扑排序）
        sorted_plugins = self._resolve_dependencies(plugins)
        
        # 按顺序加载
        for plugin_info in sorted_plugins:
            await self._activate_plugin(plugin_info)
```

**依赖解析**：

```python
def _resolve_dependencies(self, plugins: list) -> list:
    """拓扑排序解析插件依赖。"""
    # manifest.dependencies.plugins:
    # ["other_plugin:>=1.2.0", "another_plugin"]
    
    graph = {}
    for plugin in plugins:
        deps = plugin.manifest.dependencies.plugins
        graph[plugin.name] = [d.split(":")[0] for d in deps]
    
    # Kahn算法拓扑排序
    sorted_names = []
    while graph:
        # 找无依赖节点
        no_dep = [name for name, deps in graph.items() if not deps]
        if not no_dep:
            raise CircularDependencyError("插件依赖循环")
        
        sorted_names.extend(no_dep)
        
        # 移除已排序节点
        for name in no_dep:
            del graph[name]
            for deps in graph.values():
                deps.discard(name)
    
    return [p for p in plugins if p.name in sorted_names]
```

### 3.2 ToolManager

**职责**：工具schema生成、执行路由、MCP适配。

```python
# core/managers/tool_manager.py
class ToolManager:
    """工具管理器。"""
    
    def generate_schema(self, tools: list[BaseTool]) -> list[dict]:
        """生成LLM工具schema。"""
        schemas = []
        for tool in tools:
            # 从execute方法提取参数
            params = self._extract_parameters(tool.execute)
            
            schema = {
                "name": tool.tool_name,
                "description": tool.tool_description,
                "input_schema": {
                    "type": "object",
                    "properties": params,
                    "required": list(params.keys()),
                },
            }
            schemas.append(schema)
        
        return schemas
    
    def _extract_parameters(self, func: Callable) -> dict:
        """从函数签名提取参数schema。"""
        sig = inspect.signature(func)
        params = {}
        
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            
            # 类型推断
            type_hint = param.annotation
            if type_hint == str:
                param_type = "string"
            elif type_hint == int:
                param_type = "integer"
            elif type_hint == bool:
                param_type = "boolean"
            else:
                param_type = "string"  # 默认
            
            # 描述提取（从docstring）
            docstring = func.__doc__
            description = self._extract_param_desc(docstring, name)
            
            params[name] = {
                "type": param_type,
                "description": description,
            }
        
        return params
```

**执行路由**：

```python
async def execute_tool(self, tool_name: str, args: dict) -> tuple[bool, Any]:
    """执行工具调用。"""
    # 获取工具类
    tool_class = registry.get("tool", tool_name)
    
    # 获取所属插件
    plugin = plugin_manager.get_plugin(tool_class._plugin_.plugin_name)
    
    # 实例化工具
    tool_instance = tool_class(plugin=plugin)
    
    # 执行
    success, result = await tool_instance.execute(**args)
    
    # 记录执行历史
    self._history.append({
        "tool_name": tool_name,
        "args": args,
        "success": success,
        "timestamp": datetime.now(),
    })
    
    return success, result
```

### 3.3 ChatterManager

**职责**：对话流程控制、LLMUsable过滤。

```python
# core/managers/chatter_manager.py
class ChatterManager:
    """对话控制器管理器。"""
    
    async def execute_chatter(
        self,
        stream_id: str,
        chatter_name: str = "default_chatter",
    ) -> AsyncGenerator[ChatterResult, None]:
        """执行对话流程。"""
        # 获取chatter类
        chatter_class = registry.get("chatter", chatter_name)
        
        # 获取插件实例
        plugin = plugin_manager.get_plugin(chatter_class._plugin_.plugin_name)
        
        # 实例化chatter
        chatter_instance = chatter_class(stream_id=stream_id, plugin=plugin)
        
        # 执行（返回生成器）
        async for result in chatter_instance.execute():
            yield result
    
    def get_available_usables(self, stream_id: str) -> list[type]:
        """获取当前可用的LLMUsables（Action/Tool）。"""
        # 过滤逻辑：
        # 1. 检查组件激活条件
        # 2. 检查权限
        # 3. 检查依赖是否满足
        
        usables = []
        
        for action_class in registry.get_all("action"):
            if self._is_active(action_class, stream_id):
                usables.append(action_class)
        
        for tool_class in registry.get_all("tool"):
            if self._is_active(tool_class, stream_id):
                usables.append(tool_class)
        
        return usables
```

---

## 四、Prompt 系统详解

### 4.1 PromptTemplate 引擎

```python
# core/prompt/template.py
class PromptTemplate:
    """Prompt模板引擎（支持变量注入、事件订阅）。"""
    
    def __init__(self, name: str, template: str, values: dict = {}):
        self.name = name
        self.template = template
        self.values = values
    
    def build(self) -> str:
        """构建完整Prompt。"""
        # 发布事件（允许插件修改）
        event_bus.publish("on_prompt_build", {
            "name": self.name,
            "template": self.template,
            "values": self.values,
        })
        
        # 变量替换
        result = self.template.format(**self.values)
        
        return result
    
    def set_value(self, key: str, value: Any) -> Self:
        """设置变量值（链式调用）。"""
        self.values[key] = value
        return self
```

**使用示例**：

```python
# DFC构建系统提示词
template = PromptTemplate(
    name="dfc_system_prompt",
    template="""
你是{nickname}，{personality_core}

# 表达风格
{reply_style}

# 工具介绍
{tool_descriptions}
""",
)

template.set_value("nickname", config.nickname)
template.set_value("personality_core", config.personality_core)
template.set_value("reply_style", config.reply_style)

system_prompt = template.build()
```

### 4.2 System Reminder 存储

```python
# core/prompt/system_reminder.py
class SystemReminderStore:
    """System Reminder存储（注入到LLM SYSTEM payload）。"""
    
    def __init__(self):
        self._store: dict[str, dict[str, str]] = defaultdict(dict)
    
    def set(self, bucket: str, name: str, content: str):
        """设置reminder。"""
        self._store[bucket][name] = content
    
    def get(self, bucket: str, name: str) -> str | None:
        """获取单个reminder。"""
        return self._store[bucket].get(name)
    
    def get_all(self, bucket: str) -> list[str]:
        """获取bucket内所有reminder。"""
        return list(self._store[bucket].values())
    
    def clear(self, bucket: str, name: str):
        """清除reminder。"""
        if name in self._store[bucket]:
            del self._store[bucket][name]
```

**注入机制**：

```python
# core/components/base/chatter.py
class BaseChatter:
    def create_request(self, ..., with_reminder: str | None = None):
        """创建LLM请求（可选注入reminder）。"""
        request = create_llm_request(model_set, self.chatter_name)
        
        # 注入system prompt
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        
        # 注入reminder（如果指定bucket）
        if with_reminder:
            reminders = reminder_store.get_all(with_reminder)
            for reminder in reminders:
                request.add_payload(
                    LLMPayload(ROLE.SYSTEM, Text(f"<system_reminder>{reminder}</system_reminder>"))
                )
        
        return request
```

**使用示例**：

```python
# life_engine设置唤醒上下文reminder
reminder_store.set("actor", "wake_context", wake_context_text)

# DFC创建请求时注入
request = chatter.create_request(with_reminder="actor")
# 自动包含：<system_reminder>[最近事件流]</system_reminder>
```

---

## 五、消息模型详解

### 5.1 Message 统一模型

```python
# core/models/message.py
class Message:
    """统一消息模型（抽象平台差异）。"""
    
    # 基础信息
    message_id: str          # 消息唯一ID
    platform: str            # 平台标识（qq/discord/telegram）
    chat_type: str           # 聊天类型（group/private/discuss）
    stream_id: str           # 会话ID
    
    # 发送者信息
    sender_id: str           # 发送者ID
    sender_name: str         # 发送者昵称
    sender_cardname: str     # 发送者名片（群名片）
    
    # 内容
    content: str             # 原始内容
    processed_plain_text: str # 处理后的纯文本
    message_type: MessageType # 类型（text/image/at/reply）
    
    # 时间
    time: float              # 时间戳
    
    # 扩展信息
    extra: dict              # 平台特有信息（group_id等）
```

**平台抽象**：

```python
# QQ平台消息 → Message模型
{
    "platform": "qq",
    "chat_type": "group",
    "stream_id": "group_12345",
    "sender_id": "user_67890",
    "content": "[CQ:text,text=你好]",
    "processed_plain_text": "你好",
    "message_type": MessageType.TEXT,
    "extra": {
        "group_id": "12345",
        "group_name": "晨间讨论",
    }
}

# Discord消息 → Message模型（相同结构）
{
    "platform": "discord",
    "chat_type": "channel",
    "stream_id": "channel_98765",
    "sender_id": "user_11111",
    "content": "你好",
    "processed_plain_text": "你好",
    "message_type": MessageType.TEXT,
    "extra": {
        "channel_id": "98765",
        "channel_name": "general",
    }
}
```

不同平台转换为统一Message模型，core层处理统一结构，无需关心平台细节。

---

## 六、组件实现约束

### 6.1 Action vs Tool 边界

**关键区别**：

| 维度 | Action | Tool |
|-----|--------|------|
| **职责** | 执行动作（副作用） | 查询信息（无副作用） |
| **返回值** | (bool, str) | (bool, str \| dict) |
| **chat_stream注入** | 有（构造注入） | 无 |
| **使用场景** | 发送消息、写文件 | 计算器、翻译、查询 |

**实现示例**：

```python
# Action示例：发送消息
class SendTextAction(BaseAction):
    action_name = "send_text"
    action_description = "发送文本消息"
    
    async def execute(self, text: str, stream_id: str) -> tuple[bool, str]:
        # 有chat_stream（可获取消息上下文）
        chat_stream = self.chat_stream
        
        # 执行副作用：发送消息
        await chat_stream.send_message(text, stream_id)
        
        return True, "消息已发送"

# Tool示例：计算器
class CalculatorTool(BaseTool):
    tool_name = "calculator"
    tool_description = "执行数学计算"
    
    async def execute(self, expression: str) -> tuple[bool, dict]:
        # 无chat_stream（不依赖消息上下文）
        
        # 纯计算（无副作用）
        result = eval(expression)
        
        return True, {"result": result, "expression": expression}
```

### 6.2 必须遵守的硬规则

**规则1：名称属性必须定义**

```python
class MyAction(BaseAction):
    # ❌ 错误：未定义action_name
    # action_description必须有
    
    # ✅ 正确：必须定义
    action_name = "my_action"
    action_description = "我的动作"
```

**规则2：依赖写成完整签名**

```python
class MyAction(BaseAction):
    dependencies: list[str] = [
        # ❌ 错误：简写
        # "other_plugin",
        
        # ✅ 正确：完整签名
        "other_plugin:service:storage",
    ]
```

**规则3：构造函数禁止修改**

```python
class MyAction(BaseAction):
    # ❌ 错误：重写构造
    # def __init__(self, custom_arg):
    
    # ✅ 正确：保持基类构造
    # BaseAction.__init__(chat_stream, plugin)
```

**规则4：异步任务用task_manager**

```python
class MyPlugin(BasePlugin):
    async def on_plugin_loaded(self):
        # ❌ 错误：直接create_task
        # asyncio.create_task(self.background_task())
        
        # ✅ 正确：用task_manager
        tm = get_task_manager()
        tm.create_task(self.background_task(), name="my_plugin_bg")
```

---

## 七、总结

### 7.1 Core 层的核心价值

| 维度 | Kernel层 | Core层 |
|-----|---------|--------|
| **职责** | 技术基础设施 | 领域智能逻辑 |
| **抽象级别** | 工具库 | 业务组件 |
| **平台依赖** | 完全无关 | 抽象（无具体平台） |
| **复用性** | 可独立使用 | Neo-MoFox专属 |

### 7.2 插件组件系统的优势

**对比传统框架**：

| 维度 | 传统框架 | Neo-MoFox插件系统 |
|-----|---------|------------------|
| **扩展方式** | 修改核心代码 | 编写插件组件 |
| **注册机制** | 手动注册 | 自动注册（@register_plugin） |
| **依赖管理** | 无 | manifest依赖声明 |
| **生命周期** | 无 | 加载/卸载钩子 |
| **签名定位** | 类名/路径 | 统一签名格式 |

**插件化架构确保系统长期可扩展，避免核心代码腐化。**

---

*Written for Neo-MoFox Project, 2026-04-17*
*作者：Claude (Sonnet 4.6)*