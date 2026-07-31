# 通用提示词可视化 Debug 方案

## 目标

把 `kokoro_flow_chatter` 里现有的调试可视化能力，抽成一个通用的“提示词可视化 debug 插件”。

这个插件的核心职责不是“理解某个 chatter 的业务”，而是统一做三件事：

1. 捕获发送给 LLM 的完整提示词。
2. 把提示词整理成适合人工阅读的日志格式。
3. 允许按来源快速筛选，方便查看 DFC 主回复模型、`kokoro_flow_chatter`、日记插件等任意插件的提示词。

## 现状判断

当前仓库里已经有两类能力，但都不够通用：

1. `kokoro_flow_chatter` 里有专用 debug 格式化逻辑，能把 payload 整理成人类可读内容。
2. `prompt_logger` 已经能通过拦截 `LLMRequest.send()` 记录所有请求，但它更偏“原始日志记录”，不是面向人工阅读的上下文可视化。

所以现阶段最合理的路线不是继续复制单插件 debug，而是做一个通用层，把“记录”和“可读化”解耦。

## 推荐架构

### 1. 通用拦截层

在 `LLMRequest.send()` 或等价入口处做统一拦截，拿到：

1. 请求 payloads。
2. 响应 message / call_list。
3. 当前上下文标识。

这一层不关心具体插件，只负责把数据送到 debug 服务。

### 2. 通用格式化层

新增一个独立的 formatter，负责把不同来源的提示词统一渲染成结构化文本。

建议支持这些输出区块：

1. 基本元信息。
2. 系统提示词。
3. 历史消息。
4. 当前输入。
5. 工具 schema。
6. 响应摘要。

这样无论是 DFC 主回复模型，还是日记插件，只要能拿到 payloads，就能用同一个 formatter 输出可读结果。

### 3. 来源路由层

为了“容易指定看谁的提示词”，需要一个来源识别与过滤机制。

建议把每次记录附带这些标签：

1. `plugin_name`
2. `chatter_name`
3. `stream_id`
4. `request_name`
5. `model_identifier`
6. `event_type`，例如 `request` / `response`

筛选方式建议同时支持：

1. 按 `plugin_name` 查看，例如 `default_chatter`、`kokoro_flow_chatter`、`diary_plugin`。
2. 按 `chatter_name` 查看，例如主回复模型、被动触发器、日记生成器。
3. 按 `stream_id` 查看某一条会话的完整链路。
4. 按 `model_identifier` 查看某个模型的实际提示词。

## 关键设计点

### 1. 统一记录到专门日志文件

建议单独输出到 `logs/prompt_viz/` 之类的目录，不要混在普通运行日志里。

这样做的好处是：

1. 人类可读提示词更容易检索。
2. 可以单独轮转，不污染主日志。
3. 便于后续做 UI 或检索工具。

### 2. 输出格式尽量稳定

建议每条记录都使用固定标题结构，例如：

```text
=== LLM PROMPT VIZ ===
source: default_chatter
chatter: default_chatter
stream: abcd1234
model: gpt-...
type: request

--- SYSTEM ---
...

--- TOOLS ---
...

--- USER ---
...
```

这样后续无论是人工排查还是脚本解析，都比较稳定。

### 3. 支持“只看某个插件”

推荐配置里提供白名单和黑名单：

1. `include_plugins`
2. `exclude_plugins`
3. `include_chatters`
4. `exclude_chatters`

如果只想看 DFC 主回复模型，就配置只包含对应 `chatter_name` 或 `plugin_name`。

### 4. 支持“临时抓一个会话”

除了全局开关，建议再加一个临时过滤条件：

1. 指定 `stream_id`
2. 指定某个消息时间窗
3. 指定某个模型名

这样可以避免在调试时把全站提示词全打爆。

## 和现有代码的关系

### `kokoro_flow_chatter`

这里现有的 `debug/log_formatter.py` 可以直接复用其中的“payload 可视化”思路，但不应继续保持为专用实现。

