# DefaultChatter 调试模式移植与启用报告

## 背景
- 需求：参考 kokoro_flow_chatter 的 Debug 模式，在 default_chatter 中加入相同的可视化调试能力，方便查看完整提示词与工具调用摘要。
- 相关插件：default_chatter、kokoro_flow_chatter。

## 变更概览
1. **配置新增**：在 `plugins/default_chatter/config.py` 增加 `[debug]` 段，包含 `show_prompt`、`show_response` 开关，默认仅开启响应摘要。
2. **调试工具移植**：将 KFC 的 `debug/log_formatter.py` 移植到 `default_chatter/debug/`，适配 DefaultChatter 的工具命名与日志前缀 (`default_chatter_debug`)。
3. **执行流程嵌入**：在 `runners.py` 的 enhanced/classical 流程中，在 `await response.send()` 前后输出：
   - `format_prompt_for_log`：美化打印完整提示词（含 SYSTEM、人设、工具 schema、对话历史）。
   - `log_dc_result`：美化打印工具调用摘要（think、send_text、pass_and_wait、stop_conversation 等）。

## 关键文件
- `plugins/default_chatter/config.py`：新增 DebugSection 与字段。
- `plugins/default_chatter/debug/log_formatter.py`：格式化提示词与响应摘要。
- `plugins/default_chatter/runners.py`：在请求前后打点日志，遵循开关。

## 使用指南
1. 在 `config/plugins/default_chatter/config.toml` 打开开关：
   ```toml
   [debug]
   show_prompt = true      # 日志打印完整提示词
   show_response = true    # 日志打印工具调用摘要
   ```
2. 重启后，查看控制台/日志面板即可看到：
   - 发送给 LLM 的完整 payload 展示（人设、工具列表、历史+新消息）。
   - LLM 工具调用摘要（think 内容、send_text 文本、等待/结束提示等）。

## 差异说明
- 保持 DefaultChatter 自身的工具解析逻辑不变，仅增加可视化输出。
- 日志前缀改为 `default_chatter_debug`，避免与 KFC 混淆。

## 后续建议
- 如需同时调试 proactive_message_plugin，可搭配 prompt_logger 观察原始请求/响应。
- 若提示词过长，可临时关闭 `show_prompt` 以减小日志量。
