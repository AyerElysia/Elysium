# 狼人杀游戏（Werewolf Game）

> 文档状态：权威文档，与代码同步截至 2026-07-31。
> 代码位置：`plugins/werewolf_game/`（7 文件，1073 行）。
> 本文是狼人杀游戏插件的权威文档；凡与本文冲突，以本文和当前代码为准。

---

## 0. 一句话定位

Werewolf Game 是一个**确定性规则的 QQ 群狼人杀引擎**：规则判定完全由代码实现（无 LLM 调用），LLM 仅用于 AI 玩家的发言和决策表现层。

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                  WerewolfGamePlugin（插件入口）                        │
│  plugin.py — 注册 Service / Action / EventHandler                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              WerewolfEngine（确定性规则引擎）                   │   │
│  │                                                              │   │
│  │  • 无 LLM 调用，纯规则判定                                    │   │
│  │  • 隐藏裁判状态，对外仅暴露玩家视角                            │   │
│  │  • 创建/加入/退出/开始/夜晚/白天/投票/结算                    │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              WerewolfGameService（服务层）                      │   │
│  │  • 管理多群多局游戏实例                                        │   │
│  │  • 定时推进（夜晚倒计时、白天讨论超时）                        │   │
│  │  • AI 玩家行为调度（LLM 决策 + 规则执行）                     │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│  WerewolfPlayerAction — LLM Tool Calling 入口（玩家操作）            │
│  WerewolfCommandEventHandler — 群聊命令拦截（/开局 /加入 /投票...）  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 数据模型（models.py）

### 2.1 角色

| 角色 | 标识 | 能力 |
|------|------|------|
| 狼人 | `werewolf` | 夜晚选择击杀目标 |
| 预言家 | `seer` | 夜晚查验一人身份 |
| 女巫 | `witch` | 解药/毒药各一瓶 |
| 猎人 | `hunter` | 死亡时带走一人 |
| 村民 | `villager` | 无特殊能力 |

### 2.2 游戏阶段

```
WAITING → NIGHT ⇄ DAY → ENDED
```

### 2.3 核心数据结构

- `Player`：user_id、display_name、role、alive、is_bot
- `NightState`：wolf_target、seer_done、witch_done、healed/poisoned
- `GameState`：platform、group_id、players、phase、round、night_state

---

## 3. 规则引擎（engine.py）

### 3.1 设计原则

- **确定性**：所有规则判定由代码完成，不依赖 LLM
- **信息隔离**：隐藏裁判状态，提供 `player-view` 辅助函数
- **最小人数**：正式局 ≥ 6 人，测试局 ≥ 3 人

### 3.2 核心方法

| 方法 | 职责 |
|------|------|
| `create_game()` | 创建游戏实例 |
| `add_player()` / `remove_player()` | 加入/退出（仅等待阶段） |
| `start_game()` | 分配角色、进入夜晚 |
| `night_action()` | 处理夜晚操作（狼杀/查验/毒救） |
| `resolve_night()` | 结算夜晚结果 |
| `day_vote()` | 白天投票 |
| `resolve_vote()` | 结算投票、判定胜负 |
| `hunter_shot()` | 猎人开枪 |

### 3.3 角色分配

根据玩家数量自动计算角色配比，使用 `random.SystemRandom()` 洗牌分配。

---

## 4. 服务层（service.py）

`WerewolfGameService` 管理游戏运行时：

- 按 `group_id` 索引游戏实例
- 定时任务推进游戏（夜晚倒计时、白天讨论超时）
- AI 玩家（`is_bot=True`）由 LLM 生成发言/决策，再调用引擎执行
- 游戏结束后清理实例

---

## 5. 交互入口

### 5.1 WerewolfPlayerAction

LLM Tool Calling 入口，供主意识在群聊中操作：
- 加入/退出游戏
- 执行夜晚操作
- 白天投票
- 猎人开枪

### 5.2 WerewolfCommandEventHandler

群聊命令拦截器，支持斜杠命令：
- `/开局` — 创建游戏
- `/加入` — 加入游戏
- `/开始` — 开始游戏
- `/投票 <玩家>` — 投票
- 其他游戏内指令

---

## 6. 文件索引

```
plugins/werewolf_game/
├── __init__.py              # 包说明
├── plugin.py                # 插件入口
├── models.py                # 数据模型（Role/Phase/Player/GameState）
├── engine.py                # 确定性规则引擎
├── service.py               # 游戏服务（多局管理+定时推进）
├── actions.py               # LLM Action 入口
└── event_handler.py         # 群聊命令拦截
```
