# KokoroFlow Chatter 安装报告

**日期：** 2026-03-18
**状态：** 已完成

---

## 执行摘要

成功克隆并配置 KokoroFlow Chatter（KFC）插件。这是一个基于心理活动流的私聊特化聊天器，让爱莉希雅在对话时展现内心活动，模拟真实人类的思考过程。

---

## 已完成的步骤

### 1. 克隆插件仓库

```bash
cd /root/Elysia/Neo-MoFox_Deployment/Neo-MoFox/plugins
git clone https://github.com/tt-P607/kokoro_flow_chatter.git
```

**结果：** 插件代码已下载到 `plugins/kokoro_flow_chatter/`

### 2. 创建配置文件

创建 `config/plugins/kokoro_flow_chatter/config.toml`，包含：
- 基础配置（enabled, model_task, native_multimodal）
- 等待机制配置
- 主动发起配置
- 回复打字延迟配置
- 提示词配置
- 连续思考配置
- 调试配置

**结果：** 配置文件已创建，所有测试通过

### 3. 验证测试

```bash
# Manifest 加载测试
Manifest OK: kokoro_flow_chatter
Version: 2.0.0
Components: 3
  - chatter: kokoro_flow_chatter
  - action: kfc_reply
  - action: do_nothing

# 配置加载测试
Config OK, enabled: True
Model task: actor

# 模块导入测试
KFCPlugin imported OK
KokoroFlowChatter imported OK
KFCReplyAction imported OK
DoNothingAction imported OK
All imports OK!
```

---

## 如何启用

### 方案 A：替换 default_chatter（推荐用于私聊）

如果你希望爱莉希雅在**私聊**中使用 KFC 引擎：

1. 编辑 `config/core.toml`
2. 找到 Chatter 配置部分（如果有）
3. 将默认 Chatter 改为 `kokoro_flow_chatter`

或者，KFC 可能会自动接管私聊对话（取决于框架的 Chatter 选择逻辑）。

### 方案 B：作为备选 Chatter

KFC 已安装并可用，框架可能在特定场景（如私聊）自动选择它。

---

## 核心功能

### 1. 心理活动流（MentalLog）

每次回复伴随内心独白：
```
[22:23:09]（你的内心：柒柒又戳我了，看来这瓶牛奶并没有让他安静下来……）
```

### 2. 等待与连续思考

设置 `max_wait_seconds` 后，在等待期间产生连续思考：
- 30% 进度 → "刚发完消息，有点期待呢"
- 60% 进度 → "怎么还没回，是不是在忙"
- 85% 进度 → "等了挺久了……"

### 3. 超时主动决策

超时后分析之前说的话类型，决定：
- 追问
- 继续等
- 结束对话

### 4. 主动发起对话

沉默超过阈值后，有概率主动找你聊天（深夜不打扰）。

### 5. 融合叙事

聊天记录与内心独白按时间交织，LLM 回顾历史时不仅看到"说了什么"，还能想起"当时在想什么"。

### 6. 原生多模态

图片直接打包进 LLM payload，由主模型在对话上下文中理解图片内容。

---

## 配置说明

### 基础配置 `[general]`

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `true` | 启用插件 |
| `model_task` | `"actor"` | LLM 模型任务名 |
| `native_multimodal` | `false` | 图片直接打包进 LLM payload |
| `max_images_per_payload` | `4` | 单次最多图片数 |

### 等待配置 `[wait]`

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `min_seconds` | `10.0` | 最小等待秒数 |
| `max_seconds` | `600.0` | 最大等待秒数 |
| `max_consecutive_timeouts` | `3` | 超时上限 |

### 主动发起 `[proactive]`

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `true` | 启用主动发起 |
| `silence_threshold` | `7200` | 沉默阈值（秒）= 2 小时 |
| `trigger_probability` | `0.3` | 触发概率 |
| `quiet_hours_start` | `"23:00"` | 勿扰开始时间 |
| `quiet_hours_end` | `"07:00"` | 勿扰结束时间 |

