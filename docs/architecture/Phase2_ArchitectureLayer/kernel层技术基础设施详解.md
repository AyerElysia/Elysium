# Kernel 层技术基础设施详解：平台无关的工具库

> **核心定位**：Kernel 层是一个独立的工具库，无业务逻辑，可被其他项目直接使用。

---

## 一、Kernel 层的设计哲学

### 1.1 工具库性质

**Kernel 层不是 Neo-MoFox 专属**，而是一个通用的技术基础设施库：

```python
# Kernel 层可独立使用
from src.kernel.config import ConfigBase
from src.kernel.llm import LLMRequest
from src.kernel.logger import get_logger

# 无需任何 Neo-MoFox 领域概念
# 可直接用于其他 Python 项目
```

### 1.2 技术基础设施清单

| 模块 | 职责 | 技术栈 |
|-----|------|--------|
| **db** | 数据库抽象 | SQLAlchemy, asyncpg |
| **vector_db** | 向量数据库 | ChromaDB |
| **scheduler** | 任务调度 | APScheduler |
| **event** | 事件总线 | 自实现 Pub/Sub |
| **llm** | LLM 多厂商接口 | httpx, openai, anthropic |
| **config** | 配置系统 | Pydantic, toml |
| **logger** | 日志系统 | loguru |
| **concurrency** | 并发管理 | asyncio, TaskGroup |
| **storage** | 本地持久化 | aiofiles, JSON |

**注意**：Prompt系统在core层（`src/core/prompt/`），不在kernel层。kernel层只提供基础能力，prompt模板属于业务逻辑。

---

## 二、LLM 多厂商接口详解

### 2.1 统一接口设计

**Payload 抽象**：

```python
# kernel/llm/payload.py
class ROLE(Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    TOOL_RESULT = "tool_result"

@dataclass
class LLMPayload:
    """统一Payload结构（适配所有厂商）。"""
    role: ROLE
    content: Any  # Text/Tool/ToolResult/Image
```

**适配示例**：

| 厂商 | Payload格式 | Neo-MoFox统一格式 |
|-----|-----------|-----------------|
| **OpenAI** | `{"role": "user", "content": [{"type": "text", "text": "..."}]}` | `LLMPayload(ROLE.USER, Text("..."))` |
| **Claude** | `{"role": "user", "content": "..."}` | `LLMPayload(ROLE.USER, Text("..."))` |
| **本地模型** | 自定义 | `LLMPayload(ROLE.USER, Text("..."))` |

### 2.2 Request/Response 链式调用

```python
# kernel/llm/request.py
class LLMRequest:
    """链式LLM请求构建器。"""
    
    def add_payload(self, payload: LLMPayload) -> Self:
        """添加payload（可链式调用）。"""
        self._payloads.append(payload)
        return self
    
    async def send(self, stream: bool = False) -> LLMResponse:
        """发送请求（支持流式和非流式）。"""
        if stream:
            return self._stream_send()
        else:
            return self._sync_send()
```

**链式调用示例**：

```python
# 构建请求
request = LLMRequest(model_set, "my_request")
request.add_payload(LLMPayload(ROLE.SYSTEM, Text("System prompt")))
request.add_payload(LLMPayload(ROLE.USER, Text("User input")))

# 发送
response = await request.send()

# 继续链式（Response可再次添加payload）
response.add_payload(LLMPayload(ROLE.USER, Text("Follow up")))
final_response = await response.send()
```

### 2.3 Prompt Caching 支持

```python
# kernel/llm/model_client/openai_client.py (支持多厂商)
class ModelClient:
    """LLM客户端基类（支持OpenAI、Claude、本地模型）。"""

    async def call(self, payloads: list[LLMPayload]) -> LLMResponse:
        """调用LLM API（支持Prompt Caching）。"""
        # 标记可缓存的payload（Claude支持）
        cache_control_payloads = [
            {"role": p.role.value, "content": p.content, "cache_control": {"type": "ephemeral"}}
            for p in payloads[:3]  # 前三个payload可缓存
        ]
        
        response = await anthropic_client.messages.create(
            model=self.model,
            messages=cache_control_payloads + rest_payloads,
        )
        
        # 记录缓存命中率
        self._cache_hit_rate = response.usage.cache_read_input_tokens / response.usage.input_tokens
```

