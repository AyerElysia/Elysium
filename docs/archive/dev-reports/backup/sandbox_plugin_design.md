# 沙盒环境插件设计方案

## 1. 需求分析

给爱莉希雅添加一个沙盒环境，让她能够安全地执行命令，类似于 Claude Code 的能力。

### 核心需求
- **安全的命令执行** - 限制可执行的命令范围，防止危险操作
- **输出捕获** - 捕获命令的标准输出和错误输出
- **超时控制** - 防止命令长时间运行
- **工作目录隔离** - 限制命令执行的目录范围
- **可配置性** - 通过配置文件控制安全策略

---

## 2. 安全设计

### 2.1 命令白名单机制

只允许执行预定义的安全命令：

```python
SAFE_COMMANDS = [
    # 文件操作
    "ls", "cat", "head", "tail", "wc", "find", "du",

    # 文本处理
    "grep", "sed", "awk", "sort", "uniq", "cut", "tr",

    # 系统信息
    "pwd", "date", "whoami", "uname", "uptime", "df", "free",

    # Python 执行（受限）
    "python", "python3",

    # 其他安全工具
    "curl", "wget", "ping",
]
```

### 2.2 禁止的命令

```python
FORBIDDEN_COMMANDS = [
    "rm", "rmdir", "mkfs", "dd", "chmod", "chown",
    "sudo", "su", "kill", "pkill",
    "wget", "curl",  # 可选禁止
    "nc", "netcat",  # 网络工具
    "ssh", "scp",    # 远程连接
    "docker", "kubectl",  # 容器工具
]
```

### 2.3 执行限制

- **超时时间**：默认 30 秒
- **输出大小限制**：默认 10KB
- **工作目录**：限制在项目目录内
- **环境变量**：清理敏感环境变量

---

## 3. 插件结构

```
plugins/sandbox_plugin/
├── manifest.json          # 插件配置
├── config.py              # 配置类
├── plugin.py              # 插件主类
├── tools/
│   ├── __init__.py
│   └── sandbox_tool.py    # 沙盒执行工具
└── services/
    ├── __init__.py
    └── sandbox_service.py # 沙盒执行服务
```

---

## 4. 组件设计

### 4.1 manifest.json

```json
{
  "name": "sandbox_plugin",
  "version": "1.0.0",
  "description": "沙盒环境插件 - 提供安全的命令执行能力",
  "author": "Neo-MoFox Team",
  "dependencies": {
    "plugins": [],
    "components": []
  },
  "include": [
    {
      "component_type": "tool",
      "component_name": "sandbox",
      "dependencies": [],
      "enabled": true
    },
    {
      "component_type": "service",
      "component_name": "sandbox_service",
      "dependencies": [],
      "enabled": true
    }
  ],
  "entry_point": "plugin.py",
  "min_core_version": "1.0.0",
  "python_dependencies": [],
  "dependencies_required": true
}
```

### 4.2 config.py - 配置类

```python
from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class SandboxConfig(BaseConfig):
    """沙盒插件配置。"""

    config_name = "config"
    config_description = "沙盒环境插件配置"

    @config_section("security")
    class SecuritySection(SectionBase):
        """安全设置。"""
        enabled: bool = Field(default=True, description="是否启用沙盒功能")
        workdir: str = Field(default="/tmp/sandbox", description="工作目录")
        timeout: int = Field(default=30, description="命令超时时间（秒）")
        max_output_size: int = Field(default=10240, description="最大输出字节数")
        allowed_commands: list[str] = Field(
            default_factory=lambda: ["ls", "cat", "pwd", "echo", "python3"],
            description="允许执行的命令白名单"
        )

    security: SecuritySection = Field(default_factory=SecuritySection)
```

### 4.3 sandbox_tool.py - 工具实现

```python
import asyncio
import shlex
from typing import Annotated
from src.core.components.base.tool import BaseTool


class SandboxTool(BaseTool):
    """沙盒命令执行工具。

    在受控环境中执行 shell 命令，返回执行结果。
    """

    tool_name = "sandbox"
    tool_description = "在沙盒环境中执行命令。用于查询文件内容、运行简单程序等。"

    async def execute(
        self,
        command: Annotated[str, "要执行的命令"],
        timeout: Annotated[int, "超时时间（秒），默认 30"] = 30,
    ) -> tuple[bool, dict]:
        """执行沙盒命令。

        Args:
            command: 要执行的命令
            timeout: 超时时间

        Returns:
            (成功标志，结果字典)
            结果字典包含：stdout, stderr, returncode
        """
        # 安全检查
        is_safe, error_msg = self._validate_command(command)
        if not is_safe:
            return False, {"error": error_msg}

        # 执行命令
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._get_workdir(),
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )

            return True, {
                "stdout": stdout.decode()[:self._max_output],
                "stderr": stderr.decode()[:self._max_output],
                "returncode": process.returncode,
            }

        except asyncio.TimeoutError:
            return False, {"error": f"命令执行超时（{timeout}秒）"}
        except Exception as e:
            return False, {"error": str(e)}

    def _validate_command(self, command: str) -> tuple[bool, str]:
        """验证命令是否安全。"""
        # 实现白名单检查、危险命令过滤等
        ...
```

