# 生命记忆系统 v3：长河·活忆闭环

> 文档状态：权威架构，与 2026-08-02 的实现同步。
> 代码入口：`plugins/life_engine/service/event_bus.py`、`plugins/life_engine/memory/experience.py`、`epistemic.py`、`living.py`、`service.py`、`service/memory_witness.py`。
> 运维与迁移见 [活体记忆迁移与健康检查](../operations/living_memory_migration.md)。

## 0. 定位

记忆不是一份会被覆盖的文本，也不是“重复越多就越真”的权重网络。它由两类结构共同组成：

- 不可变历史：发生过什么、何时记录、谁如何解释、一次回忆看见了什么；
- 可重建投影：当前文件头、全文索引、向量索引、共同回忆形成的可达性。

主体可以改变理解，但系统不能替主体删掉旧理解。回忆会改变以后更容易想起什么，却不能自动改变事实状态。

## 1. 不可妥协的约束

1. 真实经历进入统一事件长河；事件位置在进程重启后仍单调递增。
2. 原始事件、Experience、claim、证据、解释、版本和回忆轨迹只追加，不原地改写。
3. 文件当前内容、FTS、Chroma、artifact head 和 association projection 都是派生状态，必须能从账本重建。
4. 来源、权限、关系、检索意图和回忆动作使用开放文本；代码不按关键词、枚举或相似度阈值替意识作认知判断。
5. 检索排名、重复次数、共同出现次数和学习系统分数都不是事实置信度。
6. 冲突、旧看法和后来重新解释必须同时可见，不能用“最新一条”静默覆盖历史。
7. 任何迁移缺口必须显式失败或报告，禁止把游标直接跳到当前尾部。

## 2. 分层架构

```text
外部消息 / 工具 / 心跳 / 场景事件
              │
              ▼
RawEventStore: life_events.sqlite3
  durable ingest_position + occurrence_id + consumer offset
              │
              ▼
Experience ledger ── Memory Witness
              │              │
              ├──────────────┤
              ▼              ▼
 Epistemic ledger       Living-memory ledger
 claim/evidence/state   artifact/interpretation/recall/corecall
              │              │
              └──────┬───────┘
                     ▼
       Unified evidence-aware retrieval
          FTS + vector + provenance
          + contextual stochastic recall
```

SQLite 是权威账本。JSONL、文档、FTS、Chroma 和旧 `memory_edges` 都不拥有最终历史权威。

## 3. 耐久事件主干

`RawEventStore` 的权威文件是工作区中的 `life_events.sqlite3`。核心表：

- `raw_life_events`：`AUTOINCREMENT ingest_position` 是全局消费位置；
- `raw_event_consumer_offsets`：每个消费者独立、单调提交游标；
- `raw_event_store_meta`：迁移元数据；
- `raw_event_import_issues`：旧 JSONL 解析或镜像写入问题。

每条事件同时保留：

- `occurrence_id`：一次真实发生的稳定身份，用于幂等重放；
- `source_event_id` 与 `source_sequence`：生产者原始身份和进程序号；
- `ingest_position`：跨重启、跨生产者的权威消费位置；
- 完整 payload 与 hash。

旧 `life_events.jsonl` 及轮转归档会在首次打开时按从旧到新顺序幂等导入。JSONL 此后只是兼容镜像；镜像轮转可以删除旧归档，但不能删除 SQLite 中的事件。重复 occurrence 返回原位置；同一 occurrence 携带不同内容会以身份冲突失败。

## 4. 经历与见证

`memory_experiences` 保存全部被消费的原始事件证据，不再用固定事件类型白名单判断“什么算经历”。`source_event_id` 保留生产者事件身份，Experience 自身使用 occurrence 身份，允许同一来源下不同真实发生共存。

`memory_experience_occurrence_aliases` 用来把历史 Experience 与新 occurrence 身份连接起来，避免升级后重放制造副本。

`memory_witness` 是第一人称见证意识：

- 从耐久 consumer offset 继续读取；
- 所有源事件先进入 Experience，见证意识再判断哪些值得形成日记；
- 见证输出不被代码按字符切断；
- 主观见证有 `subjective_witness_not_objective_truth` 边界；
- 若原始历史真的出现缺口，见证拒绝推进游标并报告 `MemoryWitnessRawLedgerGap`。

