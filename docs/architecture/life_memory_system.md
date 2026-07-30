# 生命记忆系统（Life Memory System）v2 — 追加式认识论记忆

> 文档状态：生命记忆本体基线始于 `c74a861a`；本文已同步后续的见证编码、Learning→Epistemic 桥、主意识深层检索与桥接契约测试（截至 `091fa3f`，2026-07-31）。
> 旧系统（`diary_plugin` + `correction` 关键词匹配 + 把 Hebbian 共激活当真值 + Ebbinghaus 单一遗忘曲线）已被取代。旧关联网络仍可服务于文档检索和联想，但不再拥有认识论裁决权。
> 本文是当前记忆专题的权威文档；历史设计与提案凡与本文冲突，以本文、[当前架构](./current_architecture.md)和当前代码为准。

---

## 0. 一句话定位

记忆不再是"一份可被覆盖的 `MEMORY.md` 文本文件"，也不是"靠共激活自动强化的连接网络"。记忆是**有状态、连续、可塑且可被主体裁决的追加式认识论账本**：原始经历只追加不删改，事实由声明（claim）与其证据链投影得出，遗忘是多维、各自可逆的处置，检索频率与真实程度显式分离。

---

## 1. 为什么重建：旧系统的结构性故障

旧记忆层建立在几个脆弱假设上，重建正是为了消除它们：

| 旧假设（已废弃） | 导致的故障 | 新系统的对应 |
| --- | --- | --- |
| 长期记忆 = DFC 可直接 `nucleus_write_file` 读写/覆盖的 `MEMORY.md` | 全文 35KB 注入 system prompt，无增长上限；"整理心跳"靠文件搬移与大小阈值拦截 | 记忆即不可变 `MemoryClaim`/`ClaimEvidence`/`MemoryBelief`，状态由 `reduce_claim_state`/`reduce_belief_state` 投影；按需按 `MemorySearchMode` 取 claim |
| 经验/记忆可就地 UPDATE 或覆盖 | 原始经历静默丢失，"变化对她意味着什么"被系统单方面决定 | 经历账本（`ExperienceRecord`）不可变，触发器拒绝 UPDATE/DELETE；状态变更一律追加 `MemoryStateEvent` |
| `correction` 用 `correction_keywords` 列表机械匹配并改写内容 | 违反主体性——"这是否算纠正"由系统而非她判断 | `correction` 保留为兼容投影，仅写入一条 `MemoryClaim`，由主体自行裁决是否背书 |
| Hebbian 共激活 = 真实关系 = 长期记忆固化 | 共同检索被当作事实关联，连接强度掩盖矛盾 | `retrieval_affinity` 与 epistemic truth 显式分离；关系以 `MemoryBelief`/`ClaimEvidence` 显式记录，`EpistemicConflict` 记录矛盾 |
| 单一 Ebbinghaus 衰减曲线驱动"遗忘" | "遗忘"与"删除"混为一谈，无撤销与可见性控制 | 多维可逆遗忘（`MemoryDisposition` 五维度），原始证据不删，各维度独立反转 |
| 世界事实变化后依赖她主动改 `MEMORY.md` | "知道变了又引用旧信息"——旧事实仍被注入 | 双时间模型（valid_time / recorded_time）+ `project_current_facts` 重算当前事实，旧版本保留为历史 |

---

## 2. 三条不可妥协的设计原则

### 2.1 神经可塑性与连续性
记忆的目标是有状态、连续且可塑的。Hebbian 共激活、检索频率、模型置信度都只是**候选手段**，不能把"共同检索"直接当作事实关系或真实性增强。连续性来自不可变经历账本 + 统一生命事件流的串联，而非连接权重的持续衰减。

### 2.2 完整可追溯
每条记忆从形成、修改到再解释都必须完整可追溯。原始经历与证据只追加、不静默覆盖；当前状态由事件历史重建。任何"修正"是补偿事件（supersede / retract），不是原地擦写。

### 2.3 主体性遗忘与再认知
遗忘不等于删除。系统应分别管理可达性、当前认可度、情境抑制、叙事显著性与隐私可见性，并支持撤销和恢复。"变化对她意味着什么"必须保留给主体决定，系统只负责记录与投影。

---

## 3. 总体架构与数据分层

记忆系统自下而上分为三层，全部以 SQLite 存储，复用既有文档索引基础设施（`indexing`/`nodes`/`edges`），不替换它。