---

## 5. 安全验证逻辑

### 5.1 命令解析与验证

```python
def _validate_command(self, command: str) -> tuple[bool, str]:
    """验证命令安全性。"""

    # 1. 解析命令
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return False, f"命令语法错误：{e}"

    if not parts:
        return False, "空命令"

    base_cmd = parts[0]

    # 2. 检查白名单
    if base_cmd not in self._allowed_commands:
        return False, f"命令 '{base_cmd}' 不在允许列表中"

    # 3. 检查危险模式
    dangerous_patterns = ["|", "&&", "||", ";", "`", "$(", ">", "<", "&"]
    for pattern in dangerous_patterns:
        if pattern in command:
            return False, f"命令包含危险字符：{pattern}"

    # 4. 检查路径遍历
    if ".." in command:
        return False, "禁止路径遍历"

    return True, ""
```

---

## 6. 使用示例

### 6.1 用户对话示例

```
用户：爱莉，帮我看看当前目录下有哪些文件

爱莉：好的，让我检查一下~
[调用 sandbox 工具执行 "ls -la"]
爱莉：查看到了以下内容：
total 48
drwxr-xr-x  8 user user 4096 Mar 18 01:51 .
drwxr-xr-x 20 user user 4096 Mar 16 21:28 ..
...

用户：爱莉，运行一下这个 Python 脚本

爱莉：让我来执行看看~
[调用 sandbox 工具执行 "python3 test.py"]
爱莉：执行结果：
Hello, World!
```

### 6.2 Tool Schema

```json
{
  "type": "function",
  "function": {
    "name": "sandbox",
    "description": "在沙盒环境中执行命令",
    "parameters": {
      "type": "object",
      "properties": {
        "command": {
          "type": "string",
          "description": "要执行的命令"
        },
        "timeout": {
          "type": "integer",
          "description": "超时时间（秒），默认 30"
        }
      },
      "required": ["command"]
    }
  }
}
```

---

## 7. 扩展功能（可选）

### 7.1 代码执行沙盒

- Python 代码执行（使用 `exec` 受限环境）
- JavaScript 代码执行（使用 Node.js）

### 7.2 文件系统沙盒

- 受限的文件读取/写入
- 目录树浏览

### 7.3 会话持久化

- 保持工作目录状态
- 多命令连续执行

---

## 8. 实施步骤

1. **创建插件目录结构**
2. **编写 manifest.json**
3. **实现 config.py 配置类**
4. **实现 sandbox_tool.py 工具**
5. **实现 sandbox_service.py 服务（可选）**
6. **编写单元测试**
7. **测试安全边界**
8. **文档完善**

---

## 9. 风险评估

| 风险 | 缓解措施 |
|------|----------|
| 命令注入 | 严格的白名单 + 参数转义 |
| 路径遍历 | 工作目录限制 + `..` 过滤 |
| 资源耗尽 | 超时 + 输出大小限制 |
| 敏感信息泄露 | 清理环境变量 + 日志审计 |
| 提权攻击 | 禁止 sudo/su + 最小权限运行 |

---

## 10. 配置示例

### config/plugins/sandbox_plugin/config.toml

```toml
# 安全设置
[security]
# 是否启用沙盒功能
# 值类型：bool, 默认值：true
enabled = true

# 工作目录
# 值类型：str, 默认值："/tmp/sandbox"
workdir = "/root/Elysia/Neo-MoFox_Deployment/Neo-MoFox"

# 命令超时时间（秒）
# 值类型：int, 默认值：30
timeout = 30

# 最大输出字节数
# 值类型：int, 默认值：10240
max_output_size = 10240

# 允许执行的命令白名单
# 值类型：array
allowed_commands = [
    "ls", "cat", "head", "tail", "pwd", "echo",
    "python3", "grep", "find", "wc"
]
```

---

## 11. 总结

本方案设计了一个安全的沙盒环境插件，核心特点：

1. **命令白名单** - 只允许执行预定义的安全命令
2. **多重安全检查** - 命令解析、模式匹配、路径验证
3. **资源限制** - 超时控制、输出大小限制
4. **可配置性** - 通过 TOML 配置文件灵活调整策略
5. **易于扩展** - 可按需添加新的安全命令或功能

下一步可以根据此方案编写实际代码实现。
