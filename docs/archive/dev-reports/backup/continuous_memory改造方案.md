# Continuous Memory 改造方案（修正版）

**日期：** 2026-03-21  
**目标：** 将 `diary_plugin` 从“按天日记”改造为“按聊天流隔离的连续记忆系统”，并在主回复链路中动态注入当前聊天的完整记忆空间。

---

## 1. 我的判断

你的目标是合理的，而且我认同方向：

1. 记忆必须按聊天隔离，不能跨私聊/群聊污染。
2. 记忆不能无限膨胀，必须自动压缩。
3. 压缩文本要保持同一个“人格视角”，所以应继续使用 diary 插件当前的第一人称写法。
4. 记忆不能只是“可读”，而是要真正进入主回复模型的上下文。

但要把这个想法落到 Neo-MoFox 现有代码上，必须改几处关键认知：

1. 当前系统稳定的隔离键不是 `chat_id`，而是 `stream_id`。
2. “自动注入”不能只靠静态 `system reminder`，因为它无法按当前会话切换内容。
3. 真正适合按会话动态插入记忆的入口，是 `on_prompt_build`。
4. `Service` 不是单例，不能把跨调用状态只放在 Service 实例字段里。

因此，本方案会保留“按聊天连续记忆 + 5 篇压缩 + 分层摘要 + 自动注入”这个目标，但会改成一条符合当前仓库实际结构的实现路径。

---

## 2. 设计原则

### 2.1 以 `stream_id` 作为唯一隔离键

当前运行时中：

- `ON_CHATTER_STEP` 事件稳定提供 `stream_id`
- `ChatStream` 稳定提供 `stream_id` 和 `chat_type`
- `default_chatter` 在构建 user prompt 时，会把 `stream_id` 放进 `PromptTemplate.values`

因此连续记忆的真实主键应为：

- `stream_id`

辅助元数据：

- `chat_type`
- `platform`
- `stream_name`

不要把设计建立在不存在或不稳定的 `chat_id` 字段上。

### 2.2 记忆真源放在 Service

连续记忆的唯一真源应是 `DiaryService`。它负责：

- 定位当前聊天流的记忆文件
- 解析现有记忆空间
- 追加新记忆
- 判断是否触发压缩
- 执行层级压缩
- 组装注入给主模型的完整记忆文本

`Action`、`Tool`、`EventHandler` 都只调用它，不各自维护独立记忆状态。

### 2.3 静态规则和动态内容分离

记忆系统分两部分注入：

1. 静态 reminder  
   作用：告诉模型“你有连续记忆系统，应如何理解它”。

2. 动态 prompt 注入  
   作用：把“当前 `stream_id` 的实际记忆空间内容”注入当前 prompt。

静态 reminder 继续保留在 `actor` bucket 没问题。  
但动态记忆文本必须通过 `on_prompt_build` 注入到目标 prompt 的 `extra` 区块。

### 2.4 压缩与自动写日记必须共享人格基底

这是本次改造的硬约束，不是可选优化。

原因：

- 连续记忆的价值不只是“保存事实”
- 还包括“以同一个主观人格记住这些事实”
- 如果自动写日记和压缩摘要由两套不同的人设提示词驱动，就会出现口吻漂移、主观性断裂、记忆不像“同一个人写的”问题

因此必须保证：

1. 压缩链路默认复用自动写日记所使用的同一个模型任务
2. 压缩链路复用自动写日记的人设提示词骨架
3. 压缩提示词只允许在共享人格基底后，额外追加“压缩任务说明”
4. 不单独设计一个“摘要机器人”式系统提示词

换句话说：

- 自动写日记是“以这个人格写新的记忆”
- 层级压缩是“以这个人格整理旧的记忆”

两者的说话者必须是同一个“我”。

---

## 3. 目标能力

改造完成后，`diary_plugin` 应具备以下行为：

### 3.1 按聊天流隔离

- 每个私聊 `stream_id` 有独立记忆文件
- 每个群聊 `stream_id` 有独立记忆文件
- 记忆不跨会话共享