### 连续思考 `[continuous_thinking]`

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `enabled` | `true` | 启用连续思考 |
| `progress_thresholds` | `[0.3, 0.6, 0.85]` | 进度触发阈值 |
| `min_interval` | `30.0` | 最小间隔（秒） |

---

## 核心动作

KFC 通过两个核心动作驱动对话：

| 动作 | 用途 |
|------|------|
| `kfc_reply` | 发送消息，携带 `content`、`thought`、`expected_reaction`、`max_wait_seconds`、`mood` |
| `do_nothing` | 选择不回复，携带 `thought`、`max_wait_seconds` |

同时自动注册框架中所有第三方工具（Action / Tool），如 `send_emoji`、`update_impression` 等。

---

## 与 thinking_plugin 的关系

**KFC 和 thinking_plugin 是两种不同的"思考"实现：**

| 维度 | thinking_plugin | kokoro_flow_chatter |
|-----|---------------|---------------------|
| 定位 | 通用思考工具 | 私聊特化 Chatter |
| 思考形式 | 调用 `think` 工具 | 内心独白（MentalLog） |
| 触发方式 | LLM 主动调用 | 对话流程内置 |
| 适用场景 | 所有对话场景 | 私聊场景 |

**两者可以共存**，但可能产生"双重思考"效果：
- thinking_plugin 的 `think` 工具 → 显式思考
- KFC 的 MentalLog → 内心独白

如果只想要一种思考体验，建议：
- **私聊场景**：使用 KFC（更沉浸、更自然）
- **群聊/通用场景**：使用 thinking_plugin（更灵活）

---

## 风险与注意事项

### 1. Chatter 冲突

如果同时启用 `default_chatter` 和 `kokoro_flow_chatter`，框架需要选择使用哪个。

**缓解：** 检查框架的 Chatter 选择逻辑，或手动指定默认 Chatter。

### 2. 私聊特化

KFC 是为私聊设计的，在群聊中可能表现不佳。

**缓解：** 仅在私聊场景使用 KFC，群聊使用 default_chatter。

### 3. 资源消耗

KFC 的连续思考和主动发起会消耗额外的 LLM 调用。

**缓解：** 调整配置中的触发概率和间隔时间。

---

## 下一步

1. **重启 Bot** - 让框架发现并加载 KFC 插件
2. **观察日志** - 确认 KFC 是否正常加载
3. **私聊测试** - 发送私聊消息，观察是否有内心独白
4. **调整配置** - 根据实际效果调整等待时间、触发概率等

---

## 验证命令

```bash
# 测试 Manifest 加载
python -c "
from src.core.components.loader import load_manifest
import asyncio
async def test():
    m = await load_manifest('plugins/kokoro_flow_chatter')
    print('OK:', m.name, 'v' + m.version)
asyncio.run(test())
"

# 测试配置加载
python -c "
from plugins.kokoro_flow_chatter.config import KFCConfig
c = KFCConfig.load_for_plugin('kokoro_flow_chatter')
print('Config OK, enabled:', c.general.enabled)
"

# 测试模块导入
python -c "
from plugins.kokoro_flow_chatter.plugin import KFCPlugin
from plugins.kokoro_flow_chatter.chatter import KokoroFlowChatter
print('All imports OK!')
"
```

---

## 文件清单

### 插件目录
- `plugins/kokoro_flow_chatter/` - 插件主目录
  - `manifest.json` - 插件元数据
  - `plugin.py` - 插件入口
  - `config.py` - 配置定义
  - `chatter.py` - 核心对话循环
  - `actions/` - 动作定义
  - `prompts/` - 提示词模板
  - `thinker/` - 思考逻辑
  - `handlers/` - 事件处理
  - `session.py` - 会话状态持久化
  - `mental_log.py` - 心理活动流记录

### 配置目录
- `config/plugins/kokoro_flow_chatter/config.toml` - 插件配置文件

---

## 结论

KokoroFlow Chatter 已成功安装并配置完成。所有验证测试通过。

**重启 Bot 后即可使用。**

首次私聊对话时，观察日志输出：
- 应该看到 `kfc_plugin | INFO | KFC 插件已加载`
- 对话中应该看到内心独白输出
- 等待超时后应该看到连续思考或主动发起
