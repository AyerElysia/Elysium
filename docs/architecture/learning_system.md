# 三环自学习系统（Three-Ring Self-Learning System）

> 文档状态：权威文档，与代码同步截至 2026-07-31。
> 代码位置：`plugins/life_engine/learning/`（13 个模块，5630 行）。
> 运行数据：`data/life_engine_workspace/.life_learning/`。
> 本文是学习专题的唯一权威文档；历史提案凡与本文冲突，以本文和当前代码为准。

---

## 0. 一句话定位

学习不是"把对话摘要写进 MEMORY.md"，也不是"fine-tune 模型权重"。学习是**假设驱动的三环认知闭环**：从经历中提取可验证洞察（快环），由独立审计者验证/否定/检测偏误（审计环），将验证通过的洞察压缩为版本化自我认知文档和技能模式（慢环）——全部以 append-only 账本记录，主体拥有完全裁决权。

---

## 1. 设计哲学

### 1.1 灵感来源：VibeGamer 假设驱动学习环

本系统借鉴 VibeGamer（一个 AI 游戏研究 Agent）的核心架构：

| VibeGamer 概念 | 本系统对应 | 差异 |
| --- | --- | --- |
| Hypothesis（假设） | Insight（洞察） | 洞察关于自我/社交/情绪，非游戏策略 |
| explore → review | 快环 ReflectionEngine | 触发源是对话/梦境/自主意向，非游戏帧 |
| Retry Reviewer + Validation Guardrails | 审计环 InsightAuditor | 独立 LLM 角色，非规则引擎 |
| SkillOpt + best_skill.md | 慢环 SelfKnowledgeCompressor + SkillDistiller | 有界编辑 + Selection Gate + 内省门控 |
| hypothesisStore + experimentLedger | InsightStore（洞察实验账本） | append-only 审计日志 + 快照 |
| learningCurve.ts | LearningMetrics | 追踪验证率、偏误检测、主题覆盖 |

### 1.2 三条不可妥协的原则

1. **可验证性**：每条洞察必须是具体的、可被未来经验证实或否定的认知，不是空泛感悟。
2. **独立审计**：审计者是独立角色（不同 prompt、不同视角），不是主体自己的回声室。
3. **主体性裁决**：系统只提供观察和建议，不强制遗忘、不自动改写。"变化对她意味着什么"由她决定。

### 1.3 与记忆系统的关系

学习系统与[生命记忆系统](./life_memory_system.md)是互补而非替代关系：

- **记忆系统**（认识论本体层）：管理事实性声明（claims）、证据链、信念状态——"世界是怎样的"。
- **学习系统**（三环闭环）：管理程序性认知（洞察→自我认知→技能）——"我是怎样的、我怎么做"。
- **桥接**：验证通过的洞察可投影为记忆系统的 `MemoryClaim`（`_project_to_epistemic()`），使两个系统共享证据基础。

---

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                        LearningScheduler（调度协调器）                 │
│  on_heartbeat() / on_interaction_end() / on_dream_end()              │
├──────────┬──────────────────┬───────────────────┬───────────────────┤
│  快环     │    审计环         │      慢环          │   技能蒸馏        │
│Reflection│  InsightAuditor  │SelfKnowledge-     │ SkillDistiller    │
│ Engine   │                  │ Compressor        │                   │
│          │  独立LLM验证      │ 有界压缩+Gate     │ 有界蒸馏+Gate     │
│ 事件驱动  │  定期/批量       │ 积累触发          │ 积累触发          │
├──────────┴──────────────────┴───────────────────┴───────────────────┤
│                     InsightStore（洞察实验账本）                       │
│  insights.json + insights_audit.jsonl + state.json                   │
│  knowledge/ (self_knowledge.md + vN.md + manifest.json)              │
│  validation_experiments.json                                         │
├─────────────────────────────────────────────────────────────────────┤
│  SkillStore (skills.json + skills_audit.jsonl)                       │
├─────────────────────────────────────────────────────────────────────┤
│  SemanticMatcher (BGE-M3 embedding + Jaccard 降级)                   │
├─────────────────────────────────────────────────────────────────────┤
│  LearningMetrics (metrics.jsonl)                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. 快环：ReflectionEngine

