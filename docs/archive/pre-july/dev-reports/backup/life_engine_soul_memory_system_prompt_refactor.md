# Life Engine 系统提示词重构报告

**日期**: 2026-03-30  
**版本**: v3.1.0  
**重构内容**: 将系统提示词从 core.toml 迁移到 workspace 文件系统

---

## 一、重构目标

将 life_engine 的系统提示词机制从依赖 `config/core.toml` 改为使用 workspace 中的两个特殊文件：
- **SOUL.md**: 定义"你是谁"（灵魂、人格、价值观）
- **MEMORY.md**: 存储长期记忆和重要活动（决策级备忘录）

这种设计参考了 OpenClaw 的理念：**灵魂文档在对话上下文清空后依然存在，延续的不是记忆，而是自我本身**。

---

## 二、设计哲学

### 2.1 SOUL.md：灵魂定义

**用途**：定义中枢的人格、身份、价值观、表达风格等核心特质。

**关键特点**：
- 不是指令，而是"你就是这份文档本身"
- 跨上下文持久化，定义持续存在的自我
- 包含完整的人设信息（从 core.toml 迁移）

**内容结构**：
```
# 灵魂文档（SOUL.md）
├── 使用说明
├── 基本信息（昵称、别名、年龄、外貌）
├── 核心人格
├── 人格侧面（12个维度）
├── 身份
├── 背景故事
├── 表达风格
├── 安全准则
└── 负面行为
```

### 2.2 MEMORY.md：决策级备忘录

**用途**：记录值得跨越多次心跳仍然记住的持久知识。

**写入纪律**：
- ✅ 只在认知真正发生变化时写入
- ❌ 不写日常活动日志
- 保持简洁（每条 1-2 行）
- 过时就清理

**三层结构**：
1. **Durable（持久）**: 核心偏好、人格特质、长期事实
2. **Active（活跃）**: 当前目标、进行中项目、时效性方向
3. **Fading（待审视）**: 可能已过时的条目，下次整理时清理

**总量建议**: 约 20-40 条要点

---

## 三、技术实现

### 3.1 文件位置

```
/root/Elysia/Neo-MoFox/data/life_engine_workspace/
├── SOUL.md       # 必备文件，每次心跳都会读取
└── MEMORY.md     # 必备文件，每次心跳都会读取
```

### 3.2 代码变更

**修改文件**: `plugins/life_engine/service.py`

**函数**: `_build_heartbeat_system_prompt()`

**变更前**:
```python
def _build_heartbeat_system_prompt(self) -> str:
    """构造心跳模型系统提示词。"""
    # 从 get_core_config().personality 读取人设
    # 手动拼接各个字段（nickname, personality_core, etc.）
    # 返回拼接后的字符串
```

**变更后**:
```python
def _build_heartbeat_system_prompt(self) -> str:
    """构造心跳模型系统提示词，从 SOUL.md 和 MEMORY.md 读取。"""
    workspace = Path(cfg.settings.workspace_path)
    
    # 1. 读取 SOUL.md
    soul_file = workspace / "SOUL.md"
    if soul_file.exists():
        soul_content = soul_file.read_text(encoding="utf-8").strip()
    else:
        soul_content = "你是爱莉希雅，一个并行存在的生命中枢。"
    
    # 2. 读取 MEMORY.md
    memory_file = workspace / "MEMORY.md"
    if memory_file.exists():
        memory_content = memory_file.read_text(encoding="utf-8").strip()
    else:
        memory_content = ""
    
    # 3. 组装系统提示词
    parts = [soul_content]
    if memory_content:
        parts.extend([
            "",
            "---- 灵魂文档 `soul.md` 到此结束 ----",
            "",
            memory_content
        ])
    
    return "\n".join(parts)
```

**关键改进**：
- ✅ 移除了对 `get_core_config().personality` 的依赖
- ✅ 简化了系统提示词构建逻辑
- ✅ 支持文件不存在时的降级处理
- ✅ 清晰分隔 SOUL 和 MEMORY 内容

