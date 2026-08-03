# 表情包系统（Emoji）

> 文档状态：权威文档，与代码同步截至 2026-07-31。
> 代码位置：`plugins/emoji/`（13 文件，3549 行）。
> 本文是表情包系统插件的权威文档；凡与本文冲突，以本文和当前代码为准。

---

## 0. 一句话定位

Emoji 插件实现**仿生表情包收藏与现场生成**双引擎：像人类一样从聊天中"看到"有趣的表情包并收藏（VLM 感知 + 向量检索），同时能通过 NovelAI 现场生成全新表情包。

---

## 1. 总体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                     EmojiPlugin（插件入口）                            │
│  plugin.py — 注册 Service / Action / Tool / Scheduler                │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              Sender（收藏发送引擎）                             │   │
│  │                                                              │   │
│  │  定时入库：media cache → VLM 决策 → 收藏/跳过                 │   │
│  │  检索发送：情感 tag 过滤 → 向量检索 → 温度采样 → 发送          │   │
│  │                                                              │   │
│  │  MemeStore（SQLite 候选池 + 图片文件 + ChromaDB 向量库）       │   │
│  │  VisualEmbedder（视觉 embedding 编码）                        │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │              Generated（现场生成引擎）                          │   │
│  │                                                              │   │
│  │  独立 NovelAI 客户端（不依赖画室插件）                         │   │
│  │  风格预设 + 身份一致性 + 字幕叠加                              │   │
│  │  生成 → 裁切 → 发送                                           │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. 收藏发送引擎（sender/）

### 2.1 定时入库流程

```
主程序 media cache（聊天中收到的图片）
    ↓ 定时扫描
VLM 感知（多模态 LLM 判断：是否表情包？是否值得收藏？）
    ↓ 决策
收藏 → 复制文件到插件 data 目录 + embedding 写入向量库
跳过 → 标记 dismissed
```

### 2.2 检索发送流程

```
LLM 决定发表情包（Action 触发）
    ↓
情感 tag 过滤（18 种预设情感）
    ↓
向量检索 topN（描述 embedding 相似度）
    ↓
阈值内温度采样（避免总是同一张）
    ↓
send_emoji() 发送
```

### 2.3 MemeStore — 三层存储

| 层 | 技术 | 职责 |
|----|------|------|
| 候选池 | SQLite | 候选状态（unreviewed/collected/dismissed）+ 来源溯源 |
| 图片文件 | 文件系统 | 收藏的表情包按 meme_id 存储 |
| 视觉向量库 | ChromaDB | 视觉 embedding + 元数据，供纯视觉检索 |

三者用 `meme_id` / `source_hash` 关联。

### 2.4 情感标签预设

18 种内置情感 tag：开心、难过、生气、惊讶、害羞、尴尬、无语、委屈、嘲讽、疑惑、赞同、否定、兴奋、疲惫、害怕、厌恶、紧张、冷漠。

### 2.5 VisualEmbedder

- 视觉 embedding 编码器
- 支持多模态向量入库
- 与描述 embedding 互补（文字检索 + 视觉检索）

---

## 3. 现场生成引擎（generated/）

### 3.1 设计原则

- **独立客户端**：不依赖画室插件，不查旧表情包库
- **NovelAI 直连**：独立的 API 交互逻辑
- **风格一致性**：通过 `EmojiStylePreset` 保持角色特征

### 3.2 生成流程

```
LLM 决定生成表情包（Action 触发）
    ↓
构建 prompt（风格预设 + 情感 + 动作描述）
    ↓
NovelAI API 生成
    ↓
后处理（裁切 + 可选字幕叠加）
    ↓
发送
```

### 3.3 字幕叠加

使用 PIL 在生成的图片上叠加文字字幕（可选），模拟真实表情包风格。

---

## 4. Action 与 Tool

| 组件 | 类型 | 职责 |
|------|------|------|
| `sender/action.py` | Action | LLM 决定从收藏中发送表情包 |
| `generated/action.py` | Action | LLM 决定现场生成表情包 |
| `sender/collection_tools.py` | Tool | 浏览候选池、收藏/跳过、查看收藏 |

---

## 5. 配置节一览

| 配置节 | 说明 |
|--------|------|
| `sender.plugin` | 启用/自启 |
| `sender.scheduler` | 定时入库间隔 |
| `sender.prompt` | VLM 感知提示词 |
| `sender.ingest` | 入库参数（批量大小、来源过滤） |
| `sender.vector` | 向量库配置（collection、维度、距离度量） |
| `sender.storage` | 存储路径（db、image_dir） |
| `sender.visual` | 视觉 embedding 模型配置 |
| `sender.collection` | 收藏策略（阈值、温度、topN） |
| `generated.plugin` | 生成引擎启用 |
| `generated.api` | NovelAI API 端点、密钥 |
| `generated.generation` | 生成参数（步数、分辨率） |
| `generated.identity` | 角色身份描述（一致性） |
| `generated.caption` | 字幕配置（字体、位置、大小） |

---

## 6. 文件索引

```
plugins/emoji/
├── __init__.py              # 包说明
├── plugin.py                # 插件入口
├── config.py                # 配置定义（13 节）
├── sender/
│   ├── service.py           # 收藏发送服务（定时入库+检索发送）
│   ├── meme_store.py        # 三层存储（SQLite+文件+ChromaDB）
│   ├── visual_embedder.py   # 视觉 embedding 编码
│   ├── action.py            # 发送表情包 Action
│   └── collection_tools.py  # 收藏管理 Tool
└── generated/
    ├── service.py           # 现场生成服务（独立 NovelAI 客户端）
    ├── action.py            # 生成表情包 Action
    └── prompt.py            # 风格预设
```
