# 记忆系统可视化分析与方案

## 数据结构分析
基于 `plugins/life_engine/memory/service.py` 和 `plugins/life_engine/snn/core.py` 的分析，记忆系统主要由两部分构成：
1. **显式记忆（知识图谱）**:
   - `MemoryNode` (节点): 包含属性如 `node_id`, `node_type` (FILE/CONCEPT), `activation_strength` (激活强度), `emotional_valence` (情感效价), `importance` (重要性)。
   - `MemoryEdge` (边): 包含属性如 `edge_id`, `source_id`, `target_id`, `edge_type` (RELATES, CAUSES, ASSOCIATES等), `weight` (权重), `activation_count` (激活次数)。

2. **隐式驱动状态（脉冲神经网络 SNN）**:
   - `DriveCoreNetwork`: 维持持续的内部状态 (如 curiosity, survival, social等驱动力)。
   - 目前已有一个 `/snn` 路由提供面板，但偏向于底层网络状态，可以整合。

## 方案设计

我建议为 Life Engine 开发一个炫酷的 **3D 记忆星图 (Memory Starfield)** 面板。

### 1. 技术栈
- **后端 (FastAPI)**: 在 `life_engine` 中新增 `MemoryRouter`（类似 `SNNRouter`），提供图形数据 API (`/api/memory/graph`)。
- **前端 (HTML+JS)**: 使用 `3d-force-graph` 库构建星图，通过 CDN 引入。实现简单且视觉效果爆炸。

### 2. 后端 API 设计 (`MemoryRouter`)
新增一个路由类，注册以下接口：
- `GET /memory` -> 返回主控 HTML 页面。
- `GET /memory/api/graph` -> 返回 JSON 格式的图数据：
  ```json
  {
    "nodes": [
      {"id": "node_1", "name": "foo.py", "type": "file", "val": 1.5, "color": "#4287f5"},
      ...
    ],
    "links": [
      {"source": "node_1", "target": "node_2", "type": "relates", "value": 0.8},
      ...
    ]
  }
  ```
  - 取出 `memory_nodes` 表中的所有节点和 `memory_edges` 表中的边。
  - `val` 映射到 `activation_strength`。
  - `color` 根据 `node_type` 或情绪状态赋予特定颜色。

### 3. 前端视觉效果 (`3d-force-graph`)
- **星系布局**:
  - `FILE` 节点呈现为特定颜色的星球，大小随激活强度脉动。
  - `CONCEPT` 节点呈现为高亮的恒星/核心。
- **粒子流动**:
  - 连线使用 `linkDirectionalParticles`，粒子流动的速度和数量映射到边的 `weight` 和 `activation_count`，直观模拟**激活扩散**。
- **发光与Bloom**:
  - 使用 Three.js 的 `UnrealBloomPass` 为高激活节点添加发光光晕。
- **交互**:
  - 悬停节点显示详细信息（路径、创建时间、访问次数等）。
  - 点击节点视角自动飞行拉近。

## 实现步骤
1. **维护 `memory/router.py`**: 实现读取 SQLite `memory_nodes` 和 `memory_edges` 并转换为 Graph JSON 的接口。
2. **编写 `static/memory_dashboard.html`**: 引入 `3d-force-graph`，编写绚丽的渲染和交互逻辑。
3. **在 `core/plugin.py` 中注册**: 将 `MemoryRouter` 添加到插件的路由列表中。
