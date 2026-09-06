# 未来意向再次遇见

文档类型：工程能力手册；不是主体 Skill，也不是主体表达。

## 安装、安排与执行

安装只让本能力的说明、来源适配与操作白名单可被运行时发现；它不会替主体创建 InitiativeSeed、登记下一次时间或执行任何意向。安装不等于安排，安排不等于执行。

主体可以通过统一机会管理面为本能力显式登记 `at` 或 `interval` 时间机会，并查询每次机会的来源；这些管理动作不扩大 manifest 中的领域权限。本能力目前是内置包中唯一已有领域来源桥的能力：它只把主体已经明确登记的 Initiative reencounter 到期事实转换成机会，不代表其他包已经具备旧 Learning、Memory、TODO 或文件计数来源桥。

## 客观能力

本能力让主体亲自留下的 InitiativeSeed 在一个明确延迟后，以一次性机会重新进入潜意识可见范围。再次遇见只恢复这条既有意向及其来源，不等于执行意向、选择外部对象、发送消息或建立循环任务。

`nucleus_proactive_query` 在本能力中只读 initiative 记录。`nucleus_proactive_command` 在本能力中只允许 `initiative.hold`、`initiative.rewrite`、`initiative.reencounter` 和 `initiative.release`；统一执行门继续校验 active actor、来源 occurrence、expected revision 与幂等身份。

## 可观察副作用

- hold、rewrite、reencounter 和 release 都会追加不可变主体决定事件并更新可重建当前视图。
- reencounter 只登记下一次到期时间；到期投递会追加技术回执并唤醒潜意识。
- 查询不改变状态，机会被忽略也不写入拒绝或自动释放。

## 安全边界

- 基础设施不得根据时间、重复次数或内容自动创建、重写、继续或释放 InitiativeSeed。
- 本能力不提供 outreach 动作，不选择外部对象与物理表面，也不形成待发送内容。
- 同一到期只允许幂等投递；投递失败保留待处理状态。
- 本能力不扩大主动系统或外部发送权限。

## 移除后的行为

移除后停止新的再次遇见机会和由此产生的潜意识唤醒。既有 InitiativeSeed、主体决定和投递历史继续保留且不会自动释放；重新安装后只能按权威历史与当前 revision 恢复。
