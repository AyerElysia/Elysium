# Prompt Logger 通用提示词可视化改造报告

## 目标

把 `prompt_logger` 从“原始提示词记录器”升级为“通用提示词可视化 debug 插件”，支持：

1. 把完整提示词写入专门日志文件。
2. 按来源筛选查看不同插件、chatter、request、model 的提示词。
3. 默认只记录 DFC 主回复模型，避免日志过载。

## 已完成改动

### 1. 通用过滤配置

在 `plugins/prompt_logger/config.py` 和 `config/plugins/prompt_logger.toml` 中新增了完整过滤能力：

- `scope = "dfc_main" | "custom" | "all"`
- `include_plugins`
- `include_chatters`
- `include_request_names`
- `include_models`
- `exclude_plugins`
- `exclude_chatters`
- `exclude_request_names`
- `exclude_models`
- `allow_unknown_source`

默认值已经改成：

- `scope = "dfc_main"`
- `truncate_content_length = 0`

这意味着默认会记录 DFC 主回复模型的完整提示词，不做截断。

### 2. 通用提示词格式化

重写了 `plugins/prompt_logger/log_formatter.py`，输出内容现在会带上：

- `source` 元信息
- `SYSTEM` 人设/关系
- `TOOLS` 工具 schema
- `SYSTEM` 历史叙事
- `USER` / `ASSISTANT` 对话内容
- `RESPONSE` 响应摘要与工具调用列表

### 3. 自动来源识别

在 `plugins/prompt_logger/interceptor.py` 中新增了来源解析：

- 优先读取显式上下文
- 其次从调用栈推断 `plugin_name`
- 再从 `LLMRequest.request_name` 和 `model_set` 补齐信息

这样即使别的插件没有手动接入，也可以尽量识别来源。

### 4. 统一拦截与落盘

在 `LLMRequest.send()` 的 monkey-patch 路径上统一记录请求和响应，并写到 `prompt_logger` 的专门日志文件中。

### 5. 兼容服务与事件组件

同步更新了：

- `plugins/prompt_logger/service.py`
- `plugins/prompt_logger/handlers/prompt_logger_handler.py`
- `plugins/prompt_logger/__init__.py`
- `plugins/prompt_logger/manifest.json`
- `plugins/prompt_logger/README.md`

## 默认策略说明

当前默认行为是：

1. 只记录 `request_name = "actor"` 的请求，也就是 DFC 主回复模型。
2. 保留完整提示词，不截断。
3. 仍然会记录响应摘要，除非你在配置里关闭 `show_response`。

## 怎么切换

### 看所有来源

```toml
[filter]
scope = "all"
```

### 只看某个插件

```toml
[filter]
scope = "custom"
include_plugins = ["diary_plugin"]
```

### 只看某个请求名

```toml
[filter]
scope = "custom"
include_request_names = ["continuous_memory_compression"]
```

## 校验

已执行 `python -m py_compile` 检查以下文件，语法通过：

- `plugins/prompt_logger/config.py`
- `plugins/prompt_logger/log_formatter.py`
- `plugins/prompt_logger/interceptor.py`
- `plugins/prompt_logger/plugin.py`
- `plugins/prompt_logger/service.py`
- `plugins/prompt_logger/handlers/prompt_logger_handler.py`

## 结论

这次改造后，`prompt_logger` 已经可以作为统一的提示词可视化入口使用。默认适合查 DFC 主回复模型，切换到 `custom` 或 `all` 后，也可以继续看日记插件、KFC、以及其他插件的提示词情况。
