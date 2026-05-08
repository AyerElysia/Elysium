# life_engine 插件深度代码考古报告

> **版本**：基于 `service/core.py` v3.3.0 考古  
> **覆盖代码量**：约 12,000 行 Python  
> **仓库路径**：`plugins/life_engine/`  
> **调研日期**：2025

---

## 目录

1. [顶层架构与子模块职责](#1-顶层架构与子模块职责)
2. [service/core.py — 心脏泵](#2-servicecorepy--心脏泵)
3. [snn/ — 脉冲神经层](#3-snn--脉冲神经层)
4. [neuromod/engine.py — 调质慢层](#4-neuromodengginepy--调质慢层)
5. [dream/ — 离线巩固系统](#5-dream--离线巩固系统)
6. [memory/ — 连续记忆网络](#6-memory--连续记忆网络)
7. [streams/、drives/、tools/、monitor/ — 辅助系统](#7-streams-drives-tools-monitor--辅助系统)
8. [事件代数](#8-事件代数)
9. [持久化](#9-持久化)
10. [配置参数全表](#10-配置参数全表)
11. [可观测性](#11-可观测性)
12. [未解之谜](#12-未解之谜)

---

## 1. 顶层架构与子模块职责

### 1.1 目录树与职责一览

```
plugins/life_engine/
├── service/          # 心跳服务核心（最关键）
│   ├── core.py       # LifeEngineService — 2155行，整个生命体的主循环
│   ├── event_builder.py  # 事件模型 + EventBuilder
│   ├── state_manager.py  # 持久化 + compress_history
│   ├── integrations.py   # DFCIntegration / SNNIntegration / MemoryIntegration
│   ├── command_handler.py # 指令路由
│   ├── event_handler.py  # 消息路由
│   ├── audit.py      # 结构化日志
│   ├── error_handling.py # retry_with_backoff
│   └── registry.py   # 全局单例注册
├── snn/              # 脉冲神经网络（皮层下快层）
│   ├── core.py       # LIFNeuronGroup / STDPSynapse / DriveCoreNetwork
│   ├── bridge.py     # SNNBridge — 特征提取 + 奖赏计算 + prompt格式化
│   └── router.py     # SNN API路由
├── neuromod/         # 神经调质慢层
│   └── engine.py     # Modulator / ModulatorSystem / HabitTracker / InnerStateEngine
├── dream/            # 做梦/离线巩固
│   ├── scheduler.py  # DreamScheduler — 调度 + NREM + REM + 觉醒
│   ├── seeds.py      # DreamSeed — 种子结构 + 多路收集
│   ├── scenes.py     # build_dream_scene — LLM梦境生成
│   ├── residue.py    # DreamResidue / DreamReport — 余韵数据结构
│   └── router.py     # Dream API路由
├── memory/           # 连续记忆图谱
│   ├── service.py    # LifeMemoryService — SQLite + ChromaDB
│   ├── nodes.py      # MemoryNode — FILE/CONCEPT节点
│   ├── edges.py      # MemoryEdge — 6类边
│   ├── decay.py      # apply_decay / dream_walk / prune_weak_edges
│   ├── search.py     # fts_search / vector_search / rrf_fusion / spread_activation
│   ├── prompting.py  # MEMORY.md解析 + prompt注入
│   ├── tools.py      # 记忆工具集
│   └── router.py     # Memory WebSocket广播路由
├── streams/          # 思考流管理
│   ├── manager.py    # ThoughtStreamManager
│   ├── models.py     # ThoughtStream数据模型
│   └── tools.py      # 思考流工具
├── drives/           # 冲动引擎
│   ├── impulse.py    # ImpulseEngine / ImpulseRule / ImpulseSuggestion
│   └── rules.py      # DEFAULT_RULES (默认冲动规则集)
├── tools/            # 工具箱（nucleus_* 系列）
│   ├── file_tools.py / grep_tools.py / exec_tools.py
│   ├── web_tools.py  # Tavily API
│   ├── todo_tools.py
│   ├── schedule_tools.py
│   ├── chat_history_tools.py
│   └── event_grep_tools.py
├── monitor/
│   └── router.py     # MessageTimelineRouter — WebUI联动
├── core/
│   ├── config.py     # LifeEngineConfig（所有可调参数）
│   ├── plugin.py     # 插件入口
│   ├── chatter.py    # life_chatter 对话模式
│   └── compat_tools.py
└── constants.py      # 全局魔法数字集中定义
```

### 1.2 模块间调用关系图

```
外部消息/DFC留言
        │
        ▼
  service/event_handler.py
  service/command_handler.py
        │ append to _pending_events
        ▼
  service/core.py ── _heartbeat_loop() ──► asyncio.sleep(interval)
        │                                  │ (每 heartbeat_interval_seconds)
        │                                  ▼
        │  ┌───────────────────────────────────────────────────────┐
        │  │  1. _in_sleep_window_now() → 是否睡眠                 │
        │  │     └─ 若睡眠: dream_scheduler.should_dream()         │
        │  │            └─ run_dream_cycle()                       │
        │  │  2. snn_integration.heartbeat_pre()                   │
        │  │     └─ snn_bridge.extract_features_from_events()      │
        │  │     └─ snn_network.step(features)                     │
        │  │     └─ inner_state.tick(snn_drives, event_stats)      │
        │  │  3. inject_wake_context() → 事件流 → system_reminder  │
        │  │  4. _run_heartbeat_model(wake_context)                │
        │  │     └─ create_llm_request(task="life")                │
        │  │     └─ 工具调用循环(最多 max_rounds_per_heartbeat)     │
        │  │  5. _record_model_reply() → _append_history()        │
        │  │  6. snn_integration.heartbeat_post()                  │
        │  │     └─ snn_bridge.record_heartbeat_result()          │
        │  │  7. 检查 dream_scheduler.should_dream() (白天小憩)    │
        │  │  8. _save_runtime_context()                           │
        │  └───────────────────────────────────────────────────────┘
        │
        ├── memory/service.py ← 工具调用 nucleus_search_memory 等
        ├── streams/manager.py ← nucleus_advance_thought_stream
        ├── drives/impulse.py ← 冲动建议注入 prompt
        └── monitor/router.py ← WebUI 实时快照
```

**关键约束**：`LifeEngineService` 是单例（`service/registry.py`），通过 `get_life_engine_service()` 全局访问。所有子系统持有 `service` 引用作为反向指针。

---

## 2. service/core.py — 心脏泵

**文件**：`plugins/life_engine/service/core.py`  
**类**：`LifeEngineService(BaseService)` [line 85]  
**版本**：`version = "3.3.0"` [line 94]

### 2.1 心跳循环 `_heartbeat_loop` 完整流程

**入口**：`async def _heartbeat_loop(self) -> None` [line 1869]

```python
# 伪代码还原
async def _heartbeat_loop():
    interval = cfg.settings.heartbeat_interval_seconds  # 默认30s
    while state.running:
        # Step 0: 等待一个 interval（通过 asyncio.wait_for 实现可中断等待）
        await asyncio.wait_for(stop_event.wait(), timeout=interval)

        # Step 1: 睡眠窗口检查
        in_sleep_window, desc = _in_sleep_window_now()
        if in_sleep_window:
            if not sleep_state_active:
                sleep_state_active = True
                dream_scheduler.enter_sleep()  # 调质层进入睡眠基线
            # 睡眠中仍检查做梦
            if dream_scheduler.should_dream(idle_count, in_sleep=True):
                report = await dream_scheduler.run_dream_cycle(event_history)
                dfc_integration.inject_dream_report(report, "sleep_window")
            continue  # 跳过LLM心跳

        # Step 2: 心跳计数
        state.heartbeat_count += 1
        state.last_heartbeat_at = now_iso()

        # Step 3: 每日记忆衰减
        await memory_integration.maybe_run_daily_decay()

        # Step 4: SNN 前置更新
        await snn_integration.heartbeat_pre()
        # 内部: extract_features → snn.step(features) → inner_state.tick(drives)

        # Step 5: 事件注入 — 构建 LLM 可见的上下文
        wake_context = await inject_wake_context()
        # inject_wake_context():
        #   drain_pending_events() → _append_history(events) → _build_wake_context_text()
        #   → store.set("actor", "生命中枢唤醒上下文", content)

        # Step 6: 静默暂停检查
        if _should_pause_llm_heartbeat_for_external_silence():
            await snn_integration.heartbeat_post()
            continue  # 跳过LLM但不跳过SNN

        # Step 7: LLM 心跳调用
        model_reply = await _run_heartbeat_model(wake_context)
        await _record_model_reply(model_reply)  # → _append_history([heartbeat_event])

        # Step 8: SNN 后置更新（奖赏计算）
        await snn_integration.heartbeat_post()

        # Step 9: 白天小憩检查
        if dream_scheduler.should_dream(idle_count, in_sleep=False):
            report = await dream_scheduler.run_dream_cycle(event_history)
            dfc_integration.inject_dream_report(report, "daytime_nap")

        # Step 10: 持久化
        await _save_runtime_context()
```

#### `_run_heartbeat_model` 内部逻辑 [line 1596]

```python
async def _run_heartbeat_model(wake_context):
    # 构建 system prompt: SOUL.md + MEMORY.md + TOOL.md
    system_prompt = _build_heartbeat_system_prompt()
    # 构建 user prompt: 头部指令 + 事件流 + 心跳状态 + 调质状态 + 思考流 + 冲动建议
    user_prompt = _build_heartbeat_model_prompt(wake_context)

    request = create_llm_request(model_set=get_model_set_by_task("life"))
    request.add_payload(SYSTEM, system_prompt)
    request.add_payload(TOOL, tools_list)    # ALL_TOOLS + TODO_TOOLS + MEMORY_TOOLS + ...
    request.add_payload(USER, user_prompt)

    response = await retry_with_backoff(request.send, max_retries=2)

    # 工具调用循环（最多 max_rounds_per_heartbeat 轮，默认3轮）
    for _ in range(max_rounds):
        response_text = await response
        call_list = response.call_list
        if not call_list:
            break
        for call in call_list:
            await _execute_heartbeat_tool_call(call, response, registry)
            # → record_tool_call() + record_tool_result() → pending_events
        response = await response.send(stream=False)  # 继续下一轮

    # 空闲计数追踪
    if tool_event_count > 0:
        state.idle_heartbeat_count = 0
    else:
        state.idle_heartbeat_count += 1

    return last_text  # 最后一轮文本响应
```

### 2.2 状态机字段

**数据类**：`LifeEngineState` [event_builder.py line 62]

| 字段 | 类型 | 含义 | 是否持久化 |
|------|------|------|-----------|
| `running` | bool | 心跳是否运行 | ❌（启动时重置） |
| `started_at` | str\|None | 启动时间 ISO | ❌ |
| `last_heartbeat_at` | str\|None | 最后心跳时间 | ✅ |
| `heartbeat_count` | int | 累计心跳次数 | ✅ |
| `pending_event_count` | int | 待处理事件数 | ✅ |
| `history_event_count` | int | 历史事件数 | ✅（间接，通过events数组） |
| `event_sequence` | int | 全局事件序列号 | ✅ |
| `last_wake_context_at` | str\|None | 最后注入上下文时间 | ✅ |
| `last_wake_context_size` | int | 最后注入事件数 | ✅ |
| `last_model_reply_at` | str\|None | 最后LLM回复时间 | ✅ |
| `last_model_reply` | str\|None | 最后LLM回复内容 | ✅ |
| `last_model_error` | str\|None | 最后错误 | ✅ |
| `last_external_message_at` | str\|None | 最后外部消息时间 | ✅ |
| `last_tell_dfc_at` | str\|None | 最后传话DFC时间 | ✅ |
| `tell_dfc_count` | int | 传话DFC累计次数 | ✅ |
| `idle_heartbeat_count` | int | 连续空闲心跳数 | ❌（运行时重置） |
| `chatter_context_cursors` | dict[str,int] | 各流已注入高水位 | ✅ |

**非持久化字段**（仅内存）：`running`、`started_at`、`idle_heartbeat_count`、`last_error`

### 2.3 与 DFC 的双向接口

**DFC = Default Chatter（对外对话模式）**，通过 `DFCIntegration` 管理 [service/integrations.py line 79]

#### 三大接口

**① `nucleus_wake_dfc` / `consult_nucleus`（DFC 查询生命中枢）**

对应 `service.query_actor_context(query)` [core.py line 588]:
```python
async def query_actor_context(self, query: str) -> str:
    return await self._dfc_integration.query_actor_context()
    # → get_dfc_snapshot() → _build_state_digest_locked()
    #   包含：调质层状态摘要 + 最近心跳独白(2条) + 工具偏好 + 梦后余韵
```

**② `nucleus_tell_dfc`（生命中枢主动传话给DFC）**

这是 `file_tools.py` 中的工具，LLM 在心跳中调用它。执行时：
1. 调用 `service.record_tell_dfc()` 更新时间戳
2. 将内容注入 DFC 的 runtime assistant payload（通过 `default_chatter.push_runtime_assistant_injection`）

**③ `inject_dream_report`（梦境记录注入DFC）**

[integrations.py line 241]:
```python
async def inject_dream_report(self, report, trigger):
    # 找最近的外部流 stream_id
    stream_id = await pick_latest_external_stream_id()
    # 序列化 DreamReport → JSON
    payload_text = build_dream_record_payload_text(report)
    # 注入 DFC 的 assistant payload 队列
    push_runtime_assistant_injection(stream_id, payload_text)
```

**状态摘要格式** [integrations.py line 130]:
```
【内在状态】好奇心充盈、精力适中、满足感平静
【最近思考】
  [5分钟前] 我在想...
【工具偏好】write_file, search_memory
[梦后余韵]（如有）
```

### 2.4 睡眠窗口判定

**函数**：`_in_sleep_window_now()` [core.py line 247]

```python
def _in_sleep_window_now() -> tuple[bool, str]:
    sleep_at, wake_at = _parse_hhmm(cfg.sleep_time), _parse_hhmm(cfg.wake_time)
    now_hm = current_time_hm()

    if sleep_at < wake_at:
        # 同日区间，如 23:00~07:00 不适用此分支
        in_sleep = sleep_at <= now_hm < wake_at
    else:
        # 跨午夜区间，如 23:00~07:00
        in_sleep = (now_hm >= sleep_at) or (now_hm < wake_at)
```

**做梦触发逻辑** [dream/scheduler.py line 158]:
```python
def should_dream(idle_heartbeat_count, in_sleep_window) -> bool:
    if is_dreaming: return False
    if time.time() - last_dream_time < dream_interval_seconds: return False
    if in_sleep_window: return True                          # 夜间: 随时做梦
    if nap_enabled and idle_count >= idle_trigger_heartbeats: return True  # 白天: 空闲触发
    return False
```

---

## 3. snn/ — 脉冲神经层

### 3.1 神经元模型：LIF (Leaky Integrate-and-Fire)

**类**：`LIFNeuronGroup` [snn/core.py line 24]

**膜电位动力学方程**：

```
dV/dt = (-(V - V_rest) + I) / τ * dt
```

对应代码 [line 56]:
```python
dv = (-(self.v - self.rest) + current) / self.tau * dt
self.v += dv
np.clip(self.v, -10.0, 10.0, out=self.v)
spikes = self.v >= self.threshold
self.v[spikes] = self.reset  # 发放后重置
```

**参数配置**：
- 隐藏层：`tau=12.0ms`, `threshold=0.15`, `reset=0.0`, `rest=0.0` [line 248]
- 输出层：`tau=25.0ms`, `threshold=0.20`, `reset=0.0`, `rest=0.0` [line 249]

**`decay_only` vs `step` 的区别** [line 63, 54]:

| 方法 | 功能 | 使用场景 |
|------|------|---------|
| `step(current, dt)` | 积分 + 噪声 + 发放检测 + STDP | 有真实输入时 |
| `decay_only(dt)` | 仅泄漏衰减，不注入电流，不检查发放，spikes[:] = False | 零输入 tick |

**设计意图**（文档注释 [line 7]）：  
> 分离 decay_only() 与 step()：零输入 tick 不再执行完整 step，避免淹没信号。

### 3.2 突触结构与软 STDP

**类**：`STDPSynapse` [snn/core.py line 92]

```
Xavier 初始化：scale = sqrt(2 / (n_pre + n_post))
W ~ Uniform(-scale, scale)
trace_decay = 0.90
```

**软 STDP 更新规则** [line 150]:
```python
def update_soft(pre_activity, post_activity, reward):
    # 更新痕迹（指数滑动平均）
    trace_pre  = trace_pre * 0.90  + pre_activity
    trace_post = trace_post * 0.90 + post_activity

    reward_factor = 1.0 + clip(reward, -1, 1)

    # LTP（长时程增强）：post活跃时，强化pre→post
    if sum(post_activity) > 0.05:
        dw_plus = lr_plus * outer(post_activity, trace_pre)
        W += dw_plus * max(reward_factor, 0.1)

    # LTD（长时程抑制）：pre活跃时，弱化post→pre
    if sum(pre_activity) > 0.05:
        dw_minus = -lr_minus * outer(trace_post, pre_activity)
        W += dw_minus * max(2.0 - reward_factor, 0.1)

    clip(W, w_min, w_max)
```

**与v1的关键差异**：使用 `sigmoid(膜电位)` 作为连续活跃度代替二值 spike，确保低放电率下仍有可塑性。

### 3.3 网络结构与输出

**类**：`DriveCoreNetwork` [snn/core.py line 214]

```
输入层 (8维) → STDPSynapse → 隐藏层 (16 LIF, tau=12) → STDPSynapse → 输出层 (6 LIF, tau=25)
```

**6维驱动输出含义** [line 232]:
```
0 - arousal:           整体激活度
1 - valence:           情感正负
2 - social_drive:      社交冲动
3 - task_drive:        推进任务冲动
4 - exploration_drive: 探索冲动
5 - rest_drive:        休息冲动
```

**EMA 平滑** [line 333]:
```python
output_ema = (1 - ema_alpha) * output_ema + ema_alpha * raw_output
# ema_alpha = 0.15
```

**自稳态阈值调节** [line 315]:
```python
hidden_rate_ema  = (1 - α) * hidden_rate_ema  + α * actual_rate
output_rate_ema  = (1 - α) * output_rate_ema  + α * actual_rate
# α = homeo_alpha = 0.03

# 阈值调节（增益控制）
hidden.threshold += homeo_threshold_lr * (hidden_rate_ema - target_hidden_rate)
output.threshold += homeo_threshold_lr * (output_rate_ema - target_output_rate)
# target_hidden_rate = 0.10, target_output_rate = 0.06

# 增益调节
input_gain      += homeo_gain_lr * (target_hidden_rate - hidden_rate_ema)
hidden_spike_gain += homeo_gain_lr * (target_output_rate - output_rate_ema)

# 边界裁剪
hidden.threshold ∈ [0.05, 0.5]
output.threshold ∈ [0.05, 0.5]
input_gain ∈ [0.8, 3.5]
```

**动态离散化**（z-score）[line 371]:
```python
z = (ema[i] - running_mean[i]) / sqrt(running_var[i])
level = "高" if z > 1.0 else "中" if z > 0.3 else "低" if z > -0.5 else "抑制"
```

### 3.4 输入特征提取（8维）

**函数**：`extract_features(events, window_seconds=600)` [snn/bridge.py line 41]

从最近 `window_seconds`（默认600秒=10分钟）的事件中提取：

```
原始统计: [msg_in, msg_out, tool_success, tool_fail, idle_beats, tell_dfc_count, new_content, silence_factor]
归一化:   前7维 → tanh(x/3.0)  范围 (-1, 1)
          silence_factor = min(silence_min/60, 1) → x*2-1  范围 (-1, 1)
```

**最终 8 维输入向量 = tanh归一化事件计数 ∈ [-1, 1]**

### 3.5 bridge.py 的奖赏计算与 prompt 注入

**奖赏信号** [bridge.py line 189]:
```python
reward = 0.0
if tool_calls > 0:     reward += 0.3
if tool_success > 0:   reward += min(tool_success * 0.15, 0.4)
if tool_calls == 0:    reward -= 0.2
if tool_fail > 0:      reward -= min(tool_fail * 0.2, 0.4)
if idle_count >= 5:    reward -= 0.3
elif idle_count >= 2:  reward -= 0.15
# clip到 [-1, 1]
```

**注入 prompt**（当 `shadow_only=False` 且 `inject_to_heartbeat=True`）[bridge.py line 260]:
```python
def format_drive_for_prompt(drive_discrete):
    # 输出: "【SNN快层】激活高、情绪中、社交低、任务中、探索高、休息抑制"
```

注意：当前代码注释显示 SNN 已降级为 shadow 模式，**调质层 (neuromod) 提供更清晰的驱动摘要** [core.py line 1313]。

### 3.6 SNN 序列化字段 [snn/core.py line 530]

```json
{
  "version": 2,
  "hidden_v": [...],          // 16维膜电位
  "output_v": [...],          // 6维膜电位
  "output_ema": [...],        // 6维EMA
  "syn_in_hid_W": [[...]],    // (16, 8)权重矩阵
  "syn_in_hid_trace_pre": [], "syn_in_hid_trace_post": [],
  "syn_hid_out_W": [[...]],   // (6, 16)权重矩阵
  "syn_hid_out_trace_pre": [], "syn_hid_out_trace_post": [],
  "hidden_threshold": 0.15,
  "output_threshold": 0.20,
  "input_gain": 2.0,
  "hidden_spike_gain": 1.5,
  "hidden_cont_gain": 0.4,
  "hidden_rate_ema": 0.05,
  "output_rate_ema": 0.03,
  "tick_count": 0,
  "real_step_count": 0,
  "output_running_mean": [...],
  "output_running_var": [...]
}
```

---

## 4. neuromod/engine.py — 调质慢层

**文件**：`plugins/life_engine/neuromod/engine.py`

### 4.1 调质因子定义

**5个调质因子** [engine.py line 66]:

| 名称 | 中文 | 初值 | τ (秒) | baseline | 作用 |
|------|------|------|--------|----------|------|
| `curiosity` | 好奇心 | 0.6 | 1800 (30min) | 0.55 | 驱动探索行为 |
| `sociability` | 社交欲 | 0.5 | 3600 (1h) | 0.50 | 驱动沟通表达 |
| `diligence` | 专注力 | 0.5 | 5400 (90min) | 0.50 | 驱动任务执行 |
| `contentment` | 满足感 | 0.5 | 1800 (30min) | 0.50 | 情绪稳定指标 |
| `energy` | 精力 | 0.6 | 10800 (3h) | 0.55 | 总体活跃度 |

### 4.2 调质 ODE 更新公式

**`Modulator.update(stimulus, dt)`** [engine.py line 42]:

```
decay   = decay_rate * (baseline - value) * dt        # 向基线回归
headroom = 1 - |value - 0.5| * 2                      # 边际效应递减 ∈ [0, 1]
impulse = stimulus * max(headroom, 0.1) * (dt / τ) * 10
value  += decay + impulse
value  ∈ [0, 1]
```

其中 `decay_rate = 0.001`（隐式）。实际上该字段在 `Modulator` 类中已定义但在 `update` 内以 `self.decay_rate` 引用。

**离散等级** [line 51]:
```
value > 0.75: "充盈"
value > 0.55: "适中"
value > 0.35: "平静"
else:         "休憩"
```

### 4.3 昼夜节律

**两个函数** [engine.py line 329]:

```python
def circadian_energy(hour: float) -> float:
    # 双峰：10:00 上午峰 + 15:00 下午峰
    morning   = exp(-0.5 * ((hour - 10) / 3)^2)
    afternoon = exp(-0.5 * ((hour - 15) / 3)^2)
    return 0.25 + 0.75 * max(morning, afternoon)
    # 范围: [0.25, 1.0]

def circadian_sociability(hour: float) -> float:
    # 晚间峰 20:00 + 午间峰 12:00
    evening = exp(-0.5 * ((hour - 20) / 3)^2)
    midday  = exp(-0.5 * ((hour - 12) / 4)^2)
    return 0.3 + 0.7 * max(evening * 0.8, midday * 0.6)
    # 范围: [0.3, 0.86]
```

**昼夜节律如何修正调质基线** [engine.py line 377]:
```python
# energy 基线随时间变化
energy_mod.baseline = 0.35 + 0.3 * circadian_energy(hour)  # [0.35, 0.65]

# sociability 基线随时间变化
social_mod.baseline = 0.3 + 0.3 * circadian_sociability(hour)  # [0.3, 0.6]
```

### 4.4 习惯追踪公式

**类**：`Habit` [engine.py line 198]

**streak 计数规则** [line 208]:
- 同一天触发：`streak` 不变（幂等）
- 相差1天：`streak += 1`（连续）
- 相差>1天：`streak = 1`（重置）

**strength 公式** [line 232]:
```
streak_bonus = min(streak / 14.0, 1.0)   # 满14天→满分
freq_bonus   = min(total_count / 50.0, 1.0)  # 满50次→满分
strength     = 0.6 * streak_bonus + 0.4 * freq_bonus
```

**显示等级**：
- `strength > 0.7`：强（streak天数）
- `strength > 0.3`：渐成
- `total_count > 0`：萌芽

**已追踪的6个习惯** [line 250]:
| 习惯名 | 中文 | 触发工具 |
|--------|------|---------|
| diary | 写日记 | nucleus_write_file |
| memory | 整理记忆 | nucleus_search_memory |
| relate | 建立关联 | nucleus_relate_file |
| todo | 管理待办 | nucleus_list_todos/create/complete |
| web_search | 联网搜索 | nucleus_web_search |
| reflection | 自我反思 | nucleus_write_file |

### 4.5 SNN 输出 → 调质刺激映射

**`compute_stimuli_from_snn_and_events(snn_drives, event_stats, circadian_energy)`** [engine.py line 90]:

```python
# curiosity: SNN探索驱动 + 沉默时间 - 最近搜索抑制
stimuli["curiosity"] = (
    0.3 * exploration_drive
    + 0.2 * min(silence_min / 30.0, 1.0)
    - 0.3 * min(recent_searches / 3.0, 1.0)
)

# sociability: 入站消息 + SNN社交驱动 - 出站消息抑制
stimuli["sociability"] = (
    0.4 * min(msg_in / 3.0, 1.0)
    - 0.2 * min(msg_out / 5.0, 1.0)
    + 0.2 * social_drive
)

# diligence: SNN任务驱动 + 工具成功 - 工具失败
stimuli["diligence"] = (
    0.3 * task_drive
    + 0.3 * min(tool_success / 3.0, 1.0)
    - 0.4 * min(tool_fail / 2.0, 1.0)
)

# contentment: 情感效价 + 工具成功 - 工具失败
stimuli["contentment"] = (
    0.4 * valence
    + 0.2 * min(tool_success / 2.0, 1.0)
    - 0.3 * min(tool_fail / 2.0, 1.0)
)

# energy: 昼夜节律 + 安静回复 + 休息驱动
stimuli["energy"] = (
    0.3 * (circadian_energy - 0.5) * 2.0
    + 0.1 * min(idle_beats / 10.0, 1.0)
    + 0.2 * rest_drive
)
```

**所有刺激 clip 到 [-1, 1]**

### 4.6 睡眠/觉醒对调质的影响

**`InnerStateEngine.enter_sleep()`** [line 409]:
- `energy.baseline = 0.25`，`energy.value = min(val, 0.4)`
- `sociability.baseline = 0.2`，`sociability.value = min(val, 0.3)`
- `curiosity.baseline = 0.3`

**`InnerStateEngine.wake_up()`** [line 432]:
- `energy.value += 0.25`（上限0.85），`energy.baseline = 0.55`
- `sociability.baseline = 0.50`，`sociability.value = max(val, 0.4)`
- `curiosity.baseline = 0.55`，`curiosity.value = max(val, 0.45)`
- `contentment.value += 0.1`（上限0.7）

### 4.7 序列化结构 [line 480]

```json
{
  "modulators": {
    "curiosity":   {"value": 0.6, "baseline": 0.55},
    "sociability": {"value": 0.5, "baseline": 0.50},
    "diligence":   {"value": 0.5, "baseline": 0.50},
    "contentment": {"value": 0.5, "baseline": 0.50},
    "energy":      {"value": 0.6, "baseline": 0.55}
  },
  "last_update_time": 1700000000.0,
  "habits": {
    "diary":     {"streak": 5, "total_count": 20, "strength": 0.374, "last_triggered": "2025-01-01"},
    ...
  }
}
```

---

## 5. dream/ — 离线巩固系统

### 5.1 做梦调度

**`DreamScheduler.should_dream()`** [scheduler.py line 158]

触发条件：
1. **夜间睡眠窗口**：`in_sleep_window=True` 时，只要间隔 ≥ `dream_interval_minutes`（默认90分钟）即触发
2. **白天小憩**：`nap_enabled=True` 且 `idle_heartbeat_count >= idle_trigger_heartbeats`（默认10次）且间隔满足

**防重叠**：`_is_dreaming` 标志，`run_dream_cycle` 用 `finally` 保证释放。

**海马体重复抑制**：维护最近 `_DREAM_HISTORY_WINDOW=5` 次的种子标题集合，防止反复做同一梦。

### 5.2 做梦周期完整流程

**`run_dream_cycle(event_history)`** [scheduler.py line 175]

四阶段流水线：

```
AWAKE → NREM → (DreamSeed生成) → REM → DreamScene → WAKING_UP → AWAKE
```

#### Phase 1: NREM — 突触稳态回放

[scheduler.py line 325, snn/core.py line 427]

- 按事件重要性加权采样 `nrem_replay_episodes`（默认3）个片段
  - message事件得分2.0，tool_call 1.5，tool_result(成功) 1.0，heartbeat 0.3
- 每片段：提取8维特征 → `snn.replay_episodes(features, speed_multiplier=5.0)`
  - 内部将 `tau` 缩短5倍（加速回放），执行 STDP 但不更新 EMA / 运行时统计
- 回放后执行 **SHY (突触稳态假说)**：`snn.homeostatic_scaling(nrem_homeostatic_rate=0.02)`
  ```
  W *= (1 - 0.02) = 0.98  # 全局权重缩减2%，弱连接衰减更快
  ```

#### Phase 2: 种子生成

**`DreamSeed`** [seeds.py line 44] 是一组"心理张力"，包含：
- `seed_id`、`seed_type`（DAY_RESIDUE/DREAM_LAG/UNFINISHED_TENSION/SELF_THEME）
- `title`、`summary`
- `affect_valence` [-1,1]、`affect_arousal` [0,1]
- `importance`、`novelty`、`recurrence`、`unfinished_score`、`dreamability`
- `score`（综合打分）
- `core_node_ids`（关联的记忆节点ID）

**4路种子来源** [scheduler.py line 401]:
1. `collect_day_residue(event_history)` — 当日事件残余
2. `collect_unfinished_tension(memory_candidates)` — 未完成的记忆张力
3. `collect_dream_lag(memory_candidates)` — 长期未进入梦境的记忆
4. `collect_self_theme(memory_candidates)` — 自我主题材料
5. `collect_thought_stream_seeds(thought_manager)` — 活跃思考流（额外来源）

选取后最多 `_MAX_DREAM_SEEDS=3` 个，应用 `repetition_decay=0.3` 惩罚最近已梦过的主题。

#### Phase 3: REM — 记忆联想扩散

[scheduler.py line 447, memory/decay.py line 139]

渐进式游走：每轮 `rem_seeds_per_round=5` 个随机种子节点，做 `dream_walk`：

```
activation = {seed_id: 1.0}
for depth in range(rem_max_depth=3):
    for node in frontier:
        for edge in get_edges_from(node, min_weight=0.05):
            propagated = activation[node] * edge.weight * decay_factor^(depth+1)
            if propagated >= 0.1:  # 梦中阈值更低
                activation[neighbor] += propagated
```

**Hebbian 强化**（做梦学习）：对共激活的 top-15 节点两两之间：
```python
# 若边存在:
delta = learning_rate * (1 - old_weight)   # learning_rate=0.05
new_weight = old_weight + delta

# 若边不存在:
新建 ASSOCIATES 边，初始 weight=0.15
```

REM 阶段还执行弱边修剪：`prune_weak_edges(threshold=rem_edge_prune_threshold=0.08)`（仅 ASSOCIATES 类型边）。

#### Phase 4: DreamScene — LLM 生成梦境叙事

[dream/scenes.py]  
调用 `build_dream_scene(seeds, rem_report, event_history)`，通过 LLM（`model_task_name="life"`）生成梦境叙事文本，结构化提取 `DreamResidue`（余韵）。

### 5.3 梦后余韵 (DreamResidue)

**数据结构** [residue.py line 51]:

```python
@dataclass
class DreamResidue:
    summary: str         # 简短摘要（注入 prompt 用）
    life_payload: str    # 给生命中枢心跳的语境
    dfc_payload: str     # 给 DFC 对话模式的语境
    dominant_affect: str # 主导情感
    strength: str        # "light" / "moderate" / "vivid"
    tags: list[str]      # 标签
    expires_at: float    # TTL = 24小时
```

余韵有效期 `_RESIDUE_TTL_SECONDS = 24 * 60 * 60`，通过 `get_active_residue_payload("life"/"dfc")` 在下次心跳时注入 prompt 的 `### 梦后余韵` 部分 [core.py line 1297]。

### 5.4 做梦对各子系统的影响总结

| 影响对象 | 做梦效果 | 触发函数 |
|---------|---------|---------|
| SNN 突触 | NREM 回放强化突触，SHY 全局等比缩减 | `replay_episodes` + `homeostatic_scaling` |
| Memory 图谱 | REM 新增/强化 ASSOCIATES 边，修剪弱边 | `dream_walk` + `prune_weak_edges` |
| 调质层 | 觉醒时精力/好奇心/满足感上调 | `inner_state.wake_up()` |
| 心跳 prompt | 梦后余韵注入 24h | `get_active_residue_payload` |
| DFC | 完整梦记录注入 runtime assistant payload | `inject_dream_report` |

---

## 6. memory/ — 连续记忆网络

### 6.1 节点模型

**类**：`MemoryNode` [memory/nodes.py line 37]

| 字段 | 类型 | 含义 |
|------|------|------|
| `node_id` | str | `"file:<md5_12>"` 或 `"concept:<md5_12>"` |
| `node_type` | NodeType | FILE / CONCEPT |
| `file_path` | str\|None | workspace相对路径（仅FILE） |
| `content_hash` | str\|None | SHA-256前16位 |
| `title` | str | 节点标题 |
| `activation_strength` | float [0,1] | 当前激活强度 |
| `access_count` | int | 访问次数（复习效应） |
| `last_accessed_at` | float\|None | 最后访问 Unix 时间 |
| `emotional_valence` | float [-1,1] | 情感效价 |
| `emotional_arousal` | float [0,1] | 情感唤醒度 |
| `importance` | float [0,1] | 主观重要性 |
| `embedding_synced` | bool | 向量同步状态 |

**节点ID生成** [line 80]:  
`node_id = f"file:{md5(normalize_path(file_path))[:12]}"`  
路径规范化：`\\` → `/`，去掉前导 `./`，`posixpath.normpath`。

**节点访问时激活增量** [line 562]:
```python
activation_strength = MIN(1.0, activation_strength + 0.1)
```

### 6.2 边模型与类型

**类**：`MemoryEdge` [memory/edges.py line 42]

6种边类型 [line 26]:

| 类型 | 方向 | 语义 |
|------|------|------|
| `RELATES` | 双向 | 相关 |
| `CAUSES` | 单向 | 因果 |
| `CONTINUES` | 单向 | 延续 |
| `CONTRASTS` | 双向 | 对比 |
| `MENTIONS` | FILE→CONCEPT | 文件提及概念 |
| `ASSOCIATES` | 双向 | 联想边（动态生成） |

边字段：`weight`、`base_strength`、`reinforcement`（增强量）、`activation_count`、`last_activated_at`、`reason`、`bidirectional`

**ASSOCIATES 边的 Hebbian 更新公式** [memory/decay.py line 289]:
```python
delta = learning_rate * (1 - old_weight)   # 边际递减
new_weight = min(old_weight + delta, 1.0)
reinforcement += delta
activation_count += 1
```
初始 weight=0.15，每次激活趋向1.0，衰减公式（见下节）将其拉回。

### 6.3 衰减算法

#### 节点衰减：Ebbinghaus-inspired [memory/decay.py line 44]

```
time_decay      = exp(-λ * days_since)         # λ = DECAY_LAMBDA = 0.05
                                                # 约14天半衰期
retrieval_bonus = log(1 + access_count) * 0.1  # 复习效应（对数）
emotional_shield = emotional_arousal * 0.2      # 情感保护
importance_shield = importance * 0.1            # 重要性保护

strength = clip(time_decay + retrieval_bonus + emotional_shield + importance_shield, 0, 1)
```

**触发时机**：每日一次，由 `memory_integration.maybe_run_daily_decay()` 在心跳中检查 [service/integrations.py]。上次衰减日期存于 `_last_decay_date` [core.py line 113]。

#### 边衰减（仅 ASSOCIATES 类型）[decay.py line 115]:

```
days_since  = (now - last_activated_at) / 86400
decay_factor = exp(-DECAY_LAMBDA * days_since)   # λ = 0.05
new_weight   = base_strength + reinforcement * decay_factor

if new_weight < PRUNE_THRESHOLD (0.1):
    DELETE edge  # 自动修剪
```

### 6.4 检索策略：三路混合

**`LifeMemoryService.search_memory(query, top_k)`** 内部流程:

```
1. FTS (BM25)        ← SQLite FTS5 全文搜索 [search.py line 224]
   safe_query = '"..转义..."'  # 防止FTS5语法注入
   score = abs(bm25_score) / 10.0  # 归一化

2. Vector (余弦相似度) ← ChromaDB + embedding API [search.py line 153]
   similarity = 1 / (1 + L2_distance)

3. RRF 融合 [search.py line 264]
   score[node_id] = Σ 1 / (RRF_K + rank_i)
   # RRF_K = 60 (常量)

4. 激活扩散 [search.py line 298]
   # 从RRF top节点出发，按边权传播激活
   for depth in range(max_depth=2):
       propagated = activation[node] * edge.weight * spread_decay^(depth+1)
       if propagated >= spread_threshold (0.3): 传播

5. 结果封装为 SearchResult（source="direct"/"associated"）
```

### 6.5 prompting：检索结果注入 prompt

两个路径：

**① 心跳系统 prompt（MEMORY.md）** [memory/prompting.py]:  
工作空间 `MEMORY.md` 解析，结构化分区 `### 持久记忆`、`### 活跃记忆`、`### 淡出记忆`，当文件超 `MEMORY_PROMPT_SOFT_LIMIT_BYTES=8KB` 或条目超限时，在心跳 user prompt 中插入维护提醒。

**② 工具调用后的检索结果注入** [memory/tools.py → nucleus_search_memory]:  
格式：
```
【直接命中的记忆】(N条)
- 标题 [path] (相关度 X.XX | .md | 今天 | 2KB)
  摘要：...
  
【联想扩散结果】(N条)
- 标题 [path] (相关度 X.XX | ...)
  摘要：...
  联想：edge_type: reason / path

💡 提示：以上仅为摘要。如需查看完整内容，可使用 fetch_life_memory 工具读取文件。
```

---

## 7. streams/、drives/、tools/、monitor/ — 辅助系统

### 7.1 streams/ — 思考流

**`ThoughtStreamManager`** [streams/manager.py]

思考流是持久化的"进行中话题"，独立于心跳上下文存在：

**`ThoughtStream` 模型** [streams/models.py]:
- `id`、`title`、`status`（active/dormant/completed）
- `curiosity_score` [0,1]：兴趣浓度，决定休眠优先级
- `advance_count`：推进次数
- `last_advanced_at`、`last_thought`（最后一次推进的内心独白）
- `related_memories`：关联记忆路径

**自动休眠**：超过 `dormancy_threshold_hours=24` 未推进 → 自动转 dormant。  
**活跃上限**：`max_active_streams=5`，超出后将 `curiosity_score` 最低的转入休眠。

**持久化**：`workspace/thoughts/streams.json`（独立于 `life_engine_context.json`）

**心跳注入**：`format_for_prompt(max_items=3)` → `### 当前思考流` 块

### 7.2 drives/ — 冲动引擎

**`ImpulseEngine`** [drives/impulse.py]

基于规则的行为建议系统，将调质状态转化为自然语言建议：

```python
@dataclass
class ImpulseRule:
    name: str
    condition: Callable[[neuromod_state, context], bool]
    suggestion: str          # 自然语言建议
    tools: list[str]         # 推荐工具
    cooldown_minutes: int    # 同一规则冷却时间（默认30分钟）
```

`DEFAULT_RULES`（drives/rules.py）包含如：
- 好奇心 > 阈值(0.65) → 建议探索/搜索
- 社交欲 > 阈值(0.6) + 静默 > 30分钟 → 建议主动说话
- 精力低 → 建议休息

**心跳注入格式**：
```
### 内在冲动

基于你当前的好奇心(75%)和社交欲(60%)：
- 建议：...
（这些只是建议，你可以选择遵循或不遵循。）
```

### 7.3 tools/ — nucleus_* 工具集

所有工具均继承自基类，通过 `execute(**args) -> (success, result)` 接口调用。

| 模块 | 工具名 | 功能 |
|------|--------|------|
| file_tools.py | nucleus_read_file, nucleus_write_file, nucleus_list_files | 工作空间文件读写 |
| grep_tools.py | nucleus_grep_file, nucleus_grep_dir | 文件内容搜索 |
| exec_tools.py | nucleus_exec | 受限代码执行 |
| web_tools.py | nucleus_web_search, nucleus_browser_fetch | Tavily搜索/网页抓取 |
| todo_tools.py | nucleus_list_todos, nucleus_create_todo, nucleus_complete_todo | TODO管理 |
| file_tools.py | nucleus_tell_dfc | 对外发声 |
| schedule_tools.py | nucleus_schedule_reminder | 定时提醒 |
| chat_history_tools.py | LifeEngineFetchChatHistoryTool | 聊天历史检索 |
| event_grep_tools.py | nucleus_grep_life_events | 事件流搜索 |
| memory/tools.py | nucleus_search_memory, nucleus_relate_file, nucleus_update_node | 记忆操作 |
| streams/tools.py | nucleus_create_thought_stream, nucleus_advance_thought_stream | 思考流操作 |

**工作空间沙箱**：所有文件操作限制在 `settings.workspace_path` 内，文件读取上限 10MB，写入上限 5MB [constants.py line 63]。

### 7.4 monitor/ — 可观测性

**`MessageTimelineRouter`** [monitor/router.py line 18]  
挂载在 `/message_timeline`，暴露3个 HTTP 端点：

| 端点 | 功能 |
|------|------|
| `GET /` | 返回 `life_message_dashboard.html` |
| `GET /api/snapshot?event_limit=24&stream_limit=12&message_limit=8` | 联合快照 |
| `GET /api/stream/{stream_id}` | 单流快照 |
| `GET /api/history_search` | 历史检索（转发给 LifeEngineFetchChatHistoryTool） |

快照内容（`get_message_observability_snapshot`）[core.py line 444]:
- `life.state`：完整 LifeEngineState
- `life.inner_state`：调质层完整快照
- `life.pending_events`：待处理事件（截断）
- `life.recent_events`：最近历史事件
- `streams`：各聊天流的消息快照（含最新消息、最近N条历史）
- `summary`：聚合摘要

**WebSocket 广播**（记忆系统）[memory/router.py]:  
`MemoryRouter.broadcast(event_type, payload)` 向所有连接的前端推送记忆图谱变化事件，事件类型包括 `memory.nodes.created`、`memory.nodes.updated`、`memory.edges.updated`、`memory.dream.walk` 等。

---

## 8. 事件代数

### 8.1 LifeEngineEvent 结构

**数据类**：`LifeEngineEvent` [service/event_builder.py line 26]

```python
@dataclass(slots=True)
class LifeEngineEvent:
    # 基础
    event_id: str          # 格式: "msg_{id}" | "hb_{n}_{seq}" | "tool_call_{seq}" | ...
    event_type: EventType  # MESSAGE | HEARTBEAT | TOOL_CALL | TOOL_RESULT
    timestamp: str         # ISO 8601 字符串
    sequence: int          # 单调递增，全局唯一，由 _next_sequence() 生成

    # 来源
    source: str            # 平台名 | "life_engine"
    source_detail: str     # 详细描述，如 "qq | 入站 | 群聊 | 测试群 | 群ID=12345"

    # 内容
    content: str           # 截断到240字符
    content_type: str      # "text" | "heartbeat_reply" | "dfc_message" |
                           # "direct_message" | "proactive_opportunity" |
                           # "chatter_inner_monologue" | "tool_call" | "tool_result"
    
    # 条件字段
    sender: str | None           # 发送者名称（消息事件）
    chat_type: str | None        # "group" | "private" | "discuss"
    stream_id: str | None        # 聊天流ID
    heartbeat_index: int | None  # 心跳编号（-1=摘要事件）
    tool_name: str | None        # 工具名（工具调用事件）
    tool_args: dict | None       # 工具参数
    tool_success: bool | None    # 工具执行结果
```

### 8.2 事件类型详解

| EventType | content_type | 触发来源 | 说明 |
|-----------|-------------|---------|------|
| MESSAGE | text | 聊天平台 | 外部用户消息 |
| MESSAGE | dfc_message | DFC/chatter | 对话模式留言 |
| MESSAGE | direct_message | 命令行 | 直接命令 |
| MESSAGE | proactive_opportunity | proactive插件 | 主动机会事件 |
| HEARTBEAT | heartbeat_reply | life_engine | LLM心跳回复 |
| HEARTBEAT | chatter_inner_monologue | life_chatter | 对话内心独白 |
| TOOL_CALL | tool_call | life_engine | 工具调用记录 |
| TOOL_RESULT | tool_result | life_engine | 工具返回记录 |

### 8.3 序列号机制

`_next_sequence()` [core.py line 266]:
```python
def _next_sequence(self) -> int:
    self._state.event_sequence += 1
    return self._state.event_sequence
```

序列号单调递增，跨重启持久化（存于 `life_engine_context.json` 的 `state.event_sequence`）。启动时恢复历史事件后，序列号设为 `max(persisted, max_seq_in_events)`。

### 8.4 压缩策略 `compress_history`

[service/state_manager.py line 171]

**触发条件**：事件数 > `limit * 0.8`（滚动压缩阈值）

**压缩策略（参考 Claude Code）**：
```
1. 保留最近 60% 的事件完整
2. 将较早的 40% 压缩为一条摘要事件
   摘要内容: 时间范围 + 消息数 + 心跳数 + 工具数 + 发送者集合 + 话题提示
   摘要事件 type=HEARTBEAT, heartbeat_index=-1（特殊标记）
```

**`generate_event_summary(events)`** 统计：消息数、心跳数、工具数、发送者集合、前3个话题。

---

## 9. 持久化

### 9.1 `life_engine_context.json` Schema

**文件路径**：`{workspace_path}/life_engine_context.json`  
**临时文件**：先写 `.tmp` 再 `rename`，保证原子性

**完整 Schema** [state_manager.py line 283]:

```json
{
  "version": 1,
  "state": {
    "heartbeat_count": 42,
    "event_sequence": 1337,
    "last_model_reply_at": "2025-01-01T12:00:00+08:00",
    "last_model_reply": "此刻很安静，但我仍在持续感受...",
    "last_model_error": null,
    "last_wake_context_at": "2025-01-01T11:59:00+08:00",
    "last_wake_context_size": 12,
    "last_external_message_at": "2025-01-01T11:30:00+08:00",
    "last_tell_dfc_at": "2025-01-01T11:55:00+08:00",
    "tell_dfc_count": 8,
    "chatter_context_cursors": {
      "stream_id_abc123": 1200
    }
  },
  "pending_events": [
    {
      "event_id": "msg_98765",
      "event_type": "message",
      "timestamp": "...", "sequence": 1338,
      "source": "qq", "source_detail": "qq | 入站 | 群聊 | ...",
      "content": "...", "content_type": "text",
      "sender": "用户名", "chat_type": "group",
      "stream_id": "group_12345",
      "heartbeat_index": null, "tool_name": null,
      "tool_args": null, "tool_success": null
    }
  ],
  "event_history": [ /* 同上格式 */ ],
  "snn_state": {
    "version": 2,
    "hidden_v": [...], "output_v": [...], "output_ema": [...],
    "syn_in_hid_W": [[...]], "syn_hid_out_W": [[...]],
    /* ... 见3.6节 */
  },
  "neuromod_state": {
    "modulators": {
      "curiosity":   {"value": 0.62, "baseline": 0.55},
      /* ... */
    },
    "last_update_time": 1700000000.0,
    "habits": { /* ... */ }
  },
  "dream_state": {
    /* DreamScheduler.serialize() 结果 */
  }
}
```

### 9.2 `StatePersistence` 存取流程

**保存** [state_manager.py line 263]:
```
1. 获取锁 (self._get_lock())
2. 构建 payload dict（state + pending + history + snn + neuromod + dream）
3. 释放锁
4. 写入 .tmp 文件（ensure_ascii=False, indent=2）
5. .tmp → 目标文件 rename（原子性保证）
```

**恢复** [state_manager.py line 331]:
```
1. 检查文件存在
2. json.loads（失败则返回空）
3. 格式校验（pending/history 必须是 list）
4. event_from_dict 反序列化（event_type 枚举恢复，sequence 恢复）
5. 裁剪 history 到 history_limit
6. 获取锁，更新 state 字段
7. 计算 max_sequence，更新 state.event_sequence
8. 返回 (pending, history, {snn_state, neuromod_state, dream_state})
```

### 9.3 崩溃恢复语义

1. **未完成的写入（.tmp 残留）**：下次读取时 `.tmp` 文件不存在于目标路径，自动降级为空状态。
2. **JSON 格式损坏**：捕获异常，返回 `([], [], {})`，服务以零状态干净启动。
3. **序列号连续性**：`event_sequence = max(persisted_state, max_in_events)`，防止序列号回退。
4. **SNN/neuromod 状态**：单独保存，形状不匹配时记录警告并跳过（不覆盖已初始化状态）。
5. **pending_events 重放**：崩溃时已接收但未被 LLM 处理的事件，恢复后在下一次心跳时重新注入。

---

## 10. 配置参数全表

**文件**：`plugins/life_engine/core/config.py`

### [settings]

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `True` | 是否启用 |
| `heartbeat_interval_seconds` | int | `30` | 心跳间隔（秒） |
| `sleep_time` | str | `""` | 睡觉时间 HH:MM |
| `wake_time` | str | `""` | 苏醒时间 HH:MM |
| `log_heartbeat` | bool | `True` | 心跳日志 |
| `context_history_max_events` | int | `100` | 事件历史上限 |
| `workspace_path` | str | `data/life_engine_workspace` | 工作空间 |
| `max_rounds_per_heartbeat` | int | `3` | 单次心跳最大工具调用轮数 |
| `idle_pause_after_external_silence_minutes` | int | `30` | 外界静默后暂停LLM（0=不暂停） |

### [model]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `task_name` | `"life"` | 模型任务名 |

### [history_retrieval]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `True` | 历史检索工具 |
| `default_cross_stream` | `False` | 默认跨流检索 |
| `adapter_signature` | napcat_adapter... | 适配器签名 |
| `adapter_timeout_seconds` | `8` | 适配器超时 |
| `max_candidate_streams` | `12` | 跨流最大扫描数 |
| `max_scan_rows_per_stream` | `240` | 每流最大扫描行 |
| `tool_default_limit` | `20` | 检索工具默认返回数 |
| `tool_max_limit` | `100` | 检索工具最大返回数 |

### [web]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `tavily_api_key` | `""` | Tavily Key |
| `tavily_api_keys` | `[]` | 多Key轮询 |
| `tavily_base_url` | `https://api.tavily.com` | API地址 |
| `search_timeout_seconds` | `30` | 搜索超时 |
| `extract_timeout_seconds` | `60` | 网页提取超时 |
| `default_search_max_results` | `5` | 默认搜索条数 |
| `default_fetch_max_chars` | `12000` | 网页最大字符 |

### [snn]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `False` | 启用SNN |
| `shadow_only` | `True` | 影子模式（不注入prompt） |
| `tick_interval_seconds` | `10.0` | SNN独立tick间隔 |
| `inject_to_heartbeat` | `False` | 注入心跳prompt |
| `feature_window_seconds` | `600.0` | 特征提取窗口 |

### [neuromod]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `True` | 启用调质层 |
| `inject_to_heartbeat` | `True` | 注入心跳prompt |
| `habit_tracking` | `True` | 习惯追踪 |

### [dream]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `True` | 启用做梦系统 |
| `nrem_replay_episodes` | `3` | NREM回放集数 |
| `nrem_events_per_episode` | `20` | 每集事件数 |
| `nrem_speed_multiplier` | `5.0` | NREM加速倍率 |
| `nrem_homeostatic_rate` | `0.02` | SHY缩减比例 |
| `rem_walk_rounds` | `2` | REM游走轮数 |
| `rem_seeds_per_round` | `5` | 每轮种子数 |
| `rem_max_depth` | `3` | 游走最大深度 |
| `rem_decay_factor` | `0.6` | 游走衰减因子 |
| `rem_learning_rate` | `0.05` | REM Hebbian学习率 |
| `rem_edge_prune_threshold` | `0.08` | 弱边修剪阈值 |
| `dream_interval_minutes` | `90` | 做梦最小间隔 |
| `idle_trigger_heartbeats` | `10` | 白天空闲触发阈值 |
| `nap_enabled` | `True` | 白天小憩做梦 |

### [thresholds]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `external_active_minutes` | `5` | 外部消息活跃窗口（分钟） |
| `idle_warning_threshold` | `5` | 空闲警告阈值（次） |
| `idle_critical_threshold` | `12` | 空闲严重警告阈值 |
| `todo_urgent_days` | `3` | TODO紧急截止天数 |

### [memory_algorithm]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `rrf_k` | `60` | RRF融合参数 |
| `spread_decay` | `0.7` | 激活扩散衰减 |
| `spread_threshold` | `0.3` | 激活扩散阈值 |
| `decay_lambda` | `0.05` | 遗忘衰减系数 |
| `prune_threshold` | `0.1` | 边剪枝阈值 |
| `dream_learning_rate` | `0.05` | 梦境学习率 |

### [chatter]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `False` | 启用对话模式 |
| `mode` | `"enhanced"` | enhanced/classical |
| `max_rounds_per_chat` | `5` | 单轮最大工具调用轮数 |
| `initial_history_messages` | `30` | 首轮注入历史消息数 |

### [streams]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `True` | 启用思考流 |
| `max_active_streams` | `5` | 活跃流上限 |
| `dormancy_threshold_hours` | `24` | 休眠触发时间 |
| `inject_to_heartbeat` | `True` | 注入心跳prompt |

### [drives]

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `True` | 启用冲动引擎 |
| `inject_to_heartbeat` | `True` | 注入心跳prompt |
| `curiosity_threshold` | `0.65` | 好奇心触发阈值 |
| `sociability_threshold` | `0.6` | 社交欲触发阈值 |
| `silence_trigger_minutes` | `30` | 社交冲动静默触发（分钟） |

---

## 11. 可观测性

### 11.1 monitor/router.py

`MessageTimelineRouter` 挂载于 `/message_timeline`，提供：

**`GET /api/snapshot`**（核心端点）返回 JSON：
```json
{
  "generated_at": "ISO时间",
  "life": {
    "state": { /* LifeEngineState全字段 + 派生字段 */ },
    "inner_state": { /* InnerStateEngine.get_full_state() */ },
    "pending_events": [...],
    "recent_events": [...],
    "latest_event": {...}
  },
  "streams": [
    {
      "stream_id": "...",
      "stream_name": "...",
      "platform": "qq",
      "chat_type": "group",
      "is_active": true,
      "unread_count": 3,
      "history_count": 120,
      "latest_message": {...},
      "recent_messages": [...]
    }
  ],
  "summary": {
    "active_stream_count": 3,
    "pending_life_events": 2,
    "recent_life_events": 24,
    "heartbeat_count": 42
  }
}
```

**`GET /api/history_search`** 透传给 `LifeEngineFetchChatHistoryTool`，支持参数：
- `query`、`stream_id`、`cross_stream`、`limit`、`source_mode`（auto/local_db/napcat）、`include_tool_calls`

### 11.2 SNN 健康信息

通过 `snapshot()` 暴露 `snn_health` [core.py line 342]，包含 tick 计数、驱动值、膜电位统计、突触权重统计、EMA 均值等（见3.3节 `get_health()` 返回结构）。

### 11.3 记忆系统 WebSocket 广播

`memory/router.py` 中 `MemoryRouter.broadcast(event_type, payload)` 向前端推送实时记忆图谱变化，支持可视化节点激活和边强化过程。

---

## 12. 未解之谜

以下是考古过程中发现的模糊点、潜在代码不一致或文档缺失之处：

### 12.1 SNN shadow_only 模式的实际效果

- **问题**：`snn.enabled=False`（默认）且 `shadow_only=True` 时，代码注释 [core.py line 1313] 说"SNN 已降级为 shadow 模式，不注入 prompt"，但 `snn.enabled` 默认是 `False`，也就是说 **SNN 默认根本不运行**。`shadow_only=True` 只有在 `snn.enabled=True` 时才有意义，但这个组合的实际生产使用情况不明。

### 12.2 `idle_heartbeat_count` 与做梦的确切关系

- **问题**：`idle_heartbeat_count` 在无工具调用但有活跃思考流时**不增加** [core.py line 1699]，但做梦的白天触发条件是 `idle_count >= idle_trigger_heartbeats=10`。这意味着思考流活跃时永远不会触发白天小憩做梦，但代码并未明确文档化这个行为是否是设计意图。

### 12.3 `DreamScheduler.serialize()` 的完整结构未见

- **问题**：`state_manager.py` 调用 `dream_scheduler.serialize()` [line 316]，但 `dream/scheduler.py` 中 `DreamScheduler` 类的 `serialize/deserialize` 方法在本次考古的代码切片中未找到完整实现（文件较大，可能在未阅读的后半部分）。`life_engine_context.json` 中 `dream_state` 字段的完整 schema 因此未能确认。

### 12.4 奖赏信号的双重定义

- **问题**：`compute_reward` [bridge.py line 189] 根据工具调用结果计算奖赏。但同时 `replay_episodes` [snn/core.py line 427] 的 `reward_signal` 参数固定为 `0.0` [scheduler.py line 341]，意味着 NREM 回放时**奖赏信号被完全忽略**。这与 STDP 学习理论（奖赏调制）不一致，原因不明。

### 12.5 `tool_event_count` 统计方式存在疑问

- **问题**：在 `_run_heartbeat_model` 中 [line 1687]，每执行一个工具调用，`tool_event_count += 2`（工具调用 + 工具结果各算1个）。但传入 `snn_bridge.record_heartbeat_result` 的 `tool_event_count` 是这个双倍值，而奖赏计算 [bridge.py line 198] 再次做了 `tool_calls = tool_event_count // 2`。这是正确的但绕弯，代码可读性差。

### 12.6 `Modulator.decay_rate` 字段在 ODE 中的角色

- **问题**：`Modulator` 定义了 `decay_rate: float = 0.001`，但在 `update()` 函数 [line 42] 中，回归基线的项为 `decay = self.decay_rate * (self.baseline - self.value) * dt`。这在数学上是 **一阶线性 ODE 的欧拉离散化**：  
  `dV/dt = decay_rate * (baseline - V)`，稳态为 `V = baseline`，时间常数为 `τ = 1/decay_rate = 1000`秒。  
  然而 `Modulator` 同时有 `tau: float = 1800.0` 字段但在 `update()` 中**根本没有使用** `self.tau`！实际时间常数由 `decay_rate=0.001` 决定（约16.7分钟），而非 `tau` 所记录的时间。`tau` 字段是历史遗留还是另有用途，文档缺失。

### 12.7 记忆衰减与 heartbeat_count 的解耦

- **问题**：`maybe_run_daily_decay()` 基于日期字符串（`_last_decay_date`）判断，而非 heartbeat 计数。但 `_last_decay_date` 初始化为 `None` [core.py line 113]，且**不在** `life_engine_context.json` 中持久化。这意味着每次重启都会在第一次心跳时执行一次记忆衰减，无论上次衰减是否刚刚发生。

### 12.8 `_event_belongs_to_life_runtime` 的过滤逻辑

- **问题**：[core.py line 1022] 中，MESSAGE 类型且 `content_type="text"` 且与当前 stream_id 相同的事件会被**排除**在 chatter 运行态注入之外（`return content_type != "text"`）。这意味着同一聊天流的普通聊天消息不会出现在 life_chatter 的运行态快照中，原因可能是防止重复（聊天框架已有消息），但逻辑略显反直觉，无注释说明。

### 12.9 `DreamSeedType.SELF_THEME` 的收集逻辑

- **问题**：`collect_self_theme(memory_candidates)` [seeds.py] 的具体收集逻辑未在本次阅读的代码中发现完整实现，种子是如何判定为"自我主题"的标准未知（可能基于文件路径规则，如包含 "SELF" 或 "reflection"）。

### 12.10 `nucleus_exec` 的安全边界

- **问题**：`tools/exec_tools.py` 中的 `nucleus_exec` 允许执行代码，但受限范围（沙箱机制、可执行语言、超时限制）在本次考古中未完整阅读，存在安全隐患的潜在风险需要进一步核实。

---

*报告由 code-archaeologist 基于静态代码分析生成，不含运行时观测数据。*  
*覆盖文件：service/core.py (2155行), snn/core.py, snn/bridge.py, neuromod/engine.py, dream/scheduler.py, dream/seeds.py, dream/residue.py, memory/nodes.py, memory/edges.py, memory/decay.py, memory/search.py, memory/prompting.py, service/state_manager.py, service/event_builder.py, service/integrations.py, core/config.py, constants.py, drives/impulse.py, streams/manager.py, monitor/router.py*
