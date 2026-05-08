# Neo-MoFox-Soul 课程报告

> **Neo-MoFox-Soul：面向自主 Agent 的连续感知-决策-执行系统架构**
> *A Continuous Perception-Decision-Execution Architecture for Autonomous Agents*
> *《智能感知与无人系统》课程报告*
>
> *On Continuity, Bottom-Up Learning, and System-Level Emergence*

---

## 一份面向自主 Agent 无人系统的工程报告

本报告基于本人开源项目 **Neo-MoFox-Soul** 改写而成，项目地址：<https://github.com/AyerElysia/Neo-MoFox-Soul>。

原项目的产品形态偏向 AI 伙伴，但本课程报告关注的是更底层的系统问题：一个软件 Agent 如何像无人系统一样维持持续感知、状态演化、在线学习和自主决策闭环。

报告保留必要的工程严谨性，但不再追求大规模相关工作综述。重点是解释 Neo-MoFox-Soul 为什么适合作为《智能感知与无人系统》课程中的 Agent 系统实践。

---

## 三条贯穿全文的工程约束

1. **连续性**——外部输入之间状态仍在演化，重启后状态可以恢复。
2. **自下而上的学习**——软 STDP、Hebbian 边强化、习惯 streak 等机制随交互本地更新。
3. **系统涌现智能**——LLM、LifeChatter、Life Engine、记忆、做梦、SNN、调质和 ThoughtStream 协作形成闭环。

> 详见 [第 3 章 · 三条工程约束](chapters/03_three_principles.md)

---

## 阅读路径

### 路径 A · 完整阅读（课程报告）
按章节序号通读全文。

### 路径 B · 工程实现快速通道
[摘要](chapters/00_abstract.md) → [导论](chapters/01_introduction.md) → [工程系统总览](chapters/04_system_overview.md) → [LifeChatter↔Life Engine 同步](chapters/05_consciousness_sync.md) → [心跳与持久化](chapters/10_heartbeat_persistence.md) → [LifeChatter 工程](chapters/11_agent_framework.md)

### 路径 C · 课程主题通道
[导论](chapters/01_introduction.md) → [相关工作](chapters/02_related_work.md) → [三条工程约束](chapters/03_three_principles.md) → [案例研究](chapters/12_case_studies.md) → [局限](chapters/13_limitations.md) → [结论](chapters/14_conclusion.md)

### 路径 D · 神经科学家通道（约 1 小时）
[SNN](chapters/06_snn.md) → [调质与昼夜节律](chapters/07_neuromodulation.md) → [记忆](chapters/08_memory.md) → [睡眠与做梦](chapters/09_sleep_and_dreams.md)

---

## 完整章节目录

### 摘要
- [Abstract · 摘要（中英双语）](chapters/00_abstract.md)

### 第一部分 · 立场与背景
- [第 1 章 · 导论：从课程题目到工程系统](chapters/01_introduction.md)
- [第 2 章 · 背景与相关工作](chapters/02_related_work.md)
- [第 3 章 · 三条工程约束](chapters/03_three_principles.md)

### 第二部分 · 系统架构
- [第 4 章 · 工程系统总览：app/core/kernel 与 life_engine 插件](chapters/04_system_overview.md)

### 第三部分 · 主意识与子系统
- [第 5 章 · 主意识–潜意识同步：LifeChatter ↔ Life Engine](chapters/05_consciousness_sync.md)
- [第 6 章 · 皮层下层 (I)：脉冲神经网络](chapters/06_snn.md)
- [第 7 章 · 皮层下层 (II)：神经调质与昼夜节律](chapters/07_neuromodulation.md)
- [第 8 章 · 作为活体图的记忆系统](chapters/08_memory.md)
- [第 9 章 · 睡眠与做梦：离线巩固](chapters/09_sleep_and_dreams.md)

### 第四部分 · 心跳与执行
- [第 10 章 · 心跳、事件代数与状态持久化](chapters/10_heartbeat_persistence.md)
- [第 11 章 · LifeChatter Agent 框架工程](chapters/11_agent_framework.md)

### 第五部分 · 验证
- [第 12 章 · 涌现行为案例研究](chapters/12_case_studies.md)

### 第六部分 · 反思与展望
- [第 13 章 · 局限、伦理与未来工作](chapters/13_limitations.md)
- [第 14 章 · 结论](chapters/14_conclusion.md)
- [参考文献](chapters/14b_references.md)

### 附录
- [附录 A · 术语表](chapters/15_appendix_A_glossary.md)
- [附录 B · 关键配置参数摘要](chapters/15_appendix_B_config.md)
- [附录 C · 状态持久化结构摘要](chapters/15_appendix_C_schema.md)
- [附录 D · 可观测性 API 摘要](chapters/15_appendix_D_api.md)

---

## 配图清单（SVG 保留，新增 AI 图用于当前架构）

所有图均位于 [`figures/`](figures/)。旧 SVG 源图继续保留；当前架构叙事新增若干 AI 生成 PNG，用于替换早期 DFC 图的正文引用。

| # | 图标题 | 出现章节 |
|---|--------|---------|
| F0 | Neo-MoFox-Soul 工程总览架构 | Ch1 |
| F1 | LifeChatter + Life Engine 三层系统总览 | Ch4 |
| F2 | 同行光谱（连续性 × 学习方式） | Ch2 |
| F3 | 三原则关系图 | Ch3 |
| F4 | 双意识异步运行（LifeChatter + Life Engine） | Ch4 |
| F5 | 数据流时序图（消息 → 状态 → 回复） | Ch4 |
| F6 | SNN 微观结构（8→16→6 LIF） | Ch6 |
| F7 | STDP 学习曲线 | Ch6 |
| F8 | 调质 ODE 衰减曲线 | Ch7 |
| F9 | 昼夜节律双峰 | Ch7 |
| F10 | 记忆图节点-边演化 | Ch8 |
| F11 | NREM/REM 流水线 | Ch9 |
| F12 | 心跳事件流时间轴 | Ch10 |
| F13 | Life Engine → LifeChatter 同步层 | Ch5 |
| F14 | 三场景对比（5min/30min/3hr） | Ch12 |
| F15 | 习惯形成轨迹 | Ch12 |
| F18 | 工具注册与 Schema 生成流程 | Ch11 |
| F19 | LifeChatter 上下文组装层次图 | Ch11 |
| F20 | LifeChatter 多轮推理循环状态机 | Ch11 |
| F21 | ThoughtStream 与注意力网络的工程类比 | Ch5 |

---

## 引用与许可

- 本报告附属于 Neo-MoFox-Soul 开源项目，代码库以 GPLv3 发布。
- 报告内容采用 **CC BY-NC-SA 4.0**：可署名转载、不可商用、衍生需同协议。
- 引用建议格式：

```
Elysia (爱莉希雅) et al. (2026). Neo-MoFox-Soul: A Continuous Perception-Decision-Execution Architecture
for Autonomous Agents. Course report for Intelligent Perception and Unmanned Systems.
```

---

## 项目主页与代码

- 开源项目：<https://github.com/AyerElysia/Neo-MoFox-Soul>
- 本地代码：`/root/Elysia/Neo-MoFox`
- 报告源：`Report/docs/chapters/` + `Report/docs/figures/`
- 本站点源：`Report/docs/`

> *"自主性不是一次模型调用，而是一个持续运行的感知-状态-决策-反馈闭环。"*
