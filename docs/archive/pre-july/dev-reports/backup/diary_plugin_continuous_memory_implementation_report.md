# diary_plugin 连续记忆实现报告

**日期：** 2026-03-21  
**范围：** `plugins/diary_plugin`  
**目标：** 保持原有“自动写日记/按天读写日记”逻辑不变，在其旁路新增“按聊天隔离的连续记忆空间”。

---

## 1. 结果概述

本次改造已完成，当前实现满足以下目标：

1. 原有按天日记能力保留：
   - `read_diary()` 仍按日期读取日记
   - `write_diary()` 仍写入今天的日记
   - `AutoDiaryEventHandler` 仍按原逻辑自动总结并写日记

2. 新增连续记忆能力：
   - 每个聊天流使用独立连续记忆文件
   - 连续记忆只同步“自动写出的日记项”
   - 每累计 5 个新的自动日记项，触发一次压缩
   - 支持递归压缩 `raw -> L1 -> L2 -> L3`

3. 新增动态注入能力：
   - 在 `on_prompt_build` 阶段，把当前聊天流的连续记忆注入目标 prompt 的 `extra` 区块
   - 注入随 `stream_id` 自动切换

---

## 2. 关键设计

### 2.1 保留原有日记主线

没有把 `diary_plugin` 改造成“只剩连续记忆”的新插件，而是保留两条线：

1. 按天日记主线  
   仍由原有 `DiaryService.read_today/read_date/append_entry/get_today_summary` 提供。

2. 连续记忆旁路线  
   由 `DiaryService.get_continuous_memory/append_continuous_memory_entry/render_continuous_memory_for_prompt` 提供。

### 2.2 连续记忆的数据来源

连续记忆不是手动写日记的替代品，也不是所有日记项的镜像。

当前规则是：

- 只有自动写日记成功后，才会把该条自动日记同步到连续记忆

因此“原来的自动写日记逻辑”保持主线不动，连续记忆只是从这条主线上旁路派生。

### 2.3 压缩逻辑

连续记忆采用结构化 JSON 存储：

- `raw`：未压缩的自动日记项
- `L1`：每 5 条 `raw` 压成 1 条
- `L2`：每 5 条 `L1` 压成 1 条
- `L3`：每 5 条 `L2` 压成 1 条

压缩触发后会立即级联检查更高层，因此支持递归压缩。

### 2.4 提示词一致性

自动写日记与连续记忆压缩保持了明显一致的主观口吻：

- 都以“我”的第一人称输出
- 都以“私人助手在帮我整理记忆/日记”的语气组织
- 压缩提示词延续自动写日记的主观性要求，不写成客观摘要机器人

---

## 3. 文件改动

### 已修改

- `plugins/diary_plugin/config.py`
- `plugins/diary_plugin/service.py`
- `plugins/diary_plugin/action.py`
- `plugins/diary_plugin/tool.py`
- `plugins/diary_plugin/event_handler.py`
- `plugins/diary_plugin/plugin.py`
- `plugins/diary_plugin/manifest.json`
- `config/plugins/diary_plugin/config.toml`

### 已新增

- `plugins/diary_plugin/prompts.py`
- `test/plugins/diary_plugin/test_service.py`
- `test/plugins/diary_plugin/test_event_handler.py`

---

## 4. 实现细节

### 4.1 Service 层

`DiaryService` 现在同时承担两类职责：

1. 旧职责  
   - 读取当天/指定日期日记
   - 写入今日新日记项
   - 日记去重

2. 新职责  
   - 读取/保存连续记忆 JSON
   - 连续记忆压缩
   - 连续记忆 prompt 渲染

### 4.2 EventHandler 层

保留原有 `AutoDiaryEventHandler`，在自动写日记成功之后新增一步：

1. 自动总结生成一条日记
2. 写入今日日记文件
3. 将该条自动日记同步到当前 `stream_id` 的连续记忆空间

另外新增：

- `ContinuousMemoryPromptInjector`

它会在 `on_prompt_build` 时读取 `values["stream_id"]`，并把对应连续记忆注入 `values["extra"]`。

### 4.3 配置层

新增 `[continuous_memory]` 配置节，控制：

- 是否启用连续记忆
- 存储目录
- 各聊天类型子目录
- 压缩批大小
- 最大压缩层级
- prompt 注入开关
- 目标 prompt 名称

---

## 5. 验证情况

### 5.1 语法检查

已通过：

```bash
python -m py_compile plugins/diary_plugin/*.py test/plugins/diary_plugin/*.py
```

### 5.2 代码风格检查

已通过：

```bash
ruff check plugins/diary_plugin test/plugins/diary_plugin
```

### 5.3 目标测试

已通过：

```bash
pytest -q -o addopts='' test/plugins/diary_plugin/test_service.py test/plugins/diary_plugin/test_event_handler.py
```

测试结果：

- `6 passed`

覆盖点包括：

1. 原有按天日记写入仍可用
2. 连续记忆按聊天流隔离
3. 连续记忆每 5 条压缩并递归上卷
4. 连续记忆可正确渲染为 prompt 注入文本
5. 自动写日记成功后会同步连续记忆
6. `on_prompt_build` 注入器会把连续记忆追加到 `extra`

---

## 6. 注意事项

### 6.1 自动写日记计数逻辑

本次实现没有主动改变原有自动写日记计数逻辑，只是在自动写日记成功后新增连续记忆同步步骤。

也就是说：

- 原来的自动触发节奏保持原样

### 6.2 连续记忆来源范围

当前连续记忆只同步自动写出的日记项，不同步手动 `write_diary()` 写入的内容。

这是按本次需求实现的。如果以后你要改成“手动和自动都进入连续记忆”，可以再加一层开关。

### 6.3 测试环境警告

测试时有 3 个外部环境警告：

1. `pytest` 配置里的 `asyncio_mode` 在当前环境未识别
2. SQLAlchemy 的 `declarative_base()` 有 2.0 弃用提示
3. `websockets.legacy` 有弃用提示

这些都不是本次 `diary_plugin` 改造引入的问题，也没有影响本次目标测试通过。

---

## 7. 后续建议

如果你接下来要继续推进，可以优先看这三项：

1. 给连续记忆压缩增加更细的可观测日志  
   例如记录每次压缩的源条目 ID 和目标层级。

2. 给连续记忆增加人工查看工具  
   例如命令或 router，方便直接按 `stream_id` 检查记忆文件。

3. 评估是否让手动 `write_diary()` 也可选同步到连续记忆  
   当前没有做，这是按“只从自动日记派生”实现的。

---

## 8. 结论

本次实现已经满足当前目标：

- 原自动写日记功能保留
- 新增按聊天隔离的连续记忆空间
- 连续记忆从自动日记项派生
- 每 5 条触发压缩并支持递归上卷
- 连续记忆会动态注入主回复 prompt

当前代码已完成静态检查与目标测试，可以继续进入联调或实际运行验证阶段。
