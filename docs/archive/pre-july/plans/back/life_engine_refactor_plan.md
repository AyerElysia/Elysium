# life_engine 重构实施计划

> 设计哲学前提：**让数字生命更好地生活，而非更能干活。所有设计服务于意识模拟，不是任务完成效率。**

---

## 一、现状问题诊断

### 1.1 工具设计问题

| 问题 | 位置 | 严重度 |
|------|------|--------|
| 缺少 grep/搜索工具 | `tools.py` | 高 |
| `nucleus_run_task` 提示词和子代理通信设计薄弱 | `tools.py` | 高 |
| 工具描述缺乏明确的"何时用/何时不用"指导 | 全部工具 | 中 |
| `relate_file` 习惯引导不足 | `memory_tools.py` | 中 |
| `nucleus_edit_file` 不支持 `replace_all` 模式 | `tools.py` | 低 |
| `nucleus_read_file` 缺少行号/偏移支持 | `tools.py` | 低 |

### 1.2 代码质量问题

| 问题 | 位置 | 说明 |
|------|------|------|
| `LifeMemoryService.__init__` 参数不一致 | `memory_service.py` vs `service.py` | service.py传的是plugin对象，但memory_service接收的是plugin（代码里当workspace用） |
| `_get_life_engine_service()` 用脆弱的属性探测找服务 | `tools.py` | 应该直接用全局单例 `LifeEngineService.get_instance()` |
| `_sync_memory_embedding` 在 `LifeEngineWriteFileTool` 和 `LifeEngineEditFileTool` 中重复定义 | `tools.py` | DRY违反 |
| `todo_tools.py` 使用 `Annotated` 但参数 `= None` 而非 `Optional` | `todo_tools.py` | 类型注解不一致 |
| 子任务工具中 `ToolResult` 构造参数错误 (`tool_call_id` vs `call_id`) | `tools.py:999` | 潜在 bug |

### 1.3 提示词/设计哲学问题

| 问题 | 位置 |
|------|------|
| 心跳中 TODO 提示强制性过强，像工作系统而非生活系统 | `service.py` |
| 记忆 relate 习惯：没有正面例子和场景引导，只有约束 | `memory_tools.py` |
| 子代理缺乏"给代理写提示词"的指导 | `tools.py` |

---

## 二、重构内容清单

### 2.1 新增工具：`nucleus_grep_file` (GrepTool 借鉴)

参考 Claude Code GrepTool，为数字生命的私人文件系统提供内容搜索：

**参数：**
- `pattern: str` — 正则表达式/关键词
- `path: str = ""` — 搜索路径（workspace相对路径）
- `glob: str = ""` — 文件通配符（如 `*.md`, `diaries/*`）
- `output_mode: Literal["content", "files_with_matches"] = "files_with_matches"`
- `case_insensitive: bool = False`
- `context_lines: int = 0` — 显示匹配行前后几行
- `max_results: int = 50`

**实现：** 用 Python `re` 模块递归搜索 workspace，不依赖外部工具。

---

### 2.2 重构：`nucleus_run_task` → `nucleus_run_agent`

完全重写，借鉴 Claude Code AgentTool 的简报哲学：

**参数重设计：**
- `task: str` — 任务简报（按简报原则写）
- `context: str = ""` — 背景（你已经知道的、排除的）
- `expected_output: str = ""` — 期望的输出形式
- `max_rounds: int = 5` — 最大工具调用轮数

**子代理提示词结构：**
> 像向刚进门的聪明同事简报一样写任务描述

---

### 2.3 改进所有工具描述（Claude Code 风格）

每个工具描述 = **一段使用指南**，包含：
- 是什么（one line）
- 何时用 ✓
- 何时不用 ✗
- 关键注意事项

---

### 2.4 记忆 relate 习惯培养（重点）

`nucleus_relate_file` 大幅改造：加入正面例子、场景引导、关联类型使用说明和 reason 写法示范。

目标：让数字生命**主动、自然地**在每次写文件后思考关联。

---

### 2.5 TODO 系统提示词重构

将心跳中 TODO 块从"工作任务系统"改为"生活愿望系统"。

截止日期要提醒，但不是枷锁，要引导"我还想做吗"的自我对话。

---

### 2.6 代码质量修复

1. 修复 `LifeMemoryService` 构造参数一致性
2. 替换 `_get_life_engine_service()` 为全局单例调用
3. 消除 `_sync_memory_embedding` 重复（提取为模块函数）
4. 修复 `ToolResult` 构造参数 bug
5. 修复 `todo_tools.py` 类型注解

---

## 三、实施顺序

```
Phase 1: 代码质量修复（不影响功能）
Phase 2: 新增 GrepTool
Phase 3: 重构 nucleus_run_task → nucleus_run_agent
Phase 4: 重写所有工具描述词
Phase 5: 重写心跳提示词（生活哲学向）
Phase 6: 文档更新 + 写报告
```

---

## 四、不改动的部分

- `memory_service.py` 核心算法（Hebbian、RRF、激活扩散）
- `service.py` 心跳循环和事件系统
- `event_handler.py` 消息收集
- `command_handler.py`
- `config.py` 配置结构
- `audit.py` 日志系统
- 框架层代码（仅修改 plugins/life_engine 内）
