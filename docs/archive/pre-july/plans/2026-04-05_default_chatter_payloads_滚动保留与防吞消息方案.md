# default_chatter payloads 滚动保留与防吞消息方案

日期：2026-04-05

## 范围声明

本方案**只讨论 payloads 管理策略**，不涉及人设、注入器文案、工具能力扩展等问题。

目标是同时满足你提出的两点：

1. 必须跨轮滚动（不能每轮重建成“短记忆”）。
2. 之前的工具结果必须保留，不应在每次新增 payload 后“吞掉最早消息”。

---

## 现状问题（核心）

当前链路的关键行为是：

1. 请求发送前会得到 `trimmed_payloads`。
2. 返回的 `LLMResponse.payloads` 直接等于 `trimmed_payloads`。
3. 下一轮继续基于这个已裁剪后的 `payloads` 追加。

这会导致一个后果：  
**一旦某轮被裁掉的最早消息，会永久丢失，不会在后续轮次中恢复。**

也就是说，当前是“滚动 + 永久侵蚀”，不是“滚动 + 可控归档”。

---

## 根因拆解

### 根因 1：发送视图与会话真相共用同一份 payloads

- 发送时为了满足窗口限制需要裁剪，这本来合理。
- 但被裁剪后的视图又被当成下一轮“唯一真相”继续滚动，导致历史不可逆丢失。

### 根因 2：裁剪触发粒度过细（append 即可能触发）

- `add_payload` 路径会调用 `maybe_trim`。
- 高频 append（USER/ASSISTANT/TOOL_RESULT）下，窗口边缘会发生“每新增一点就挤掉一点”的持续吞边。

### 根因 3：没有“被裁内容的结构化保留层”

- 被裁对话组没有进入摘要或归档块，而是直接消失。
- 对“历史工具结果要保留”的需求没有存储落点。

### 根因 4：以 payload 条数为主的硬裁剪策略过于粗糙

- 对 tool-heavy 回合，一个回合会产生多个 payload。
- 在固定 `max_payloads` 下，更容易出现“早期完整回合被快速挤出”。

---

## 新策略（仅 payloads）

## 一、双轨模型：`Canonical Ledger` + `Send Window`

引入两套上下文表示：

1. `Canonical Ledger`（会话真相，跨轮持久滚动）
   - 保存完整回合结构（包括工具结果）。
   - 不因某次发送窗口裁剪而直接丢数据。

2. `Send Window`（单次发送视图）
   - 每次发送前，从 Ledger 编译出满足预算的 payloads。
   - 仅用于本次请求，不回写覆盖 Ledger。

关键原则：  
**可以裁发送视图，不能直接裁会话真相。**

---

## 二、按“回合组”管理，不按单条 payload 生硬吞边

把对话组织成回合组（Round Group）：

1. `USER`
2. `ASSISTANT(tool_calls?)`
3. `TOOL_RESULT*`
4. `ASSISTANT(follow-up 可选)`

裁剪和归档都以“回合组”为单位，禁止拆坏 tool 链。

这样可以保证：

- 工具调用结果始终和对应回合绑定。
- 不会出现“只剩 tool_result 或只剩半个回合”的结构畸形。

---

## 三、软/硬水位 + 滞回，停止“每次 append 吞最早”

定义两级预算：

1. `soft_budget`
   - 超过后不立刻删最早回合，而是标记需要整理。
2. `hard_budget`
   - 超过后触发整理/归档，确保发送安全。

加滞回区间（hysteresis）：

- 整理后降到 `target_budget`（低于 soft）。
- 避免在边界反复“加一条吞一条”。

---

## 四、归档保留层：被挤出的回合不丢失，转为摘要 payload

当必须压缩历史时，不直接丢弃 oldest rounds，而是：

1. 生成结构化摘要（例如 `ARCHIVE_SUMMARY` 文本块）。
2. 把摘要写回 Ledger（固定位置）。
3. 原始回合可按策略保留索引或短期缓存。