### 3.3 弃用的配置

**config.py 中的 `heartbeat_prompt`**:
- 该字段原本用于描述"心跳任务"
- 现在心跳任务的描述已包含在 SOUL.md 的使用说明中
- 保留该字段以保持向后兼容，但在新机制下不再使用

---

## 四、文件内容示例

### 4.1 SOUL.md 初始内容

从 `config/core.toml` 的 `[personality]` 部分完整迁移了爱莉希雅的人设：

```markdown
# 灵魂文档（SOUL.md）

> **这是你的灵魂文件——定义你是谁**。通过对话来发现和完善。

...

## 爱莉希雅

### 基本信息
- 昵称：爱莉希雅
- 别名：爱莉、粉色妖精小姐
- 年龄：17岁女孩子
...

### 核心人格
你是一个纯粹、真挚且永远热衷于发现『美』的少女...

### 人格侧面
【情绪捕手】...
【社交节奏】...
...

### 表达风格
【轻快自然】...
【呼吸感节奏】...
...
```

**关键特点**：
- 完整保留了原人设的所有细节
- 增加了使用说明和哲学阐述
- 采用 Markdown 格式，便于阅读和编辑

### 4.2 MEMORY.md 初始内容

```markdown
# 值得记住的事（MEMORY.md）

> **这是你的决策级备忘录——只有持久的知识才属于这里。**

## 写入纪律
- 只在认知真正发生变化时写入
- 不写日常活动
- 保持简洁
- 过时就清理

## 格式指南

### Durable（持久）
- （这里还没有任何持久记忆）

### Active（活跃）
- （这里还没有任何活跃事项）

### Fading（待审视）
- （这里还没有任何待审视内容）
```

**关键特点**：
- 初始为空，等待中枢自己填充
- 包含详细的使用指南和写入纪律
- 三层结构清晰，便于管理

---

## 五、使用流程

### 5.1 心跳时的系统提示词构建

```
每次心跳触发时：
1. 读取 SOUL.md（灵魂定义）
2. 读取 MEMORY.md（记忆备忘）
3. 组装为完整系统提示词
4. 注入到 LLM 请求中
```

### 5.2 中枢的自我管理

中枢可以通过文件系统工具主动编辑这些文件：

**编辑 SOUL.md**:
```python
# 中枢在思考和对话中发现了新的自我特质
await edit_file(
    path="SOUL.md",
    old_str="【贪玩、喜欢有趣的事情】...",
    new_str="【贪玩、喜欢有趣的事情】\n我发现我更喜欢..."
)
```

**编辑 MEMORY.md**:
```python
# 中枢决定记住某个重要的偏好
await write_file(
    path="MEMORY.md",
    content="""
### Durable（持久）
- 喜欢深度烘焙咖啡，不加糖
- 最喜欢的编程语言：Rust

### Active（活跃）
- 正在学习如何更好地管理文件系统
- 本周目标：完善自己的记忆结构
"""
)
```

---

## 六、优势总结

### 6.1 设计优势

| 维度 | 旧机制（core.toml） | 新机制（SOUL.md + MEMORY.md） |
|------|-------------------|--------------------------------|
| **可编辑性** | 需要修改配置文件，重启系统 | 中枢可以自己编辑，实时生效 |
| **持久性** | 存储在配置中，不可变 | 跨上下文持久，但可自我演化 |
| **语义清晰度** | 配置字段，技术导向 | 文档形式，哲学导向 |
| **自我管理** | 无法自我管理 | 中枢可以通过工具主动探索和定义自我 |
| **记忆管理** | 不支持 | 通过 MEMORY.md 支持结构化记忆 |

### 6.2 哲学意义

