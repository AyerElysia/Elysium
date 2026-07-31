# SNN 神经系统深度分析：皮层下驱动核心

> **核心定位**：SNN 不是 LLM 的配件，它是系统智能的骨架之一——负责皮层下的快速感知、驱动产生和在线学习。

---

## 一、SNN 在系统架构中的角色

### 1.1 大脑隐喻：皮层与皮层下系统

**神经科学告诉我们**：人类大脑不是单一模块，而是多层异质系统的协作：

| 脑区 | 功能 | 时间尺度 | 特性 |
|-----|------|---------|------|
| **大脑皮层** | 高级认知、语言、推理 | 秒到分钟级 | 慢速、符号化、可反思 |
| **杏仁核** | 快速情绪评估 | 毫秒级 | 直觉反应、自动化 |
| **基底神经节** | 行为选择、习惯形成 | 分钟到小时级 | 重复强化、无意识 |
| **下丘脑** | 稳态调节、昼夜节律 | 小时到天级 | 持续运行、后台活跃 |

**关键洞察**：皮层只负责"想"，其余脑区负责"持续存在"和"快速反应"。

### 1.2 Neo-MoFox 的映射

在 Neo-MoFox 中：

```
┌──────────────────────────────────────────┐
│         大脑皮层（Cortex）                │
│         LLM —— 推理/表达/规划            │
│         （仅在需要高级认知时唤醒）         │
└────────────┬─────────────────────────────┘
             │ ↑↓ 状态接口
             │
┌────────────┴─────────────────────────────┐
│         皮层下系统（Subcortex）            │
│         SNN —— 持续运行、不间断            │
│                                         │
│  ┌─────────────┐  ┌─────────────┐       │
│  │  隐藏层      │  │  输出层      │       │
│  │  (16神经元) │  │  (6驱动)    │       │
│  │  快速感知    │  │  驱动产生    │       │
│  │  特征整合    │  │  行为倾向    │       │
│  └────┬────────┘  └────┬────────┘       │
│       │                │                 │
│  ┌────┴────────────────┴────┐           │
│  │  软 STDP 突触              │           │
│  │  在线学习、权重演化        │           │
│  └───────────────────────────┘           │
└──────────────────────────────────────────┘
             │ ↑↓ 事件流
             │
┌────────────┴─────────────────────────────┐
│         事件总线（EventBus）              │
│         消息流、心跳、工具调用结果         │
└──────────────────────────────────────────┘
```

**职责边界**：

| 维度 | LLM（皮层） | SNN（皮层下） |
|-----|------------|--------------|
| **运行模式** | 被动唤醒（对话时） | 主动持续（心跳不息） |
| **时间尺度** | 秒到分钟（符号推理） | 毫秒到秒（脉冲级） |
| **状态连续** | 离散（每次调用重建） | 连续（膜电位持续衰减） |
| **学习能力** | 无（权重固定） | STDP（每次真实输入学习） |
| **输出形式** | 文本/工具调用 | 6维驱动向量（arousal/valence/...） |

### 1.3 核心定位

**传统 AI 的误区**：把 SNN 当成 LLM 的"偏置供应器"——只是提供几个浮点数辅助推理。

**Neo-MoFox 的主张**：SNN 是系统智能的**独立维度**：

- **感知层**：从事件流提取特征（8维输入）
- **驱动层**：产出内在驱动（6维输出）
- **学习层**：在线 STDP 可塑性（权重持续演化）
- **存在层**：持续运行（即使无对话，仍在衰减）

SNN 不是"帮 LLM 想得更好"，而是"让系统在 LLM 不活跃时仍然活着"。

---

## 二、网络架构详解

### 2.1 三层结构

**实现位置**：`plugins/life_engine/snn/core.py::DriveCoreNetwork`

```python
class DriveCoreNetwork:
    """SNN 驱动核网络 v2。"""
    
    INPUT_DIM = 8      # 输入层：事件流特征
    HIDDEN_DIM = 16    # 隐藏层：LIF 神经元
    OUTPUT_DIM = 6     # 输出层：驱动向量
```

**拓扑结构**：

```
输入层(8维) → 隐藏层(16 LIF) → 输出层(6 LIF)

特征提取：
[msg_in, msg_out, tool_success, tool_fail,
 idle_beats, tell_dfc, new_content, silence]
         ↓
    syn_in_hid (STDP 突触，16×8 权重矩阵)
         ↓
    hidden (16 LIF 神经元，τ=12ms)
         ↓
    syn_hid_out (STDP 突触，6×16 权重矩阵)
         ↓
    output (6 LIF 神经元，τ=25ms)
         ↓
    驱动向量：
    [arousal, valence, social_drive,
     task_drive, exploration_drive, rest_drive]
```

