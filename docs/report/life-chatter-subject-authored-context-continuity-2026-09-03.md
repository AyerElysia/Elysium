# Life Chatter 主体自述式上下文连续性实施报告（2026-09-03）

## 结论

旧版把任意消息和工具结果片段拼进 `<compressed_life_chatter_context>` 的自动压缩链已退出生产语义。新实现把容量治理拆为三件互不冒充的事：系统报告 content-free 压力、active consciousness 亲自书写连续性检查点、工程硬边界只保留可续读精确引用。

## 本次改动

- 新增 `core/context_stewardship.py`：task-aware token 压力、稳定 group manifest、主体检查点校验、安全边界安装、content-addressed 精确归档、UTF-8 分页读取和机械 omission。
- `LifeChatter` 在发送前给出瞬态容量通知；工具轮后只安装主体显式提交的检查点；全局 reset 清理未决命令。
- `author_self_continuity_checkpoint` 与 `read_context_group` 进入 chat tool manifest；前者不增加模型轮，也不从 hidden reasoning 代写正文。
- 当前 attempt 的机械 ref 尚未持久化时，读取工具只在当前私有 runtime 中按 hash 精确匹配，先落 content-addressed archive 再分页返回。
- 容量通知与机械引用均受 8 KiB UTF-8 硬上限保护；引用过多只减少条目，不占用主体正文预算或复制内容。
- 派生快照超限时先归档再机械省略；归档失败保持运行上下文并放弃本次有损写入。
- LLM context hook 在为了容纳 omission 元数据继续删除组时，重新以最终删除集合构造引用，避免审计缺口。
- 严格旧 summary envelope 在派生快照加载时停止续写；权威 trajectory、Life Event、聊天历史和普通用户文本保持不变。
- `AGENTS.md` 固化“语义连续性只能由 active consciousness 书写”的项目级不变量。
- 复核 Kimi Code 的公开配置与 compaction 源码：借鉴 token 水位/输出预留、完整轮次边界、近期尾部与原历史可导出等工程合同；明确拒绝另一个模型替主体生成第一人称 summary，以及用 synthetic USER 文本冒充真实 turn 的做法。

## 安全边界

- 未启动、停止、重启或修改任何 Elysium/NapCat 进程。
- 未改写正式数据、主体文件、Life Event、Memory 或 trajectory。
- 本任务没有删除旧归档文档；历史资料继续作为历史保留。
- 工作树原有其他修改保持原样，提交时必须使用精确文件/精确 hunk 暂存。
- 按项目规范，代码测试通过后仍只允许本地提交；必须等待用户手动启动和真实链路验收后才能推送。

## 自动化验收

最终验收结果：

- 上下文连续性专项：`21 passed`；覆盖主体检查点、工具注册、通知 UTF-8 硬上限、机械引用、归档 CAS 竞态、8 路本地并发写、UTF-8 分页与篡改失败关闭；
- 精确暂存索引快照中的 Chatter / LLM context / config / manifest / 历史兼容联合回归：`248 passed`；证明本提交不依赖共享工作树里未暂存的其他任务改动；
- 共享工作树联合回归：`250 passed`；Life Engine 全量风险回归：`1964 passed / 16 skipped`；
- 工作树与精确暂存索引快照的 Ruff（F/E9/I）、compileall，以及 `git diff --check`：通过。

覆盖范围包括：

- 主体检查点 schema、actor、manifest/revision、释放边界、重复/未知 ref 与 open tool chain；
- 精确归档、CAS/本地路径、UTF-8 分页、篡改与缺工作空间；
- 旧 summary 只读清理、普通标签文本保留、下一次正常保存规范化；
- Chatter 正常轮、工具续轮、媒体、历史快照和发送行为；
- task token 预算、kernel hook 追加裁剪、配置与工具 manifest/插件注册；
- Ruff、compileall、diff-check 和 Life Engine 风险范围。

## 尚待真实验收

用户手动启动 Elysium 后应核对：

1. Life Engine 与 Life Chatter 正常加载，普通消息及工具发送链无回归；
2. 旧滚动快照不再把复制式 summary 送给模型；
3. 达到压力阈值时通知无正文泄漏，爱莉可以选择忽略或在同轮亲自建立检查点；
4. 检查点后重启仍保留主体连续性，`ctxg_` 引用可分页复原；
5. 日志只出现 content-free 容量、ref/revision 和错误类型。