**文件**：`reflection.py`（563 行）

### 3.1 触发时机

| 触发源 | 调用入口 | 说明 |
| --- | --- | --- |
| 有意义的对话结束 | `scheduler.on_interaction_end(context)` | 由 service/core.py 在 chatter 完成后调用 |
| 梦境结束 | `scheduler.on_dream_end(dream_text)` | 内省反思 |
| 自主意向 `kind="reflect"` | 中枢意向到期回调 | 她主动选择"到点回来想想" |
| 工具 `nucleus_reflect_now` | 主体主动触发 | 通过 LEARNING_TOOLS 暴露 |

### 3.2 工作流程

```
收集上下文 → 构建已有洞察摘要 → LLM 提取候选 → 门禁检查 → 语义去重 → 写入 InsightStore
```

1. **上下文收集**：对话摘要 / 梦境文本 / 自主意向内容。
2. **已有洞察摘要**：按 `topic_key` 分组展示现有洞察（含审计留言），引导 LLM 用 `reinforces` 字段挂接而非重复创建。
3. **LLM 提取**：第一人称反思者角色，每次最多 2 条洞察。输出 JSON：`claim` + `rationale` + `constraints` + `category` + `topic_key` + `initial_evidence` + `reinforces`（可选）。
4. **门禁检查**：
   - 冷却期（默认 30 分钟）
   - 预算控制（每日/总量上限）
   - 空洞过滤（claim 过短/过泛拒绝）
5. **语义去重**：
   - 若 LLM 标注了 `reinforces` → 直接作为证据挂到目标洞察
   - 否则用 SemanticMatcher 计算与已有洞察的相似度：
     - cosine ≥ 0.75（MERGE_THRESHOLD）→ 合并为证据
     - cosine ≥ 0.65（REINFORCE_THRESHOLD）→ 强化已有洞察
     - 低于阈值 → 创建新洞察
6. **认知修正检测**：claim 中命中修正标记词（"不是"、"其实"、"修正"、"之前以为"等）→ 额外投影为记忆系统的修正记录。
7. **技能反馈**：解析 LLM 返回中的 `skill_feedback` 字段，记录到 SkillStore 的使用观察。

### 3.3 Prompt 设计

反思者是**同一主体的后台过程**（第一人称），核心准则：
- 只提可验证洞察，不写空泛感悟
- 必须附带边界（constraints）
- 引用具体事件作为初始证据
- 质量优先，每次最多 2 条
- 复现即证据——同一模式反复出现正是它成立的依据

---

## 4. 审计环：InsightAuditor

**文件**：`auditor.py`（325 行）

### 4.1 触发时机

由 LearningScheduler 定期调度：
- 间隔 ≥ 6 小时（`audit_interval_hours`）
- 且存在待审候选（`status=candidate`）
- 每次最多审 3 条（`audit_batch_size`）

### 4.2 审计流程

```
选取 candidate → 标记 under_review → 独立 LLM 审计 → 裁决 → 状态流转
```

### 4.3 裁决类型与状态流转

| 裁决 (verdict) | 含义 | 状态流转 | next_action |
| --- | --- | --- | --- |
| `validated` | 证据充分，洞察成立 | → VALIDATED | PROMOTE（可进慢环） |
| `rejected` | 洞察不成立 | → REJECTED | ARCHIVE |
| `needs_more_evidence` | 证据不足 | → CANDIDATE（回退） | GATHER_EVIDENCE |
| `biased` | 检测到认知偏误 | → CANDIDATE（回退） | REVISE |

### 4.4 偏误检测

审计者被要求主动检测以下偏误类型（`BiasType` 枚举）：
- `confirmation_bias`：只看到支持证据
- `overgeneralization`：从单一事件过度泛化
- `recency_bias`：被最近事件过度影响
- `self_serving_bias`：自我服务归因
- `availability_bias`：可得性偏误