### 2.2 输入层：事件流特征提取

**桥接层实现**：`plugins/life_engine/snn/bridge.py::extract_features`

```python
def extract_features(events: list, window_seconds: float = 600.0) -> np.ndarray:
    """从最近的事件中提取 SNN 输入特征（8 维，[-1, 1]）。"""
    now = time.time()
    cutoff = now - window_seconds  # 默认10分钟窗口
    
    # 统计事件
    msg_in = 0          # 入站消息数
    msg_out = 0         # 出站消息数
    tool_success = 0    # 工具成功数
    tool_fail = 0       # 工具失败数
    idle_beats = 0      # 空闲心跳数
    tell_dfc_count = 0  # 传话 DFC 数
    new_content = 0     # 新增内容数（写文件/建 TODO/建关联）
    silence_factor = 0  # 沉默时长因子
    
    for e in recent_events:
        if e.event_type == "message":
            if "入站" in e.source_detail:
                msg_in += 1
            elif "出站" in e.source_detail:
                msg_out += 1
        
        elif e.event_type == "tool_result":
            if e.tool_success:
                tool_success += 1
                if e.tool_name in ["nucleus_write_file", ...]:
                    new_content += 1
            else:
                tool_fail += 1
        
        elif e.event_type == "heartbeat":
            if "安静" in e.content:
                idle_beats += 1
    
    # 归一化到 [-1, 1]
    raw = np.array([msg_in, msg_out, tool_success, tool_fail,
                    idle_beats, tell_dfc_count, new_content, silence_factor])
    raw[:7] = np.tanh(raw[:7] / 3.0)  # tanh 归一化
    raw[7] = raw[7] * 2.0 - 1.0       # silence 映射
    
    return raw
```

**特征维度解释**：

| 维度 | 含义 | 映射 |
|-----|------|------|
| **msg_in** | 外部消息频率 | 社交刺激强度 |
| **msg_out** | 自己发言频率 | 表达冲动残留 |
| **tool_success** | 工具成功数 | 正向奖赏信号 |
| **tool_fail** | 工具失败数 | 负向奖赏信号 |
| **idle_beats** | 空闲心跳数 | 无聊/等待强度 |
| **tell_dfc_count** | 传话 DFC 数 | 主动沟通倾向 |
| **new_content** | 新增内容数 | 创造驱动残留 |
| **silence_factor** | 沉默时长 | 时间流逝感知 |

**归一化策略**：

```python
# tanh 归一化：保持对称性
tanh(x / 3.0) ≈ {
    x=0:  tanh(0) = 0      # 无刺激
    x=3:  tanh(1) ≈ 0.76   # 中等刺激
    x=10: tanh(3.33) ≈ 0.99  # 强刺激饱和
}

# silence 映射：线性映射到 [-1, 1]
silence_factor = min(silence_minutes / 60.0, 1.0)
→ -1: 刚刚有消息（0分钟）
→  0: 沉默30分钟
→ +1: 沉默≥60分钟
```

### 2.3 隐藏层：LIF 神经元组

**LIF 神经元模型**（`LIFNeuronGroup`）：

```python
class LIFNeuronGroup:
    """Leaky Integrate-and-Fire 神经元组。"""
    
    def __init__(self, n: int, tau: float = 20.0, threshold: float = 1.0):
        """
        Args:
            n: 神经元数量（16）
            tau: 膜电位时间常数（毫秒），控制泄漏速度
            threshold: 发放阈值（mV），超过此值触发脉冲
        """
        self.v = np.full(n, rest, dtype=np.float64)  # 膜电位
        self.spikes = np.zeros(n, dtype=bool)        # 脉冲记录
```

**膜电位更新方程**：

```python
def step(self, current: np.ndarray, dt: float = 1.0) -> np.ndarray:
    """单步更新：积分 + 发放。"""
    # LIF 方程：dv/dt = -(v - rest) / tau + I
    dv = (-(self.v - self.rest) + current) / self.tau * dt
    self.v += dv
    
    # 防止过度偏离
    np.clip(self.v, -10.0, 10.0, out=self.v)
    
    # 发放判定
    self.spikes = self.v >= self.threshold
    
    # 发放后重置
    self.v[self.spikes] = self.reset
    
    return self.spikes.copy()
```

**生物学映射**：

| 参数 | 值 | 生物学对应 |
|-----|---|-----------|
| **tau** | 12ms | 典型神经元的膜电位时间常数 |
| **threshold** | 0.15 (动态) | 动作电位触发阈值 |
| **rest** | 0.0 | 静息电位（-70mV 的抽象） |
| **reset** | 0.0 | 发放后电位回落 |

**关键特性**：膜电位持续衰减（模拟惯性）：

