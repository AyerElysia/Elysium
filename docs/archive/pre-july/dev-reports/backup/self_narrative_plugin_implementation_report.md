# self_narrative_plugin 实现报告

## 实现结果

`self_narrative_plugin` 已完成落地，作为“自我连续性”层运行。当前实现包含：

- 每个聊天流独立的自我叙事文件
- 每日 0 点自动更新
- `/self_narrative` 命令手动触发
- prompt 构建时自动注入自我叙事摘要
- 更新历史保留与查看
- 稳定边界显示开关
- 近期历史显示开关
- 自动调度开关，关闭后仅保留手动更新

## 文件结构

- 插件入口：`plugins/self_narrative_plugin/plugin.py`
- 配置：`plugins/self_narrative_plugin/config.py`
- 服务：`plugins/self_narrative_plugin/service.py`
- prompt：`plugins/self_narrative_plugin/prompts.py`
- 命令：`plugins/self_narrative_plugin/commands/self_narrative_command.py`
- 事件处理器：
  - `plugins/self_narrative_plugin/components/events/startup_event.py`
  - `plugins/self_narrative_plugin/components/events/prompt_injector.py`
- 默认配置：`config/plugins/self_narrative_plugin/config.toml`

## 核心行为

### 自动更新

- 按配置在每天 `00:00` 触发一次日更
- 启动时可补跑一次未执行的日更
- 更新输入来自：
  - 前一天日记
  - 当前聊天流连续记忆
  - 当前自我叙事状态
  - 可选的睡眠状态快照
- 自我叙事更新默认使用 `diary` 模型任务，和旧的自动写日记逻辑分离

### 手动命令

- `/self_narrative update`
- `/self_narrative view`
- `/self_narrative history`
- `/self_narrative reset`

### prompt 注入

- 默认注入到 `default_chatter_user_prompt`
- 只注入摘要，不默认暴露详细历史
- 可通过配置控制是否注入稳定边界和历史记录
- 当当前状态没有可注入内容时，不再输出空壳 prompt 块

## 持久化说明

- 采用 JSON 文件持久化
- 按聊天流隔离
- 默认路径：
  - `data/self_narratives/private/<stream_id>.json`
  - `data/self_narratives/group/<stream_id>.json`
  - `data/self_narratives/discuss/<stream_id>.json`

## 设计取舍

- 自我叙事更新是严格 JSON 输出，降低解析失败概率
- 使用增量合并而不是全量重写，降低人格漂移风险
- 命令更新加了冷却，避免短时间内反复刷写
- 日更以“前一天”作为参考日期，符合 0 点触发语义
- 自动调度可以单独关闭，关闭后不会再创建定时任务
- 稳定边界与近期历史都属于可选注入，默认不把详细历史塞进主回复模型

## 额外修正

- 修正了 `schedule.enabled` 未生效的问题，现在关闭后不会启动自动调度
- 修正了 prompt 注入在无可展示内容时仍输出空壳区块的问题
- 同步更新了过时的模型配置测试，避免默认提供商和模型表的旧断言误报失败

## 验证结果

已完成以下验证：

- `test/plugins/self_narrative_plugin/test_self_narrative_plugin.py`
- `test/plugins/diary_plugin/test_service.py`
- `test/plugins/diary_plugin/test_event_handler.py`
- `test/core/config/test_model_config.py`

结果：

- `53 passed`
- 仅保留仓库既有的 pytest / SQLAlchemy / websockets 警告，没有新的功能性报错

## 风险与后续

- 真实在线联调还需要在运行中确认：
  - `on_prompt_build` 是否按预期注入
  - 调度器在 0 点是否稳定触发
  - `self_narrative` 与 `diary/continuous_memory` 的上下文融合是否自然