### 3.2 连续追加

- 自动日记写入时，不再写到“今天的某个日记文件”
- 而是写入“当前聊天流的连续记忆空间”

### 3.3 分层压缩

- 每累计新增 5 篇原始记忆，触发一次 L1 压缩
- 当 L1 摘要累计到阈值时，再压成 L2
- 当 L2 摘要累计到阈值时，再压成 L3
- 近期原始记忆保留在底部，旧内容逐步上卷

### 3.4 动态注入

- 主回复模型每次构建 prompt 时
- 自动读取当前 `stream_id` 对应的完整记忆空间
- 注入固定区域
- 切换聊天时自动切换记忆内容

---

## 4. 总体架构

### 4.1 组件划分

本次改造仍然保留 `diary_plugin` 的现有组件结构，但职责调整如下：

| 文件 | 角色 | 责任 |
| --- | --- | --- |
| `config.py` | Config | 连续记忆存储、压缩、注入相关配置 |
| `service.py` | Service | 记忆读写、解析、压缩、注入文本构建 |
| `action.py` | Action | 手动写入当前聊天流记忆 |
| `tool.py` | Tool | 主动读取当前聊天流记忆 |
| `event_handler.py` | EventHandler | 自动总结写入、`on_prompt_build` 动态注入 |
| `plugin.py` | Plugin | 注册组件、同步静态 reminder |

### 4.2 数据流

#### 写入链路

1. `ON_CHATTER_STEP` 达到阈值
2. `AutoDiaryEventHandler` 读取当前 `stream_id` 最近对话
3. 调用 LLM 生成第一人称记忆
4. 调用 `DiaryService.append_memory_entry(stream_id, ...)`
5. Service 写入原始层
6. 如果原始层累计达到 5 条，则触发层级压缩

#### 注入链路

1. `default_chatter` 构建 user prompt
2. `PromptTemplate.build()` 触发 `on_prompt_build`
3. `DiaryPromptInjector` 拿到 `values["stream_id"]`
4. 读取该 `stream_id` 的完整记忆空间
5. 将记忆块追加到 `values["extra"]`
6. 当前聊天自动获得对应记忆

---

## 5. 存储模型

### 5.1 路径设计

建议目录结构：

```text
data/continuous_memories/
├── private/
│   ├── <stream_id>.json
│   └── ...
├── group/
│   ├── <stream_id>.json
│   └── ...
└── discuss/
    ├── <stream_id>.json
    └── ...
```

理由：

- 当前系统已有 `chat_type`
- `stream_id` 本身已是稳定唯一键
- 用 `.json` 比 `.memory` 纯文本更适合分层结构与后续演进

### 5.2 记忆文件结构

建议直接落 JSON，而不是继续硬拼文本。

```json
{
  "version": 1,
  "stream_id": "xxx",
  "chat_type": "private",
  "platform": "qq",
  "stream_name": "Alice",
  "updated_at": "2026-03-21T16:30:00+08:00",
  "layers": {
    "raw": [
      {
        "id": "raw_001",
        "created_at": "2026-03-21T10:00:00+08:00",
        "section": "上午",
        "content": "我和用户讨论了连续记忆插件的结构。"
      }
    ],
    "L1": [
      {
        "id": "l1_001",
        "created_at": "2026-03-21T12:00:00+08:00",
        "source_ids": ["raw_001", "raw_002", "raw_003", "raw_004", "raw_005"],
        "content": "我和用户集中讨论了连续记忆的基本结构，用户强调按聊天隔离与自动压缩。"
      }
    ],
    "L2": [],
    "L3": []
  }
}
```

### 5.3 为什么不用纯文本文件做真源

纯文本适合“展示”，不适合“层级压缩的真源”。  
如果继续拿纯文本做真源，会遇到这些问题：

- 很难准确知道哪 5 条已经压过
- 很难做 L1/L2/L3 递归压缩
- 很难做幂等重试
- 很难做测试

