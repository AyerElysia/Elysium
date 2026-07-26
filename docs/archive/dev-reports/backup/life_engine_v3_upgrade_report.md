# life_engine v3.0.0 升级报告

## 概述

本次升级将 `life_engine` 插件从 v2.0.0 重构为 v3.0.0，实现了统一事件流模型和文件系统操作能力。

## 变更摘要

### 1. 统一事件流模型

**问题**：v2.0.0 将消息分为"外部消息"和"内部消息"两类，分开展示，破坏了时间连续性。

**解决方案**：引入 `LifeEngineEvent` 统一事件模型，所有交互（消息、心跳、工具调用、工具结果）都是事件，按时间顺序连续展示。

**新增类型**：
```python
class EventType(str, Enum):
    MESSAGE = "message"          # 外部消息
    HEARTBEAT = "heartbeat"      # 心跳回复（内部思考）
    TOOL_CALL = "tool_call"      # 工具调用
    TOOL_RESULT = "tool_result"  # 工具返回结果
```

**上下文格式示例**：
```
[2024-03-30T10:00:00] 📨 onebot | 群聊 | 测试群
    └─ 小明: 你好啊
[2024-03-30T10:00:30] 💭 心跳#4 内部思考
    └─ 刚才收到了小明的问候...
```

### 2. 可配置事件可见范围

**新配置项**：`context_history_max_events`（原 `context_history_max_messages`）

控制中枢能看到多少个最近事件，默认 100。

### 3. 文件系统工具

**新增 8 个工具**，所有操作限制在 `workspace_path` 内：

| 工具 | 功能 |
|------|------|
| `nucleus_read_file` | 读取文件 |
| `nucleus_write_file` | 写入文件 |
| `nucleus_edit_file` | 编辑文件（查找替换） |
| `nucleus_move_file` | 移动/重命名 |
| `nucleus_delete_file` | 删除文件/目录 |
| `nucleus_list_files` | 列出目录 |
| `nucleus_file_info` | 获取文件信息 |
| `nucleus_mkdir` | 创建目录 |

**安全机制**：
- 路径解析使用 `resolve()` 获取绝对路径
- 验证路径在 workspace 内（使用 `relative_to`）
- 阻止路径遍历攻击

### 4. 配置变更

**新增**：
- `workspace_path`：文件系统工作空间路径

**重命名**：
- `context_history_max_messages` → `context_history_max_events`

## 文件变更

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `service.py` | 重写 | 新事件流模型，替换消息记录模型 |
| `config.py` | 修改 | 新增配置项 |
| `tools.py` | 新增 | 8 个文件系统工具 |
| `plugin.py` | 修改 | 注册新工具 |
| `manifest.json` | 修改 | 声明新组件 |
| `README.md` | 重写 | 更新文档 |

## 技术决策

### 为什么使用统一事件流？

参考 Claude Code 的设计：

1. **时间连续性**：用户消息和中枢思考交织在一起，形成连贯的意识流
2. **可扩展性**：工具调用、系统事件都可以统一处理
3. **简化逻辑**：不需要分别管理不同类型的记录

### 为什么限制 workspace？

1. **安全性**：防止中枢意外修改系统文件
2. **可控性**：所有文件操作都在已知范围内
3. **可追溯**：方便审计和备份

### 事件序列号设计

每个事件有 `sequence` 字段，用于：
- 确保排序稳定（时间戳可能相同）
- 追踪事件顺序
- 未来实现事件回放

## 后续扩展方向

1. **工具调用记录**：当中枢调用工具时，自动记录到事件流
2. **DFC 唤醒**：中枢可以主动唤醒 DFC 发送消息
3. **DFC 查询**：DFC 可以查询中枢状态
4. **文件历史**：实现 `file_history` 工具，记录文件修改历史

## 验证

```bash
cd /root/Elysia/Neo-MoFox
python3 -c "
from plugins.life_engine.service import LifeEngineService, EventType
from plugins.life_engine.tools import ALL_TOOLS
from plugins.life_engine.plugin import LifeEnginePlugin
print('导入成功')
print(f'事件类型: {[e.value for e in EventType]}')
print(f'工具数量: {len(ALL_TOOLS)}')
"
```

## 迁移说明

重启服务后：
1. 旧的消息历史会被清空
2. 配置文件需要更新（如果手动配置过 `context_history_max_messages`）
3. 工作空间目录会自动创建

---

**版本**: 3.0.0  
**日期**: 2024-03-30  
**作者**: Claude (via Copilot CLI)
