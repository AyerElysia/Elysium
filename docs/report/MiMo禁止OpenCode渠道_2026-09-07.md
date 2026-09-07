# MiMo 禁止走 OpenCode 渠道（2026-09-07）

## 1. 现场

学习反思与路由请求打 `mimo-v2.5` / `mimo-v2.5-pro` 时，New API 把一部分请求送到 OpenCode Console Go（`https://opencode.ai/zen/go`），上游回 400 `MissingSessionID`（缺少 `x-opencode-session`）。操作者要求：**MiMo 必须走小米渠道，禁止走 OpenCode。**

## 2. 根因

New API SQLite `/root/Elysia/new-api/one-api.db`：

| 渠道 | 名称 | 上游 | priority | 是否登记 MiMo 聊天模型 |
|------|------|------|----------|------------------------|
| 34 | MiMoCN-2 | `xiaomimimo.com` | 100 | 是（应为唯一入口） |
| 48 | OpenCode-Go-New-1 | `opencode.ai/zen/go` | -1 | 是（限流/重试会落到这里） |

同一 `default` 组里，Xiaomi 优先，但 `RetryTimes=2` 仍会把失败请求交给 OpenCode。今日消耗日志里 OpenCode 仍服务了数十次 MiMo。Elysium `models.toml` 的 id 仍是 `mimo-v2.5*`，本身没错；错在中转站让两个渠道广告同一名字。

## 3. 已执行操作

可逆：备份 `/root/Elysia/new-api/one-api.db.bak-mimo-xiaomi-20260907T161057Z`。

- 脚本 `scripts/pin_mimo_to_xiaomi_channel.py --apply`：凡 `base_url` 指向 OpenCode/Zen Go 的渠道，从 `models` 去掉 `mimo-v2.5` / `mimo-v2.5-pro`，并删除对应 `abilities`。
- 实际改动渠道 id：`48`（`OpenCode-Go-New-1`）。未关闭该渠道（Qwen/GLM 等仍需要）。
- 未改 Elysium 模型 id。未重启 Elysium（`main.py` PID 166572 保持不变）。
- `systemctl restart new-api`：PID `250` → `198784`，`:3000` 仍由新 PID 监听，`ActiveState=active`。
- 契约测试：`test/scripts/test_pin_mimo_to_xiaomi_channel.py`（1 passed）。
- 不变量写入 `docs/architecture/LLM内核.md`、`docs/operations/deployment_and_usage.md`，以及 `config/models.toml` / `config/models.toml.example` 注释。

## 4. 验收

应用前：渠道 48 的 `models` 与 `abilities` 含 `mimo-v2.5` / `mimo-v2.5-pro`；渠道 34 已有同名 ability，priority 100。

应用并重载后：

- `opencode_with_mimo` / `opencode_mimo_abilities` 均为空。
- 渠道 48 仍启用，剩余 14 个非 MiMo 模型（含 glm / qwen / deepseek 等）。
- `mimo-v2.5` 与 `mimo-v2.5-pro` 的 `abilities` 只剩 `channel_id=34`、`priority=100`。
- 小米渠道上的 TTS 等同族模型未动。

## 5. 现场复验（2026-09-08 00:14 CST）

钉住时间：`created_at >= 1788797457`（`20260907T161057Z` / 00:10:57 CST）。未再改库、未重启 Elysium。

| 窗口 | 渠道 34 小米 | 渠道 48 OpenCode | 其他渠道 |
|------|--------------|------------------|----------|
| 钉住前 24h 的 `mimo-v2.5*` consume | 550 | **89** | 0 |
| 钉住后至 00:14 的 `mimo-v2.5*` consume | **2** | **0** | 0 |

钉住后两笔均为生产 token「爱莉希雅」、`mimo-v2.5-pro`、type=2 成功记账，渠道 id=34：

- `request_id=202609071611338246132448268d9d6A7SGpGv9`（00:11:41 CST）
- `request_id=202609071613067827267718268d9d6eS3UmJkX`（00:13:19 CST）

当前 `abilities` 仍只有 34 提供这两个聊天模型名；渠道 48 的 `models` 不含它们，但渠道本身仍启用。