因此：

- JSON 是存储真源
- 文本格式化只在“给模型看”时临时生成

---

## 6. 核心数据结构

### 6.1 建议新增 dataclass

```python
@dataclass
class MemoryEntry:
    entry_id: str
    created_at: str
    section: str
    content: str


@dataclass
class MemorySummary:
    summary_id: str
    level: int
    created_at: str
    source_ids: list[str]
    content: str


@dataclass
class ContinuousMemory:
    stream_id: str
    chat_type: str
    platform: str
    stream_name: str
    raw_entries: list[MemoryEntry]
    summaries_by_level: dict[int, list[MemorySummary]]
```

### 6.2 Service 对外接口

建议 `DiaryService` 提供这些稳定方法：

```python
def get_memory(stream_id: str, chat_type: str, *, platform: str = "", stream_name: str = "") -> ContinuousMemory

def append_memory_entry(
    stream_id: str,
    chat_type: str,
    content: str,
    *,
    section: str = "其他",
    platform: str = "",
    stream_name: str = "",
) -> tuple[bool, str]

async def compress_if_needed(
    stream_id: str,
    chat_type: str,
    *,
    platform: str = "",
    stream_name: str = "",
) -> bool

def render_memory_for_prompt(stream_id: str, chat_type: str) -> str
```

---

## 7. 压缩机制

### 7.1 正确的压缩单位

你提的是“每自动写入 5 篇日记压缩一次”，我同意。  
但这里要明确压缩对象：

- `raw` 层以 5 条为一组压成 1 条 `L1`
- `L1` 层以 5 条为一组压成 1 条 `L2`
- `L2` 层以 5 条为一组压成 1 条 `L3`

这才是真正的递归压缩。

不是：

- “原始层到 5 条就把所有旧内容整体压一次”

后者会让层次不可追踪，也不利于幂等。

### 7.2 递归压缩算法

建议算法如下：

1. 检查 `raw` 层未压缩条目数是否 `>= 5`
2. 若满足，取最老的 5 条，生成一条 `L1`
3. 删除这 5 条 `raw`
4. 检查 `L1` 层是否 `>= 5`
5. 若满足，取最老的 5 条 `L1`，生成一条 `L2`
6. 继续向上递归直到某层不足 5 条

伪代码：

```python
async def cascade_compress(memory: ContinuousMemory) -> bool:
    changed = False

    changed |= await compress_level(memory, source_level=0, target_level=1)

    level = 1
    while level < max_level:
        changed_at_level = await compress_level(
            memory,
            source_level=level,
            target_level=level + 1,
        )
        if not changed_at_level:
            break
        changed = True
        level += 1

    return changed
```

### 7.3 为什么不建议“保留 10 条再压”

你当前的目标是“每 5 篇自动压缩”。  
那压缩阈值和保留阈值就不应互相打架。

我建议：

- `compression_batch_size = 5`
- `recent_raw_keep = 0`

也就是每满 5 条旧原始记忆就上卷压缩。  
如果你确实想保留一点近期细节，可以改为：

- `recent_raw_keep = 3`

但这时语义就变成：

- “超过 3 条的旧 raw 每累计 5 条压缩一次”

两种都能做，但要明确，不要配置互相冲突。

### 7.4 压缩提示词

压缩提示词要保持 diary 的主观性：

```text
[自动写日记共享人格基底]
你是……
你写的是“我的记忆”……
必须使用第一人称“我”……
保持主观连续性……

[压缩任务补充]
你现在不是记录新的记忆，而是在整理自己已有的旧记忆。
以下是同一聊天中较早的 5 条记忆，请将它们压缩为一段新的高层记忆。

要求：
1. 延续同一人格、同一口吻、同一主观视角
2. 保留用户偏好、关键事实、重要情绪和长期上下文
3. 忽略重复和琐碎细节
4. 输出 60-120 字纯文本
5. 这是“我对这段经历的记忆浓缩”，不是客观会议纪要
```