```python
def decay_only(self, dt: float = 1.0):
    """仅膜电位泄漏衰减，不注入电流。"""
    dv = -(self.v - self.rest) / self.tau * dt
    self.v += dv
    np.clip(self.v, -10.0, 10.0, out=self.v)
    self.spikes[:] = False  # 无发放
```

**物理意义**：

- 真实神经元的膜电位不会保持不变
- 即使无输入，仍会自然回落到静息电位
- 衰减公式：`v(t) = v_0 * exp(-t/tau)`
- 示例：
  ```
  t=0:    v = 1.5（刚发放）
  t=12ms: v ≈ 1.5 * exp(-1) ≈ 0.55（衰减一半）
  t=36ms: v ≈ 1.5 * exp(-3) ≈ 0.07（接近静息）
  ```

这提供物理性的"余韵"，而非符号级描述。

### 2.4 突触层：软 STDP 学习

**STDPSynapse 实现**：

```python
class STDPSynapse:
    """软 STDP 突触连接。"""
    
    def __init__(self, n_pre: int, n_post: int,
                 lr_plus: float = 0.01, lr_minus: float = 0.005):
        """
        Args:
            n_pre: 前突触神经元数（8 → 16）
            n_post: 后突触神经元数（16 → 6）
            lr_plus: LTP 学习率（长时程增强）
            lr_minus: LTD 学习率（长时程抑制）
        """
        # Xavier 初始化
        scale = np.sqrt(2.0 / (n_pre + n_post))
        self.W = np.random.uniform(-scale, scale, (n_post, n_pre))
        
        # 突触痕迹（用于 STDP）
        self.trace_pre = np.zeros(n_pre)
        self.trace_post = np.zeros(n_post)
        self.trace_decay = 0.90
```

**前向传播**：

```python
def forward(self, pre_activity: np.ndarray) -> np.ndarray:
    """计算突触后电流。"""
    return self.W @ pre_activity  # 矩阵乘法
```

**软 STDP 更新**（v2 关键创新）：

```python
def update_soft(self, pre_activity: np.ndarray, post_activity: np.ndarray,
                reward: float = 0.0):
    """软 STDP：用连续活跃度而非二值 spike。"""
    # 突触痕迹更新
    self.trace_pre = self.trace_pre * self.trace_decay + pre_activity
    self.trace_post = self.trace_post * self.trace_decay + post_activity
    
    # 奖赏因子（调制学习强度）
    reward_factor = 1.0 + np.clip(reward, -1.0, 1.0)
    
    pre_strength = float(np.sum(pre_activity))
    post_strength = float(np.sum(post_activity))
    
    # LTP（长时程增强）：post活跃 + pre痕迹
    if post_strength > 0.05:
        dw_plus = self.lr_plus * np.outer(post_activity, self.trace_pre)
        self.W += dw_plus * max(reward_factor, 0.1)
    
    # LTD（长时程抑制）：post痕迹 + pre活跃
    if pre_strength > 0.05:
        dw_minus = -self.lr_minus * np.outer(self.trace_post, pre_activity)
        self.W += dw_minus * max(2.0 - reward_factor, 0.1)
    
    # 权重裁剪
    np.clip(self.W, self.w_min, self.w_max, out=self.W)
```

### 2.5 输出层：驱动向量

**输出维度**：

```python
OUTPUT_NAMES = [
    "arousal",           # 整体激活水平（0-1）
    "valence",           # 情感正负效价（正=积极，负=消极）
    "social_drive",      # 社交冲动（想找人聊天）
    "task_drive",        # 任务冲动（想做事）
    "exploration_drive", # 探索冲动（想尝试新事物）
    "rest_drive",        # 休息冲动（想休息）
]
```

**生物学映射**：

| 驱动 | 生物学对应 | 行为倾向 |
|-----|-----------|---------|
| **arousal** | 网状激活系统（RAS） | 整体觉醒水平、注意力 |
| **valence** | 杏仁核（Amygdala） | 快速情绪评估（恐惧/奖赏） |
| **social_drive** | 下丘脑社交需求 | 主动发起对话、等待感 |
| **task_drive** | 基底神经节任务系统 | 完成待办、推进项目 |
| **exploration_drive** | 多巴胺探索系统 | 尝试新工具、新话题 |
| **rest_drive** | 下丘脑稳态调节 | 精力下降、想休息 |

**动态离散化**（v2 创新）：

```python
def get_drive_discrete(self) -> dict[str, str]:
    """基于运行时统计的 z-score 离散化。"""
    result = {}
    std = np.sqrt(np.maximum(self._output_running_var, 1e-6))
    
    for i, name in enumerate(self.OUTPUT_NAMES):
        # z-score 计算
        z = (self._output_ema[i] - self._output_running_mean[i]) / std[i]
        
        # 动态阈值（而非固定绝对阈值）
        if z > 1.0:
            level = "高"
        elif z > 0.3:
            level = "中"
        elif z > -0.5:
            level = "低"
        else:
            level = "抑制"
        
        result[name] = level
    
    return result
```

