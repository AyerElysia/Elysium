# QQ 群聊本地语音故障分析与修复

## 结论

现场失败并非 IndexTTS 无法生成群语音，而是一次表达选择了 QQ 的 `send_group_ai_record` 平台角色接口，并用显示名称猜测 `character`；QQ 返回 `retcode=1200`。同一时段本地 TTS 在私聊通过普通 `voice`/OneBot `record` 链成功，证明两条能力此前被混淆。

同时发现一个独立回执缺口：NapcatAdapter 在构造时把 `core_sink=None` 交给 CommandHandler，AdapterManager 后续只更新 adapter 的 live sink，CommandHandler 仍保存旧值。平台命令虽然可能已在 NapCat 执行，查询/失败结果却不能返回等待方，进一步诱发超时和猜测。

## 修复合同

- 普通本地爱莉音色统一走 `life_send_voice`，当前目标是私聊还是群聊由聊天流决定；群聊出站映射为 `send_group_msg` + `record`。
- QQ `send_group_ai_record` 继续保留，但明确标为 QQ 内置角色音色；不得冒充本地 TTS，角色标识必须来自平台真实列表。
- NapCat 处理 command / adapter_command 前重新绑定 adapter 当前 live CoreSink，成功和失败回执都返回调用方。
- 不增加关键词路由、消息类型默认行为或“群聊必须发语音”规则；主体仍自由选择文本、语音、文件、其他动作或沉默。

## 验收边界

离线测试必须证明：群聊本地音频使用 OneBot `record`；constructor-time sink 为空而 Manager 后绑定时，平台命令的成功/失败回执均可达；Life Chatter 与 QQ skill 对两种能力给出一致身份说明。最终真实验收需在用户手工启动后的当前版本上，让主体从群聊实例主动调用一次 `life_send_voice`，核对 TTS、`send_group_msg` 和平台消息回执完整闭环。