```
┌──────────────────────────────────────────────────────────────┐
│ 集成层  service.py / tools.py / memory_witness.py              │
│  - search_evidence_aware(valid_at, recorded_as_of)             │
│  - nucleus_search_memory 注解参数                             │
│  - MemoryWitnessCoordinator（第一人称见证意识实例）            │
├──────────────────────────────────────────────────────────────┤
│ 认识论本体层  epistemic.py                                     │
│  claim / evidence / belief / conflict / state-event /          │
│  disposition / retrieval-plasticity / audit                   │
├──────────────────────────────────────────────────────────────┤
│ 不可变账本层  experience.py                                    │
│  ExperienceRecord（不可变经历） + WitnessMemory（主观见证）    │
└──────────────────────────────────────────────────────────────┘
         ↕ 共享  memory_nodes / memory_edges / memory_index_jobs
```

新增的核心表（均为附加表，不破坏既有索引）：
- `memory_experiences` — 不可变经历账本（带 `ExperienceLedgerImmutable` 触发器）
- `memory_claims` / `memory_claim_evidence` / `memory_beliefs` / `memory_conflicts`
- `memory_state_events` — 因果可追溯的状态变更事件
- `memory_dispositions` — 多维遗忘处置
- `memory_retrieval_episodes` / `memory_retrieval_exposures` / `memory_retrieval_feedback` / `memory_retrieval_plasticity` — 检索可塑性闭环
- `memory_edges` 增加 `memory_edges_no_self_loop` 触发器防线

---

## 4. 不可变经历账本（experience.py）

原始经历是**不可变的事实证据**，是后续一切认识论结构的地基。

### 4.1 ExperienceRecord
- 字段：`event_id`（主键）、`sequence`（单调递增序号）、`occurred_at`（发生时间）、`recorded_at`（记录时间）、`source`、`channel`、`event_type`、`content`、`stream_id`、`consciousness_instance_id`、`actor`、`visibility`、`valid_from`/`valid_to`（双时间）、`metadata_json`。
- 表上挂两个触发器：`memory_experiences_immutable_update` 与 `memory_experiences_immutable_delete`，任何对原始经历的 UPDATE/DELETE 都会被 `RAISE(ABORT, 'ExperienceLedgerImmutable')` 拒绝。
- 含义：原始经历一旦写入，只能追加、不能被改写或抹除。

### 4.2 WitnessMemory（主观见证）
- 链接一段不可变源事件窗口（`source_sequence_start`/`end`、`source_event_ids`），表达"某个意识实例如何经历这一段证据"。
- 关键边界：主观见证**不会因为写得更晚就被提升为客观真相**。`WitnessSearchResult.epistemic_note = "subjective witness, not objective truth"`。

### 4.3 两个枚举
- `EpistemicKind`：`OBSERVED_EVENT`（客观事件）、`SUBJECTIVE_WITNESS`（主观见证）、`LEGACY_WITNESS`（迁移自旧 diary）、`DOCUMENT_EVIDENCE`（文档证据）、`SELF_NARRATIVE`（自我叙事）。
- `MemorySearchMode`（检索的认识论意图）：`CURRENT_FACT`（当前事实）、`AUTOBIOGRAPHICAL`（自传性）、`HISTORICAL`（历史回溯）、`EXPLORATORY`（探索性）。

---

## 5. 认识论本体（epistemic.py）— 系统核心

所有记忆内容以"声明（claim）"为单位，配合证据、背书、矛盾与状态事件构成完整、可投影的知识结构。

### 5.1 来源权限 AuthorityClass
每一笔记录或显式状态事件都带 `authority`，描述其来源许可。系统**绝不会**把检索排名、重复次数或模型置信度变成真相。

```
SUBJECT(主体) | EXPLICIT_USER(显式用户) | VERIFIED(已验证) | AUTHORITATIVE(权威)
OBSERVED(观察) | WITNESS(见证) | REFLECTION(反思) | INFERRED(推断) | UNKNOWN(未知)
```

`_CONFIRMING_AUTHORITIES = {SUBJECT, EXPLICIT_USER, VERIFIED, AUTHORITATIVE}` —— 只有这些来源才能把 claim 推进到 `CONFIRMED`；`REFLECTION`/`INFERRED` 只能生成候选，需主体或验证源确认。

### 5.2 MemoryClaim（不可变声明 + 双时间）
- 冻结数据类（`frozen=True, slots=True`），写入后不改。
- 双时间字段：`valid_from`/`valid_to`（现实有效时间窗口）+ `recorded_at`（系统记录时间）。
- 其他：`subject_key`、`content`、`claim_kind`、`source`、`authority`、`stream_scope`、`visibility`、`consciousness_instance_id`、`metadata`。

