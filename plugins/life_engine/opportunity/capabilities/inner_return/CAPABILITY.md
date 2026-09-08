# 内心回声交还

文档类型：工程能力手册；不是主体 Skill，也不是主体表达。

## 安装、安排与执行

安装只让本能力的说明与操作白名单可被发现；它不会登记时间策略、产生动态机会或调用任何操作。安装不等于安排，安排不等于执行。

主体可以通过统一机会管理面为本能力显式登记 `at` 或 `interval` 时间机会，并查询每次机会的来源；这些管理动作不扩大 manifest 中的领域权限。除 `initiative_reencounter` 已有的 Initiative 来源桥外，当前内置包不会根据旧 Learning、Memory、TODO、文件计数或其他领域到期事实自动生产机会。本能力没有这样的旧领域来源桥。

## 客观能力

本能力让潜意识查看由表达层主动沉下、仍等待交还的 inner_dialogue receipt，并可把潜意识当前亲自写下的回声交还给原始表达窗口。交还只唤醒那个窗口重新判断；回声不会被机械当成对外回复。

`nucleus_proactive_query` 在本能力中只读 inner_dialogue。`nucleus_proactive_command` 在本能力中只允许 heartbeat actor 提交 `inner.return`，并校验 receipt、原始 stream、occurrence、actor 和内容哈希。

## 可观察副作用

- 成功 return 会先追加不可变回声事件，再幂等唤醒原始表达窗口并追加投递事件。
- 唤醒后的表达窗口仍可表达、再次沉入 inner_dialogue，或保持安静。
- 交还事件已经耐久而即时唤醒失败时，后台只重放投递，不重写回声内容。

## 安全边界

- 只有潜意识 heartbeat 可执行 inner.return；表达窗口不能自行关闭刚沉下的 receipt。
- 基础设施不能生成回声 statement，也不能把原 thought 自动复制成回声或外发内容。
- return 必须绑定原始 stream；不能借 receipt 选择另一窗口或外部目标。
- 本能力不增加表达、发送或主动系统权限。

## 移除后的行为

移除后停止新的 inner.return 机会与由本能力发起的交还唤醒。既有 inner_dialogue、return 和 delivery 事件继续保留，未交还 receipt 不会被伪装成已关闭。
