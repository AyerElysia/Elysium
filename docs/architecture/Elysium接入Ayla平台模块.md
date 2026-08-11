# Elysium 接入 Ayla 平台模块

> 文档状态：待实施（本文是新增 Ayla 平台的权威契约与实施步骤，供 AI 直接执行；实施后以代码与契约测试为准，不以本文行数为验收证据）
>
> 文档性质：权威文档。Elysium 侧新增「第四个聊天通道」Ayla 的接入方案，与 [平台适配器.md](./平台适配器.md) 同级的接入契约；凡与本文冲突，以本文与当前代码为准。
>
> 上位规划：阶段三 [Elysium 应用后端接口导出开发步骤](./阶段三-Elysium应用后端接口导出开发步骤.md)（§9 聊天模块、§7.3 SSE 契约、§21 鉴权 scope）；阶段四「Elysia 多媒体独立应用」Ayla 侧桥接（`Ayla/backend/apps/elysia_bridge/`，M4-4）。

---

## 0. 一句话定位

Ayla 是 Elysium 的**第四个聊天通道**，平台标识为 `ayla`。它不是又一个 WebSocket 聊天协议（如 QQ/飞书/Kook），而是**独立应用（Ayla 后端 + 前端）作为爱莉的一个受信出入口**：

- **入站**：Ayla 应用内用户给爱莉发消息 → Ayla 后端 `POST /chat/messages:inject` 注入 Elysium 主链（不依赖任何平台 Adapter）；
- **出站**：爱莉在 Elysium 侧的 `life_send_text` 回复，经 **Ayla 后端自行订阅 SSE 观察 Life Event 并投影** 为应用内消息（`elysia.reply`）送达用户；
- Elysium 侧仍需为 `platform="ayla"` 注册一个 **Adapter 实例**，保证 `MessageSender` 出站链路能找到它（否则 `life_send_text` 会像历史 bug 一样 `ConnectError` 反复重试）。

---

## 1. 平台标识与注册形态

### 1.1 平台标识

| 项目 | 值 |
|------|----|
| platform 标识 | `ayla` |
| Adapter 插件名 | `plugins/ayla_adapter/` |
| Adapter 类 `platform` 属性 | `"ayla"` |
| 注入时 `platform` 字段 | `"ayla"`（Ayla 侧 `ElysiaProfile.platform` 默认值由 `elysia-app` 改为 `ayla`） |
| 独立应用平台名（foundation） | `"ayla"` |

与现有平台对齐：

| 平台 | Adapter 插件 | `platform` 属性 | 注册位置 |
|------|-------------|----------------|---------|
| QQ | `napcat_adapter` | `"qq"` | `plugin.py:60` |
| 飞书 | `feishu_adapter` | `"feishu"` | `adapter.py:143` `PLATFORM` |
| Kook | `kook_adapter` | `"kook"` | `plugin.py:40` |
| **Ayla** | **`ayla_adapter`（新增）** | **`"ayla"`** | **本文 §3** |

### 1.2 为什么需要 Elysium 侧 Adapter

`MessageSender.send_message`（`src/core/transport/message_send/message_sender.py`）在 `life_send_text` 出站时按 `message.platform` 推断 Adapter：

```python
# _infer_adapter_signature (message_sender.py:589)
for sig, adapter_cls in adapters.items():
    if hasattr(adapter_cls, "platform") and adapter_cls.platform == message.platform:
        return sig
# 找不到 → warning + return None → send_message 返回 False
```

- 若没有 `platform="ayla"` 的 Adapter：`life_send_text` 在 ayla 流上出站返回 False，且日志出现「未找到匹配的 Adapter: platform=ayla」。
- 若 stream 被误路由成 `feishu`（历史 bug）：`_send_to_stream` 用 `self.chat_stream.platform="feishu"` → `FeishuAdapter._send_platform_message` → `ConnectError`（飞书未连接时）并反复重试。

因此 Ayla Adapter 是本模块的**必要工程约束**（不是可选项），与 `life_send_text` 出站链路闭环直接相关。

### 1.3 双通道模型（关键语义）

Ayla 出站有**两条可能路径**，必须明确主次，避免重复投递：

