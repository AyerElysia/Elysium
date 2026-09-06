# 心跳压缩循环与 Minecraft 账号冲突（2026-09-06）

## 1. 心跳压缩 WARNING 不是实现崩溃

03:18–03:19 的日志：

```
心跳压缩回合未完成，本拍不把新经历写入滚动链: #5275
life_engine 心跳 #5275 压缩回合未完成，不推进潜意识游标
已注入 31 条事件
```

这是 fail-closed，不是压缩器坏了。滚动已超过 `context_compaction_trigger_chars`。同一套合同要求当前心跳意识调用 `author_self_continuity_checkpoint` 亲笔 `continuity_text`。她写了 `[观察]/[感受]/[意图]/[内在动作]` 而没有调用该工具，所以：

- 不推进 `heartbeat_context_cursor`；
- 不把本拍新经历写入滚动快照；
- 下一拍仍是压缩回合，同一页约 31 条事件再次出现。

权威 Life Event 仍在。WARNING 每拍都会打，是因为心跳 30 秒一轮，不是因为历史被丢掉。

## 2. 与 Chatter 合同：共享核心，运行时曾不一致

共享且本来就一致：

- `plugins/life_engine/core/context_stewardship.py`
- 压缩清单 `<context_compression_required>`
- 检查点工具与精确归档（heartbeat / chatter namespace 隔离）
- 未检查点则完整滚动留下

此前不一致、会让心跳永远完不成压缩：

1. Chatter 压缩回合会拦截普通工具，只允许 `read_context_group` / `author_self_continuity_checkpoint`。心跳会照常执行 `nucleus_search_memory` 等工具。
2. 心跳系统提示仍把 `[观察]/[感受]/[意图]/[内在动作]` 写成默认输出，并写“没有明确需要可以不调用工具”。这会把强制维护回合读成可选建议。
3. 未消费时仍打 `已注入 31 条事件`，看起来像已经处理完。

本次已把心跳对齐 Chatter：压缩回合拦截非 stewardship 工具；提示写明观察正文不能代替检查点；未消费时日志改为“准备了 N 条事件，本拍未消费”。系统仍然不会代写 `continuity_text`。

## 3. 16:55 `life_engine` 加载失败

```
插件启动事务失败: native client must not reuse the human account name
```

来源：`MCConfig.__post_init__`。`LifeEngineConfig.MinecraftSection` 把人类离线名 `offline_username` 和主体客户端 `agent_shared_username` 都默认成 `Elysia`。`minecraft.enabled=true` 时 `model_dump()` 灌进 `MCConfig`，启动直接失败，整个生命引擎进不来。

`MCConfig` 数据类自己的人类默认名是 `AyerElysia`，配置 schema 漂移了。生产手册示例只写了 `agent_shared_username = "Elysia"`，未写人类名，会踩同一坑。

本次：

- schema 人类默认名改回 `AyerElysia`；
- `enabled=true` 时配置校验拒绝重名；
- `enabled=false` 时仍可读旧的重名 TOML，避免无关启动被拦。

若本机 TOML 已显式写了两个 `Elysia` 且 `enabled=true`，需要把 `offline_username` 改成人类账号（例如 `AyerElysia`）后再手工重启。代码不会代她选游戏名。

本次已把本机 `config/plugins/life_engine/config.toml` 的 `offline_username` 改为 `AyerElysia`，`agent_shared_username` 仍为 `Elysia`。

## 4. 验证

定向测试覆盖 Minecraft 默认身份、启用时重名拒绝、心跳压缩回合拦截普通工具。Elysium 需用户手工重启后，启动日志不再出现该 ValueError；心跳在检查点完成前仍会 WARNING。

2026-09-06 17:26 现场（重启后仍是旧进程日志）：#5890 压缩未完成且 `deadline_exhausted`，游标未推进，但 INFO 仍打 `已消费 43 条事件`。那是把准备阶段的 `acknowledged_event_ids` 当成消费证明。工作区已改为只在 `_commit_heartbeat_context` 发出 `HeartbeatConsumptionReceipt` 后才说“已消费”；未提交时打“准备了 N 条事件，本拍未消费”。需再一次手工重启后这条假消费才会消失。
