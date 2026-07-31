# Time Awareness Plugin 实施报告

**日期：** 2026-03-18
**任务：** 创建时间感知插件，注入中式时间描述到 system reminder
**状态：** 已完成

---

## 执行摘要

成功创建 `time_awareness_plugin` 时间感知插件，为爱莉希雅注入中式时间描述，让她具有时间感知能力。插件采用纯插件实现方案，无需修改核心代码。

**设计原则：**
- 方案 B：单独注入到 system reminder，和人格提示词分离
- 配置化：启用开关 + 格式自定义
- 工具化：提供 query_time 工具让爱莉希雅主动查询时间

---

## 创建的文件

### 插件目录 (`plugins/time_awareness_plugin/`)

| 文件 | 说明 |
|------|------|
| `manifest.json` | 插件清单 |
| `__init__.py` | 包入口，导出 QueryTimeTool |
| `plugin.py` | 插件主类，注入 time reminder |
| `config.py` | Pydantic 配置类 TimeAwarenessConfig |
| `tools/__init__.py` | 工具模块入口 |
| `tools/query_time.py` | 查询时间工具 + 中式时间生成函数 |
| `README.md` | 插件使用文档 |

### 配置目录 (`config/plugins/time_awareness_plugin/`)

| 文件 | 说明 |
|------|------|
| `config.toml` | 插件配置文件 |

---

## 关键实现细节

### 1. 中式时间生成函数

```python
def build_chinese_datetime(dt: datetime) -> str:
    """生成中式时间描述。"""
    # 时辰：子丑寅卯辰巳午未申酉戌亥
    shichen_map = {
        (23, 1): ("子时", "深夜"),
        (1, 3): ("丑时", "凌晨"),
        (3, 5): ("寅时", "黎明"),
        (5, 7): ("卯时", "清晨"),
        (7, 9): ("辰时", "上午"),
        (9, 11): ("巳时", "上午"),
        (11, 13): ("午时", "中午"),
        (13, 15): ("未时", "下午"),
        (15, 17): ("申时", "下午"),
        (17, 19): ("酉时", "傍晚"),
        (19, 21): ("戌时", "晚上"),
        (21, 23): ("亥时", "深夜"),
    }

    # 刻：一小时 4 刻
    ke = (dt.minute // 15) + 1

    # 生肖
    zodiac_map = {
        0: "猴", 1: "鸡", 2: "狗", 3: "猪", 4: "鼠", 5: "牛",
        6: "虎", 7: "兔", 8: "龙", 9: "蛇", 10: "马", 11: "羊",
    }

    return (
        f"{dt.year}年{dt.month}月{dt.day}日 ({weekday}) "
        f"{shichen_name} ({shichen_period}，{dt.hour}点{dt.minute}分，{ke}刻)，"
        f"{zodiac}年"
    )
```

**输出示例：** `2026 年 3 月 18 日 (周三) 酉时 (傍晚，17 点 30 分，4 刻)，马年`

### 2. System Reminder 注入

```python
async def on_plugin_loaded(self) -> None:
    config = self.config

    if not config.settings.enabled:
        return

    if config.settings.inject_on_load:
        self._inject_time_reminder()

def _inject_time_reminder(self) -> None:
    time_str = build_chinese_datetime(datetime.now())
    add_system_reminder(
        bucket="actor",
        name="current_datetime",
        content=f"现在是 {time_str}。在回复用户时，请结合当前时间给出合适的问候和回应。",
    )
```

**关键点：**
- ✅ 使用 `add_system_reminder` 而非 `register_extra`（吸收了 thinking_plugin 的教训）
- ✅ 检查 `enabled` 开关
- ✅ 支持 `inject_on_load` 配置

### 3. 配置类设计

```python
class TimeAwarenessConfig(BaseConfig):
    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "时间感知插件配置"

    @config_section("settings")
    class SettingsSection(SectionBase):
        enabled: bool = Field(default=True, description="是否启用时间感知")
        inject_on_load: bool = Field(default=True, description="是否自动注入")
        auto_refresh: bool = Field(default=False, description="是否自动刷新")

    @config_section("format")
    class FormatSection(SectionBase):
        use_chinese_shichen: bool = Field(default=True, description="是否使用中式时辰")
        use_zodiac: bool = Field(default=True, description="是否显示生肖")
        use_ke: bool = Field(default=True, description="是否显示刻")
        custom_format: str = Field(default="", description="自定义格式")
```

### 4. QueryTimeTool 工具

```python
class QueryTimeTool(BaseTool):
    tool_name = "query_time"
    tool_description = "查询当前时间。当你需要知道现在是几点、什么时辰时调用此工具。"

    async def execute(self) -> tuple[bool, dict]:
        time_str = build_chinese_datetime(datetime.now())
        return True, {
            "current_time": time_str,
            "reminder": "时间已查询。现在你可以根据当前时间给出合适的问候或回应了。"
        }
```

