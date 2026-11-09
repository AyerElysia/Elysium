# DeepSeek Harness 重构现状审计

日期：2026-10-23。范围：只读检查 Spark 仓库与本次定向单元测试；没有改动功能、配置、主体文件或正式数据，没有启动/停止/重启服务，没有发送平台消息或调用真实模型。

## 检查对象

- 主仓库：`/home/ayerelysia/Elysia/Elysium`，HEAD `1f7e9247`。
- 子模块：`deepseek-harness`，HEAD `7ee79557ca9dd98339339e78f0e598cf1cca3443`，本次检查前后工作树干净。
- 子模块地址已改为 `AyerElysia/Elysia`；目录名仍为 `deepseek-harness`。
- 父仓库存在其他未提交修改，本次没有覆盖、暂存或提交它们。
- 本地 Windows 工作区主要为历史快照，不能作为近期实现的主证据。

## 已实现的垂直切片

10 月 21—22 日的自定义提交增加了五个源目录：`conscious-mind`、`qq-napcat`、`feishu-app`、`kook-bot`、`request-archive`。

`conscious-mind` 按配置顺序读取身份提示词文件，注册统一 `send_text` 工具和内存 pending 队列；QQ bridge 将消息送入 pending，以会话为单位批量回复。`send_text` 将外显正文与 thought/mood/decision/expected_response 参数区分，按换行分段，并保留发送结果。

QQ、飞书、KOOK 已有文字发送和协议解析适配。请求归档插件可在 `llm/stream` 上将请求写成独立 JSON；归档写入失败会阻断该框架调用。

这些实现表明文字通道原型已经形成，不代表 Elysium 的意识、记忆、学习、语音和具身链路已经完成底座迁移。

## 需要优先闭合的问题

1. **QQ 主路径尚未使用原生持久 Agent/Session。** `packages/elysia/conscious-mind/src/turn.ts:35` 每次以 system 和当前 batch 重新构造 messages，并直接 fetch chat/completions；没有带入跨轮对话历史。`qq-napcat/src/bridge.ts` 自建 Context 并装配这条调用。README 的“一条长期 session”不能视为已验收事实。终端入口另有进程内 history，也不是耐久 Session。
2. **请求归档不覆盖这条直接 fetch 路径。** `request-archive/src/index.ts:45` 监听 `llm/stream`，QQ bridge 未挂载该插件且不经 ctx.llm.stream。因此不能声称当前 QQ 模型请求已获得这份归档保障。
3. **失败后可能丢失待处理消息。** `conscious-mind/src/pending.ts:95` 先移出 batch，再执行 turn；异常只输出日志，没有重新入队或耐久确认。两次无有效正文时 turn 也直接返回，未完成消费的批次没有保留。
4. **缺失 thought 被基础设施补写。** `conscious-mind/src/turn.ts:74` 使用固定“回这句”作为缺省，与“主体亲自提交、不得补造内心记录”的目标不一致。
5. **三渠道统一入站尚未完成。** 飞书 `index.ts:59`、KOOK `index.ts:61` 只发出各自 message 事件，监听默认关闭；当前源码未发现把这些事件送入 inboundPending 的消费者。QQ bridge 的入站已接 pending。不能把三条发送适配等同于三渠道意识闭环。
6. **正式装配和生命周期仍需补齐。** 五个新目录没有独立 package.json/tsconfig；当前是源码路径集成。尚未证明经受支持的 dsh profile、Loader 和完整 build/check 的应用装配。依赖外部绝对路径/具体 .pnpm 布局，以及连接取消、重连 timer 和关闭所有权，也需要进一步核验。

pending 与拒绝日志还会直接输出聊天正文/工具原参。正式部署前应按项目隐私约束改为可追踪但不泄露私人原文的日志。

## 本次验证

在子模块运行：

```sh
env PATH=/home/ayerelysia/.local/share/node-v22/bin:/usr/local/bin:/usr/bin:/bin pnpm exec vitest run packages/elysia --maxWorkers=1 --no-file-parallelism
```

结果：9 个测试文件、36 项测试全部通过。测试确实由根 Vitest glob 发现，不存在“这些测试完全未纳入”的结论。现有用例不等同于耐久 Session、重启恢复、失败消息保留、真实平台收发或统一活动账本验收。

本次没有执行完整构建、全仓测试、真实模型测试和平台验收脚本。检查时没有观察到 8097/8087 监听或实际 packages/elysia 入口进程；这只是当时的运行态观察，不推断此前从未运行过。

## 建议下一步

先将 conscious-mind 接入 Harness 原生 Agent/Session、LLM 与工具执行路径，并把请求归档放进同一真实调用链；随后补 pending 的耐久确认/重试与取消关闭，移除 thought 补造。再接飞书/KOOK 统一入站，验证重启、失败重试不丢消息、投递未知不重发、内在字段不外泄和来源归属。记忆、心跳、语音、Minecraft 等扩面应在上述基础路径验收之后进行。

这里只记录现状与建议，没有实施上述修复。新增审计文档为可撤销的工程记录，不触及主体语义或运行数据。