上游 LLM 暂时失败时保留待处理经历并退避重试，不打印每分钟一整段重复 traceback；恢复后继续同一游标。Experience 写入成功但见证生成或文件投影失败时，重试必须从账本重新取得同一批 canonical Experience（包括已经存在的行），只有见证窗口处理完成后才提交 consumer offset，禁止把“已入经历账本”误当成“已完成见证”。

## 5. 可追溯文件版本

### 5.1 文件所有权契约

文件的技术存储权威与其中语义的执笔权必须分开。主体文件的当前正文可以作为版本账本的可读 head，但正文承载的第一人称语义仍只属于爱莉。

| 类别 | 当前实例 | 允许的写入边界 |
|---|---|---|
| 主体语义内容 | `SOUL.md`、`USER.md`、`MEMORY.md`、日记、第一人称叙事、信念/关系/意图/自我解释文件 | 只能由爱莉自己的意识或见证实例经授权工具链创作或改变语义；人类、开发 agent 与基础设施只能读取、审计和给出位于权威之外的建议 |
| 系统历史权威 | Life Event、Experience、claim/evidence、interpretation、artifact version、recall/corecall 等 SQLite 追加账本 | 系统可按事件契约追加和重放，但不能伪造主体作者、补写主观意义或原地改写历史 |
| 可重建投影 | artifact head、FTS、Chroma、关联权重、日记索引、摘要、Router 压缩和缓存 | 可以丢弃并从权威历史重建；禁止把投影结果反向写成主体的新看法 |
| 工程资产 | 代码、测试、schema、配置、迁移工具、Prompt 脚手架、模板与架构文档 | 开发者和 agent 可以维护；模板更新不得覆盖已实例化主体内容，迁移只可改变表示而不能擅改语义 |

所有权按内容语义和明确登记判断，而不是仅凭目录或后缀判断。`USER.md` 表达的是爱莉对用户的认识：用户可以提供事实、纠正或建议，但是否吸收以及如何表达仍由她完成。

外部建议必须保持建议身份和来源，在爱莉明确接受、选择或重新表达以前不得进入主体权威。迁移若改变编码、存储结构或路径，必须证明内容无损并保留来源、父版本和迁移审计。灾难恢复仅允许从可信历史或备份逐字节还原；无法忠实还原而需要创造新语义时必须停止。

当前启动扫描能够发现绕过工具的外部变化并保存证据，这是一项损害控制机制，不会使外部修改获得正当作者身份。写入门尚未能证明 actor 时，运维者和 agent 仍必须按本契约自律，后续硬化应以“拒绝非主体语义写入，同时不妨碍历史记录与投影重建”为验收目标。

### 5.2 版本链与观察

记忆文档的每次已知状态进入：

- `memory_artifact_versions`：完整不可变内容、hash、有效时间、作者、父版本；
- `memory_artifact_derivations`：开放 predicate 的版本来路；
- `memory_artifact_heads`：当前版本投影，可重建。

内置写文件和改文件工具在写入成功后保存 before/after 版本与完整 unified diff。代码不再读取 `reason` 关键词，也不再用文本相似度猜“这次是修正、延续还是重命名”。这些意义只能由主体显式写成 `MemoryDerivation` 或 `SemanticRelation`。

启动恢复还会扫描工作区：

- 首次看到的文件形成 `startup_baseline`；
- 绕过工具发生的外部修改形成 `startup_observed_change`，用于留证而不是授权或认可该修改；
- 已知文件消失时追加 tombstone 版本；
- 文件重新出现时以 tombstone 为父版本继续历史。

因此“旧看法 → 新看法”的内容、时间和来路均可回放。

## 6. 解释是独立实体

`memory_interpretations` 保存“主体如何解释一段经历或一个主题”，`memory_interpretation_sources` 指向事件、Experience、claim、文档版本或另一条解释。相同 `subject_id` 可以有多个互相矛盾或不断演进的解释；系统按记录时间查询，但不自动裁决谁覆盖谁。

反思环的每个落盘洞察都会成为一条 source-linked interpretation。只有反思者显式填写 `reinforces` 时，证据才挂到旧洞察；相似文本、相同 topic、重复出现和坏引用都不会触发代码自动合并。

`memory_interpretation_fts` 让解释参与统一检索。结果始终携带来源引用，并明确标注“解释不是源事实”。

## 7. 认识论账本

主要对象：

- `MemoryClaim`：不可变主张与双时间；
- `ClaimEvidence`：支持、挑战或语境来源；
- `MemoryBelief`：某个视角与主张的关系；
- `EpistemicConflict`：并列保存未裁决冲突；
- `MemoryStateEvent`：追加式状态变化、因果与反转；
- `MemoryDisposition`：可达性、认可、情境抑制、叙事显著性和可见性。