---

## 三、配置系统详解

### 3.1 类型安全配置

```python
# kernel/config/base.py
class ConfigBase(BaseModel):
    """配置基类（Pydantic + TOML）。"""
    
    @classmethod
    def load(cls, path: str) -> Self:
        """从TOML加载配置（自动验证）。"""
        data = toml.load(path)
        return cls.model_validate(data)  # Pydantic验证
```

### 3.2 配置节设计

```python
# kernel/config/section.py
@dataclass
class SectionBase:
    """配置节基类。"""
    
    def to_toml_section(self, name: str) -> str:
        """转换为TOML节格式。"""
        lines = [f"[{name}]"]
        for field_name, field_value in self.__dict__.items():
            lines.append(f"{field_name} = {toml.dumps(field_value)}")
        return "\n".join(lines)

def config_section(name: str):
    """装饰器：标记配置节名称。"""
    def decorator(cls):
        cls._section_name = name
        return cls
    return decorator
```

**配置示例**：

```python
# config/plugins/life_engine/config.toml
[settings]
enabled = true
heartbeat_interval_seconds = 30
workspace_path = "/data/life_engine_workspace"

[model]
task_name = "life"

[snn]
enabled = false
shadow_only = true
```

对应Python定义：

```python
class LifeEngineConfig(ConfigBase):
    @config_section("settings")
    class SettingsSection(SectionBase):
        enabled: bool = Field(default=True)
        heartbeat_interval_seconds: int = Field(default=30, ge=1)
        workspace_path: str = Field(default="/data/life_engine_workspace")
    
    settings: SettingsSection = Field(default_factory=SettingsSection)
    
    @config_section("model")
    class ModelSection(SectionBase):
        task_name: str = Field(default="life")
    
    model: ModelSection = Field(default_factory=ModelSection)
```

---

## 四、并发管理详解

### 4.1 TaskManager

```python
# kernel/concurrency/task_manager.py
class TaskManager:
    """统一任务管理器（替代 asyncio.create_task）。"""
    
    def create_task(
        self,
        func: Callable,
        name: str,
        daemon: bool = False,
    ) -> TaskInfo:
        """创建后台任务（自动监控）。"""
        task = asyncio.create_task(func(), name=name)
        
        # 注册到 WatchDog
        self._watchdog.register(task, name=name, daemon=daemon)
        
        return TaskInfo(task_id=task.get_name(), name=name)
    
    async def group(
        self,
        name: str,
        timeout: float | None = None,
        cancel_on_error: bool = True,
    ) -> TaskGroup:
        """创建任务组（自动清理、超时控制）。"""
        return TaskGroup(
            name=name,
            timeout=timeout,
            cancel_on_error=cancel_on_error,
            watchdog=self._watchdog,
        )
```

### 4.2 TaskGroup

```python
# kernel/concurrency/task_group.py
class TaskGroup:
    """任务组（类似 asyncio.TaskGroup，但增加监控）。"""
    
    async def __aenter__(self):
        """进入任务组上下文。"""
        self._tasks = []
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """退出时自动清理。"""
        if exc_type:
            # 异常时取消所有任务
            for task in self._tasks:
                task.cancel()
        
        # 等待所有任务完成
        await asyncio.gather(*self._tasks, return_exceptions=True)
        
        # 从 WatchDog 注销
        self._watchdog.unregister_all(self._tasks)
    
    def create_task(self, func: Callable) -> asyncio.Task:
        """在组内创建任务。"""
        task = asyncio.create_task(func())
        self._tasks.append(task)
        self._watchdog.register(task, name=f"{self.name}_{len(self._tasks)}")
        return task
```

---

## 五、数据库抽象详解

### 5.1 CRUDBase

```python
# kernel/db/crud.py
class CRUDBase:
    """通用CRUD操作基类。"""
    
    async def get_by(self, **filters) -> Model | None:
        """按字段查询（支持多字段）。"""
        query = select(self.model).filter_by(**filters)
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def create(self, obj: Model) -> Model:
        """创建记录。"""
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return obj
    
    async def update(self, obj: Model, **fields) -> Model:
        """更新记录。"""
        for key, value in fields.items():
            setattr(obj, key, value)
        await self.session.commit()
        return obj
    
    async def delete(self, obj: Model) -> bool:
        """删除记录。"""
        await self.session.delete(obj)
        await self.session.commit()
        return True
```