### 7.5 压缩模型调用方式

压缩必须是异步的，不能写成同步函数里 `await`。

因此：

- `append_memory_entry()` 保持同步写入真源
- `compress_if_needed()` 与内部 `_compress_level()` 设计成 `async`
- 调用方如果是 `EventHandler`，直接 `await`
- 如果未来想转后台任务，再统一接 `task_manager`

当前先不建议在首次版本里上后台任务，优先保证正确性和可测试性。

### 7.6 提示词复用方式

为了把“主观性一致”做成代码层面的强约束，建议把 diary 插件的人设提示词拆成两段：

1. 共享人格基底
2. 任务补充段

建议结构：

```python
def build_diary_persona_prompt(config: DiaryConfig) -> str:
    ...


def build_auto_diary_prompt(config: DiaryConfig) -> str:
    return build_diary_persona_prompt(config) + "\n\n" + AUTO_DIARY_TASK_PROMPT


def build_memory_compression_prompt(config: DiaryConfig, level: int) -> str:
    return build_diary_persona_prompt(config) + "\n\n" + build_compression_task_prompt(level)
```

这样能保证：

- 自动写日记改了人设，压缩链路自动同步
- 压缩链路不会悄悄长出第二套人格
- L1/L2/L3 压缩天然保持同源

### 7.7 模型选择策略

模型选择也应做成同源默认值：

- 自动写日记默认使用 `config.model.task_name`
- 压缩默认也使用 `config.model.task_name`
- 只有在 `continuous_memory.compression_model_task` 明确配置时，才允许覆盖

因此语义应明确为：

- “默认同模同人格”
- “允许显式覆盖模型，但不允许脱离共享人格基底”

---

## 8. 自动写入链路改造

### 8.1 保留 AutoDiaryEventHandler，但把“按天”改成“按流”

当前 `AutoDiaryEventHandler` 已经是按 `stream_id` 计数，这部分方向本来就是对的。  
改造重点是：

- 总结后不再写“今天日记”
- 而是写“当前 `stream_id` 的连续记忆”

### 8.2 自动总结时需要传入的上下文

自动总结应拿到：

- `stream_id`
- `chat_type`
- `platform`
- `stream_name`
- 最近 N 条对话
- 当前已有的近期连续记忆摘要，用于去重

建议不要再用“今天已有日记”做去重基准，而是用当前流的：

- 最近 `raw` 条目
- 最近 `L1` 摘要若干条

### 8.3 写入时机

保留现有配置项：

- `auto_diary.message_threshold`

行为：

1. `ON_CHATTER_STEP` 每次触发时对该 `stream_id` 计数
2. 达到阈值后总结最近若干对话
3. 写入 `raw` 层
4. 写入后立即尝试级联压缩

---

## 9. 动态注入方案

### 9.1 不使用静态 reminder 承载动态记忆

静态 reminder store 的作用只适合放：

- 规则说明
- 使用约定
- “你拥有连续记忆系统”

它不适合放：

- 当前聊天的具体记忆内容

因为 reminder store 是全局 bucket，不是按会话实例化的。

### 9.2 通过 `on_prompt_build` 注入

这是本次方案最关键的一点。

当前 `default_chatter` 构建 user prompt 时，会把 `stream_id` 放进 template values。  
因此可以新增一个 EventHandler，例如：

- `ContinuousMemoryPromptInjector`

职责：

1. 订阅 `on_prompt_build`
2. 只处理目标 prompt，例如 `default_chatter_user_prompt`
3. 从 `params["values"]` 读取 `stream_id`
4. 通过 stream manager 或上下文补齐 `chat_type`
5. 调用 `DiaryService.render_memory_for_prompt(stream_id, chat_type)`
6. 把结果追加到 `values["extra"]`

### 9.3 注入格式

建议给主模型看到的文本长这样：