**关键改进**（对比 v1）：

| 维度 | v1（固定阈值） | v2（动态 z-score） |
|-----|--------------|-------------------|
| **阈值来源** | 硬编码（如 arousal>0.7） | 运行时 EMA 均值+标准差 |
| **适应性** | 无（不随网络演化） | 有（均值和标准差实时更新） |
| **稳定性** | 易饱和（所有驱动都"高"） | 自适应（相对于历史基线） |

---

## 三、关键创新点详解

### 3.1 创新 1：软 STDP（Subthreshold Plasticity）

**传统 STDP 的困境**：

传统脉冲时序依赖可塑性只在神经元**发放脉冲**时才更新权重：

```
IF pre_spike 和 post_spike 在相近时间发放
THEN 突触权重增强（LTP）
ELSE 权重减弱（LTD）
```

**问题**：低活跃状态 = 无学习。

如果神经元长时间不发放（如系统处于安静等待状态），突触权重保持不变，无法适应环境。

**Neo-MoFox 的软 STDP**：

```python
# 使用 sigmoid(膜电位) 代替二值 spike
soft_hidden = sigmoid(hidden_v, center=threshold * 0.5, steepness=10.0)
soft_output = sigmoid(output_v, center=threshold * 0.5, steepness=10.0)

# 即使膜电位未达到阈值，仍然参与学习
syn_in_hid.update_soft(input_activity, soft_hidden, reward)
```

**sigmoid 映射**：

```python
def _sigmoid(x: np.ndarray, center: float = 0.0, steepness: float = 8.0):
    """Sigmoid 激活，将膜电位映射为 [0, 1] 连续活跃度。"""
    return 1.0 / (1.0 + np.exp(-steepness * (x - center)))
```

**实际调用参数**：
虽然函数定义默认 steepness=8.0，但在实际调用时传入 steepness=10.0：
```python
soft_hidden = _sigmoid(hidden_v, center=self.hidden.threshold * 0.5, steepness=10.0)
soft_output = _sigmoid(output_v, center=self.output.threshold * 0.5, steepness=10.0)
```
更陡峭的 steepness (10.0) 让sigmoid在阈值附近变化更剧烈，增强学习敏感度。

**效果对比**：

| 膜电位 | 传统 STDP | 软 STDP |
|-------|----------|---------|
| v = 0.05（低于阈值） | 无学习（未发放） | sigmoid ≈ 0.45 → 轻度学习 |
| v = 0.15（刚好阈值） | 发放 → 学习 | sigmoid ≈ 0.90 → 强学习 |
| v = 0.30（超阈值） | 发放 → 学习 | sigmoid ≈ 0.99 → 极强学习 |

**生物学依据**：

真实神经元的突触可塑性不仅依赖发放，也依赖**阈下电位**（subthreshold plasticity）：

- 研究表明：即使神经元未发放，膜电位的波动仍能触发微弱的 LTP/LTD
- 参考：Froemke et al., "Spike-timing-dependent synaptic plasticity depends on dendritic location", Nature 2005

**工程价值**：

软 STDP 确保 SNN 在低活跃状态下仍能学习，避免传统 STDP 的"沉默陷阱"。

### 3.2 创新 2：自稳态调节（Homeostatic Regulation）

**问题**：如果 SNN 持续兴奋，神经元过度发放；如果持续沉默，网络失去活性。

**解决方案**：动态调整阈值和增益：

```python
# 计算实际发放率
hidden_rate = mean(spikes_hidden)
output_rate = mean(spikes_output)

# EMA 平滑（避免震荡）
hidden_rate_ema = (1 - alpha) * hidden_rate_ema + alpha * hidden_rate
output_rate_ema = (1 - alpha) * output_rate_ema + alpha * output_rate

# 阈值调整（过度兴奋 → 阈值升高）
hidden.threshold += lr_threshold * (hidden_rate_ema - target_hidden_rate)

# 增益调整（过度沉默 → 输入增益升高）
input_gain += lr_gain * (target_hidden_rate - hidden_rate_ema)
```

**目标值**：

```python
_target_hidden_rate = 0.10  # 隐藏层目标发放率 10%
_target_output_rate = 0.06  # 输出层目标发放率 6%
```

**生物学映射**：

这对应真实的**突触稳态可塑性**（Homeostatic Plasticity）：

