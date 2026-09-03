# Minecraft 陪玩现场状态

> 最后核对：2026-09-03
> 当前状态：自动化生产候选通过，真实世界端到端尚未验收，不能写成“已打通”。

## 正确的陪玩路线

用户继续使用自己的 Minecraft 客户端；爱莉通过 `body_name="bot"`
以独立 Mineflayer 玩家加入同一个 LAN 世界。`agent` 是控制已有客户端的
另一种身体，不是两个人一起玩的默认路线。

```text
玩家客户端 / Elysian Realm / LAN :25565
                   ↑
         Mineflayer 独立身体 Elysia
                   ↕
     authenticated bridge :18767
                   ↕
      MinecraftSession + 专属场景意识
                   ↕
        Life Event / 统一主体活动谱系
```

## 当前不能宣称已完成的原因

- 现场没有 Minecraft/Java 进程，25565 与 18767 均未监听；
- 正在运行的 Elysium 尚未加载本轮代码；
- `LifeEngineService` 仍缺 `record_minecraft_body_event` 的最后接线，
  带高层任务能力的 bot 会因此在 session start 时 fail closed；
- 游戏内聊天、专属意识回复、高层任务终态及重连重放还未在同一次真实
  session 中留下证据。

## 代码完成后的真实验收顺序

1. 用户手动重启 Elysium；agent 不代替用户停止或重启主进程。
2. 用户启动 `G:GameMinecraftPCLLaunchElysia.bat`，进入
   `Elysian Realm`，把世界以固定 `25565` 端口开放到 LAN。
3. 在 Elysium Console 做 Minecraft preflight，并以
   `body_name="bot"` 启动 session。
4. 确认玩家列表出现 `Elysia`，随后验证：
   - 玩家聊天能落入带来源的 Life Event 并提前唤醒 MC 意识；
   - 爱莉自主决定是否用 `chat.send` 回复，回复在游戏中可见；
   - 跟随或采集存在 accepted / progress / terminal / 新观察闭环；
   - 长任务期间仍能接收聊天和重新考虑；
   - 未 ACK 事件重连后原样重放，已落账事件不重复；
   - 连续 15–30 分钟没有请求堆积、上下文膨胀或身体锁泄漏；
   - stop 只释放爱莉的 bot，不关闭用户客户端。

完整、持续更新的依据见：

- `docs/operations/minecraft_production_runbook.md`
- `docs/report/minecraft-companion-community-and-readiness-2026-09-03.md`
