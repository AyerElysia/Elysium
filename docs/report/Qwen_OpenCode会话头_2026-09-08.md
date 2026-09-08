# Qwen 400 MissingSessionID 与 OpenCode 会话头（2026-09-08）

## 1. 为什么不走 Qwen

不是模型本身坏了。生产 `tasks.core` / `tasks.expression` 在 S4 探测后把 `qwen3.8-flash` 拿掉，注释写「Qwen 实测 400」。中转站 24 小时内也没有任何 Qwen 成功消耗；上次成功记账停在 2026-09-03。

## 2. 根因

`qwen3.8-flash` / `qwen3.8-max` 只挂在 New API 渠道 48 `OpenCode-Go-New-1`（`https://opencode.ai/zen/go`）。Console Go 现在要求请求带 `x-opencode-session`，否则 400 `MissingSessionID`。Elysium 和渠道 48 原先都不送这个头。

同渠道上的 `glm-5.3-flash`、`qwen3.7-plus`、`minimax-m3`、`kimi-k3` 无头时同样 400。这是 OpenCode 路由头，不是 Qwen 特有的 thinking 参数错误。`reasoning_effort=xhigh` 在补头后可 200。

这与把 MiMo 打到 OpenCode 的事故同类，但处理相反：MiMo 必须离开 OpenCode、走小米；Qwen 没有小米渠道，必须让 OpenCode 渠道合法带会话头。禁止把会话头加到小米渠道，也禁止借此把 `mimo-v2.5*` 加回渠道 48。

## 3. 已执行操作

可逆：备份 `/root/Elysia/new-api/one-api.db.bak-opencode-session-20260907T211353Z`。

- 渠道 48 `header_override` 写入稳定的 `x-opencode-session`（`ses_` + 26 位）。只作用于该渠道上游请求。
- 未改 Elysium 客户端，未给 MiMo 补会话头。MiMo ability 仍仅渠道 34。
- 未重启 Elysium。未必须重启 New API：写库后中转站立即对 Qwen 返回 200。
- 生产路由恢复：`tasks.core` / `tasks.expression` 首选 `qwen3.8-flash`（xhigh，非流式）。表达层保留 Astra/Terra 故障转移。

## 4. 验收（补头后，经 `localhost:3000`）

| 探测 | HTTP | 结果 |
|------|------|------|
| `qwen3.8-flash` 短文本 | 200 | 可见正文 1 字 |
| 同上 + thinking + `reasoning_effort=xhigh` | 200 | 可见正文 1 字 |
| 同上 + `life_send_text` 工具 | 200 | `finish=tool_calls`，1 次工具调用 |
| `glm-5.3-flash` | 200 | 不再 MissingSessionID |
| `mimo-v2.5` | 200 | ability 仍只在渠道 34 |

## 5. 仍待用户操作

Elysium 只在启动时加载 `models.toml`。要让爱莉真正走 Qwen，必须手动重启 Elysium。agent 不得代启。
