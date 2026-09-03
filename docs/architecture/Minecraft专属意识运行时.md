# Minecraft 专属意识运行时

> 状态：v1 生产候选设计。它刻意保留扩展接缝，不把当前三种技术决定形状当作爱莉思维的永久分类。真实生产结论必须经过运行手册中的现场端到端验收。

## 为什么需要独立运行时

Minecraft 是持续、高频、带身体后果的场景。核心 heartbeat 负责主体的长期生命节奏，不能因为进入游戏就被缩短为五秒循环；具身 planner 负责把一个既有意图变成可核验动作，也不应该反过来替主体决定想做什么。

因此当前链路分为三层：

```text
同一持续主体
  │ 统一主体投影 / 近期潜意识
  ▼
MinecraftConsciousnessRuntime
  │ speech + 一个 advertised 高层任务；也可等待或离场
  ▼
MinecraftTaskEngine（默认陪玩路径）
  │ 单一 BodyGate、连续执行、进度/终态事件
  ▼
Agent / Bot / Biomimetic Body
  │ 终态回执 + 动作后新观察
  └──────────────────────► 下一次场景轮次
```

三层共享同一个主体和证据账本，但拥有不同职责。专属意识不输出任意低层 bridge 命令；任务引擎不创作高层目标；身体不解释成功，只返回事实。外部工具直接给出的开放文本意图仍由 `EmbodimentRuntime + ModelPlanner` 执行，但与专属意识共用 intent lock，不能并发争抢身体。

## 启动与身份绑定

严格顺序是：加载并验证 `projection_kind=minecraft` 的主体投影，启动/连接身体并通过 playable 判定，注册 Presence，打开 scene，最后创建专属意识受管任务。任一步失败都按逆序清理已经取得的资源。

主体投影同时覆盖 `SOUL.md`、`USER.md` 和 `MEMORY.md` 的预算化派生视图。运行时固定其 source digest、projection hash、version、算法、UTF-8 字节数和预算；身份正文为空、来源缺失、profile 不匹配、哈希无效或字节不一致都会拒绝启动，不能改用临时 persona。

## 一个轮次看到什么

每次轮次只组装当前需要的材料：

- 固定主体投影，作为独立 system Text，并登记 exact delivery；
- 新的 Bridge `WorldObservation`，完整版本先进入 durable trace，模型版本按 UTF-8 上限投影并显式携带完整内容哈希、原始字节数与截断信息；
- agent/biomimetic 可取得的第一人称 JPEG，以原生 `Image` part 发送；bot 没有窗口时不伪造像素；
- 最近 N 个已经提交的潜意识因果组，单独 Text、只读、有界、精确送达；
- 最近少量动作结果的 content-free 引用，避免把旧观察和 prompt 正文反复塞回下一轮；
- session goal 和 wake reason，只作为场景/传输事实，不作为命令。

模型响应成功并不自动证明材料送达。主体、观察和非空潜意识都必须有 `EffectiveContextReceipt`，且 `exact_present`、UTF-8 字节数与 SHA-256 同时一致，否则本轮不产生决定。

这些材料不是 `life_chatter` 后缀。Minecraft 使用独立的 `life_minecraft_consciousness` 请求；主体、当前观察、近期潜意识分别是独立 `Text` part。默认硬预算是 8192/8192/4096 UTF-8 字节，近期潜意识最多 3 个完整因果组，近期结果最多 4 个 content-free 引用。World Perception 不参加默认跨意识同步，也不会把自身投影重新塞回下一轮。

观察超限时不做替主体判断意义的摘要。投影使用固定的 `minecraft-core-facts-v1` 技术顺序，让 world readiness、世界标识、自身玩家、游戏聊天和 BodyGate 状态先于大型 players/inventory/entities 集合进入 UTF-8 前缀；完整规范观察、字节数和 SHA-256 仍保留在 trace/引用中。这样即使周围实体很多，也不会先把“我是谁、同伴说了什么、当前任务是否占锁”挤出可见窗口。

## 自主节奏

模型每轮只使用一个技术控制 envelope：

- `pursue`：可发送一段原样游戏聊天，并启动一个身体明确广告的高层任务；
- `wait`：可说话，也可安静地自己选择何时再次观察；
- `end_session`：可告别并结束这个 Minecraft 场景。

