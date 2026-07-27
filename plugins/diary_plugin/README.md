# Diary Plugin - 日记插件

**版本：** 2.0.0  
**作者：** Neo-MoFox Team

日记插件为模型提供两类能力：

1. 按天记录与读取日记。
2. 按聊天流维护连续记忆，并把自动写出的日记同步到记忆层。

当前实现已经把自动写日记模型、连续记忆压缩模型与主回复模型的人设提示词对齐，避免它们各自用一套不同的上下文风格。

---

## 功能

### 1. 按天日记

- `read_diary`：读取指定日期或今天的日记。
- `write_diary`：写入今天的日记内容。
- 写入前会先做重复检查，避免重复记录。
- 日记文件按月份目录分层保存。

### 2. 自动写日记

- 在聊天达到阈值后自动总结当前对话。
- 自动总结后写入今天的日记文件。
- 支持只在私聊触发，默认不在群聊触发。
- 自动总结时会复用主回复模型的完整人设提示词。
- 每次自动写入成功后，会把“自动日记摘要”排队为下一次 user prompt 的 `extra` 一次性注入。
- 注入内容会附带使用提示：它是前面对话的小总结，仅用于快速了解发生了什么，不要求必须逐句引用。

### 3. 连续记忆

- 自动写出的日记项会同步进入按聊天流隔离的连续记忆空间。
- 每个聊天流都有独立的连续记忆文件。
- 每累计 5 条新的自动日记项，会自动压缩成更高层摘要。
- 连续记忆会在目标 prompt 构建时自动注入到 `extra` 区块。

### 4. 人设对齐

- 自动写日记模型默认复用 `default_chatter` 的完整系统人设 prompt。
- 连续记忆压缩模型也默认复用同一份完整人设 prompt。
- 只对完全匹配的核心昵称做身份锁定，避免把相似名字误当成本体。

---

## 目录结构

```text
diary_plugin/
├── manifest.json          # 插件清单
├── plugin.py              # 插件入口
├── config.py              # 配置定义
├── service.py             # 日记与连续记忆核心服务
├── tool.py                # 读日记工具
├── action.py              # 写日记动作
├── event_handler.py       # 自动写日记与连续记忆注入事件
├── prompts.py             # 提示词构建辅助函数
└── README.md              # 本文档
```

---

## 配置

配置文件位于 `config/plugins/diary_plugin/config.toml`，首次运行会自动生成。

### `[plugin]`

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用日记插件 |
| `inject_system_prompt` | `true` | 是否向 actor bucket 注入日记引导语 |
| `inherit_default_chatter_persona_prompt` | `true` | 自动日记和连续记忆压缩是否复用主回复模型完整人设 |
| `strict_identity_name_lock` | `true` | 是否启用严格名字锁定 |

### `[storage]`

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `base_path` | `"data/diaries"` | 日记根目录 |
| `date_format` | `"%Y-%m"` | 月份目录格式 |
| `file_format` | `"%Y-%m-%d.md"` | 日记文件名格式 |

### `[format]`

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enable_header` | `true` | 是否启用文件头 |
| `enable_section` | `true` | 是否启用时间段分类 |
| `time_format` | `"%H:%M"` | 日记时间戳格式 |
| `default_section` | `"其他"` | 默认时间段 |

### `[dedup]`

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用写前去重 |
| `similarity_threshold` | `0.8` | 相似度阈值 |
| `min_content_length` | `5` | 最小内容长度 |

### `[reminder]`

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `bucket` | `"actor"` | system reminder 注入桶 |
| `name` | `"关于写日记"` | reminder 名称 |
| `custom_instructions` | `""` | 自定义补充说明 |

### `[auto_diary]`

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用自动写日记 |
| `message_threshold` | `20` | 自动总结触发阈值 |
| `allow_group_chat` | `false` | 是否允许群聊自动写日记 |

### `[model]`

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `task_name` | `"diary"` | 写日记使用的模型任务名 |

### `[continuous_memory]`

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enabled` | `true` | 是否启用连续记忆 |
| `base_path` | `"data/continuous_memories"` | 连续记忆根目录 |
| `private_subdir` | `"private"` | 私聊子目录 |
| `group_subdir` | `"group"` | 群聊子目录 |
| `discuss_subdir` | `"discuss"` | 讨论组子目录 |
| `batch_size` | `5` | 压缩批大小 |
| `max_levels` | `3` | 最大压缩层级 |
| `max_items_top_level` | `0` | 最高压缩层（`Lmax_levels`）最大保留条目数（`0` 表示不限制，超出会裁剪最旧条目） |
| `inject_prompt` | `true` | 是否注入到目标 prompt |
| `include_recent_entries_in_prompt` | `false` | 是否注入近期原始条目 |
| `target_prompt_names` | `["default_chatter_user_prompt"]` | 允许注入的 prompt 名称 |
| `recent_entry_limit` | `5` | 近期条目展示上限 |
| `summary_limit_per_level` | `3` | 每层摘要展示上限 |
| `compression_model_task` | `""` | 连续记忆压缩模型任务名，留空则复用 `model.task_name` |

