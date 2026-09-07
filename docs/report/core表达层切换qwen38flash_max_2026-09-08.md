# core / expression 切换到 qwen3.8-flash max 档（2026-09-08）

## 1. 请求

操作者要求：爱莉的 **core** 与 **expression** 都换成 **qwen3.8-flash 的 max 档**。

这不是独立模型 `qwen3.8-max`。生产注册表注释与 OpenCode 协议约定：`qwen3.8-flash` 的最高档是 `reasoning_effort=xhigh`。旧 max 变体若带 `budgetTokens=31999`，会在 `tokens=32000` 时截断正文，禁止同时设 `thinking_budget` / `budget_tokens`。

## 2. 变更

权威文件：`config/models.toml`（`/config/*` 本地配置，不入库）。

| 任务 | 变更前首选 | 变更后首选 | 思考档 |
|------|------------|------------|--------|
| `tasks.core` | `gpt-5.6-terra` | `qwen3.8-flash` | `reasoning_effort=xhigh` |
| `tasks.expression` | `gpt-6-astra`（单候选） | `qwen3.8-flash` | `reasoning_effort=xhigh` |

- 模型级保持 `stream = false`、`vision = true`（表达层识图；避免 2026-09-01 流式分块空响应）。
- core 其余候选后移作故障转移；expression 保留 `gpt-6-astra` 为第二候选，避免再次单模型 504 静默。
- 未改 learning / witness / router 等任务。
- 未重启 Elysium。`init_models_config` 只在启动时加载，**必须由用户手动重启后才生效**。

## 3. 渠道与额度

`qwen3.8-flash` 当前只在 New API 渠道 48 `OpenCode-Go-New-1` 有 ability。OpenCode Go 月度窗口在切换前已用约 87%。把心跳和表达都打到该渠道会加速月度消耗。

## 4. 文档

纠正「当前首选是 DeepSeek / MiMo-V2.5-Pro」这类过期名单：`docs/architecture/LLM内核.md`、`docs/architecture/生命引擎核心.md`、`docs/operations/deployment_and_usage.md` 改为指向 `config/models.toml`，不再复制生产名单。

## 5. 测试

`test/kernel/test_models_registry.py::test_qwen38_flash_max_tier_is_nonstream_vision_and_xhigh` 覆盖 max 档 extra、非流式与 vision。
