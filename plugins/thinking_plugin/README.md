# Thinking Plugin - 思考动作插件

**版本：** 1.0.0
**作者：** Neo-MoFox Team

## 功能介绍

思考插件为 Elysia 提供一个 `action-think`，让她在发送回复前先补一段内心动作。

### 核心能力

- **展现思考过程**：让爱莉希雅的回复更有"人情味"
- **发送前强制思考**：准备发送文本前，必须同轮先调用 `action-think`
- **同轮组合约束**：`action-think` 必须与 `action-send_text` 同时使用，且顺序在前

## 使用场景

### 1. 历史相关问题

```
用户：你还记得上次我说的那件事吗？

爱莉希雅：
"嗯…'那件事'具体指什么呢？让我想想…"
（先查日记）
（真正回复时，同轮调用 action-think + action-send_text）
```

### 2. 信息查询问题

```
用户：帮我分析一下这个情况…

爱莉希雅：
"让我先整理一下思路…"
（先查看文件）
（真正回复时，同轮调用 action-think + action-send_text）
```

### 3. 复杂决策问题

```
用户：这件事你怎么看？

爱莉希雅：
"这个问题有点复杂，让我组织一下说法…"
（同轮调用 action-think + action-send_text）
```

## 安装配置

### 1. 启用插件

在 `config/core.toml` 中添加：

```toml
[bot]
plugins = ["thinking_plugin"]
```

### 2. 配置提示词（可选）

思考动作的引导提示词可以在配置文件中自定义：

**文件位置：** `config/plugins/thinking_plugin/config.toml`

```toml
[prompt]
thinking_habit = """
# 思考的习惯
在这里自定义你的思考引导提示词…
"""
```

**修改后无需重启**，配置支持热重载。

### 3. 重启 Bot

```bash
python -m src.app.main
```

## 技术原理

### 当前设计

`think` 现在是一个 **Action**（不是 Tool）：

| 类型 | 返回值 | FOLLOW_UP | 用途 |
|------|--------|-----------|------|
| Tool | dict | ✅ 触发 | 查询信息 |
| Action | str | ❌ 不触发 | 主动响应 |

因此 `action-think` 不再负责“思考后继续查资料”，而是负责“发送回复前，先记录一段思考动作”。

## 技术原理

```
plugins/thinking_plugin/
├── manifest.json          # 插件清单
├── __init__.py            # 包入口
├── plugin.py              # 插件主类
├── actions/
│   ├── __init__.py
│   └── think_action.py    # 思考动作定义
└── README.md              # 本文档
```

## Action 接口

### think action

**动作名称：** `action-think`

**功能描述：** 在发送回复前记录一段内心思考动作

**参数：**
- `thought` (str, 必填): 你的心理活动，写下你此刻的想法和分析过程

**返回值：**
`"思考动作已记录。请在同一轮内继续调用 action-send_text 发送最终回复。"`

## 最佳实践

### 好的思考示例

✅ 具体、真诚：
```
action-think(thought="我已经查到上下文了，现在要把意思说清楚，不要答得太硬。")
```

✅ 有分析过程：
```
action-think(thought="重点要先给结论，再补两句理由，不然会显得拖沓。")
```

### 不好的思考示例

❌ 敷衍、空洞：
```
action-think(thought="嗯…")
```

❌ 没有实质内容：
```
action-think(thought="我在思考")
```

## 故障排除

### 问题：爱莉希雅没有同时调用 `action-think`

**可能原因：** 模型试图直接 `action-send_text`

**解决方法：**
1. 检查插件是否已加载（查看启动日志）
2. 检查 `default_chatter` 是否已更新到带有 think/send_text 同轮校验的版本
3. 检查是否有其他系统提示词覆盖了该约束

## 版本历史

### 1.0.0 (2026-03-18)

- 初始版本
- 提供 think action
- 强制与 `action-send_text` 同轮使用

## 代码结构

## 相关文件

- 方案文档：`report/thinking_plugin_proposal.md`
- Action 基类：`src/core/components/base/action.py`
- 调用流校验：`plugins/default_chatter/runners.py`