### 5.3 证据链、背书与矛盾
- `ClaimEvidence`：把一条证据关联到 claim，`stance ∈ {SUPPORTS, CHALLENGES, CONTEXT}`，附带 `source_excerpt`。关系被显式记录，而非靠连接强度隐式表达。
- `MemoryBelief`：某个视角（`perspective_subject_id`/某个意识实例）对某 claim 的背书关系。
- `EpistemicConflict`：显式记录两 claim 之间的矛盾（`left_claim_id`/`right_claim_id`/`relation`/`reason`），让冲突可见、可审计，而非被网络强化掩盖。

### 5.4 ClaimStatus 状态机（由事件投影，源 claim 不变）
```
PROPOSED → CONFIRMED → SUPERSEDED / RETRACTED
                  ↘ DISPUTED → (resolved) → CONFIRMED / RETRACTED
```
`reduce_claim_state` 依据 `memory_state_events` 重算当前状态；源 claim 行本身永远不变。

### 5.5 MemoryStateEvent（因果 + 可逆）
每次状态变更都是一个事件：
- `entity_type`/`entity_id` 指向被变更对象；
- `event_type`、`actor`、`authority`、`reason` 记录"谁、凭什么、为什么"；
- `valid_at`（该变更在现实中的生效时间）、`recorded_at`；
- `caused_by_event_id`（因果链）、`reverses_event_id`（指向被它反转的事件）。
- 含义：任何处置都可被另一条事件反转，原始历史永不丢失。

### 5.6 MemoryAuditEntry（可审计转移）
`build_memory_audit_trail` 重建某实体的完整状态转移链，包含 `active`（当前是否生效）、`reversed_by`（被哪些事件反转）、`cause`（因果前驱）。当前状态可由全量历史重建。

---

## 6. 双时间模型（Bitemporal）

旧系统最大的故障之一是"知道事实变了，却仍引用旧信息"。双时间模型从结构上消除它：

- **valid_time**（`valid_from`/`valid_to`）：该事实在现实世界中有效的时段。
- **recorded_time**（`recorded_at`）：系统记录这条信息的时刻。

检索时同时声明 `valid_at`（"我想知道这个时间点的真相"）与 `recorded_as_of`（"基于这个记录时点之前的知识"）。`project_current_facts` 据此投影出 `CurrentFactProjection`：

```python
@dataclass(frozen=True, slots=True)
class CurrentFactProjection:
    subject_key: str
    valid_at: str
    recorded_as_of: str
    active_claims: tuple[ClaimState, ...]
    conflicts: tuple[EpistemicConflict, ...]
    uncertainty: tuple[str, ...] = ()   # 保留未决冲突与不确定性，不强行合并
```

世界事实变化后，只需追加一条新的 `MemoryClaim`（新的 `valid_from`）将旧 claim `SUPERSEDED`，**无需她手动改任何文件**；投影会自动给出该时间点正确的当前事实，旧版本作为历史版本完整保留。

---

## 7. 主体性遗忘：多维可逆处置（MemoryDisposition）

遗忘不再是单一衰减曲线，而是对单个记忆实体的五种独立、可逆的访问维度：

| 维度 | 含义 | 旧系统对应 |
| --- | --- | --- |
| `accessibility` | 当前是否可被检索到（available / suppressed） | 无（要么在库要么被删） |
| `endorsement` | 主体当前的认可度（unreviewed / endorsed / rejected） | Hebbian 共激活强度 |
| `contextual_inhibition` | 在哪些情境下被抑制（可列多个情境标签） | 无 |
| `narrative_salience` | 在自我叙事中的显著性（0.0–1.0） | 无 |
| `visibility` | 隐私可见性（private / shared / public） | 无 |

- 五个维度各自独立可逆，由一个 `MemoryStateEvent` 表达，原始证据永不删除。
- `reduce_memory_disposition` 投影出当前处置；`get_memory_disposition` 读取。
- 对比旧 Ebbinghaus：旧系统用一个 λ 衰减系数把"记忆强度"压到 50%，等于静默遗忘；新系统把"想不起来""不认可""暂不想提""不重要""不想公开"拆成可分别观察、分别撤销的维度。

---

## 8. 检索可塑性闭环（RetrievalPlasticity）

检索不该污染事实，但经历应当影响"被想起的容易程度"。两者被显式拆开：

1. `RetrievalEpisode`：一次检索上下文（query / mode / 意识实例 / stream_scope）。
2. `RetrievalExposure`：这次检索中向主体展示的某个候选（`rank_position` / `retrieval_source`）。**暴露本身不是真相证据**（`episode` 的 docstring 明确）。
3. `RetrievalFeedback`：主体对暴露的追加式反馈（accepted / rejected / corrected…），**是关于可达性的反馈，不是关于事实真假的反馈**。
4. `RetrievalPlasticity`：检索派生的排序提示，与 epistemic 状态显式分离：