| 通道 | 方向 | 说明 | 角色 |
|------|------|------|------|
| **Ayla 侧 SSE 观察投影** | Elysium → Ayla 应用 | Ayla 后端 `run_bridge_loop` 订阅 `GET /events/stream`（`event_type=chat.message`、`stream_id=ayla流`）→ 投影为应用内消息 + 广播 `elysia.reply` | **主通道（唯一投递通道）** |
| Elysium 侧 `ayla_adapter` 出站 | Elysium → Ayla 后端 | 让 `life_send_text` 出站不失败；默认只做虚拟确认/审计，**不重复推送给 Ayla 应用** | 兜底/占位 |

> **决策**：Ayla 应用内用户看到爱莉回复的唯一来源是 Ayla 侧 SSE 投影（已由 M4-4 闭环）。Elysium 侧 `ayla_adapter` 不向 Ayla 应用再推一份（避免与 SSE 双写重复）。Adapter 出站职责是「让 `MessageSender` 出站成功 + 落发送历史 + 发投递事件」，投递本身由 SSE 投影完成。若未来要求 Elysium 主动推送，需在本文新增「Elysium 侧主动推送」章节并定义去重（见 §6 已知取舍）。

---

## 2. 入站链路（inject，不依赖 Adapter）

### 2.1 链路

```
Ayla 前端 ──应用 WS──> Ayla 后端(chat consumer)
   └─ on_user_message_to_elysia (elysia_bridge/services.py:406)
        └─ inject_user_message (elysia_bridge/services.py:442)
             └─ elysia_client.inject_message (elysia_client.py:288)
                  └─ POST /api/v1/chat/messages:inject
                       └─ InboundInjector.inject (src/app/api/v1/inbound_messages.py:52)
                            └─ ON_MESSAGE_RECEIVED → Distributor → Chatter
```

### 2.2 注入请求关键字段（`InboundMessageInjectRequest`）

| 字段 | 值 | 说明 |
|------|----|------|
| `stream_id` | **`ayla_<user_hash>`**（`generate_stream_id("ayla", user_id)` 独立流） | 见 §5，从根上避开历史飞书流 |
| `platform` | `"ayla"`（显式传） | 快速路径，不扫描账本投影 |
| `chat_type` | `"private"` | 私聊 |
| `content` | 用户文本 | |
| `sender_id` | 应用内用户 ID | 回显来源 |
| `sender_name` | 应用内昵称 | |

`InboundInjector.inject` 逻辑（`inbound_messages.py:58-86`）：显式 `platform`/`chat_type` 直接采用（快速路径）；省略时才 `find_stream_target` 从账本投影。**Ayla 必须显式传 `platform="ayla"`**，避免投影回退。

### 2.3 注入的 `chat:write` scope

`POST /chat/messages:inject` 需要 `chat:write` scope（`inbound_messages.py:132`）。Ayla 后端使用阶段三 service credential + session token，其授权范围必须包含 `chat:write`（阶段三 §21）。

---

## 3. Elysium 侧注册改动清单

以下为最小闭环改动。新增 `plugins/ayla_adapter/` 插件 + 注册平台映射。

### 3.1 新增 `plugins/ayla_adapter/`

```
plugins/ayla_adapter/
├── __init__.py
├── plugin.py        # AylaAdapterPlugin（@register_plugin）+ AylaAdapter
├── config.py        # AylaAdapterConfig（plugin 节 + ayla 节：backend_url/credential 等）
└── sender.py        # 出站发送：MessageEnvelope → Ayla 后端（可选，本期占位）
```

`AylaAdapter` 关键属性与方法（对齐 `BaseAdapter` 契约，`src/core/components/base/adapter.py`）：

```python
class AylaAdapter(BaseAdapter):
    adapter_name = "ayla_adapter"
    adapter_version = "1.0.0"
    adapter_description = "Ayla 独立应用聊天通道（platform=ayla）"
    platform = "ayla"

    async def on_adapter_loaded(self) -> None:
        # 校验配置（backend_url 等），不主动连接（Ayla 入站不依赖本 Adapter）

    async def on_adapter_unloaded(self) -> None:
        # 清理

    async def from_platform_message(self, raw) -> MessageEnvelope | None:
        # 本期不接收入站（入站走 inject），返回 None 或抛 NotImplementedError
        # 契约：本 Adapter 只出站，见 §1.3

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:
        # 本期：虚拟确认（记录已发送 + 可选 POST 到 Ayla 后端占位）。
        # 不重复推给 Ayla 应用（SSE 投影是主通道）；若实现 POST，必须带幂等键。

    async def get_bot_info(self) -> dict:
        return {"bot_id": "elysia", "bot_name": "爱莉", "platform": "ayla"}

    async def health_check(self) -> bool:
        # 返回配置是否有效（Ayla 无长连接，不依赖连接状态）
        return self._config_ok
```