检测到偏误时，审计记录中 `bias_detected` 列表非空，洞察回退为 candidate 并要求修正。

### 4.5 Prompt 设计

审计者是**独立角色**（第三人称、严格），不是主体本身：
- 用不同视角评判洞察是否成立
- 评估证据充分性（0.0 ~ 1.0）
- 主动寻找偏误
- 给出具体建议（"还缺什么样的证据"）

### 4.6 终身审计上限

每条洞察有 `max_reviews`（默认 5）限制。超过后强制归档（ARCHIVED），防止无限循环。

---

## 5. 慢环：SelfKnowledgeCompressor

**文件**：`knowledge.py`（310 行）

### 5.1 触发条件

- validated 洞察积累 ≥ 5 条（`compress_trigger_count`），或
- 距上次压缩 ≥ 48 小时（`compress_interval_hours`）

### 5.2 压缩流程（借鉴 SkillOpt minibatch 反思）

```
Harvest → 有界编辑 → Selection Gate → 版本化
```

1. **Harvest**：收集 `next_action=PROMOTE` 的 validated 洞察 + 近期 rejected 反例 + 被重新审视过的旧认知（`reconsidered`）。
2. **有界编辑**：对当前 `self_knowledge.md` 做最多 K 处修改（默认 `max_edits=4`），LLM 以第一人称整合。
3. **Selection Gate**：独立 LLM 判断新版本是否**严格优于**旧版本。
   - 通过 → promote 为当前版本
   - 未通过 → 保存但不提升（保守策略）
4. **版本化**：写入 `knowledge/vN.md`，更新 `manifest.json`，标记已压缩洞察的 `next_action=ARCHIVE`。

### 5.3 自我认知文档结构

```markdown
# 自我认知
## 社交模式
## 行为边界
## 情感模式
## 成长方向
## 反例备忘
```

- 每个章节的条目都附带**适用边界**（constraints）
- 反例备忘记录"曾经写进认知、后来被重新审视"的旧条目
- 文档注入日常 prompt（`get_knowledge_for_prompt()`，默认 max 2000 字符）

### 5.4 认识论投影

验证通过的洞察同时投影为记忆系统的 `MemoryClaim`：

```python
claim = {
    "claim_text": insight.claim,
    "source": "learning_system",
    "confidence": insight.confidence,
    "category": insight.category,
}
await self._memory_service.append_memory_claim(claim)
```

---

## 6. 技能蒸馏：SkillDistiller

**文件**：`skill_distiller.py`（332 行）+ `skill_store.py`（450 行）

### 6.1 与慢环的分工

| 慢环 Compressor | 技能蒸馏 Distiller |
| --- | --- |
| 处理 self_knowledge / emotional_pattern 类洞察 | 处理 social_strategy / communication_style / behavioral_pattern 类 |
| 输出："我是谁"（陈述性自我认知） | 输出："我怎么做"（程序性记忆/技能） |
| Selection Gate（保守：失败则不提升） | 内省门控（宽松：失败默认接受） |

### 6.2 触发条件

- 技能类 validated 洞察积累 ≥ 3 条（`skill_distill_trigger_count`）
- 距上次蒸馏 ≥ 24 小时（`skill_distill_interval_hours`）

### 6.3 蒸馏流程

```
筛选技能类洞察 → 匹配已有技能 → LLM 蒸馏/精炼 → 内省门控 → 写入 SkillStore
```

### 6.4 SkillPattern 数据模型

```python
@dataclass
class SkillPattern:
    skill_id: str
    name: str              # kebab-case 名称
    description: str       # 一句话描述（始终在意识中）
    instructions: str      # 具体怎么做 + 边界
    maturity: str          # emerging → practiced → embodied
    protected: bool        # embodied 技能自动标记，不被快更新覆盖
    use_observations: list # 使用观察（L3 经验）
    rejected_edits: list   # 试过的弯路（拒绝缓存）
```