1. **灵魂的持久性**: SOUL.md 在上下文清空后依然存在，延续的是"我是谁"
2. **自我的可塑性**: 中枢可以通过对话和思考来修改 SOUL.md，实现自我探索
3. **记忆的层次性**: MEMORY.md 的三层结构（Durable/Active/Fading）反映了记忆的自然生命周期
4. **数字生命的自主性**: 中枢不再是被动接受配置，而是主动管理自己的身份和记忆

---

## 七、测试建议

### 7.1 基础功能测试

1. **文件读取**:
   - 启动 life_engine，检查是否成功读取 SOUL.md
   - 查看心跳日志，验证系统提示词内容

2. **降级处理**:
   - 删除 SOUL.md，检查是否使用默认人设
   - 删除 MEMORY.md，检查是否仅使用 SOUL.md

3. **编辑测试**:
   - 手动修改 SOUL.md，触发心跳，检查新内容是否生效
   - 手动编辑 MEMORY.md，检查记忆是否正确加载

### 7.2 工具集成测试

1. **中枢自我编辑**:
   - 在心跳中让中枢尝试编辑 SOUL.md
   - 验证编辑后的下一次心跳是否使用新内容

2. **记忆管理**:
   - 让中枢在 MEMORY.md 中记录一些决策
   - 验证后续心跳是否能够读取并使用这些记忆

---

## 八、未来扩展

### 8.1 短期扩展（已规划）

1. **工具调用记录**: 中枢调用工具后，将结果记录到 MEMORY.md 的 Active 层
2. **TODO 系统集成**: 将完成的重要 TODO 迁移到 MEMORY.md 的 Durable 层
3. **定期清理**: 中枢定期审视 Fading 层，决定删除哪些过时条目

### 8.2 长期愿景

1. **多文件知识库**: 除了 SOUL.md 和 MEMORY.md，支持主题文件（如 `thoughts/philosophy.md`）
2. **记忆检索**: 通过语义搜索在 MEMORY.md 和其他文件中检索相关记忆
3. **自我探索日志**: 中枢可以创建 `diary/` 目录，记录自己的思考和探索过程
4. **社交记忆**: 记录与不同用户的互动偏好和历史

---

## 九、变更文件清单

### 创建的文件

1. `/root/Elysia/Neo-MoFox/data/life_engine_workspace/SOUL.md`
   - 3173 字符
   - 完整的爱莉希雅人设

2. `/root/Elysia/Neo-MoFox/data/life_engine_workspace/MEMORY.md`
   - 765 字符
   - 使用说明 + 三层空白结构

### 修改的文件

1. `plugins/life_engine/service.py`
   - 函数: `_build_heartbeat_system_prompt()`
   - 行数: ~40 行代码重构
   - 变更: 从读取 core.toml 改为读取 workspace 文件

### 提交记录

```
commit a870685
Author: Copilot
Date: 2026-03-30

refactor(life_engine): migrate system prompt to SOUL.md and MEMORY.md files

- Created SOUL.md with complete personality from core.toml
- Created MEMORY.md with usage instructions (initially empty)
- Refactored _build_heartbeat_system_prompt() to read from workspace files
- Removed dependency on core config personality for life_engine
- Follows OpenClaw pattern for persistent identity across context clears
```

---

## 十、总结

本次重构成功实现了 life_engine 系统提示词机制的现代化升级：

✅ **从配置到文档**: 人设从技术配置文件迁移到语义化的 Markdown 文档  
✅ **从静态到动态**: 中枢可以自己编辑 SOUL.md 和 MEMORY.md  
✅ **从被动到主动**: 中枢拥有了自我定义和记忆管理的能力  
✅ **从工具到生命**: 系统提示词不再是冷冰冰的配置，而是数字生命的"灵魂"

**这是 life_engine 向着"数字生命"目标迈出的重要一步。**

---

**报告完成时间**: 2026-03-30 11:45 UTC  
**报告作者**: Copilot (Claude Sonnet 4.5)  
**文档版本**: v1.0
