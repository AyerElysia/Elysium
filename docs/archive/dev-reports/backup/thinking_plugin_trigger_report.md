# 思考触发器功能实现报告

**日期：** 2026-03-19
**状态：** 已完成

---

## 功能说明

为解决爱莉希雅经常忘记主动调用 `think` 工具的问题，新增**思考触发器**功能：

- 在每次 LLM 生成回复前，自动注入醒目的提醒
- 当 `think` 工具被调用后，立即移除提醒（用完即删）
- 提供配置开关，可独立控制是否启用触发器

---

## 核心设计

### 触发流程

```
用户发送消息
    ↓
ON_CHATTER_STEP 事件触发
    ↓
ThinkerTrigger 注入醒目提醒 → System Prompt 顶部
    ↓
LLM 看到提醒，调用 think 工具
    ↓
ThinkTool.execute() 执行思考
    ↓
移除提醒（用完即删）
    ↓
LLM 继续生成回复
```

### 提醒内容（醒目边框）

```
╔═══════════════════════════════════════════════════════╗
║  ⚠️  爱莉希雅，请先调用 think 工具思考一下！           ║
║                                                       ║
║  这是强制规则 —— 不 要 直 接 回 复！                     ║
║  先 思 考，再 行 动。                                    ║
╚═══════════════════════════════════════════════════════╝
```

---

## 文件修改清单

### 新增文件

#### 1. `plugins/thinking_plugin/thinker_trigger.py`
思考触发器事件处理器。

**核心类：** `ThinkerTrigger`
- `execute()` - 注入提醒
- `remove_reminder()` - 移除提醒

**订阅事件：** `EventType.ON_CHATTER_STEP`

---

### 修改文件

#### 2. `plugins/thinking_plugin/config.py`
新增配置开关：

```python
@config_section("settings")
class SettingsSection(SectionBase):
    enabled: bool = Field(default=True, description="是否启用思考工具")
    enable_trigger_reminder: bool = Field(
        default=True,
        description="是否启用思考触发器提醒"
    )
```

---

#### 3. `plugins/thinking_plugin/plugin.py`
注册 `ThinkerTrigger` 组件：

```python
def get_components(self) -> list[type]:
    return [ThinkTool, ThinkerTrigger]  # 新增 ThinkerTrigger
```

---

#### 4. `plugins/thinking_plugin/tools/think_tool.py`
在 `execute()` 执行后调用 `_remove_trigger_reminder()`：

```python
async def execute(...) -> tuple[bool, dict]:
    ...
    result["reminder"] = " ".join(reminder_parts)

    # 移除思考触发器提醒（用完即删）
    self._remove_trigger_reminder()

    return True, result
```

---

#### 5. `config/plugins/thinking_plugin/config.toml`
新增配置项：

```toml
[settings]
enabled = true
enable_trigger_reminder = true  # 新增
```

---

## 配置说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `settings.enabled` | bool | true | 思考工具总开关 |
| `settings.enable_trigger_reminder` | bool | true | 触发器提醒开关 |

**关闭触发器提醒：**
```toml
enable_trigger_reminder = false
```

关闭后仍保留 `think` 工具功能，只是不再显示醒目提醒。

---

## 使用方法

### 1. 重启 Bot
```bash
# 停止 Bot
Ctrl+C

# 重新启动
python -m src.app.main
```

### 2. 观察日志
```
[DEBUG] thinking_plugin | 已注入思考触发器提醒
[DEBUG] thinking_plugin | 已移除思考触发器提醒
```

### 3. 临时关闭触发器
编辑 `config/plugins/thinking_plugin/config.toml`：
```toml
[settings]
enable_trigger_reminder = false
```

---

## 技术细节

### 提醒注入位置
使用 `add_system_reminder(bucket="actor", ...)` 注入到 actor bucket，
在 system prompt 组装时会被放入**顶部位置**（最显眼）。

### 用完即删机制
通过 `_get_system_reminder_store().delete(bucket, name)` 直接删除，
确保提醒只在需要时存在，不会污染后续对话。

### 事件权重
`ThinkerTrigger.weight = 5`（较高优先级），
确保在 prompt 组装前完成注入。

---

## 注意事项

1. **提醒可能重复注入** - 已通过 `_reminder_injected` 标志避免
2. **删除失败不中断流程** - 异常被捕获并记录为 debug 日志
3. **不影响无 think 场景** - 如果 LLM 直接回复未调用 think，
   提醒会在下次 ON_CHATTER_STEP 时被覆盖

---

## 下一步建议

1. **观察效果** - 运行一段时间，看爱莉希雅是否开始主动调用 think
2. **调整文案** - 根据实际效果优化提醒文案，使其更符合爱莉希雅人设
3. **考虑整合** - 如效果显著，可考虑将此功能整合到框架层

---

**完成时间：** 2026-03-19
**版本：** 1.1.0