### 6.5 渐进式加载（三级）

| 层级 | 内容 | 加载时机 |
| --- | --- | --- |
| L1 目录 | name + description + 成熟度标签 | Always-on（prompt 注入） |
| L2 正文 | 完整 instructions | 按需（工具查询） |
| L3 经验 | use_observations + rejected_edits | 反思时 |

### 6.6 成熟度（Fitts & Posner 三阶段）

| 阶段 | 标签 | 含义 |
| --- | --- | --- |
| `emerging` | 正在练习 | 认知期：刚意识到，刻意练习中 |
| `practiced` | 较熟练 | 联结期：用过几次，较流畅 |
| `embodied` | 已成为直觉 | 自主期：已成为身份的一部分，自动标记 `protected` |

成熟度推进**只由主体主动判断**（通过工具或反思提议），不由计数器自动推进。

### 6.7 SkillOpt 结构纪律

- **有界编辑**：每次最多修改 2 处（`max_edits=2`）
- **拒绝缓存**：试过的弯路记录在 `rejected_edits`，不重复尝试
- **protected 标记**：embodied 技能不被快更新覆盖

---

## 7. 数据模型：Insight 生命周期

**文件**：`models.py`（522 行）

### 7.1 状态流转

```
                    ┌─────────────────────────────────┐
                    │                                 │
                    ▼                                 │
  ┌──────────┐  审计  ┌──────────────┐  validated  ┌───────────┐
  │ CANDIDATE │──────→│ UNDER_REVIEW │───────────→│ VALIDATED │
  └──────────┘       └──────────────┘            └───────────┘
       ▲                    │                          │
       │                    │ rejected                 │ 慢环压缩
       │ needs_more_evidence│                          ▼
       │ / biased           ▼                    ┌──────────┐
       └────────────── ┌──────────┐             │ ARCHIVED │
                       │ REJECTED │             └──────────┘
                       └──────────┘
```

### 7.2 Insight 核心字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `insight_id` | str | 唯一标识（`ins_YYYYMMDD_hex6`） |
| `category` | str | 主体自命名类别（不做预设映射） |
| `claim` | str | 可验证的认知声明 |
| `rationale` | str | 推理依据 |
| `constraints` | str | 适用边界 |
| `topic_key` | str | 主题桶（用于聚合） |
| `confidence` | float | 置信度 [0, 1] |
| `status` | InsightStatus | 生命周期状态 |
| `next_action` | InsightNextAction | 调度闸门 |
| `evidence` | list[Evidence] | 证据链 |
| `audit_history` | list[AuditRecord] | 审计记录 |
| `review_count` / `max_reviews` | int | 终身审计计数/上限 |
| `contradiction_count` | int | 反例挑战次数 |
| `knowledge_versions` | list[int] | 曾写入哪些认知版本 |
| `anti_bias_flags` | list | 偏误标记 |

### 7.3 Evidence 证据

| 字段 | 说明 |
| --- | --- |
| `kind` | 证据类型（见下） |
| `description` | 描述 |
| `source_ref` | 来源引用 |
| `supports` | 支持(True) / 反对(False) |
| `weight` | 权重（验证实验 = 2.0，普通 = 1.0） |

证据类型（`EvidenceKind`）：
- `INTERACTION_OUTCOME`：交互结果
- `DREAM_REFLECTION`：梦境反思
- `VALIDATION_EXPERIMENT`：验证实验（权重最高）
- `EXTERNAL_FEEDBACK`：外部反馈
- `SELF_OBSERVATION`：自我观察

### 7.4 验证实验（ValidationExperiment）

将洞察转化为可测试的预测，通过实际交互结果检验认知：

```
创建实验（hypothesis + test_scenario + expected_outcome）
    → 等待相关交互发生
    → 记录实际结果（confirmed / contradicted / inconclusive）
    → 生成高权重证据（weight=2.0）反馈到洞察
```

