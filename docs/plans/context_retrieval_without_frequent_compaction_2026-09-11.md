# 面向 Elysium 的低频压缩与按需上下文回查方案

日期：2026-09-11

## 结论先行

不建议把当前机制改成“永不压缩”。模型每次推理仍受上下文窗口上限约束；真正成熟的做法是：

1. 权威历史永久追加保存，绝不因为窗口管理删除；
2. 模型窗口只携带当前任务的固定指令、最近活动、精确状态和小型索引；
3. 旧内容通过带范围、来源和游标的回查工具按需读取；
4. 工具输出、文件和媒体先做结构化/分层投影，只有模型明确需要时才展开；
5. 压缩变成跨过硬阈值时的低频兜底，且必须有明确的净释放量和完成后水位验收。

这样可以消除“每次只释放一点、又很快再次维护”的循环，同时保留完整可追溯历史。它不是把旧上下文魔法般放进模型脑中，而是把“可回查”与“当前已注入”分开。

## 公开成熟方案的共同点

### OpenAI Codex / Responses

OpenAI 的 Codex 工程文章说明，Codex 当前仍会在 `auto_compact_limit` 超过时自动压缩；Responses API 的 `/responses/compact` 返回可继续使用的列表，其中包含不透明的加密 compaction item。它没有公开“完全不压缩、模型自动任意查询全部历史”的实现。文章还明确说明，Codex 为了无状态和 ZDR 场景目前不依赖 `previous_response_id`，而是管理输入并在需要时压缩：

