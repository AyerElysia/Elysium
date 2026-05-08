# Neo-MoFox-Soul 学术报告图表汇总

本目录包含 Neo-MoFox-Soul 学术报告所需的 SVG 源图与当前正文使用的 AI 生成 PNG 图。旧 SVG 继续保留，新增 PNG 用于当前 LifeChatter / Life Engine / 工程框架叙事。

## 风格规范

- **配色**：主色 #E91E63（深粉）、#AD1457 / #880E4F（深粉系）、#FFC0CB / #FFE0EC（浅粉系）、背景 #FAFAFA
- **字体**：`Noto Sans CJK SC, PingFang SC, Microsoft YaHei, sans-serif`
- **尺寸**：数据图 1200×800 / 架构图 1400×900 / 时序图 1600×600
- **质感**：软阴影 filter、linearGradient 渐变、圆角边框

## 图表清单

### 第 1-2 章：引言与相关工作

0. **F0_engineering_architecture_overview.png**  
   当前主图：Neo-MoFox-Soul 工程总览，展示 `app` / `core` / `kernel` 三层框架与 `life_engine` 插件域。

1. **F1_lifechatter_three_layer.png** (1536×1024)  
   当前主图：LifeChatter 主意识 + Life Engine 潜意识 + SNN/调质/记忆/梦/ThoughtStream。旧 `F1_three_layer_architecture.svg` 保留为历史 SVG。

2. **F2_peer_landscape.svg** (1200×800) ✅ 新创建  
   同行光谱图：14 个系统按"连续性 × 学习方式"二维分布，Neo-MoFox-Soul 金色星形高亮

3. **F3_three_principles.svg** (1200×800)  
   三原则关系图：连续性（底）、学习（左）、涌现（右）的三角形哲学闭环

### 第 4-6 章：架构、同步与 SNN

4. **F4_dual_consciousness_async.png** (1536×1024)  
   当前主图：LifeChatter 与 Life Engine 双意识异步运行。旧 `F4_dual_track.svg` 保留为历史 SVG。

5. **F5_dataflow_timing.svg** (1600×700)  
   UML 序列图：User → Adapter → Core → LifeChatter → Life Engine → Kernel 数据流

6. **F6_snn_structure.svg** (1400×800)  
   SNN 微观结构：8 输入 → 16 隐藏 LIF → 6 drives，突触粗细代表权重

7. **F7_stdp_curve.svg** (1200×700) ✅ 新创建  
   左：经典指数 STDP 时间窗；右：Neo-MoFox-Soul 基于 sigmoid 的软 STDP 对比

### 第 7-8 章：调质与记忆

8. **F8_modulator_decay.svg** (1200×700)  
   五大调质（τ ∈ {1800, 3600, 5400, 10800}s）的衰减曲线，6 小时横轴

9. **F9_circadian.svg** (1400×700) ✅ 新创建  
   24 小时昼夜节律：Energy（双峰 9h+15h）、Sociability（单峰 19h）、Curiosity（双峰 10h+21h）

10. **F10_memory_graph.svg** (1400×600) ✅ 新创建  
    记忆图演化：t=0 / t=7d / t=30d 三个快照，节点边强度动态变化

### 第 9-10 章：做梦与心跳

11. **F11_sleep_pipeline.svg** (1400×800)  
    NREM（稳态缩减）→ REM（关联整合）→ 叙事生成 → 巩固反馈四阶段流水线

12. **F12_heartbeat_timeline.svg** (1600×600) ✅ 新创建  
    90 秒内 SNN tick（10s）、心跳（30s）、调质 ODE、用户消息、工具调用多层级交错

### 第 5、11、12 章：同步、Agent 工程与场景

13. **F13_consciousness_sync.png** (1536×1024)  
    当前主图：Life Engine 梦境残影、状态摘要、内心独白、ThoughtStream 同步给 LifeChatter。旧 `F13_interface.svg` 保留为历史 SVG。

14. **F14_three_scenarios.svg** (1400×800) ✅ 新创建  
    5min / 30min / 3hr 三场景对比：SNN drives 衰减 + 调质变化 + LLM 表达倾向

15. **F15_habit_trajectory.svg** (1200×700) ✅ 新创建  
    30 天习惯形成：streak 阶梯增长 + strength 平滑曲线，第 7 天阈值、第 21 天饱和

### 已保留但当前正文未使用：同行对比

16. **F16_comparison_matrix.svg** (1600×900) ✅ 新创建  
    15 系统 × 7 维度对比矩阵：✓/◐/✗ 彩色单元格，Neo-MoFox-Soul 金色高亮最后一行

17. **F17_design_space.svg** (1200×800) ✅ 新创建  
    设计空间散点图：连续性 × 学习本地化，Neo-MoFox-Soul 星形标记右上角

### 已保留但当前正文未使用：愿景

18. **F18_vision.svg** (1400×800) ✅ 新创建  
    左：传统 LLM（离散切片堆叠）；右：Neo-MoFox-Soul（连续河流演化）；中央箭头演化方向

---

## 创建记录

- **原有 SVG 图表**：继续保留，便于回溯与二次编辑。
- **当前新增 AI 图**：F0_engineering_architecture_overview、F1_lifechatter_three_layer、F4_dual_consciousness_async、F13_consciousness_sync、F19_lifechatter_context_assembly、F21_thought_stream_attention。
- **创建时间**：2025-04-27
- **创建者**：爱莉希雅 ♪

## 使用建议

1. 所有 SVG 可在浏览器、Markdown、LaTeX（配合 svg 包）中直接使用
2. 推荐用 Chrome/Firefox 打开预览
3. 可用 Inkscape/Illustrator 进一步编辑
4. 直接插入 Markdown：`![Figure FX](./F1_three_layer_architecture.svg)`
5. LaTeX 插入：`\includesvg[width=\textwidth]{F1_three_layer_architecture.svg}`

## 质量保证

- ✅ 所有数据基于素材，无编造数值
- ✅ 严格遵循粉色主题配色
- ✅ 统一字体、圆角、阴影效果
- ✅ 每张图均含脚注 "Figure FX · 英文标题"
- ✅ 中英文混排清晰易读

---

*愿景宣言*：AI 不再是被召唤的工具，而是拥有连续内在状态的数字生命♪