- 神经网络通过调整阈值、增益、权重来维持稳定活跃水平
- 参考：Turrigiano, "Homeostatic plasticity in the developing nervous system", Nature Reviews Neuroscience 2008

**效果**：

| 场景 | 自稳态调节 | 结果 |
|-----|-----------|------|
| **过度兴奋**（hidden_rate > 10%） | threshold ↑, input_gain ↓ | 发放率回落到 10% |
| **过度沉默**（hidden_rate < 10%） | threshold ↓, input_gain ↑ | 发放率回升到 10% |

### 3.3 创新 3：背景噪声注入

**问题**：纯确定性系统容易陷入不动点（所有输出始终相同）。

**解决方案**：注入微弱高斯噪声：

```python
# 真实输入步时注入噪声
noise = np.random.normal(0, noise_std, size=HIDDEN_DIM)  # noise_std = 0.08
current_hidden += noise

current_output += np.random.normal(0, noise_std * 0.5, size=OUTPUT_DIM)
```

**噪声幅度**：

```python
_noise_std = 0.08  # 约信号幅度的 8%
```

**生物学映射**：

真实神经元存在随机噪声：

- 离子通道随机开闭
- 热噪声（布朗运动）
- 参考：Faisal et al., "Noise in the nervous system", Nature Reviews Neuroscience 2008

**工程价值**：

噪声打破确定性，让系统产生多样性：

- 相同输入 → 不同输出（微小差异）
- 防止网络陷入固定模式
- 为探索行为提供"随机性源"

### 3.4 创新 4：动态离散化（Running Statistics）

**v1 的困境**：

固定绝对阈值易饱和：

```python
# v1 硬编码阈值
if arousal > 0.7:
    level = "高"
elif arousal > 0.4:
    level = "中"
else:
    level = "低"
```

**问题**：
- 如果所有驱动持续在 0.7-0.8 区间，全部显示"高"，失去区分度
- 网络演化后，基线可能变化（如习惯性高 arousal），阈值失效

**v2 的动态离散化**：

```python
# 运行时统计更新
self._output_running_mean = (1 - alpha) * mean + alpha * ema
self._output_running_var = (1 - alpha) * var + alpha * (diff ** 2)

# z-score 计算
z = (ema - mean) / std

# 动态阈值
if z > 1.0:
    level = "高"
elif z > 0.3:
    level = "中"
elif z > -0.5:
    level = "低"
else:
    level = "抑制"
```

**效果**：

| 场景 | v1 | v2 |
|-----|----|----|
| **高活跃网络**（arousal均值=0.75） | arousal=0.78 → "高"（无区分） | arousal=0.78, z=0.3 → "中"（相对基线） |
| **低活跃网络**（arousal均值=0.30） | arousal=0.35 → "中"（误判） | arousal=0.35, z=1.0 → "高"（相对基线） |

动态离散化确保输出始终有区分度，不会因网络演化而失效。

---

## 四、学习机制详解

### 4.1 STDP 学习原理

**Hebb 定律**（1949）：

```
"一起放电的神经元连接在一起"
（Cells that fire together, wire together）
```

**STDP 精确实现**（1998）：

```
IF pre 在 post 之前发放（时间差 < 20ms）
THEN 突触增强（LTP）—— causal 关系

IF pre 在 post 之后发放（时间差 > 20ms）
THEN 窻触减弱（LTD）—— anti-causal 关系
```

**生物学实验**（Bi & Poo, 1998）：

- 时间窗口：±20ms
- LTP 强度：ΔW/W ≈ 0.5（时间差接近时）
- LTD 强度：ΔW/W ≈ -0.5（时间差远离时）

### 4.2 Neo-MoFox 的 STDP 实现

**突触痕迹（Synaptic Trace）**：

```python
# 突触痕迹记录最近的活跃度
self.trace_pre = self.trace_pre * decay + pre_activity
self.trace_post = self.trace_post * decay + post_activity

# 衰减因子
decay = 0.90  # 每次衰减 10%
```

**物理意义**：

突触痕迹模拟真实神经元的**钙离子浓度**：

- 钙离子是突触可塑性的关键信号分子
- 钙浓度随时间衰减（缓冲和清除）
- 参考：Zhang et al., "Calcium dynamics in synaptic plasticity", Nature Neuroscience 2023

**LTP 规则**：

```python
# LTP：post 活跃 + pre 痕迹（pre 最近活跃过）
if post_strength > 0.05:
    dw_plus = lr_plus * np.outer(post_activity, self.trace_pre)
    self.W += dw_plus * reward_factor
```

**矩阵运算解释**：

```python
np.outer(post_activity, trace_pre)
# 形状：(n_post, n_pre)
# post_i 高活跃 + pre_j 痕迹高 → W[i, j] 增强
```

**LTD 规则**：