建议把其中的格式化逻辑拆到通用模块，然后 KFC 只保留自己的业务摘要适配。

### `default_chatter`

这个插件当前只有调试日志工具文件，但没有形成真正的 debug 接入点。

如果改成通用插件，`default_chatter` 就不需要再维护自己的专用 debug 逻辑，只要在统一拦截层带上来源信息即可。

### `prompt_logger`

它适合作为“统一拦截入口”的底座，但现在的日志偏原始，更适合扩展成：

1. 原始请求记录。
2. 人类可读提示词视图。
3. 结构化筛选与归档。

也就是说，`prompt_logger` 可以演进为底层服务，通用可视化 debug 则是它之上的输出模式之一。

## 推荐实现方案

### 方案 A：在 `prompt_logger` 上直接扩展

做法：

1. 保留现有拦截器。
2. 增加一个 `viz` 输出模式。
3. 增加来源标签与筛选配置。

优点：

1. 改动范围小。
2. 复用现有请求拦截能力。
3. 容易快速落地。

缺点：

1. `prompt_logger` 职责会变重。
2. 未来如果再做 UI 或检索，模块会偏大。

### 方案 B：新建 `prompt_viz` 通用插件

做法：

1. `prompt_logger` 保持原样，继续做底层请求采集。
2. 新建 `prompt_viz` 插件，专门负责格式化、路由、可视化输出。
3. 两者通过公共事件或共享服务联动。

优点：

1. 职责更清晰。
2. 更适合后续扩展 UI、过滤器、检索索引。
3. 可视化和原始日志分层更明确。

缺点：

1. 初期代码量更多。
2. 需要一点点基础设施联动。

## 我建议的选择

优先选 **方案 B**，但实现时尽量复用 `prompt_logger` 的拦截能力，避免重复造轮子。

原因很直接：

1. 你想要的是“通用可视化层”，不是某个 chatter 的局部修补。
2. 未来你明确要看 DFC 主回复模型、日记插件等多个来源，分层设计更稳。
3. 以后如果要做“只看某个 stream / 某个 plugin / 某个 model”的能力，独立插件更好维护。

## 配置建议

建议新插件至少提供这些配置：

```toml
[general]
enabled = true
log_file = "logs/prompt_viz/prompt_viz.log"
mode = "human_readable"   # human_readable | raw | both

[filter]
include_plugins = []
exclude_plugins = []
include_chatters = []
exclude_chatters = []
include_models = []
exclude_models = []
stream_ids = []

[format]
show_request = true
show_response = true
show_tools = true
truncate_content_length = 0
group_by_source = true
include_timestamp = true
include_metadata = true
```

## 落地顺序

### 第一阶段

1. 抽出通用 payload formatter。
2. 统一记录来源元信息。
3. 接入专门日志文件。

### 第二阶段

1. 给 `prompt_logger` 增加可视化模式。
2. 支持按 plugin/chatter/model/stream 筛选。
3. 让 `default_chatter`、`kokoro_flow_chatter`、日记插件都能复用。

### 第三阶段

1. 增加更强的检索能力。
2. 如果需要，再做 WebUI 面板或日志浏览器。

## 风险点

1. 有些插件不一定走统一的 `LLMRequest.send()` 路径，可能需要补一层统一适配。
2. 某些 payload 中图片、工具 schema、链式响应较复杂，格式化时要保证不丢关键信息。
3. 如果所有来源都默认写完整提示词，日志量会很大，所以过滤和轮转是必须的。

## 结论

这件事最适合做成“通用提示词可视化 debug 插件”，而不是继续给每个插件各写一套 debug。

最实用的能力不是“能打印”，而是：

1. 能统一抓到所有提示词。
2. 能看得懂。
3. 能很方便地指定看某个插件、某个 chatter、某个模型、某个会话。

如果你要继续，我下一步可以直接给你写一份更具体的实现拆分方案，或者开始落代码骨架。