- [Unrolling the Codex agent loop](https://openai.com/index/unrolling-the-codex-agent-loop/)（上下文窗口、prompt cache、`auto_compact_limit`）；
- [Compact a response — OpenAI API Reference](https://developers.openai.com/api/reference/java/resources/responses/methods/compact)（compaction item 与继续请求）；
- [How GPT-5.6 fuses frontier intelligence with frontier efficiency](https://openai.com/index/gpt-5-6-frontier-intelligence-efficiency/)（延迟发现、工具输出上限、追加式历史和精确前缀缓存）。

可借鉴的不是“去掉压缩”，而是：工具延迟发现、工具输出限额、历史追加式保持缓存命中、必要时再用正式 compaction。

### Claude Code

Claude Code 会清理旧工具输出，再在接近上限时总结历史；项目根规则和自动记忆会重新注入，路径限定规则在再次读取相关文件时才加载。它还提供 `/context` 查看各类内容的占用，并支持带重点的 `/compact`。

来源：[Explore the context window — Claude Code Docs](https://code.claude.com/docs/en/context-window)、[How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)。

可借鉴：固定规则放在持久文件而不是只放在对话里；工具输出优先清理；让用户能看到上下文组成。

### Cursor

Cursor 将对话总结与文件/文件夹 condensation 分开：大文件先给结构、类、方法和签名，模型需要时再展开具体文件；历史对话可从 Past Chats 按需引用，而不是每次把所有旧聊天重新塞入窗口。

来源：[Cursor Summarization](https://docs.cursor.com/en/agent/chat/summarization)、[Cursor History](https://docs.cursor.com/en/agent/chat/history)。

可借鉴：文件结构投影和精确展开优先于全文压缩；跨会话历史保留为可引用资源。

### Aider

Aider 每轮发送受 token 预算约束的 repo map，只选最相关的文件/符号；模型通过 map 判断需要哪些文件，再请求展开。repo map 用依赖图排序，而不是把仓库全文放进聊天。

来源：[Aider Repository Map](https://aider.chat/docs/repomap.html)、[Aider configuration](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/config/aider_conf.md)。

可借鉴：代码库的结构索引应当是长期可复用投影，和聊天压缩独立。

### Gemini CLI / Cline

Gemini CLI 的当前配置把几个层次拆开：历史压缩阈值、保留 token、单轮消息上限、工具输出蒸馏和最近一轮保护；还有 `PreCompress` hook 供保存状态。其实现对压缩失败采取一次截断兜底，避免不断重复调用失败的摘要模型。Cline 的 `/newtask` 把计划、已完成工作、文件和下一步提炼到新任务，`/smol` 才是同任务内压缩；也有 checkpoints 可恢复。

来源：[Gemini CLI configuration](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md)、[Gemini chat compression implementation](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/context/chatCompressionService.ts)、[Cline Auto Compact](https://github.com/cline/cline/blob/main/docs/features/auto-compact.mdx)、[Cline task commands](https://github.com/cline/cline/blob/main/docs/core-workflows/using-commands.mdx)。

可借鉴：保留最近活动、限制单轮工具结果、压缩失败不重试成死循环、任务阶段切换时开干净上下文。

## 对 Elysium 的具体设计

### A. 两层状态，而不是一个不断膨胀的 runtime snapshot

**权威层**：现有 `raw_life_events` / Life Event ledger，只追加、带 occurrence、来源、时间、因果和 payload hash，继续保存完整原文与媒体引用。

**工作层**：每个 consciousness instance 保存很小的 `working_context`：

- 固定身份、权限、当前 stream/session；
- 最近若干完整活动组；
- 未完成工具链和待处理用户事件；
- 精确 cursor / revision / source manifest；
- 结构索引（事件类型、时间、参与工具、文件路径、媒体 descriptor）；
- 最近任务的 `goal / decisions / open_questions / next_actions`，但这些必须标注为活动投影，不能冒充权威主体记忆。

运行态快照只保存上述工作层，不把整个 event history 序列化进去。这样可以避免本次 16 MiB 故障，即使聊天上下文本身只有约 200 KiB。

### B. `recall_context`：按需回查而非自动全量注入

增加一个受授权的主体工具（名称可另定）以结构化方式回查：

```text
recall_context(
  query,
  stream_id?,
  time_range?,
  event_types?,
  occurrence_refs?,
  page_cursor?,
  max_bytes?
)
```

返回内容必须包含：`source_manifest`、精确 occurrence/event refs、时间、stream、分页 cursor、投影类型（全文/节选/结构/摘要）和 UTF-8 字节数。默认返回结构和短节选；模型明确需要时再读取指定事件或附件原件。权限和跨 stream 隔离仍由工具执行，不能让模型通过 query 绕过。

不要用向量相似度直接替主体裁决事实。向量/FTS 只负责找候选；返回候选后保留原文、来源和冲突并列，让主体自行判断。

### C. 分层上下文预算

每次请求按固定顺序组装：

1. system/developer/安全边界；
2. 稳定工具 schema（未使用的插件延迟发现）；
3. 精确工作状态与最近活动；
4. 当前用户输入；
5. 仅在模型调用回查工具后追加的历史片段。

工具结果默认硬上限 10,000 tokens，长文件先发结构 map，媒体先发 descriptor；展开必须是明确的二次工具调用。所有可复用前缀追加式写入，保持 prompt cache 命中。

### D. 压缩改为滞回式低频兜底

建议以 token 和序列化字节分别计量，不能混用字符数：

- 低于 60%：不压缩，只按层次回查；
- 60%–80%：清理可重建工具输出、关闭的临时链和重复结构投影；不调用摘要模型；
- 高于 80%：标记 `pressure_pending`，继续当前安全轮，不在用户消息到达时阻塞普通发送；
- 高于 85% 或预计本轮会越界：在安全边界调度一次压缩；
- 压缩必须至少释放一个可配置的净比例（建议 25%）并使水位回到 60% 以下，否则不得记录为“完成”；
- 同一 revision 在冷却窗口内不得再次触发；新消息到达时只进入待处理队列，不能因为一次低效释放反复启动维护。

压缩生成的摘要只能是带 source manifest 的可重建投影；完整历史仍在权威层。若摘要失败，先使用确定性的结构截断/工具输出清理和 `recall_context`，不要每轮重复调用失败的摘要模型。

### E. 用户体验状态机

维护状态对用户可见但不打断普通聊天：

```text
normal → pressure_pending → background_maintenance
       → ready (净释放达标) / degraded (保留待处理并可回查)
```

只有已经无法安全形成下一次模型请求时才阻断发送；阻断必须给出可观测原因、预计剩余工作和 pending 数量。维护过程记录 `started_at / finished_at / before / after / released / retrieval_fallback / failure`，用于真正计算端到端等待，而不是拿后台轮耗时代替。

### F. 和现有主体性边界的兼容

- 基础设施可以分页、索引、限量和按 ref 回查；不能替主体挑选“重要记忆”。
- 事件摘要必须保留 actor、source、revision、原文 ref 和可继续读取路径。
- 主体自己提出的连续性 checkpoint 可以作为活动事件进入谱系，但不能由后台自动生成第一人称信念。
- 新旧 context projection 都是可重建投影，不能覆盖或删除 raw ledger。

## 分阶段实施建议

### Phase 0：只读验证

1. 为每轮记录 `context_tokens_before/after`、`working_context_bytes`、`retrieval_bytes`、`pressure_reason` 和 `maintenance_wait_ms`。
2. 把当前 runtime snapshot 的 event history 与 pending 分离统计，验证事件 ledger 已完整保留。
3. 给 `recall_context` 做只读 shadow mode，不改变模型提示，比较命中率、重复回查和延迟。

### Phase 1：减少噪声

1. 工具延迟发现与 deterministic ordering。
2. 大工具输出结构化截断；保留原始结果 ref。
3. 文件/媒体使用结构 map + 精确展开。
4. 压缩阈值改为滞回式，但暂不删除现有 checkpoint。

### Phase 2：切换工作层

1. `working_context` 不再嵌入完整 event history。
2. 主动回查工具投入正常可用链路，返回 manifest/cursor。
3. 只有硬阈值越界才进行 compaction；完成条件采用真实净释放与 post-watermark。

### Phase 3：故障与体验验收

- 连续 100 轮工具任务中不出现低净释放压缩循环；
- 旧事件能按 occurrence、时间和 stream 精确回查；
- 重启、部分失败、重复回查、游标推进和权限隔离都通过；
- 用户消息到最终投递的 P50/P95 可计算；
- 写入权丢失时不继续使用旧 claim，pending 可恢复；
- 16 MiB 运行态快照压力下不删除权威历史、不阻塞普通发送；
- 压缩摘要不可用时仍能依靠结构投影和 `recall_context` 工作。

## 不建议采用的做法

- 每次达到小阈值就调用一次 LLM 摘要；
- 只把最近 N 条消息硬截断并称为记忆；
- 用向量最高分自动决定哪些主体内容可以遗忘；
- 把完整 event history 和工作上下文继续打包进一个 SQLite JSON；
- 用更大的模型窗口或更长 timeout 掩盖 writer、工具协议和 pending 消费故障；
- 通过 `previous_response_id` 或供应商隐藏状态假定历史永远可见；它受 provider、ZDR、保留策略和请求状态约束，不能替代本地可追溯账本。

## 推荐决策

接受“低频压缩 + 按需回查”的方向，拒绝“永不压缩”的字面目标。先做 Phase 0/1 的只读与噪声治理，再切换工作层；在没有完成 ledger、cursor、权限和端到端投递验收前，不应把当前频繁压缩逻辑直接替换成不可回滚的新路径。
