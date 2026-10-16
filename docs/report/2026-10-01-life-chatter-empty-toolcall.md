# 2026-10-01 life_chatter 空正文误判诊断

## 结论

本次不回复的直接根因不是 QQ WebSocket，也不是三个模型都不可用，而是请求层把“空正文但带有效工具调用”的流式响应误判为 LLMEmptyResponseError。

## 时间线与证据

- 00:03:54：Elysium 重启并加载表达路由 gpt-6.1-sol -> gpt-6-sol -> gpt-6-luna，路由快照 digest 为 c1f863a971793415。
- 00:04:08：NapCat WebSocket 重新连接，Bot 3427056465 连接成功。
- 00:04:30：gpt-6.1-sol 被记录为 LLMEmptyResponseError，并切换到 gpt-6-sol。
- 00:05:09：gpt-6-sol 同样被记录为空回复，并切换到 gpt-6-luna。
- 00:06:44：gpt-6-luna 也被记录为空回复。

请求检视器中的同一批请求显示，三个候选模型均返回了 reasoning_content 和一个 action-life_send_text 工具调用；response.content 为空是因为文字被放在工具参数中，并非模型没有生成发送内容。

## 根因

config/models.toml 将三个模型声明为 stream = true，因此请求层启用了 force_stream_mode。流式响应由 precollect_stream_for_non_stream 收集后，工具调用位于 resp.call_list；但 src/kernel/llm/request.py 的空正文检查仍使用 client.create 返回的局部 tool_calls 变量。强制流式路径中该变量保持为空，于是空正文被错误抛出 LLMEmptyResponseError，合法工具调用没有机会执行。

## 影响与当前状态

- Elysium 和 NapCat 已成功重启，健康接口返回 200，QQ WebSocket 已连接。
- 表达层的新三模型故障转移链已加载，但会被上述判断共同误伤。

## 修复

- src/kernel/llm/request.py 现在同时检查 client.create() 返回的 tool_calls 与强制流式收集后的 resp.call_list。
- 新增回归测试，覆盖空正文工具调用不应触发故障转移；请求层定向测试结果为 22 passed。
- 代码修复已完成，但当前运行进程尚未加载新代码；需要用户手动重启 Elysium 后进行 QQ 实链验收。

## 表达层非流式配置

为避免表达层再次进入 SSE 分块收集路径，gpt-6.1-sol、gpt-6-sol、gpt-6-luna 的模型配置已统一设置为 stream = false。表达任务顺序保持不变。配置已通过 TOML 解析和模型路由校验；新配置需要用户手动重启 Elysium 后加载。
