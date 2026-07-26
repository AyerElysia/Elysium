# SNN v2 + 神经调质层实施报告

> 日期: 2026-04-11  
> 版本: v3.4.0 (SNN v2 + Neuromod Layer)

## 背景

### 问题诊断

对运行中的 SNN v1 系统进行了深度诊断，分析了 773 条审计日志，发现：

| 问题 | 严重程度 | 影响 |
|------|---------|------|
| 权重冻结 | 🔴 致命 | 所有突触权重完全相同至 5 位小数 |
| exploration_drive 永久抑制 | 🔴 致命 | 持续锁定在 -0.38 |
| 零输入 tick 调用 full step() | 🔴 致命 | 85% 的 STDP 更新基于纯噪声 |
| 隐藏层放电率仅 3.6% | 🟡 严重 | 二值 STDP 几乎无法触发 |
| 同态调节越限 | 🟡 严重 | input_gain=4.0, threshold=0.08 均触顶 |
| 固定阈值不适应 | 🟠 中等 | 6 个输出维度用相同绝对阈值 |

**根因**：v1 的 tick loop 每 10 秒调用一次 `step(zero_vector)`，导致 STDP 在噪声上学习。权重向均匀分布坍缩，同态调节系统全力补偿直到参数越限。

## 实施内容

### 1. SNN Core v2 (`snn_core.py`)

**完全重写。** 核心改动：

- **`decay_only()` 方法**：新增。只衰减膜电位和迹变量，不注入电流、不检查脉冲、不触发 STDP。tick loop 改为调用此方法。
- **`step()` 仅在真实事件时调用**：只有心跳期间有真实输入才执行完整的前向传播 + 学习。
- **软 STDP**：用 sigmoid(membrane_potential) 作为连续活动量 [0, 1]，替代二值脉冲。学习门限：sum(activity) > 0.05。即使放电率很低也能学习。
- **噪声注入**：每次 step 时注入 σ=0.08 的高斯噪声到隐藏层电流，促进探索。
- **动态 z-score 阈值**：输出层维护运行均值/方差（EMA α=0.01），阈值基于 z-score 动态调整，不再使用固定的 [0.6, 0.3, -0.3]。
- **更温和的同态调节**：threshold_lr 0.01→0.005, gain_lr 0.2→0.08，收窄 clipping 范围。
- **`real_step_count`**：新增计数器，区分真实输入步数和总 tick 数。

### 2. 神经调质层 (`neuromod.py`)

**全新模块。** 三大子系统：

**ModulatorSystem** — 5 种神经调质因子：
| 调质因子 | 时间常数 τ | 基线 | 作用 |
|---------|-----------|------|------|
| 好奇心 curiosity | 1800s (30min) | 0.60 | 驱动探索、搜索 |
| 社交欲 sociability | 3600s (1h) | 0.50 | 驱动聊天、互动 |
| 专注力 diligence | 5400s (1.5h) | 0.50 | 驱动任务完成 |
| 满足感 contentment | 1800s (30min) | 0.50 | 正反馈信号 |
| 精力 energy | 10800s (3h) | 0.50 | 整体活跃度 |

ODE 更新公式：
```
dM/dt = (baseline - M) / τ + stimulus × headroom
headroom = 1.0 - M  (when stimulus > 0)
         = M         (when stimulus < 0)
```

headroom 因子实现了"边际递减效应"——已经很高的值难以继续升高。

**HabitTracker** — 6 种习惯：
- 写日记、回顾记忆、网络搜索、完成任务、主动聊天、文件整理
- 追踪连续天数（streak）和频率
- 强度公式：0.6 × streak_bonus + 0.4 × freq_bonus

**昼夜节律**：
- 精力：双高斯峰（10:00 和 15:00），夜间低谷
- 社交欲：傍晚峰值（19:00），凌晨低谷

### 3. SNN 桥接层更新 (`snn_bridge.py`)

- 新增 `extract_event_stats()` 从事件流提取统计供调质层使用
- `format_drive_for_prompt()` 加入 `【SNN快层】` 前缀