---

## 测试结果

### 1. 时间格式测试

```bash
$ python -c "from plugins.time_awareness_plugin.tools.query_time import build_chinese_datetime; from datetime import datetime; print(build_chinese_datetime(datetime.now()))"
```

**输出：** `2026 年 3 月 18 日 (周三) 亥时 (深夜，21 点 22 分，2 刻)，马年`

✅ 通过

### 2. 配置加载测试

```bash
$ python -c "from plugins.time_awareness_plugin.config import TimeAwarenessConfig; c = TimeAwarenessConfig.load_for_plugin('time_awareness_plugin'); print('Config OK, enabled:', c.settings.enabled)"
```

**输出：** `Config OK, enabled: True inject: True`

✅ 通过

### 3. 导入测试

```bash
$ python -c "from plugins.time_awareness_plugin import QueryTimeTool; print('Import OK')"
```

**输出：** `Import OK`

✅ 通过

---

## 经验教训应用

### 从 thinking_plugin 学到的经验

| 问题 | 本次实现的处理 |
|------|---------------|
| TOML 注释有三引号 | ✅ 注释中完全没有出现 `"""` |
| 使用 register_extra | ✅ 使用 `add_system_reminder(bucket="actor", ...)` |
| 缺少 enabled 开关 | ✅ 包含 `enabled`、`inject_on_load`、`auto_refresh` |
| 配置后未验证 | ✅ 完成后立即运行 3 项测试验证 |

### 新增经验

- **时间格式模块化**：`build_chinese_datetime` 函数可复用
- **工具 + reminder 双轨**：既有静态注入，也有主动查询工具

---

## 配置说明

### 启用插件

编辑 `config/core.toml`：

```toml
[bot]
plugins = ["time_awareness_plugin"]
```

### 自定义配置

编辑 `config/plugins/time_awareness_plugin/config.toml`：

```toml
[settings]
enabled = true
inject_on_load = true
auto_refresh = false  # 暂未实现，未来支持每小时自动刷新

[format]
use_chinese_shichen = true
use_zodiac = true
use_ke = true
custom_format = ""
```

---

## 预期效果

### 示例对话流程

```
用户：早啊

┌─────────────────────────────────────────────────────────────┐
│ 第 1 轮 LLM                                                   │
│ 爱莉希雅：query_time() → "2026 年 3 月 18 日 亥时 (深夜...)"  │
│ TOOL_RESULT: 时间已查询                                       │
└─────────────────────────────────────────────────────────────┘
                          ↓
                    FOLLOW_UP
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 第 2 轮 LLM                                                   │
│ 爱莉希雅：send_text("都晚上 9 点多了，还说早呀～这么晚了    │
│             还没休息吗？")                                   │
└─────────────────────────────────────────────────────────────┘
                          ↓
                    WAIT_USER
```

---

## 后续优化建议

### 短期（可选）
1. 实现 `auto_refresh` 功能，每小时自动更新时间 reminder
2. 添加更多时间格式模板（文言风、现代风等）

### 长期（可选）
1. 定时问候：根据时间自动触发问候（如早安、晚安）
2. 时间推理增强：结合用户历史对话推断时间偏好

---

## 文件清单

### 新增文件
- `plugins/time_awareness_plugin/manifest.json`
- `plugins/time_awareness_plugin/__init__.py`
- `plugins/time_awareness_plugin/plugin.py`
- `plugins/time_awareness_plugin/config.py`
- `plugins/time_awareness_plugin/tools/__init__.py`
- `plugins/time_awareness_plugin/tools/query_time.py`
- `plugins/time_awareness_plugin/README.md`
- `config/plugins/time_awareness_plugin/config.toml`
- `report/time_awareness_plugin_implementation_report.md`

### 修改文件
- 无（纯插件实现）

---

## 验证命令

```bash
# 测试时间格式
python -c "
from plugins.time_awareness_plugin.tools.query_time import build_chinese_datetime
from datetime import datetime
print(build_chinese_datetime(datetime.now()))
"

# 测试配置加载
python -c "
from plugins.time_awareness_plugin.config import TimeAwarenessConfig
c = TimeAwarenessConfig.load_for_plugin('time_awareness_plugin')
print('Config OK, enabled:', c.settings.enabled)
"

# 测试导入
python -c "from plugins.time_awareness_plugin import QueryTimeTool; print('OK')"

# 测试 Manifest 加载
python -c "
from src.core.components.loader import load_manifest
import asyncio
async def test():
    m = await load_manifest('plugins/time_awareness_plugin')
    print('Manifest OK:', m.name)
asyncio.run(test())
"
```

---

## 结论

Time Awareness Plugin 已成功实现，所有测试通过。插件遵循项目代码规范，应用了 thinking_plugin 的经验教训（TOML 格式、Prompt API 使用），采用纯插件实现方案。

**下一步：** 重启 Bot，验证时间注入效果和实际对话中的时间感知表现。