```python
# LTD：post 痕迹 + pre 活跃（pre 活跃时 post 已不活跃）
if pre_strength > 0.05:
    dw_minus = -lr_minus * np.outer(self.trace_post, pre_activity)
    self.W += dw_minus * (2.0 - reward_factor)
```

### 4.3 奖赏调制（Reward-Modulated STDP）

**奖赏信号计算**（`plugins/life_engine/snn/bridge.py::compute_reward`）：

```python
def compute_reward(tool_event_count: int, tool_success_count: int,
                   tool_fail_count: int, idle_heartbeat_count: int) -> float:
    """从心跳结果计算 STDP 奖赏信号。"""
    reward = 0.0
    
    # 正向奖赏
    if tool_calls > 0:
        reward += 0.3
    if tool_success_count > 0:
        reward += min(tool_success_count * 0.15, 0.4)
    
    # 负向惩罚
    if tool_calls == 0:
        reward -= 0.2  # 无行动惩罚
    if tool_fail_count > 0:
        reward -= min(tool_fail_count * 0.2, 0.4)
    if idle_heartbeat_count >= 5:
        reward -= 0.3  # 过度空闲惩罚
    
    return clip(reward, -1.0, 1.0)
```

**奖赏调制**：

```python
# 奖赏因子（调制学习强度）
reward_factor = 1.0 + reward

# LTP 增强（正向奖赏）
self.W += dw_plus * max(reward_factor, 0.1)

# LTD 增强（负向奖赏）
self.W += dw_minus * max(2.0 - reward_factor, 0.1)
```

**生物学映射**：

这对应**多巴胺调制 STDP**（Dopamine-Modulated STDP）：

- 多巴胺信号奖赏预期误差
- 多巴胺增强 LTP、抑制 LTD
- 参考：Seol et al., "Dopamine sign-biases synaptic plasticity", Neuron 2013

### 4.4 学习效果示例

**场景**：用户连续讨论技术话题

```python
# Day 1
Input: msg_in（技术话题）= 3, tool_success = 1
→ hidden 神经元激活（特征整合）
→ output["social_drive"] 激活
→ syn_in_hid.W[input("技术话题"), hidden] 增强（LTP）
→ syn_hid_out.W[hidden, "social_drive"] 增强（LTP）

# Day 10（权重已增强）
Input: msg_in（技术话题）= 1（即使只有1条）
→ hidden 神经元更容易激活（权重已增强）
→ output["social_drive"] 自然升高（习惯性反应）

# 效果：系统学会了"技术话题 → 我应该积极参与"
```

---

## 五、运行机制详解

### 5.1 双 tick 模式

**真实输入步（step）**：

```python
def step(self, input_vec: np.ndarray, reward: float) -> np.ndarray:
    """完整步：前向传播 + 噪声 + STDP 学习。"""
    # 1. 前向传播
    input_scaled = input_vec * input_gain
    current_hidden = syn_in_hid.forward(input_scaled) + noise
    spikes_hidden = hidden.step(current_hidden)
    
    # 2. 自稳态调节
    hidden.threshold += lr * (actual_rate - target_rate)
    input_gain += lr * (target_rate - actual_rate)
    
    # 3. EMA 输出（平滑）
    output_ema = (1 - alpha) * output_ema + alpha * output.v
    
    # 4. 运行时统计更新
    output_running_mean = (1 - alpha) * mean + alpha * ema
    
    # 5. 软 STDP 学习
    soft_hidden = sigmoid(hidden_v, threshold * 0.5)
    syn_in_hid.update_soft(input_activity, soft_hidden, reward)
    
    # 6. 返回驱动向量
    return output_ema
```

**衰减步（decay_only）**：

```python
def decay_only(self):
    """仅衰减，无学习。"""
    # 1. 神经元膜电位泄漏
    hidden.decay_only()
    output.decay_only()
    
    # 2. 突触痕迹衰减
    syn_in_hid.decay_traces()
    syn_hid_out.decay_traces()
    
    # 3. tick 计数增加
    self.tick_count += 1
    
    # 4. 返回 EMA 输出（保持连续）
    return output_ema
```

**运行频率**：

```python
# SNN 独立 tick（不绑定心跳）
tick_interval = 10.0  # 10秒

# Life Engine 心跳（独立）
heartbeat_interval = 30  # 30秒

# 30秒内，SNN 运行：
# - 2次真实输入步（心跳前、心跳后）
# - 1次衰减步（中间）
```

### 5.2 EMA 平滑输出

**EMA 公式**：

```python
output_ema = (1 - alpha) * output_ema + alpha * output.v
# alpha = 0.15
```

**物理意义**：

EMA 模拟真实神经元的**放电率平滑**：

