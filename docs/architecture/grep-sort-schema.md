# 文件搜索排序参数

文件 grep 的对外 schema 使用非空 `sort` 枚举 `auto | mtime | name`，默认 `auto`。自动模式在 `files_with_matches` 输出中按修改时间排序，其余输出按名称排序。执行层保留对旧直接调用空字符串的兼容；新模型工具 schema 不再声明空枚举项。


`test/plugins/life_engine/test_tool_manifests.py` 验证新 schema 不再声明空枚举项。兼容处理只影响工具输入与执行参数，不修改搜索结果、主体文件或历史。
