# API v1 前端最小示例

> 这是 `/api/v1` 的前端最小参考实现，演示：① 建立受限会话；② 用 Bearer 调用
> 只读接口；③ 读取事件流（SSE）。它不是生产脚手架，仅用于契约验收与联调。
>
> 安全边界（与阶段三 6.1 一致）：
> - Bearer token **不得**落入浏览器长期明文存储；示例仅存内存；
> - bootstrap 必须绑定 Origin 与一次性 challenge，不能把 localhost 视为天然可信；
> - 普通前端 audience 不能访问 `/admin/*`，管理操作走管理员 audience。

## 1. 环境假设

- 后端挂载点：`/api/v1`
- 用户前端 audience：`elysium-user-frontend`
- 本机 bootstrap：`POST /api/v1/auth/sessions`，`grant_type=bootstrap_challenge`

## 2. 最小示例（TypeScript / Fetch）

```ts
// api-client.ts —— 仅内存持有会话，不写 localStorage
const ORIGIN = window.location.origin;
const AUDIENCE = "elysium-user-frontend";

type Session = {
  access_token: string;
  expires_at: string;
  identity: { actor_id: string; role: string; scopes: string[] };
};

let session: Session | null = null;

export async function bootstrapSession(
  challenge: string
): Promise<Session> {
  const res = await fetch("/api/v1/auth/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      grant_type: "bootstrap_challenge",
      audience: AUDIENCE,
      bootstrap_challenge: challenge,
      origin: ORIGIN,
    }),
  });
  if (!res.ok) throw await toError(res);
  session = (await res.json()) as Session;
  return session;
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (session) headers.set("Authorization", `Bearer ${session.access_token}`);
  const res = await fetch(`/api/v1${path}`, { ...init, headers });
  if (!res.ok) throw await toError(res);
  return (await res.json()) as T;
}

function toError(res: Response) {
  return res.json().then((body) => {
    const e = body?.error;
    return new Error(`${res.status} ${e?.code ?? "unknown"}: ${e?.message ?? ""}`);
  });
}

// 只读调用示例：当前身份 + 健康状态
export async function whoami() {
  return call<{ actor_id: string; role: string; scopes: string[] }>("/auth/me");
}

export async function health() {
  return call<{ status: string }>("/health");
}
```

## 3. SSE 事件流订阅（含断线恢复）

```ts
// events.ts —— 使用 Last-Event-ID 恢复；cursor 与 Last-Event-ID 一致才有效
let lastEventId = "";

export function subscribeEvents(
  filter: Record<string, string | string[]>,
  onEvent: (envelope: unknown) => void,
  onError: (err: Error) => void,
  signal: AbortSignal
): void {
  const params = new URLSearchParams();
  Object.entries(filter).forEach(([k, v]) => {
    if (Array.isArray(v)) v.forEach((item) => params.append(k, item));
    else params.append(k, v);
  });
  const headers: Record<string, string> = {};
  if (lastEventId) headers["Last-Event-ID"] = lastEventId;

  fetch(`/api/v1/events/stream?${params}`, {
    headers: { Authorization: `Bearer ${sessionToken()}`, ...headers },
    signal,
  })
    .then((res) => {
      if (!res.ok || !res.body) throw new Error(`stream ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const pump = () =>
        reader.read().then(({ done, value }) => {
          if (done) return;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            const id = /^id:\s*(\S+)/m.exec(frame)?.[1];
            const data = /^data:\s*(.+)$/m.exec(frame)?.[1];
            if (id) lastEventId = id;
            if (data) onEvent(JSON.parse(data));
          }
          pump();
        });
      return pump();
    })
    .catch((err) => onError(err as Error));
}

// 占位：实际应来自会话模块
function sessionToken(): string {
  return "";
}
```

## 4. 调用示例：聊天历史 + 事件

```ts
// 查询最近聊天流
const streams = await call<{ streams: Array<{ stream_id: string }> }>(
  "/chat/streams?limit=10"
);

// 拉取某流消息（cursor 分页，禁止 offset）
const page = await call<{ messages: unknown[]; next_cursor: string; has_more: boolean }>(
  `/chat/streams/${streams.streams[0].stream_id}/messages?limit=20`
);
```

## 5. 权限与错误处理要点

- `401 unauthenticated`：会话过期，引导重新 bootstrap；
- `403 scope_required / role_required`：当前身份无权，前端不尝试"切换管理员"；
- `409 idempotency_conflict`：命令重放冲突，展示原命令状态；
- `429 rate_limited`：退避重试；
- 所有写入类命令（`POST /commands`、`/chat/messages:send` 等）需要
  `Idempotency-Key`；
- `delivery_unknown` 不自动重发，展示"投递结果未知"。

> 完整 schema 见同目录 `openapi.json`；事件与错误码见 `events.md`、`errors.md`。