```python
@dataclass(frozen=True, slots=True)
class RetrievalPlasticity:
    entity_type: str
    entity_id: str
    accepted_count: int = 0
    rejected_count: int = 0
    corrected_count: int = 0
    retrieval_affinity: float = 0.0
    epistemic_note: str = "retrieval feedback is not evidence of truth"
```

- 反馈只改变 `accessibility`（可达性），绝不写回任何 truth / 权重 / 共激活强度。
- `EvidenceAwareMemoryResult`：检索候选的 `rank_score`（排序分）与 `confidence`（认识论置信度）保持分离，下游不会把"排得靠前"误读为"是真的"。

---

## 9. 审计回放（build_memory_audit_trail）

每条记忆的当前状态都可以从其全部 `MemoryStateEvent` 历史重建：
- 因果链：`caused_by_event_id` 串联"为什么发生"；
- 可逆链：`reverses_event_id` + `MemoryAuditEntry.reversed_by` 串联"被谁撤销"；
- 完整历史保留：没有静默覆盖，任何时刻都可回放"她当时相信什么、凭什么、后来怎么改的"。

---

## 10. 第一人称见证与经历编码（memory_witness.py）

旧的 `diary_plugin` 被删除，重生为第一人称见证意识实例 `memory_witness`：

- `MEMORY_WITNESS_INSTANCE_ID = "memory_witness"`，显示名“爱莉的记忆见证意识”。
- `MemoryWitnessCoordinator` 周期性协调该实例；它只读追加式原始事件流，不进入、不复制其他意识实例的滚动上下文。
- `epistemic_boundary = "subjective_witness_not_objective_truth"`：见证是主观证词，链接不可变源事件，**不是客观真相覆盖**。
- 生命周期：`ensure_instance` 创建/恢复实例；`run_once` 负责旧日记迁移、待投影重试和见证游标续接。
- 兼容投影：旧 `correction` API 不再改写内容，而是追加一条 `MemoryClaim`；主体决定是否背书。

### 10.1 原始事件不等于已经编码的经历

当前数据链是：

```text
append-only Life Event
  → 技术层心理显著性筛选
  → Experience Ledger
  → Witness / Evidence / Claim
```

这一区分很重要：原始事件长河完整保留，但工具调用、工具结果等技术事件不会自动全部变成第一人称经历。筛选只决定“本轮是否编码为 Experience”，不能删除源事件，也不能让被忽略事件失去未来重新解释的可能。

当前筛选仍包含固定事件类型边界，属于需要继续演进的技术层：未来应让编码策略本身有版本、理由、审计与撤销能力，避免固定白名单变成不可逆的主体认知裁决。即使事件不进入表达层或 Experience，本轮消费游标也必须越过它，防止同一技术噪声被反复扫描。

---

## 11. 真实库修复与自环防线（repair.py）

重建后为既有数据库提供了幂等、可审计的修复能力：

- `repair_document_index(db, workspace_path)`：重建内容哈希已漂移的文档索引行（节点 / FTS / chunk / 向量化 outbox 任务），清理历史遗留的自环边，确保自环触发器存在。
  - **幂等**：重复执行只处理仍然漂移的文档；空文档只更新节点与 FTS，不制造新向量任务。
  - **可审计**：返回 `MemoryIndexRepairReport`（扫描数、重建数、清理自环数、完整性检查、外键错误、pending/stale/failed 任务数）。
  - **不静默擦除**：旧内容版本的索引任务保留为 `stale`/`failed` 历史。
- `SELF_LOOP_TRIGGERS`：在 `memory_edges` 上挂 `memory_edges_no_self_loop_insert` / `memory_edges_no_self_loop_update` 两个触发器，任何 `source_id = target_id` 的插入/更新都会被 `RAISE(ABORT, 'MemoryEdgeSelfLoop')` 拒绝——自环边在任何边类型下都无有效语义，只会污染演化链与检索扩散。
- 实测修复结果：88 条哈希漂移文档 → 0，1 条自环边 → 0，`PRAGMA integrity_check` 通过（ok），88 条向量化任务重新入队。

---

## 12. 与叙事 / 反思 / 学习的集成