### 4. 服务层集成 (`service.py`)

- tick loop：`decay_only()` 替代 `step(zero_input)`
- 心跳前：SNN `step(real_input)` + 调质层 `tick()`
- 心跳后：奖励信号 + 习惯追踪
- 持久化：SNN + 调质层序列化/反序列化
- 健康端点：输出完整 SNN + 调质层状态

### 5. API 路由更新 (`snn_router.py`)

- `GET /snn/` — 可视化仪表盘
- `GET /snn/api/state` — 完整系统状态 JSON（SNN + 调质 + 桥接）
- `GET /snn/api/weights` — 突触权重矩阵 + 统计

### 6. 可视化仪表盘 (`snn_dashboard.html`)

完全重建，包含：
- SNN 六维驱动条形图（带离散等级标签）
- 调质因子浓度百分比 + 等级标签
- 隐藏层（16 个）+ 输出层（6 个）神经元膜电位可视化
- 突触权重热力图（input→hidden, hidden→output）
- 驱动历史折线图（最近 60 个采样）
- 调质浓度历史折线图
- 习惯标签（强/形成中）
- 昼夜节律时钟 + 精力/社交基线
- 系统参数面板（阈值、增益、放电率、权重统计）
- 3 秒自动刷新

### 7. 配置 (`config.py` + `config.toml`)

新增 `[neuromod]` 配置段：
```toml
[neuromod]
enabled = true
inject_to_heartbeat = true
habit_tracking = true
```

## 测试结果

### 权重学习验证 ✅
```
30 次真实输入步骤后权重变化量: 11.2004 (v1: ~0.0000)
100 次 decay_only 后权重变化量: 0.000000 (确认 decay 不影响权重)
```

### 驱动多样性验证 ✅
```
50 步后驱动值:
  task_drive: -1.5549 (抑制)
  exploration_drive: -0.2421 (抑制)
  social_drive: 0.029 (低)
  arousal: 0.0048 (低)
→ 至少 2 种不同离散等级 (v1: 所有维度相同)
```

### 序列化往返验证 ✅
```
SNN serialize/deserialize match: True
Neuromod serialize/deserialize match: True
```

### 调质层响应验证 ✅
```
初始: curiosity=0.600, energy=0.600
60s tick + 5条消息后: curiosity=0.630, energy=0.632
→ 调质因子对刺激有响应
```

## 文件清单

| 文件 | 状态 | 行数 |
|------|------|------|
| `plugins/life_engine/snn_core.py` | 完全重写 | ~370 |
| `plugins/life_engine/neuromod.py` | 新建 | ~440 |
| `plugins/life_engine/snn_bridge.py` | 重写 | ~300 |
| `plugins/life_engine/snn_router.py` | 重写 | ~70 |
| `plugins/life_engine/static/snn_dashboard.html` | 重建 | ~420 |
| `plugins/life_engine/service.py` | 多处修改 | ~2360 |
| `plugins/life_engine/config.py` | 新增段 | +15 |
| `config/plugins/life_engine/config.toml` | 新增段 | +5 |
| `Abstract/智能不是模型而是系统.md` | 新建 | ~100 |
| `notion/SNN_系统诊断与方向重新审视.md` | 先前提交 | ~200 |
| `plan/2026-04-11_snn_修复与调质层方案.md` | 先前提交 | ~150 |

## 已知限制与后续方向

1. **习惯追踪需要多日运行才能看到效果** — 当前测试只覆盖了即时响应
2. **z-score 阈值冷启动期** — 前 ~50 个真实步骤中运行均值/方差不稳定
3. **调质→SNN 反向通道尚未实现** — 当前调质层是 SNN 的下游消费者，未来应形成闭环
4. **中枢主动唤醒 DFC 未实现** — 架构已预留可能性
5. **SNN 拓扑固定** — 未来可探索动态增删神经元

---

*Report generated for Elysia Life Engine, 2026-04-11*