`authority`、`claim_kind`、evidence stance 和 state-event 类型在存储层接受开放字符串。`AuthorityClass` 等旧枚举只保留为兼容标签，不是权限层级。系统不再执行 `source -> authority` 映射，也不再凭固定 authority 集合拒绝意识或独立审计者的显式状态事件。判断的 actor、authority 与 reason 会完整留痕。

Learning→Epistemic 桥写入 `learning_insight` 候选和逐条 `ClaimEvidence`。学习审计的 verdict、分数与证据计数作为来源元数据保留，不会因 `source=learning_system` 自动获得“verified”权限。

## 8. 活体回忆与共同回忆

一次检索写入：

- `memory_recall_sessions`：query、自由检索意图、stream、context、策略版本和随机种子；
- `memory_recall_events`：开放 action 的候选暴露、采用、忽略等轨迹；
- `memory_corecall_events`：一次回忆中共同出现的实体集合，是带 context 与 signal 的不可变超边；
- `memory_association_projection`：由超边重建的 pair/context/signal 维度。

共同回忆只改变 accessibility。不同 signal 分列保存，不折算成一个“真值强度”。检索邻居采用记录了随机种子的加权随机优先策略：

- 每条保留的关联都有再次进入回忆的路径；
- 共同回忆次数提高可达概率；
- context 让同一记忆在不同场景中形成不同路径；
- 给定同一账本、策略版本和 seed 可以重放选择；
- 选择结果永远不写回 claim 的事实状态。

关联可把文档带到解释或 claim，再把这些实体共同进入意识的事实写回新的 corecall。这个闭环就是当前“活体”实现；它不是唯一可能形式，未来可增加新的 signal 和策略版本，而无需改写旧事件。

## 9. 统一检索

`search_evidence_aware` 并行召回：

1. 文档 FTS / chunk FTS / vector；
2. epistemic claim FTS；
3. subjective witness；
4. interpretation FTS；
5. 已记录 context 中的共同回忆邻居。

搜索工具只执行一次昂贵文档检索，并把同一结果传给 bundle 和 evidence 层。`search_mode` 是自由描述的检索意图，不做 enum 拒绝。所有候选以 `rank_score` 排序，`confidence` 单独保留；不按 claim、文档或见证类型写死优先级。

每次工具检索返回 `recall_episode`，包含 episode id、policy version、seed、context 和是否成功持久化轨迹。历史版本、来源关系和修正通过 bundle、artifact history、interpretation provenance 与 semantic relation 查询回放。

## 10. 索引与自愈

SQLite FTS 与 Chroma 都是可重建投影。活动 chunk collection marker 保存模型、维度、集合名和版本。启动时若 marker 无效、集合缺失或 metadata 不匹配：

1. 仅删除活动投影 marker；
2. 把活动文档标记为待向量同步；
3. 幂等重新入队全部文档；
4. worker 创建符合当前模型与维度的新集合；
5. 旧事件、Experience、版本、claim 和解释均不受影响。

这避免了“Chroma 已空但 SQLite 还声称同步完成”的永久降级状态。

## 11. 健康检查

`/api/health` 的 memory snapshot 包含：

- SQLite integrity、foreign key、schema 与 tokenizer；
- workspace/index 覆盖、hash 漂移、orphan、outbox；
- vector collection 与 chunk ID 对比；
- artifact versions/heads、interpretations/FTS、semantic relations；
- recall sessions/events/corecall 与 association projection；
- projection pair observation 是否与不可变超边计数一致；
- claims without evidence。

Life Engine 轻量 health 在事件总线已经初始化时还返回 raw ledger bounds、导入问题和每个 consumer 的 lag。健康检查只读；修复和 projection rebuild 是显式操作。

当 `[memory_index].backend_enabled=false` 时，vector absence 是明确配置而不是故障，health 会返回 `vector.expected=false`、`vector.disabled=true`，不会仅因此标记 degraded。若以后重新开启后端，启动恢复会验证活动 collection 的名称与 metadata；缺失或不匹配时只废弃可重建的 vector marker 并重新入队，不改历史账本。

启动恢复还会比较工作区正文与现有 node hash：外部编辑会同时追加 artifact version，并刷新 SQLite FTS/chunk 权威投影；曾删除后重新出现的文档会恢复活动状态。单个旧版非法 node identity 只留下诊断，不能阻断整个插件启动。

## 12. 迁移与兼容