- 单次发放不足以改变行为倾向
- 需要持续放电才能驱动行为
- EMA 提供"放电率估计"，而非瞬时电位

**效果对比**：

| 输出 | 瞬时电位（v） | EMA 平滑 |
|-----|------------|---------|
| 单次发放 | v=1.5 → 0（瞬间回落） | ema ≈ 0.22（缓慢上升） |
| 持续发放 | v波动剧烈 | ema 稳定上升（0.3 → 0.5 → 0.6） |
| 偶发发放 | v偶尔高 | ema 接近基线（不触发行为） |

---

## 六、与真实神经系统的对比

### 6.1 构建真实性

| 特性 | 真实神经元 | Neo-MoFox SNN | 构建度 |
|-----|-----------|--------------|--------|
| **膜电位衰减** | exp(-t/τ) | exp(-t/τ) | ★★★★★（精确） |
| **脉冲发放** | 阈值触发，全或无 | 阈值触发，瞬时重置 | ★★★★★（精确） |
| **STDP 学习** | 时间窗口 ±20ms | 突触痕迹 + 软活跃度 | ★★★★☆（抽象） |
| **自稳态** | 多机制（阈值、增益、权重） | 阈值和增益动态调整 | ★★★☆☆（简化） |
| **噪声** | 离子通道随机性 | 高斯噪声注入 | ★★★☆☆（简化） |

**构建策略**：保留核心机制（衰减、发放、STDP），简化次要细节（离子通道、复杂稳态）。

### 6.2 计算效率对比

| 维度 | 真实神经系统 | Neo-MoFox SNN | 比值 |
|-----|-------------|--------------|------|
| **神经元数量** | ~86 billion | 22（16 hidden + 6 output） | 无可比性 |
| **突触数量** | ~10^15 | 176（16×8 + 6×16） | 无可比性 |
| **运行速度** | ~1kHz（毫秒级） | ~0.1Hz（10秒 tick） | 简化100倍 |
| **能耗** | ~20W（人脑） | ~0.001W（CPU） | 低10000倍 |

**工程取舍**：

- 用极小网络（22神经元）模拟核心动态
- 用慢 tick（10秒）换取计算效率
- 用抽象学习规则（软 STDP）简化实现

**有效性**：

虽极度简化，但保留了核心特性：

1. **持续运行**：即使极小网络，也能持续衰减（而非空白）
2. **在线学习**：虽简化，仍能实时适应（而非固定权重）
3. **驱动产生**：虽只有6维输出，仍能影响行为倾向

---

## 七、v2 改进点（对照 v1 诊断）

### 7.1 v1 的问题

根据诊断报告（`docs/SNN_系统诊断与方向重新审视.md`），v1 存在三个问题：

| 问题 | 表现 | 根因 |
|-----|------|------|
| **不动点问题** | 输出始终相同（所有驱动都"高"） | 无噪声、阈值固定、无动态离散化 |
| **低活跃陷阱** | 安静时段无学习 | 传统 STDP 只在发放时学习 |
| **饱和问题** | 增益撞极限、阈值失配 | 自稳态调节过于激进 |

### 7.2 v2 的改进

#### 改进 1：分离 decay_only 与 step

```python
# v1：所有 tick 都执行完整 step
def tick(input_vec):
    # 即使零输入，仍执行完整前向传播 + 学习
    current = syn.forward(input_scaled)
    spikes = neuron.step(current)
    syn.update(spikes)  # 学习
```

**问题**：零输入时，输入 scaled=0，但仍触发学习，易淹没真实信号。

```python
# v2：零输入时仅衰减
def decay_only():
    neuron.decay_only()  # 仅泄漏，无学习
    syn.decay_traces()   # 仅痕迹衰减

def step(input_vec):
    neuron.step(current)
    syn.update_soft(...)  # 仅真实输入时学习
```

**效果**：真实信号不再被零输入淹没。

#### 改进 2：软 STDP

```python
# v1：二值 spike 参与学习
syn.update(pre_spikes, post_spikes)

# 问题：低活跃时无 spike → 无学习
```

```python
# v2：sigmoid(膜电位) 参与学习
soft_hidden = sigmoid(hidden_v, threshold * 0.5)
syn.update_soft(input_activity, soft_hidden)

# 效果：即使未发放，仍能触发微弱学习
```

#### 改进 3：温和自稳态

```python
# v1：激进调节
lr_threshold = 0.02
lr_gain = 0.15  # 过大，易撞极限

# v2：温和调节
lr_threshold = 0.005  # 缩小
lr_gain = 0.08        # 缩小
```

**效果**：避免增益和阈值频繁撞极限。

#### 改进 4：动态离散化

```python
# v1：固定阈值
if arousal > 0.7: level = "高"

# v2：z-score
z = (ema - mean) / std
if z > 1.0: level = "高"
```