### 5.2 QueryBuilder

```python
# kernel/db/query_builder.py
class QueryBuilder:
    """SQL查询构建器（链式调用）。"""
    
    def filter(self, **conditions) -> Self:
        """添加过滤条件。"""
        self._filters.update(conditions)
        return self
    
    def order_by(self, field: str, desc: bool = False) -> Self:
        """排序。"""
        self._order = (field, desc)
        return self
    
    def limit(self, n: int) -> Self:
        """限制数量。"""
        self._limit = n
        return self
    
    async def all(self) -> list[Model]:
        """获取所有结果。"""
        query = select(self.model).filter_by(**self._filters)
        if self._order:
            field, desc = self._order
            query = query.order_by(desc(field) if desc else asc(field))
        if self._limit:
            query = query.limit(self._limit)
        
        result = await self.session.execute(query)
        return result.scalars().all()
```

---

## 六、事件总线详解

### 6.1 Pub/Sub 模型

```python
# kernel/event/bus.py
class EventBus:
    """最小Pub/Sub事件总线。"""
    
    def subscribe(self, event_name: str, handler: Callable) -> str:
        """订阅事件（返回订阅ID）。"""
        subscriber_id = uuid.uuid4().hex
        self._subscribers[event_name][subscriber_id] = handler
        return subscriber_id
    
    def unsubscribe(self, event_name: str, subscriber_id: str) -> bool:
        """取消订阅。"""
        if subscriber_id in self._subscribers[event_name]:
            del self._subscribers[event_name][subscriber_id]
            return True
        return False
    
    async def publish(self, event_name: str, params: dict) -> list[Any]:
        """发布事件（异步执行所有处理器）。"""
        handlers = list(self._subscribers[event_name].values())
        results = await asyncio.gather(*[h(event_name, params) for h in handlers])
        return results
```

---

## 七、日志系统详解

### 7.1 统一日志接口

```python
# kernel/logger/__init__.py
def get_logger(name: str, display: str | None = None) -> Logger:
    """获取logger实例。"""
    logger = loguru.logger.bind(name=name, display=display or name)
    
    # 配置格式
    logger.add(
        "logs/{name}.log",
        rotation="1 day",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name} | {message}",
    )
    
    return logger
```

**日志示例**：

```
2026-04-17 10:30:23 | INFO | life_engine | life_engine heartbeat #42
2026-04-17 10:30:24 | DEBUG | snn | SNN tick: hidden_rate=0.12
2026-04-17 10:30:25 | WARNING | dfc | Context payload too large
```

---

## 八、存储系统详解

### 8.1 JSON 本地持久化

```python
# kernel/storage/json_storage.py
class JSONStorage:
    """JSON文件存储。"""
    
    async def save(self, path: str, data: dict) -> bool:
        """保存JSON文件。"""
        async with aiofiles.open(path, "w") as f:
            await f.write(json.dumps(data, ensure_ascii=False))
        return True
    
    async def load(self, path: str) -> dict | None:
        """加载JSON文件。"""
        if not pathlib.Path(path).exists():
            return None
        async with aiofiles.open(path, "r") as f:
            content = await f.read()
            return json.loads(content)
```

---

## 九、总结

### 9.1 Kernel 层的核心价值

| 维度 | 传统实现 | Neo-MoFox Kernel |
|-----|---------|------------------|
| **业务耦合** | 技术代码包含业务逻辑 | 纯技术基础设施，无业务 |
| **平台绑定** | 包含QQ/Discord代码 | 平台无关，纯抽象 |
| **可复用** | 无法独立使用 | 可直接用于其他项目 |
| **可测试** | 无法独立测试 | 每个模块可独立测试 |

### 9.2 Kernel 层的独立性

**Kernel 层是一个独立工具库**：

- 无 Neo-MoFox 领域概念
- 无平台代码
- 无业务逻辑
- 可被其他 Python 项目直接使用

这才是真正的"基础设施层"。

---

*Written for Neo-MoFox Project, 2026-04-17*