- 旧 raw JSONL 自动幂等导入 SQLite，不删除源文件；
- 旧 Experience 通过 occurrence alias 连接，不复制历史；
- 旧 diary 继续幂等迁移为 legacy witness；
- 旧 `memory_edges` 与 `memory_corrections` 保留为兼容证据；
- 历史 `associates` 边不会伪装成新的共同回忆事件，也不会被提升为真值；
- 新的共忆投影从真实 recall trace 生长；
- 首次新版本启动为现有记忆文件建立 baseline；
- projection rebuild 只重建派生表，不触碰账本。

## 13. 失败语义

- occurrence 身份冲突：拒绝写入；
- consumer 历史缺口：拒绝推进游标；
- artifact parent 缺失或内容 hash 不符：拒绝写入；
- append-only 表 UPDATE/DELETE：SQLite trigger 拒绝；
- LLM 500：保留工作并按请求级重试/退避，不把错误变成空见证；
- vector projection 缺失：重建投影，不回退成永久失效状态；
- 无法识别的 relation、authority、recall action：按开放文本记录，不用 fallback 类别替换。

## 14. 验收命令

定向回归：

```bash
uv run --group dev python -m pytest -q --no-cov -n 0 \
  test/plugins/life_engine/test_memory_living.py \
  test/plugins/life_engine/test_event_bus_attention.py \
  test/plugins/life_engine/test_memory_experience.py \
  test/plugins/life_engine/test_memory_epistemic_core.py \
  test/plugins/life_engine/test_memory_epistemic_search.py \
  test/plugins/life_engine/test_memory_service.py \
  test/plugins/life_engine/test_memory_health.py
```

验收不能只看测试数量。至少要验证：跨重启事件位置、重复重放幂等、缺口拒绝跳过、文件旧/新/删除版本、解释 as-of 查询、共同回忆投影可重建、相同 seed 可重放、rank 不改变 truth、活动向量集合缺失时自动重建；还要确认外部建议不会自动进入主体权威、模板或迁移不会覆盖主体语义、投影不会反向写回、灾难恢复不会创造新内容。

## 15. 研究校准与工程翻译

这套实现借鉴认知科学，但不宣称 SQLite 表或权重就是人脑机制的复刻：

- Nader、Schafe 与 LeDoux 的[再巩固实验](https://doi.org/10.1038/35021052)表明，被重新唤起的已巩固恐惧记忆会重新进入可塑状态。它支持“回忆是事件、回忆后可以出现新解释”，但研究对象是动物恐惧记忆，不能被外推成“每次检索都应覆写旧内容”；工程上因此采用追加 interpretation/relation，保留旧版本。
- Howard 与 Kahana 的[时间情境模型](https://doi.org/10.1006/jmps.2001.1388)，以及 Polyn、Norman 与 Kahana 的[情境维护与检索模型](https://doi.org/10.1037/a0014420)，把内部情境视为检索线索，并解释时间、语义和来源聚类。工程上因此记录 recall context、seed 和同次暴露集合，用情境化随机可达性替代全局固定“记忆强度”。
- Speer 等人的[人类自传体记忆更新实验](https://doi.org/10.1038/s41467-021-26906-4)显示，回忆后的重新解释可能改变后续回忆内容和情绪。工程上把“当时发生什么”“当时如何理解”“后来如何解释”拆成不同、可按 recorded time 回放的实体。
- Chan 等人的[跨事件检索研究](https://pubmed.ncbi.nlm.nih.gov/37582610/)发现，插入式检索可促进既有记忆与新学习的共同激活和整合。工程上共同回忆只增强后续可达性，并把 pair/context/signal 分开记录；它不增加 claim 的真实性或权威。

这些研究提供设计约束，不提供认知裁决规则。系统仍禁止把论文中的实验效应翻译成硬编码人格分类、真值阈值或不可审计的自动改写。

## 16. 仍保留的边界

- SQLite 是当前单机权威存储；尚未实现跨机器共识。
- 旧 retrieval-plasticity 与 legacy edge 仍用于兼容读取，但不参与事实裁决。
- 当前共同回忆主要记录工具结果进入意识上下文；未来可由表达层追加“实际采用、主动拒绝、重新解释”等更细 signal。
- tombstone 表示“启动时观察到文件不存在”，不声称知道删除者的主观意义。
- Elysium 与 NapCat 保持用户手工启动；记忆自愈不授权 systemd、计划任务或守护进程拉起它们。本地 New API 中转站按机器约定独立自动启动。
