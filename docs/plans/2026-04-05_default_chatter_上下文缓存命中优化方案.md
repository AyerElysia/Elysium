# default_chatter 上下文缓存命中优化方案

日期：2026-04-05

## 目标

在不破坏当前多插件协同能力（diary/self_narrative/drive_core/life_engine）的前提下，提升上游模型 Prompt Prefix Cache 命中率，降低平均首 token 延迟与输入成本波动。

本方案聚焦：

1. 降低 system/user prompt 的随机性和高频抖动。
2. 把“可稳定前缀”和“高变化上下文”分层管理。
3. 建立可观测指标，避免“感觉上变快”但无法验证。

---

## 现状结论（基于当前仓库）

### 1) 有利因素

- `default_chatter` 在 `enhanced` 模式中会复用会话请求上下文，不是每轮完全重建。
- 上下文管理器具备结构校验与裁剪能力，基础链路完整。

### 2) 主要阻碍命中的因素

1. **概率人设注入导致 system prompt 非确定性**
   - `probabilistic_persona_injection_enabled = true`，并按概率注入多个人设字段。
2. **多注入器叠加导致 system/user 文本高频漂移**
   - `diary` 连续记忆注入、`drive_core` 注入、`self_narrative` 注入都在 `on_prompt_build` 动态追加文本。
3. **subconscious 状态持续变化**
   - `life_engine` 会周期更新并注入 `subconscious_state`，导致 system 尾部频繁变化。
4. **裁剪边界突变**
   - 触发 `max_payloads/token_budget` 后，历史分组裁剪会让上下文前后缀突然变化。

结论：当前策略下，缓存命中不稳定，尤其是跨轮与跨请求场景。

---

## 设计原则

1. **Prefix 稳定优先**
   - 先保证固定前缀尽可能“字节级稳定”，再讨论动态能力。
2. **动态内容后置**
   - 变化频繁内容集中放在末尾动态区，减少对前缀命中的破坏。
3. **随机机制可控化**
   - 生产默认“确定性优先”；随机策略改为显式开关和低频触发。
4. **先观测后优化**
   - 没有指标的优化一律视为不可靠。

---

## 目标架构（缓存友好上下文）

将 prompt 逻辑拆成两层：

1. `Stable Prefix Layer`（强稳定）
   - system 固定人格骨架
   - 工具规则与行为边界
   - 固定格式说明

2. `Dynamic Tail Layer`（可变化）
   - continuous_memory / self_narrative / drive_core
   - runtime extra
   - subconscious_state

要求：

- 动态层统一在末尾注入，且顺序固定。
- 动态层每个块有字数上限、空值跳过、标题格式统一。

---

## 分阶段实施

## Phase 0：快速稳态（配置级，无代码）

目标：当天即可降低抖动，先拿到可观测改善。

1. 关闭概率人设注入：
   - `default_chatter.plugin.probabilistic_persona_injection_enabled = false`
2. 连续记忆注入保持摘要优先：
   - `diary_plugin.continuous_memory.include_recent_entries_in_prompt = false`
3. 限制动态块长度：
   - 对 `self_narrative/drive_core/continuous_memory` 的展示条数采用更保守上限。

交付标准：

- 不改代码仅改配置后，连续 50 轮对话中 system prompt 哈希变化次数显著下降。

---

## Phase 1：可观测性建设（小改代码，低风险）

目标：能量化“命中友好度”。

新增观测项（建议写入 request_inspector）：

1. `system_prompt_hash`
2. `stable_prefix_hash`（去除动态块后）
3. `dynamic_tail_hash`
4. `payload_count`、`estimated_input_tokens`
5. 每轮“动态块来源列表+字数”

说明：

- 先做哈希与分段统计即可，不依赖上游 provider 是否返回 cached tokens。
- 如果上游支持 usage 里的 cached 字段，再追加采集。

交付标准：

- WebUI/日志可看到连续轮次的 prefix 变化趋势。

---

## Phase 2：结构化注入聚合（核心改造）

目标：把多插件“各自 append 文本”改成“统一聚合后再渲染”。

### 2.1 增加动态块聚合器（建议新增一个 event handler）

职责：

1. 收集各插件动态块（memory/narrative/drive/subconscious）
2. 按固定顺序渲染
3. 统一限长与去重
4. 一次性写入 `extra_info`（system）或 `extra`（user）

固定顺序建议：

1. `continuous_memory`
2. `self_narrative`
3. `drive_core`
4. `subconscious_state`
5. `flashback`（如果保留）

### 2.2 规范各注入器输出

要求各注入器不再直接拼接最终文案，而是输出结构化字段：

- `source`
- `priority`
- `content`
- `max_chars`

再由聚合器统一渲染。

收益：

- 解决注入顺序与文风漂移问题。
- 减少“不同插件重复改同一字段”造成的抖动。

---

## Phase 3：上下文稳定性增强（可选）

目标：继续提高中长会话命中稳定性。

1. 引入“分层裁剪”策略：
   - 固定层不裁；动态层与历史对话优先裁。
2. 大块动态内容摘要化：
   - 超预算时优先压缩 dynamic tail，而不是打断 stable prefix。
3. 每流 system prompt 快照（可选）：
   - 当核心配置不变时复用稳定层快照，只更新动态层。

---

## 风险与回滚

### 风险

1. 过度追求稳定可能导致“语义新鲜度”下降。
2. 动态块限长过严可能损失关键信息。
3. 聚合器改造若处理不当会影响现有插件注入逻辑。

### 回滚策略

1. 每阶段独立开关（feature flag）。
2. 保留旧注入路径一个版本周期。
3. 出现语义退化时先回滚 Phase 2，仅保留 Phase 0/1。

---

## 验收指标（建议）

1. **Prefix 稳定率**
   - 同一 stream 连续 N 轮中 `stable_prefix_hash` 不变比例。
2. **输入波动率**
   - `estimated_input_tokens` 的标准差下降幅度。
3. **响应延迟**
   - 首 token / 完整响应 p50、p95。
4. **质量守护**
   - 人设一致性与工具调用正确率不下降。

---

## 建议执行顺序

1. 先落地 Phase 0（配置）并观察 1 天。
2. 再做 Phase 1（可观测），确认波动源。
3. 最后做 Phase 2（结构聚合），一次性收敛注入链路。

这个顺序的好处是：每一步都可验证、可回滚、改动面可控。