> **注意**：`BaseAdapter.health_check` 默认调 `is_connected()`；Ayla 无传输层，恒为 False 会导致每 30 秒误判不健康并触发 `reconnect()`（参考 kook_adapter 的 `health_check` 覆写，`kook_adapter/plugin.py:136`）。AylaAdapter **必须覆写 `health_check`** 返回配置有效性，不能沿用默认。

> **注意**：Ayla 无长连接，`on_adapter_loaded` 不应抛「连接失败」；只做配置校验。配置无效时按现有 Adapter 惯例抛 `RuntimeError` 阻止加载。

### 3.2 注册平台映射（`src/app/api/v1/foundation.py`）

`_KNOWN_ADAPTER_PLUGINS`（foundation.py:92）追加：

```python
_KNOWN_ADAPTER_PLUGINS = {
    "feishu_adapter": "feishu",
    "napcat_adapter": "qq",
    "neko_surface": "neko",
    "ayla_adapter": "ayla",   # 新增
}
```

这样 foundation 的 capabilities/readiness 能识别 `ayla_adapter` 插件，provider=`"ayla"`。

### 3.3 命令端点 ProviderFacadeRegistry（`src/app/api/v1/chat_runtime.py`）

`create_chat_command_service` 的 `ProviderFacadeRegistry`（chat_runtime.py:34-43）追加 ayla 平台映射。Ayla 是「应用通道」，命令操作（recall/read 等）由 Ayla 应用内自有逻辑处理，Elysium 侧 facade 可声明 `capabilities` 全为 False（`capability_disabled`），或映射到占位 client：

```python
from .chat_platforms import AylaChatFacade  # 新增

providers=ProviderFacadeRegistry({
    "feishu": FeishuChatFacade(...),
    "qq": NapCatChatFacade(...),
    "ayla": AylaChatFacade(_LateBoundAylaClient()),  # 新增
})
```

`AylaChatFacade` 见 §3.4。若 Ayla 平台暂不支持任何命令操作，`capabilities()` 可返回全 False——`chat_commands.py:368` 会以 `capability_disabled` 拒绝，保证命令端点对 ayla 流可观测、不误路由到其它平台 facade。

### 3.4 新增 `AylaChatFacade`（`src/app/api/v1/chat_platforms.py`）

对齐 `FeishuChatFacade`/`NapCatChatFacade` 形态（`chat_platforms.py`）：

```python
@dataclass(slots=True)
class AylaChatFacade:
    """Ayla 应用通道命令 facade（本期能力空）。"""
    client: AylaActionClient | None = None
    platform: str = "ayla"

    def capabilities(self) -> Mapping[ChatAction, bool]:
        return {action: False for action in ChatAction}  # 本期不开放命令操作

    async def perform(self, action, *, target, payload):
        raise CapabilityError(f"Ayla does not support {action.value!r}")
```

> **决策**：Ayla 命令操作（撤回/已读/表情等）由 Ayla 应用内自有逻辑处理，Elysium 命令端点不代为执行。本期 capabilities 全 False；后续如要开放，再按 Ayla 应用 REST 契约实现并补充测试。

### 3.5 出站 MessageSender 识别（`message_sender.py`）

`_infer_adapter_signature`（message_sender.py:589）通过 registry 按 `adapter_cls.platform == message.platform` 匹配。注册 `AylaAdapter` 后，`life_send_text` 在 ayla 流上出站会自动命中 `ayla_adapter`，不再「未找到匹配 Adapter」。**无需改 `_should_use_virtual_send`**（virtual 平台集合 `{"live", "game.sts2.operator", "game.minecraft.operator"}` 不包含 ayla；ayla 走真实 Adapter 出站，见 §1.3）。

---

## 4. 出站链路（SSE 观察为主）

### 4.1 链路

```
爱莉 life_send_text (LifeChatter)
   └─ MessageSender.send_message
        └─ _infer_adapter_signature → "ayla_adapter"（§3.5）
             └─ AylaAdapter._send_platform_message（虚拟确认，§1.3）
                  └─ 写发送历史 + ON_MESSAGE_DELIVERED
Ayla 后端 run_bridge_loop（SSE 订阅 stream_id=ayla流）
   └─ GET /events/stream?event_type=chat.message&stream_id=<ayla流>
        └─ _handle_envelope → aproject_elysia_reply → 落库 + elysia.reply 广播
             └─ Ayla 前端渲染
```

