# unfinished_thought_plugin 改造报告

## 结论

`unfinished_thought_plugin` 已完成一次“碎片化”改造：扫描侧不再把完整复盘信息喂给模型，提示词也从“整理总结”改成了“采集未完成念头”，同时加了轻量输出过滤，降低总结化内容进入池子的概率。

## 已完成修改

### 1. 扫描输入瘦身

新增了 `UnfinishedThoughtState.scan_snapshot()`，扫描模型现在优先读取轻量状态视图，只保留：

- `thought_id`
- `title`
- `content`
- `status`
- `priority`

这样可以避免 `reason`、历史记录、统计信息等复盘型字段反向污染扫描输出。

### 2. 提示词重写

`prompts.py` 里的扫描系统提示词已经改成“未完成念头采集器”语义，不再强调整理和总结，而是明确要求：

- 输出短、碎、轻
- 优先保留“还在想”“还没说完”的感觉
- 禁止写成完整总结
- 禁止复盘口吻

主 prompt 注入块也改成了更紧凑的碎片格式，减少“标题：解释”的说明文风格。

### 3. 输出过滤

在 `_apply_scan_result()` 中加入了对明显总结化内容的轻量过滤：

- 过长内容会被视为高风险
- 含有明显总结标记的条目会被挡掉
- 更新项如果内容过于总结化，只忽略该字段，不会影响状态更新

### 4. 配置开关

新增了几个开关，方便后续继续调：

- `scan.use_compact_snapshot`
- `scan.reject_summary_like_output`
- `prompt.compact_mode`
- `prompt.max_fragment_length`

默认都保持开启，保证当前行为偏向碎片化。

## 验证结果

已完成以下验证：

- `python -m py_compile` 通过
- `pytest -q -o addopts='' test/plugins/unfinished_thought_plugin/test_unfinished_thought_plugin.py` 通过

测试结果为 `9 passed`。

## 风险说明

这次改动的策略是“先减少总结化输入，再收紧输出”，因此整体更稳，但仍有两个边界风险：

- 如果过滤过严，可能会减少扫描产出。
- 如果历史上下文本身过于完整，模型仍可能偶尔产出偏总结的内容。

不过从当前验证结果看，插件的原始功能没有被破坏，自动扫描、prompt 注入、命令管理仍然正常。