```text
## 连续记忆

以下内容是你在当前聊天中的连续记忆，请将其视为你已经记得的上下文，而不是当前用户刚刚重复告诉你的新信息。

### 压缩记忆・L3
- 我逐渐确认，用户偏好简洁、直接、少废话的实现方式。

### 压缩记忆・L2
- 我们围绕 diary_plugin 的重构边界反复讨论，最终确定要用 stream_id 做隔离键。

### 压缩记忆・L1
- 用户最近明确提出要把连续记忆动态注入主回复模型，而不是只做只读日记。

### 近期详细记忆
- [03-21 15:10] 我们讨论了按聊天隔离记忆空间。
- [03-21 15:20] 用户要求每 5 条自动压缩。
- [03-21 15:30] 用户要求老内容继续递归压缩。
```

### 9.4 注入位置

统一注入到 `extra`，不要改系统主模板结构。

原因：

- 当前已有插件就是通过 `values["extra"]` 做附加内容注入
- 改动面最小
- 与现有 prompt 系统兼容性最好

---

## 10. 对现有 diary_plugin 的改造策略

### 10.1 不建议“在旧按天结构上硬补”

当前 [`plugins/diary_plugin/service.py`](/root/Elysia/Neo-MoFox_Deployment/Neo-MoFox/plugins/diary_plugin/service.py) 的核心假设是：

- 文件按日期组织
- 只能写今天
- 读取接口是 `read_today/read_date`

这套模型和连续记忆模型不是同一个东西。  
如果继续在原实现上局部打补丁，后面会越来越乱。

### 10.2 建议保留插件名，重做 Service 内部模型

建议：

- 保留插件目录和 `plugin_name = "diary_plugin"`
- 保留 `WriteDiaryAction` / `ReadDiaryTool` / `AutoDiaryEventHandler`
- 重写 `DiaryService` 的内部数据模型和接口

也就是说：

- 外部仍然叫 `diary_plugin`
- 内核语义从“当日日记”升级成“连续记忆”

### 10.3 兼容层建议

如果担心旧调用点，可以保留一小层兼容方法：

- `read_today()` 返回当前流记忆的展示文本
- `read_date()` 明确标记为废弃或仅兼容旧接口

但新逻辑应以：

- `get_memory`
- `append_memory_entry`
- `render_memory_for_prompt`

为主。

---

## 11. 配置设计

建议在现有配置上新增一个独立 section：

```python
@config_section("continuous_memory")
class ContinuousMemorySection(SectionBase):
    enabled: bool = Field(default=True, description="是否启用连续记忆模式")
    base_path: str = Field(
        default="data/continuous_memories",
        description="连续记忆存储根目录",
    )
    batch_size: int = Field(
        default=5,
        description="每层每次压缩的批大小",
    )
    max_levels: int = Field(
        default=3,
        description="最大压缩层数",
    )
    inject_prompt: bool = Field(
        default=True,
        description="是否动态注入当前聊天记忆到主回复 prompt",
    )
    target_prompt_names: list[str] = Field(
        default_factory=lambda: ["default_chatter_user_prompt"],
        description="允许注入的 prompt 模板名列表",
    )
    recent_raw_limit: int = Field(
        default=5,
        description="向主模型展示的近期原始记忆条数",
    )
    summary_limit_per_level: int = Field(
        default=3,
        description="每层最多向主模型展示多少条摘要",
    )
    compression_model_task: str = Field(
        default="",
        description="压缩专用模型任务名，留空则复用 diary 模型",
    )
```

并在 `DiaryConfig` 中加入：

```python
continuous_memory: ContinuousMemorySection = Field(
    default_factory=ContinuousMemorySection
)
```

---

## 12. 关键实现细节

### 12.1 缓存不能依赖 Service 实例字段

由于 `get_service()` 每次都会返回新 Service 实例，不能把缓存只放在实例字段并假设可复用。

可以选两种方式：

1. 首版不做内存缓存  
   直接每次读 JSON 文件，简单可靠。

2. 做进程级缓存  
   通过模块级 cache 字典维护。