---

## 使用方式

### 1. 读取日记

日记内容可通过 `read_diary` 工具读取。

常见用途：

- 查看今天已经记过什么。
- 在写新内容前先检查上下文。
- 回顾历史日记。

### 2. 写入日记

使用 `write_diary` 动作写入今天的内容。

注意：

- 只能写今天的日记，不能改历史日记。
- 写入前会先读取已有内容并做重复检查。
- 内容建议简洁、自然、带有事件感。

### 3. 自动写日记

自动写日记默认会在聊天消息达到阈值后触发。

写入流程：

1. 取最近一段对话。
2. 调用日记总结模型。
3. 检查是否与今天已有内容重复。
4. 写入当天日记。
5. 同步到连续记忆。
6. 把摘要一次性注入到下一次 user prompt（`extra`）后自动清空，避免重复注入。

### 4. 连续记忆注入

连续记忆会在 `on_prompt_build` 阶段注入到目标 system prompt 的专用 `continuous_memory` 区块中，不会和 `history` / `unreads` 混在一起。

默认目标模板是：

- `default_chatter_system_prompt`

如果你想让其他插件也吃到连续记忆，只要把模板名加到：

- `continuous_memory.target_prompt_names`

---

## 提示词对齐说明

当前版本中，日记相关模型不会再单独使用一套很短的孤立 prompt，而是：

1. 先拿主回复模型完整系统人设。
2. 再叠加自动写日记或连续记忆压缩的任务说明。
3. 再加严格名字锁定，防止相似名字误认。

这对下面几类问题尤其有帮助：

- 自动日记里“我 / 你”混乱。
- 日记主体从角色视角漂成用户视角。
- 相似名字被误并成本体。
- 连续记忆压缩后丢失角色气质。

---

## 文件存储

### 日记文件

默认按月份分目录保存：

```text
data/diaries/2026-03/2026-03-22.md
```

### 连续记忆文件

默认按聊天类型分目录保存：

```text
data/continuous_memories/private/<stream_id>.json
data/continuous_memories/group/<stream_id>.json
data/continuous_memories/discuss/<stream_id>.json
```

---

## 常见问题

### 为什么自动写出来的日记有时会变成“我”的日记？

通常是模型没有拿到完整人设，或者指代约束不够强。

当前版本已经补了两层保护：

1. 复用 `default_chatter` 的完整系统 prompt。
2. 在任务提示里明确规定“我”只能指代本体。

### 为什么有些相似名字会被误认？

因为名字太接近时，模型容易做模糊归一。

当前版本默认只接受完全匹配的核心昵称，其他名字都当作不同用户处理。

### 连续记忆为什么不注入到所有 prompt？

这是为了避免上下文膨胀。

默认只注入到 `default_chatter_system_prompt`，如果你需要别的插件也看到连续记忆，可以手动扩展 `target_prompt_names`。

---

## 安装

把 `diary_plugin/` 放入 `plugins/` 目录即可。

首次启动时通常会自动生成对应配置文件。

---

## 相关文档

- 实现报告：`report/2026-03-22_diary_plugin_persona_prompt_alignment_report.md`
- 旧连续记忆报告：`report/diary_plugin_continuous_memory_implementation_report.md`
