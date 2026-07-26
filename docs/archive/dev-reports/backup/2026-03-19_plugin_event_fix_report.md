# 事件处理与插件加载问题总结（2026-03-19）

## 背景
- 启动日志出现 `event_manager | ERROR | 事件处理器 ... 执行失败: CONTINUE`，同时提示 `Bot 已初始化，加载了 11/14 个插件（3 个失败）`，未加载的包括 `proactive_message_plugin`、`time_awareness_plugin`、`kokoro_flow_chatter`。
- 用户手动修改 `config/plugins/time_awareness_plugin/config.toml`、`config/plugins/proactive_message_plugin/config.toml` 时，混入了非法字符 `【`，导致 TOML 解析失败。
- `default_chatter` 的 `stop_conversation` 动作需要被禁用。

## 问题与根因
1. **事件处理器返回值错误**
   - `EventDecision` 枚举仅有 `SUCCESS`、`STOP`、`PASS`，不存在 `CONTINUE`。
   - `time_awareness_plugin` 和 `proactive_message_plugin` 的 `execute` 返回了 `EventDecision.CONTINUE`，触发 `AttributeError` 被安全包装捕获并记录为报错。

2. **配置文件语法错误**
   - `config/plugins/time_awareness_plugin/config.toml` 与 `config/plugins/proactive_message_plugin/config.toml` 中的 `enabled` 行尾包含 `【`，导致 TOML 解析失败，插件无法加载。

3. **停止对话动作未彻底禁用**
   - `default_chatter` 的 `get_components` 列表曾重新包含 `StopConversationAction`，LLM 仍可调用 `action-stop_conversation`。

## 已采取的修复
- 将事件处理器返回值统一为合法枚举：
  - `plugins/time_awareness_plugin/plugin.py` 的 `TimeAwarenessEventHandler.execute` 改为返回 `EventDecision.SUCCESS`。
  - `plugins/proactive_message_plugin/plugin.py` 的 `ProactiveMessageEventHandler.execute` 改为返回 `EventDecision.SUCCESS`。
- 配置语法修正（用户已完成）：
  - `config/plugins/time_awareness_plugin/config.toml` 行尾去除 `【`，`enabled = true`。
  - `config/plugins/proactive_message_plugin/config.toml` 行尾去除 `【`，`enabled = false`（保持禁用亦可，不影响加载）。
- 禁用停止对话动作：
  - `plugins/default_chatter/plugin.py` 的 `get_components` 仅保留 `DefaultChatter`、`SendTextAction`、`PassAndWaitAction`，不再注册 `StopConversationAction`。

## 当前状态（需重启验证）
- 代码侧的 `CONTINUE` 返回值已修正，配置文件语法已修正；重启后应不再出现 `CONTINUE` 报错。
- 请关注启动日志中的 “Bot 已初始化，加载了 X/Y 个插件” 提示，确认 `proactive_message_plugin`、`time_awareness_plugin`、`kokoro_flow_chatter` 是否加载成功。
- `kokoro_flow_chatter` 若仍失败，需查看启动时的具体异常（可能与其配置或依赖模型缺失有关）。

## 复现 / 自查指引
1. 确认 `EventDecision` 只使用 `SUCCESS` / `STOP` / `PASS`，不要再使用 `CONTINUE`。
2. 检查 TOML 配置是否存在不可见或非法字符；保存为 UTF-8，键值后不添加多余符号。
3. 若插件未加载，查看启动日志首个报错堆栈；常见原因：
   - 配置解析失败（语法错误、路径不存在）。
   - 依赖资源缺失（模型/数据文件）。
   - 插件内部抛异常（可在 `plugins/<name>/plugin.py` 搜索可能的 import/初始化错误）。

## 后续建议
- 启动后观察 1-2 轮消息，确认无事件处理器报错；如有，请记录首条异常堆栈定位。
- 若需重新启用 `proactive_message_plugin`，将 `enabled = true` 后重启，留意其等待/触发阈值配置。
- `kokoro_flow_chatter` 如需使用，先确保其配置与依赖已就绪，再查看启动日志定位具体错误。