我建议首版先选 1。  
连续记忆文件是按单流读取，体量不会太夸张，先求正确。

### 12.2 并发写入

同一 `stream_id` 可能在相近时间内触发多次写入。  
建议在 Service 内做：

- 模块级 `asyncio.Lock` 映射，按 `stream_id` 加锁

虽然首版不做后台任务，但写入和压缩最好仍然串行化，避免文件竞争。

### 12.3 去重策略

建议保留现有简单去重，但作用范围要改成：

- 只对当前流最近若干 `raw` 条目做检查
- 可附带最近 1-2 条 L1 摘要做弱去重

不要拿全历史做全量字符串相似度比较，成本高且意义不大。

### 12.4 Prompt 长度控制

完整记忆空间是“逻辑完整”，不是“把全部历史都塞进去”。

对主模型注入时建议：

- 每层摘要限制条数
- raw 只展示最近若干条
- 超限时优先保留高层摘要和最新 raw

因此“完整记忆空间”在运行时应理解为：

- 当前可用的层级化完整视图

而不是：

- 全量无裁剪历史

---

## 13. 推荐落地步骤

### 阶段 1：替换存储模型

1. 在 `config.py` 增加 `continuous_memory` 配置
2. 重写 `service.py` 的内部模型为按 `stream_id` 的 JSON 存储
3. 保留 `ReadDiaryTool` 和 `WriteDiaryAction` 对外名称不变

### 阶段 2：接通自动写入

1. 修改 `AutoDiaryEventHandler`
2. 自动总结后写入当前流记忆
3. 写入后调用级联压缩

### 阶段 3：接通动态注入

1. 在 `event_handler.py` 新增 `ContinuousMemoryPromptInjector`
2. 订阅 `on_prompt_build`
3. 向 `values["extra"]` 注入当前流记忆块

### 阶段 4：保留静态 reminder

1. `plugin.py` 保留 reminder 同步
2. 但内容改为“连续记忆规则说明”
3. 不再假装 reminder 本身承载动态记忆

### 阶段 5：补测试

至少覆盖：

1. `stream_id` 隔离读写
2. raw -> L1 压缩
3. L1 -> L2 递归压缩
4. prompt 注入按 `stream_id` 切换
5. 去重与空记忆场景

---

## 14. 风险与取舍

### 14.1 最大风险：把“动态注入”做成静态 reminder

这是最容易误做、也最容易表面上“看起来能用”的地方。  
但这样做最终不会真正按聊天切换内容。

### 14.2 第二个风险：继续沿用按天文件模型

如果还是：

- `YYYY-MM-DD.md`

那连续记忆一定会越来越别扭，因为你的核心维度已经从“日期”变成“聊天流”了。

### 14.3 第三个风险：压缩策略没有可追踪来源

如果摘要不记录 `source_ids`，以后：

- 无法判断哪些内容已被压缩
- 无法做幂等恢复
- 无法调试错误压缩

所以摘要必须保留来源引用。

---

## 15. 最终结论

我的结论是：

1. 你的目标完全值得做，而且和当前 `diary_plugin` 的演化方向一致。
2. 真正正确的隔离键应是 `stream_id`，不是 `chat_id`。
3. 真正正确的动态注入点应是 `on_prompt_build`，不是全局静态 reminder。
4. 真正可维护的存储真源应是结构化 JSON，而不是纯文本拼接。
5. 真正的递归压缩应按层批量上卷：`raw -> L1 -> L2 -> L3`。

因此我建议把这次改造定义为：

> 保留 `diary_plugin` 外部身份，但将其内部能力正式升级为“按 `stream_id` 隔离的连续记忆插件”。

---

## 16. 后续实现建议

如果按这个方案开始编码，我建议实际改动顺序是：

1. 先重写 `service.py`
2. 再改 `event_handler.py`
3. 再补 `plugin.py` 的静态 reminder 文案
4. 最后补测试

不要先碰 prompt 文案或 Action 描述。  
先把真源、压缩和注入链路打通，文案层最后收口。