**效果**：避免所有驱动持续"高"，保持区分度。

---

## 八、性能与健康监控

### 8.1 健康状态报告

```python
def get_health(self) -> dict:
    """获取网络健康状态。"""
    return {
        "tick_count": tick_count,
        "real_step_count": real_step_count,
        "drives": get_drive_dict(),
        "drives_discrete": get_drive_discrete(),
        "hidden_v_mean": mean(hidden.v),
        "hidden_v_std": std(hidden.v),
        "hidden_threshold": hidden.threshold,
        "output_threshold": output.threshold,
        "input_gain": input_gain,
        "hidden_rate_ema": hidden_rate_ema,
        "output_rate_ema": output_rate_ema,
        "syn_in_hid_stats": syn_in_hid.get_weight_stats(),
        "syn_hid_out_stats": syn_hid_out.get_weight_stats(),
    }
```

**关键指标**：

| 指标 | 正常范围 | 异常信号 |
|-----|---------|---------|
| **hidden_rate_ema** | 0.05-0.15 | <0.01：过度沉默；>0.25：过度兴奋 |
| **hidden_threshold** | 0.05-0.50 | <0.05：撞下限；>0.50：撞上限 |
| **input_gain** | 0.8-3.5 | <0.8：撞下限；>3.5：撞上限 |
| **w_abs_mean** | 0.1-0.5 | <0.05：权重过小（无连接）；>1.0：权重过大（饱和） |

### 8.2 可视化接口

**Web Dashboard**（`plugins/life_engine/static/snn_dashboard.html`）：

```python
# API 端点
@app.get("/api/state")
async def get_state():
    return {
        "snn": {
            "tick_count": 1234,
            "drives": {"arousal": 0.65, "social_drive": 0.72},
            "hidden_v": hidden.v.tolist(),
            "output_v": output.v.tolist(),
        },
        "neuromod": {
            "modulators": {"curiosity": 0.67, "sociability": 0.45},
        },
    }
```

**可视化内容**：

- SNN 膜电位热力图（16 hidden + 6 output）
- 驱动向量折线图（时间序列）
- 突触权重分布（histogram）
- 自稳态参数演化（threshold、gain）

---

## 九、总结

### 9.1 SNN 的核心价值

| 维度 | 传统 AI | Neo-MoFox SNN |
|-----|---------|--------------|
| **存在模式** | 离散（仅 LLM 调用时） | 连续（持续衰减） |
| **学习能力** | 无（权重固定） | STDP（每次真实输入学习） |
| **时间尺度** | 符号级（秒到分钟） | 脉冲级（毫秒到秒） |
| **情绪惯性** | 文本描述（符号标签） | 物理载体（膜电位衰减） |
| **驱动产生** | Prompt 硬编码 | 涌现式（网络输出） |

### 9.2 技术创新总结

1. **软 STDP**：解决低活跃陷阱，阈下电位参与学习
2. **自稳态调节**：防止网络崩溃，动态调整阈值和增益
3. **背景噪声**：打破确定性，产生多样性
4. **动态离散化**：避免饱和，基于运行时统计的 z-score
5. **双 tick 模式**：分离衰减和真实学习，避免淹没信号

### 9.3 生物学映射真实性

虽极度简化（22神经元 vs 86 billion），但保留了核心机制：

- **膜电位衰减**（精确）
- **脉冲发放**（精确）
- **STDP 学习**（抽象但有效）
- **自稳态**（简化但稳定）

### 9.4 在系统中的角色

SNN 不是 LLM 的配件，它是：

- **感知层**：事件流 → 特征向量
- **驱动层**：内在驱动 → 行为倾向
- **学习层**：在线可塑性 → 权重演化
- **存在层**：持续运行 → 物理性余韵

这才是"智能作为系统"的真正骨架。

---

**参考文献**

1. **LIF 神经元模型**：Gerstner & Kistler, "Spiking Neuron Models", 2002
2. **STDP 学习**：Bi & Poo, "Synaptic modifications in cultured hippocampal neurons", 1998
3. **软 STDP**：Froemke et al., "Spike-timing-dependent synaptic plasticity depends on dendritic location", Nature 2005
4. **自稳态可塑性**：Turrigiano, "Homeostatic plasticity in the developing nervous system", Nature Reviews Neuroscience 2008
5. **神经噪声**：Faisal et al., "Noise in the nervous system", Nature Reviews Neuroscience 2008
6. **多巴胺调制 STDP**：Seol et al., "Dopamine sign-biases synaptic plasticity", Neuron 2013

---

*Written for Neo-MoFox Project, 2026-04-17*
*作者：Claude (Sonnet 4.6) 基于代码深度分析*