### 4.2 SSE 订阅关键参数（Ayla 侧 `stream_sse`，`elysia_client.py:372`）

| 参数 | 值 | 说明 |
|------|----|------|
| `event_type` | `chat.message` | 只订阅聊天事件 |
| `stream_id` | **Ayla profile 绑定的 ayla 流** | 必须与 inject 用的流一致（§5），否则爱莉回复事件过滤不匹配 |
| `include_payload` | `true` | 需要 payload.content |
| `projection` | `full` | |
| `Last-Event-ID` / `cursor` | 断线续传 | history_gap 按 recovery cursor 重连 |

### 4.3 SSE 事件类型与投影

Ayla `_handle_envelope`（`elysia_bridge/services.py:506`）按 `envelope.stream_id == profile.stream_id` 过滤，命中后 `aproject_elysia_reply` 投影为应用内消息（幂等键 `elysia-<event_id 哈希>`）并广播 `elysia.reply`。

**依赖条件**：Ayla 侧 `ElysiaProfile.stream_id` 必须是 ayla 独立流（§5），SSE 订阅的 `stream_id` 与之一致。若仍是历史飞书流，SSE 过滤永远不匹配 → 爱莉回复投影不到应用内（这正是本次修复的核心之一）。

---

## 5. 独立 ayla stream（避开历史飞书流）

### 5.1 背景与根因

历史 bug：Ayla profile 绑定的 stream_id 是 Elysium 侧一个历史飞书私聊流。bridge inject 带 `platform="elysia-app"`，`get_or_create_stream(platform="elysia-app", stream_id=<飞书流hash>)` 发现流已存在 → 直接返回缓存的飞书流，platform 保持 `"feishu"`。于是：

- `chat_stream.platform = "feishu"` → LifeChatter 决策显示「赩汐的私聊」；
- `life_send_text` → `_send_to_stream` → `platform="feishu"` → `FeishuAdapter` → `ConnectError` 反复重试。

`get_or_create_stream`（`stream_manager.py:109`）语义：**stream_id 已存在则直接返回缓存/数据库中的流，platform 不更新**。所以「复用旧流 + 显式传新 platform」无法生效，platform 永远来自首次创建时的值。

### 5.2 修复方案：独立 ayla 流

`ChatStream.generate_stream_id`（`stream.py:304`）的 key 规则：

```python
# private: f"{platform}_{user_id}_private"
# group:   f"{platform}_{group_id}"
# → sha256(key)
```

**platform 参与 stream_id 哈希**。因此：

1. **Ayla 侧 `ElysiaProfile.platform` 默认值 `elysia-app` → `ayla`**（`Ayla/backend/apps/elysia_bridge/models.py:44`）；
2. **Ayla 侧 `ElysiaProfile.stream_id` 重新生成为 `generate_stream_id("ayla", <应用内 user 的稳定外部键>)`**，与任何历史飞书流（key=`feishu_<uid>_private`）都不同，天然是独立流；
3. **inject 时显式 `platform="ayla"` + 新 `stream_id`**（`inbound_messages.py` 快速路径直接采用），不再命中历史飞书流；
4. **SSE 订阅 `stream_id` 用同一新流**（`run_bridge_loop`），过滤匹配。

> **Ayla 侧同步改动**（跨仓库，属本模块 Ayla 侧契约）：`models.py` platform 默认值、`stream_id` 生成逻辑、inject 传 `platform`。Ayla 侧已实现 `elysia_profile.platform` 字段（默认 `elysia-app`），将其改为 `ayla` 并与 `stream_id` 联动即可。

### 5.3 验收判据

- `chat_streams` 表中 Ayla 爱莉流：`stream_id` 前缀哈希对应 `ayla_<uid>_private`，`platform="ayla"`、`group_name` 不含「赩汐的私聊」等飞书痕迹；
- 不再出现「赩汐的私聊」/ `FeishuAdapter.ConnectError` 日志；
- Ayla profile 的 `stream_id` 与 inject、SSE 订阅三处一致。

---

## 6. 已知取舍与待确认（先记录，不阻塞）