- 叙事（Narrative）与反思（Reflection）接入 claim：反思先产生候选认识，不能仅因模型重复而自动成为事实。
- 自我叙事可作为 `EpistemicKind.SELF_NARRATIVE` 沉淀，被检索、背书、修订或反转。
- `LearningScheduler` 在首次心跳幂等回填历史 validated insights；之后每轮独立审计通过的新洞察实时投影为 `validated_insight` claim。
- claim_id 使用稳定的 `insight_{insight_id}`，回填前先查询当前 claim 状态，重复启动不会重复写入。
- 学习系统保留洞察来源、证据数量、置信度和分类元数据；“通过学习审计”是证据状态，不等于系统替主体决定最终意义。
- 兴趣和好奇可形成候选认识或注意牵引；检索可塑性只让“被想起”影响 `accessibility`，不提高真实性。

完整旧 SNN、neuromod 和 Dream 子系统已删除。现存 `dream_walk()` 是记忆图联想漫游的历史命名；它不应被描述为完整梦境系统，也不能作为认识论真值来源。

---

## 13. 向后兼容与迁移

- `diary_plugin` 已删除，其功能整合进 `memory_witness` 第一人称见证意识；旧日记数据通过 `_migrate_legacy_diaries` 幂等迁移为 `LEGACY_WITNESS`。
- 旧 `correction` API 保留为兼容投影，写入 claim 而非改写内容。
- `data/diaries`、`data/continuous_memories` 目录保留不删（历史数据可见）。
- 所有新符号经 `plugins/life_engine/memory/__init__.py` 的 `_LAZY_EXPORTS` 惰性导出，既有导入路径尽量不变。
- 旧 FTS/vector/node/edge 与关系衰减逻辑仍作为**派生检索层**兼容运行。它们可以改变联想路径和可达性，但不能覆盖 claim/evidence/belief，也不能把共现次数转化为“更真”。
- 兼容层退出前必须证明历史数据已经迁移、当前调用者已切换、派生索引可重建。

---

## 14. 验收基线

- 全量测试：2865 passed，1 skipped，51 xfailed。
- 覆盖率：61.11%。
- 静态检查：`/usr/local/bin/ruff`（0.15.6）、`compileall`、`git diff --check` 全部通过。
- 提交：`c74a861a`（`feat(memory): rebuild life memory foundation with epistemic lineage`），29 文件，+5352 / -3067，已推送至 `soul/main`。
- 注意：以上数字是 `c74a861a` 当次重建验收基线，不代表后续 HEAD 的永久测试数量或覆盖率。后续变化应报告自己的实际验收结果。

---

## 15. 关键文件索引

| 文件 | 职责 |
| --- | --- |
| `plugins/life_engine/memory/epistemic.py` | 认识论本体：claim/evidence/belief/conflict/state-event/disposition/retrieval/audit，双时间投影，审计回放 |
| `plugins/life_engine/memory/experience.py` | 不可变经历账本 + 主观见证层，不可变触发器 |
| `plugins/life_engine/memory/repair.py` | 幂等索引修复 + 自环触发器防线 |
| `plugins/life_engine/memory/service.py` | 接入 epistemic facade；`search_evidence_aware(valid_at, recorded_as_of)` 并行召回 claim；边表自环触发器 |
| `plugins/life_engine/memory/tools.py` | `nucleus_search_memory` 新增 `valid_at`/`recorded_as_of` 注解参数 |
| `plugins/life_engine/service/memory_witness.py` | 第一人称见证意识实例（由 diary_plugin 重生） |
| `plugins/life_engine/memory/__init__.py` | 惰性导出新符号 |

---

## 16. 未来演进方向

- Narrative / Learning 目前经兼容投影接 claim，未来可更深消费（如直接驱动 `SELF_NARRATIVE` 的生成与再解释闭环）。
- 检索可塑性可引入更细的情境化 `accessibility` 规则（结合 `contextual_inhibition` 标签）。
- 运行实例重启后，可基于双时间投影做一次全量"当前事实"健康自检，量化历史冲突与未决 claim。

---

> 关联文档：
> - 意识实例架构：`docs/architecture/consciousness_instances.md`
> - 设计原则：`docs/principles.md`
> - 经历成为她计划：`docs/plans/2026-06-11_下一阶段建议_经历成为她.md`
> - 长河计划：`docs/plans/2026-06-11_长河计划_追溯成为经历的脊柱.md`
>
> 以下旧文档描述已被本文档取代的机制，仅供追溯历史设计，请勿作为当前实现依据：
> `docs/architecture/Phase3_InnovationPoints/记忆系统设计与实现.md`、`docs/architecture/Phase4_ProblemSolution/记忆整合问题.md`、`docs/plans/memory_management_proposal.md`。