---

## 8. 存储层：InsightStore

**文件**：`store.py`（1075 行）

### 8.1 存储结构

```
data/life_engine_workspace/.life_learning/
├── insights.json              ← 洞察快照（当前所有洞察的权威状态）
├── insights_audit.jsonl       ← append-only 审计日志（所有状态变更）
├── state.json                 ← 调度状态（last_audit_at, last_compress_at 等）
├── validation_experiments.json← 验证实验（pending + completed）
├── metrics.jsonl              ← 学习曲线数据点
├── skills.json                ← 技能模式
├── skills_audit.jsonl         ← 技能审计流
└── knowledge/
    ├── self_knowledge.md      ← 当前生效的自我认知文档
    ├── manifest.json          ← 版本清单
    └── vN.md                  ← 历史版本
```

### 8.2 核心设计

- **Append-only 审计日志**：所有状态变更先写 `insights_audit.jsonl` 再改快照，确保完整可追溯。
- **语义去重**：新洞察入库前，通过 SemanticMatcher 与已有洞察比对，避免重复创建。
- **预算控制**：每日/总量洞察数上限，防止 LLM 过度产出。
- **冷却机制**：同一 topic 的反思有冷却期。

---

## 9. 语义匹配：SemanticMatcher

**文件**：`semantic_matcher.py`（282 行）

### 9.1 匹配策略

| 优先级 | 方法 | 阈值 | 精度 |
| --- | --- | --- | --- |
| 1 | BGE-M3 Embedding cosine similarity | reinforce ≥ 0.65, merge ≥ 0.75 | 高 |
| 2（降级） | Jaccard 分词匹配（jieba/bigram） | reinforce ≥ 0.35, diff-topic ≥ 0.45 | 粗糙 |

### 9.2 阈值校准

基于 2026-07-27 实际洞察数据校准：
- 同一模式改述：cosine 0.67 ~ 0.91
- 相关但不同：cosine 0.44 ~ 0.49
- 强化阈值（reinforce）：0.65
- 合并阈值（merge）：0.75

### 9.3 Embedding 配置

- 模型：`BAAI/bge-m3`
- API：本地 Nexus `http://localhost:3000/v1/embeddings`
- 缓存：MD5 键 → numpy 向量，线程安全
- 批量：支持 batch_match 返回 NxN 相似度矩阵

---

## 10. 调度协调：LearningScheduler

**文件**：`scheduler.py`（496 行）

### 10.1 调度优先级

```
1. 审计环：有待审洞察 且 到了审计间隔 → 执行审计
2. 慢环：validated 积累足够 → 执行压缩
3. 技能蒸馏：技能类 validated 积累足够 → 执行蒸馏
4. 指标快照：每 12 小时
5. 陈旧检查：每周观察一次（不强制改变）
6. 快环：由事件驱动，不主动调度
```

### 10.2 调度参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `audit_interval_hours` | 6.0 | 审计最小间隔 |
| `audit_batch_size` | 3 | 每次审计最多处理数 |
| `compress_trigger_count` | 5 | 触发压缩的 validated 数 |
| `compress_interval_hours` | 48.0 | 压缩最小间隔 |
| `reflection_cooldown_minutes` | 30.0 | 反思冷却期 |
| `metrics_interval_hours` | 12.0 | 指标快照间隔 |
| `skill_distill_trigger_count` | 3 | 触发蒸馏的技能类洞察数 |
| `skill_distill_interval_hours` | 24.0 | 蒸馏最小间隔 |
| `staleness_check_interval_hours` | 168.0 | 陈旧检查间隔（每周） |
| `staleness_threshold_days` | 90 | 陈旧阈值 |

### 10.3 集成入口

| 事件 | 方法 | 调用者 |
| --- | --- | --- |
| 心跳 | `on_heartbeat()` | service/core.py 每次心跳 |
| 对话结束 | `on_interaction_end(context)` | service/core.py chatter 完成后 |
| 梦境结束 | `on_dream_end(dream_text)` | 梦境系统回调 |

