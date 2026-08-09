# Elysium API v1 文档

阶段三导出 `/api/v1` 公共接口的配套文档。OpenAPI schema 由
`scripts/generate_api_openapi.py` 生成，其余文档为人工维护并需与实现核对。

| 文件 | 内容 | 生成方式 |
| --- | --- | --- |
| `openapi.json` | 完整 OpenAPI schema（134 操作，无重复 operation id） | `python scripts/generate_api_openapi.py` |
| `events.md` | 事件目录：事件信封与各通道事件类型 | 人工维护 |
| `errors.md` | 错误码目录：HTTP 状态与 `error.code` 语义 | 人工维护 |
| `permissions.md` | 权限矩阵：身份、scope、资源授权与实现状态 | 人工维护 |
| `frontend-example.md` | 前端最小参考实现（会话、调用、SSE） | 人工维护 |
| `verification.md` | 阶段三 P3-14 验证报告（已验收/暂不验收/已回退） | 人工维护 |

## 生成与校验

```bash
# 重新生成 openapi.json（只注册路由，不执行 endpoint，使用轻量 provider）
python scripts/generate_api_openapi.py

# 校验无重复 operation id、与 inventory 契约差异
python - <<'PY'
import json
with open("docs/api/openapi.json", encoding="utf-8") as f:
    schema = json.load(f)
ids = [
    op["operationId"]
    for methods in schema["paths"].values()
    for method, op in methods.items()
    if method in {"get", "post", "put", "delete", "patch"}
]
dups = sorted({op for op in ids if ids.count(op) > 1})
assert not dups, f"duplicate operation ids: {dups}"
print(f"operations={len(ids)} duplicate_ids={dups or 'NONE'}")
PY
```

> schema 不含任何凭据、路径、私聊原文或运行数据；WebSocket 端点不进入
> OpenAPI `paths`（FastAPI 不支持），由 `permissions.md` 单独记录。