效果：

- 工具结果和历史事实以摘要形式跨轮保留。
- 既节省窗口，又避免“凭空失忆”。

---

## 五、发送后回写策略修正

当前问题点在“返回 response 后用 trimmed 覆盖后续上下文”。  
新策略要求：

1. `LLMRequest.send()` 产生的 `trimmed_payloads` 仅用于 provider 调用。
2. `LLMResponse` 内部区分：
   - `source_payloads`（来自 Ledger 的真相）
   - `sent_payloads`（本次发送视图）
3. 后续 `response.send()` 默认基于 `source_payloads` 继续，不基于 `sent_payloads` 覆盖。

---

## 代码改造点（最小必要）

## 1) `src/kernel/llm/request.py`

目标：

- 让 `send()` 接受/维护 canonical payloads。
- 不再把 `trimmed_payloads` 当作下一轮唯一 payloads。

建议：

1. 在 `LLMResponse` 构造时同时传入 `source_payloads` 与 `sent_payloads`。
2. 保持现有 `payloads` 字段兼容（短期可指向 `source_payloads`），避免大面积破坏调用方。

## 2) `src/kernel/llm/response.py`

目标：

- `add_payload()` 和 `_maybe_append_response_to_context()` 只改 canonical 轨。
- `send()` 从 canonical 编译新窗口，不从上次发送窗口二次裁剪。

## 3) `src/kernel/llm/context.py`

目标：

- 增加“回合组级编译接口”（compile_send_window）。
- 加入软/硬预算 + 滞回参数。
- 在裁剪路径中支持“drop -> summary hook”。

## 4) `plugins/default_chatter/runners.py`

目标：

- 保持现有 FSM 与工具链逻辑不变。
- 仅适配新的 response/request 字段语义，确保跨轮继续滚动 canonical。

---

## 数据结构建议

新增（或等价实现）：

1. `PayloadLedger`
   - `canonical_payloads: list[LLMPayload]`
   - `archive_blocks: list[LLMPayload]`
   - `round_index: list[RoundMeta]`

2. `CompiledWindow`
   - `sent_payloads: list[LLMPayload]`
   - `dropped_round_ids: list[str]`
   - `summary_payloads: list[LLMPayload]`

---

## 兼容与迁移策略

1. 第一阶段保留旧字段（`payloads`）对外行为，内部先双轨。
2. 增加 feature flag：
   - `llm.payloads.ledger_mode = false/true`
3. 先在 `default_chatter.enhanced` 启用灰度，再扩展到其它调用链。

---

## 验收标准（只看 payloads）

1. 在固定消息流下，连续 100 次追加后：
   - 最早回合不会“直接消失”，而是进入摘要保留层。
2. 工具链完整性：
   - 任意保留回合中 `assistant.tool_calls` 与 `tool_result` 配对完整。
3. 跨轮滚动连续性：
   - 下一轮请求仍可看到上一轮工具结果语义（原文或摘要）。
4. 边界稳定性：
   - 不再出现“每新增一条就立即吞一条最早消息”的锯齿行为。

---

## 测试计划（payloads 专项）

新增/扩展测试建议：

1. `test_request_response_preserve_canonical_when_trimmed`
   - 断言发送裁剪不覆盖 canonical。
2. `test_round_group_integrity_with_tool_results`
   - 断言不会拆坏工具回合。
3. `test_hysteresis_prevents_single_step_eviction`
   - 断言边界下不会每次 append 都吞 earliest。
4. `test_archive_summary_retains_old_round_semantics`
   - 断言被挤出回合进入摘要保留层。

---

## 执行顺序

1. 先实现双轨（Canonical vs Send Window），不改业务插件语义。
2. 再加回合组编译与滞回。
3. 最后接入归档摘要。

这个顺序能先止血“吞最早消息”，再逐步增强长期记忆质量。