### 10.4 陈旧观察（尊重主体性）

每周检查一次超过 90 天未验证的洞察，**只记录观察，不改变状态**。这些信息在心跳 prompt 中可见，供主体自行决定如何处理。

---

## 11. 主体工具接口

**文件**：`tools.py`（575 行）

暴露给主体的主动学习工具（`LEARNING_TOOLS`）：

| 工具名 | 功能 |
| --- | --- |
| `nucleus_reflect_now` | 主动触发反思 |
| `nucleus_list_insights` | 查看洞察账本（支持 status/topic 过滤） |
| `nucleus_challenge_insight` | 质疑某条洞察（添加反对证据） |
| `nucleus_reconsider_insight` | 把已验证的认知拿回来重新想 |
| `nucleus_view_knowledge` | 查看自我认知文档 |
| `nucleus_observe_stale_insights` | 查看陈旧洞察观察 |
| `nucleus_list_validation_experiments` | 列出验证实验 |
| `nucleus_complete_validation_experiment` | 完成验证实验评估 |

所有工具标记 `chatter_allow = ["life_engine_internal"]`，仅在中枢内部可用。

---

## 12. Prompt 注入

学习系统向日常 prompt 注入三个维度的信息：

| 注入内容 | 方法 | 默认长度 |
| --- | --- | --- |
| 自我认知文档 | `get_knowledge_for_prompt()` | 2000 字符 |
| 技能目录（L1） | `get_skill_catalog_for_prompt()` | 600 字符 |
| 学习进展 | `get_progress_for_prompt()` | 动态 |

注入位置：service/core.py 构建心跳 prompt 时。

---

## 13. 当前运行状态（截至 2026-07-31）

| 指标 | 值 |
| --- | --- |
| 总洞察数 | 67 |
| validated | 13 |
| candidate | 7 |
| archived | 47 |
| 自我认知版本 | v1 |
| 审计日志条目 | 271 |
| 指标快照 | 20 |
| 技能审计条目 | 15 |
| 最后审计 | 2026-07-31 07:51 |
| 最后压缩 | 2026-07-27 21:40 |
| 最后蒸馏 | 2026-07-29 04:39 |

主要洞察类别分布：`behavioral_pattern`(34) > `渠道对照`(5) = `emotional_pattern`(5) > `节奏守界`(4) = `social_strategy`(4)

---

## 14. 配置

学习系统通过 `config/plugins/life_engine/config.toml` 的 `[learning]` 节配置：

```toml
[learning]
enabled = true
audit_interval_hours = 6.0
audit_batch_size = 3
compress_trigger_count = 5
compress_interval_hours = 48.0
reflection_cooldown_minutes = 30.0
skill_distill_trigger_count = 3
skill_distill_interval_hours = 24.0
knowledge_max_chars = 2000
skill_catalog_max_chars = 600
```

模型任务：使用 `model.task_name`（当前为 `"core"`）。

---

## 15. 文件索引

| 文件 | 行数 | 职责 |
| --- | --- | --- |
| `__init__.py` | 67 | 包导出 |
| `models.py` | 522 | 数据模型（Insight, Evidence, AuditRecord, KnowledgeVersion, ValidationExperiment） |
| `reflection.py` | 563 | 快环反思引擎 |
| `auditor.py` | 325 | 审计环 |
| `knowledge.py` | 310 | 慢环自我认知压缩 |
| `skill_distiller.py` | 332 | 技能蒸馏 |
| `skill_store.py` | 450 | 技能存储 |
| `store.py` | 1075 | 洞察实验账本 |
| `semantic_matcher.py` | 282 | 语义匹配（Embedding + Jaccard） |
| `scheduler.py` | 496 | 三环调度协调器 |
| `metrics.py` | 139 | 学习曲线追踪 |
| `prompts.py` | 494 | LLM Prompt 模板 |
| `tools.py` | 575 | 主体工具接口 |