这些名字只是调度协议，不是欲望、情绪、任务类型或回应优先级。运行时代码不分析关键词，也不规定她必须回复聊天。`wait` 在配置的技术上下限内；公共聊天、私聊、玩家进出、生命变化、死亡、spawn、断开、任务进度/终态、外部中断和停止可以提前唤醒。所有模型轮严格串行，长任务在身体侧异步继续，因此她不会因跟随或采集而失去说话和重新判断的机会。模型失败使用指数上限退避并暴露状态，不影响核心 heartbeat。

身体任务采用稳定 task ID 与载荷摘要，同 ID 同载荷重放已有状态，异载荷拒绝；一个 BodyGate 同时只允许一个移动/采集/合成任务。默认技术期限为 180 秒，可接受的协议范围为 5–600 秒；超时先释放 pathfinder、挖掘和控制，再产生明确 `timed_out` 终态，不能伪装完成。

## 决策与动作一致性

一个可观察决定必须先写入不可变 Life Event，之后才允许产生物理动作。事件包含 decision ID、开放文本决定、当前 instance/session/stream 归属、该轮实际生成的 provider reasoning/assistant message/transport request，以及 content-free 的 turn/subject/observation/subconscious 引用。具身 planner 的成功模型轮同样在动作前落入统一意识活动链；它的输入世界证据仍保留外部来源，不会因为进入 planner Prompt 就自动成为主体信念。

如果落账结果不确定，运行时重试完全相同的决定；service 以 decision ID 幂等复用相同事件。进入物理执行后则绝不自动重放未知动作。具身 trace 的耐久 context 保存 `consciousness_decision_id`，用于把 `intent.issued` 与高层决定连接起来。

成功解析的模型轮随决定事件保留 transport request、provider reasoning 与原始 assistant message。若响应已经生成、但 exact delivery 校验或 JSON 协议解析失败，它不会只留在日志里：session 先把该失败轮作为 `minecraft_scene_consciousness_failed_turn` 意识活动落入统一 Life Event，再进入有界退避；失败轮正文不写入 World receipt，近期潜意识即使读取它也仍受自身 UTF-8 总预算与 excerpt/ref 规则约束。

专属意识已经读取过近期潜意识，因此它发出的高层任务不会再复制同一正文。外部工具直接发出的 MC 意图是另一个 frontier，仍可读取一次有界近期潜意识。两条入口通过同一 intent lock 和 BodyGate 串行，避免 planner 与专属意识争抢身体。

游戏身体事件采用“先落账、后确认”：bot 把事件留在有界 journal，session 取 FIFO 头并写入当前 Minecraft instance 的 Life Event，成功后才发送 ACK 并唤醒 scene。控制器断开时监听器继续等待同一认证身体；未 ACK 事件原 ID/序列重放，ACK 丢失但已消费的事件只补 ACK。这样聊天不会因 WebSocket 抖动丢失，也不会因重连被回答两次。

## 停止、超时与恢复

停止顺序是：通知专属意识停止，向身体中断当前意图，等待意识任务退出，关闭身体控制，再关闭 World scene 与 Presence。用户的 agent 游戏客户端默认保持运行；bot 子进程属于 session，可以随 stop 结束。

模型主动 `end_session` 时通过新的受管任务请求 session stop，避免意识任务等待自己。`max_session_minutes` 是场景控制权的技术上限，不是对 Elysium 或用户 Minecraft 进程的自动生命周期授权。

`status` 至少暴露 phase、turn count、active decision、last intention/error、consecutive failures、last success、subject reference 和剩余 session 时间。后续如增加事件驱动聊天、危险即时唤醒、长期计划或多身体切换，应扩展 wake source 和场景状态，不应重新耦合核心 heartbeat。

## 不变量与未来扩展

v1 必须长期保留以下不变量：

1. 同一爱莉主体，不创建 MC 专属平行人格；
2. 专属场景任务与核心 heartbeat 生命周期、节奏和载荷相互独立；
3. 高层决定先落账，planner 只执行，不替主体创作目标；
4. 当前观察和像素来自身体，动作完成由终态回执与后置观察证明；
5. transient 正文不进入 World receipt，不形成 prompt 回灌；
6. 不因模型空响应、裁剪或存储不确定而伪造决定或重复物理动作；
7. Elysium 的启动/重启仍由用户手动执行；session 只拥有自己创建的 bot 子进程，不能关闭用户的 Minecraft 客户端。

允许未来替换的是调度策略、事件唤醒来源、场景长期计划表示、视觉采样策略、模型选择以及多人协作能力。替换时必须继续满足上述不变量，并重新完成真实端到端验收。