| 项 | 取舍 | 说明 |
|----|------|------|
| 出站双通道 | SSE 投影为主，Adapter 虚拟确认 | 避免重复投递；若未来要求 Elysium 主动推送，需另文定义去重 |
| 命令操作 | Ayla 平台 `capabilities` 全 False | 撤回/已读等由 Ayla 应用内自有逻辑处理，不代为执行 |
| 入站 Adapter | AylaAdapter 不接收入站 | 入站走 inject，Adapter 只出站 + 状态展示 |
| Adapter 出站实现 | 本期虚拟确认 | 不 POST 到 Ayla 后端（避免与 SSE 双写）；后续如需主动推送再实现 POST + 幂等 |
| `_should_use_virtual_send` | 不改 | virtual 平台集合不含 ayla；ayla 走真实 Adapter |
| Ayla 侧默认值 | `elysia-app` → `ayla` | 跨仓库契约，需 Ayla 侧一并改 |

---

## 7. 实施清单（供 AI 直接执行）

### 7.1 Elysium 侧

- [ ] 新增 `plugins/ayla_adapter/`（plugin.py + config.py + sender.py），`AylaAdapter.platform="ayla"`，覆写 `health_check`/`get_bot_info`/`from_platform_message`/`_send_platform_message`
- [ ] `src/app/api/v1/foundation.py` `_KNOWN_ADAPTER_PLUGINS` 追加 `"ayla_adapter": "ayla"`
- [ ] `src/app/api/v1/chat_platforms.py` 新增 `AylaChatFacade`（capabilities 全 False）
- [ ] `src/app/api/v1/chat_runtime.py` `ProviderFacadeRegistry` 追加 `"ayla": AylaChatFacade(...)`
- [ ] 契约测试：AylaAdapter 出站不抛错 + `_infer_adapter_signature("ayla")` 命中；foundation 识别 ayla_adapter；chat_commands 对 ayla 流返回 `capability_disabled`

### 7.2 Ayla 侧（跨仓库）

- [ ] `ElysiaProfile.platform` 默认 `elysia-app` → `ayla`
- [ ] `ElysiaProfile.stream_id` 用 `generate_stream_id("ayla", ...)` 重新生成（避开历史飞书流）
- [ ] inject 显式传 `platform="ayla"` + 新 `stream_id`
- [ ] SSE 订阅 `stream_id` 与 profile 一致
- [ ] 契约测试：profile 默认值、inject 带 ayla 平台、SSE 过滤匹配 ayla 流

### 7.3 文档登记

- [ ] `docs/README.md` 「从这里开始」补一条本文链接

---

## 8. 验收标准

### 8.1 注册级验收（本期）

| 项 | 标准 |
|----|------|
| 平台标识 | `ayla` 注册进 registry，`_infer_adapter_signature("ayla")` 命中 `ayla_adapter` |
| 出站 | `life_send_text` 在 ayla 流上出站返回成功（虚拟确认），不再 `ConnectError`/「未找到匹配 Adapter」 |
| 路由 | ayla 流不误路由到 feishu/qq/kook |
| 命令 | ayla 流命令返回 `capability_disabled`（可观测、不误路由） |
| 契约测试 | 上述全部有对应契约测试且全绿 |
| 独立流 | Ayla profile `stream_id` 为 ayla 独立流，三处一致 |

### 8.2 端到端验收（待真实环境，如实标注）

以下需真实 Ayla 后端 + 真实 Elysium + service credential 才可验收，本期**未验收**，如实标注：

- [ ] 用户经 Ayla 应用发消息 → 爱莉回复经 SSE 投影显示在 Ayla 前端（未验收）
- [ ] Ayla 断线重连后按 cursor 续传，不重复/不丢（未验收）
- [ ] Ayla 平台真实消息撤回/已读（本期 capabilities 全 False，未开放）（未验收）

---

## 9. 相关文档

- [平台适配器.md](./平台适配器.md) — QQ/飞书/Kook/N.E.K.O 适配器权威文档（Ayla 为第四个通道）
- [阶段三 - Elysium 应用后端接口导出开发步骤](./阶段三-Elysium应用后端接口导出开发步骤.md) — inject / SSE / 命令 / scope 契约
- Ayla 侧 `Ayla/backend/apps/elysia_bridge/` — M4-4 爱莉桥接（入站 inject + SSE 出站投影）
- Ayla 侧 `Ayla/docs/plans/阶段四-M4-4爱莉桥接开发步骤.md` — M4-4 开发步骤与验收清单
