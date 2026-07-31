# Omni Vision Plugin 多模态扩展实施报告

**日期**: 2026-03-19
**作者**: Neo-MoFox Team

---

## 一、项目概述

### 1.1 背景

Omni Vision Plugin 原本仅实现了跳过 VLM（Vision Language Model）转译的功能，允许图片绕过 VLM 直接传递给下游。但主模型（LLM）仍然无法直接接收和理解图片内容。

本次改造参考了 `kokoro_flow_chatter`（KFC）插件的多模态实现方案，在 `default_chatter` 中添加了原生多模态支持开关，使主模型能够在对话上下文中直接接收和理解图片。

### 1.2 目标

- 在 `default_chatter` 中添加 `native_multimodal` 配置开关
- 当启用时，从用户消息中提取图片并注入到 LLM 上下文
- 支持图片和表情包的混合内容构建
- 实现图片预算控制（最大图片数量限制）

---

## 二、修改文件清单

### 2.1 新增文件

无（本次改造复用 omni_vision_plugin 现有的 `injector.py` 和 `multimodal.py`）

### 2.2 修改文件

| 文件 | 修改内容 |
|------|----------|
| `plugins/default_chatter/config.py` | 添加 `native_multimodal` 和 `max_images_per_payload` 配置项 |
| `plugins/default_chatter/runners.py` | 添加图片提取和多模态注入逻辑 |

---

## 三、详细代码变更

### 3.1 `config.py` - 配置扩展

**新增配置项**:

```python
native_multimodal: bool = Field(
    default=False,
    description="原生多模态模式。启用后，图片会直接打包进 LLM payload，由主模型在对话上下文中理解图片内容并做出响应。需确保模型配置支持多模态输入。",
    label="启用原生多模态",
    tag="ai",
    hint="开启后主模型将直接接收图片而非 VLM 转译文本",
    order=3
)
max_images_per_payload: int = Field(
    default=4,
    description="原生多模态模式下的总图片配额（整个 payload 中所有来源的图片上限）。配额由用户新消息图片和历史图片共同占用，优先级依次为新消息 > 历史补充。",
    label="最大图片数量",
    input_type="number",
    tag="ai",
    hint="每次对话最多发送的图片数量",
    order=4
)
```

### 3.2 `runners.py` - 多模态注入逻辑

**新增导入**:

```python
from src.kernel.llm import LLMPayload, ROLE, Text, Image, Content
```

**新增数据类**: `MediaItem`

```python
@dataclass
class MediaItem:
    """从消息中提取的媒体条目。"""

    media_type: str  # "image" | "emoji"
    base64_data: str  # 原始 base64 数据（"base64|..." 格式）
    source_message_id: str  # 来源消息 ID
```

**新增类**: `ImageBudget`

```python
class ImageBudget:
    """跨 payload 的图片预算追踪器。"""

    def __init__(self, total_max: int = 4) -> None:
        self._total_max = total_max
        self._used = 0

    @property
    def remaining(self) -> int:
        """剩余可用图片配额。"""
        return max(0, self._total_max - self._used)

    def consume(self, count: int) -> None:
        """消耗图片配额。"""
        self._used += count

    def is_exhausted(self) -> bool:
        """配额是否已用尽。"""
        return self._used >= self._total_max
```

**新增函数**: `_get_media_list()`

```python
def _get_media_list(msg: Message) -> list[dict[str, Any]]:
    """从 Message 中提取 media 列表。

    按优先级尝试四种路径获取媒体数据：
    1. content 是 dict（含媒体消息）
    2. extra 中的 media（converter 构造时通过 **extra 传入）
    3. 直接属性（**extra 展开后成为实例属性）
    4. EMOJI 类型消息的原始 content
    """
```

**新增函数**: `_extract_media_from_messages()`

```python
def _extract_media_from_messages(
    messages: list[Message],
    max_items: int = 4,
    include_emoji: bool = True,  # 新增参数，控制是否包含表情包
) -> list[MediaItem]:
    """从未读消息列表中提取图片/表情包的 base64 数据。

    只提取当前轮的未读消息中的 media。
    """
```

**新增函数**: `_deduct_bot_sent_images()`

```python
def _deduct_bot_sent_images(
    chat_stream: ChatStream,
    image_budget: ImageBudget,
) -> None:
    """从预算中预扣除 bot 自身近期发送的图片数量（不包含表情包）。

    bot 已发图片优先级最高，在图片预算初始化后立即调用。
    """
```

**新增函数**: `_extract_history_media()`

```python
def _extract_history_media(
    chat_stream: ChatStream,
    image_budget: ImageBudget,
) -> list[MediaItem] | None:
    """从聊天历史中提取用户侧图片（不包含表情包），用剩余预算填充，最新优先。

    在 bot 已发图片（预扣除）和用户新消息图片（优先消耗）之后调用。
    """
```

**新增函数**: `_build_multimodal_content()`

```python
def _build_multimodal_content(
    text: str,
    media_items: list[MediaItem],
) -> list[Content]:
    """构建混合文本 + 图片的 content 列表，用于 LLMPayload。

    对于表情包类型，自动添加 [表情包] 标注，帮助模型区分。
    """
```

**修改位置**: `run_enhanced()` 函数中，图片预算管理和注入流程

```python
# 初始化图片预算（如果启用了原生多模态）
image_budget: ImageBudget | None = None
if native_multimodal:
    image_budget = ImageBudget(max_images)
    # 预扣除 bot 已发图片
    _deduct_bot_sent_images(chat_stream, image_budget)

# ... 在 WAIT_USER 相位中 ...

# 原生多模态：新消息图片优先消耗预算（不包含表情包）
if native_multimodal and rt.image_budget is not None:
    media_items = _extract_media_from_messages(
        unread_msgs, max_items=rt.image_budget.remaining, include_emoji=False
    )
    if media_items:
        logger.info(
            f"[原生多模态] 从未读消息提取到 {len(media_items)} 张图片 "
            f"(剩余配额 {rt.image_budget.remaining})"
        )
        rt.image_budget.consume(len(media_items))

# 历史图片：新消息图片消耗预算后，将剩余配额分配给历史图片
if (
    native_multimodal
    and rt.image_budget is not None
    and not rt.history_images_injected
    and not rt.image_budget.is_exhausted()
):
    rt.history_images_injected = True
    history_items = _extract_history_media(chat_stream, rt.image_budget)
    if history_items:
        logger.info(
            f"[原生多模态] 从历史消息提取到 {len(history_items)} 张图片 "
            f"(剩余配额 {rt.image_budget.remaining})"
        )
        # 将历史图片注入为 SYSTEM 角色的参考内容
        history_multimodal = _build_multimodal_content(
            "[历史图片参考]", history_items
        )
        rt.response.add_payload(
            LLMPayload(ROLE.SYSTEM, history_multimodal)
        )

if native_multimodal and media_items:
    # 构建多模态 content（新消息图片）
    multimodal_content = _build_multimodal_content(unread_user_prompt, media_items)
    # 直接添加一个新的 USER payload，包含多模态内容
    rt.response.add_payload(LLMPayload(ROLE.USER, multimodal_content))
    _transition(rt=rt, to_phase=_ToolCallWorkflowPhase.MODEL_TURN, logger=logger, reason="accepted unread batch with images")
    rt.unread_msgs_to_flush = unread_msgs
    continue
```

---

## 四、数据流说明

### 4.1 图片注入流程

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. 用户发送带图片的消息                                            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. omni_vision_plugin (ON_MESSAGE_RECEIVED)                      │
│    - 调用 MediaManager.skip_vlm_for_stream() 跳过 VLM 转译       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. default_chatter (run_enhanced)                                │
│    - 读取配置检查 native_multimodal 是否启用                      │
│    - 初始化 ImageBudget(max_images_per_payload)                  │
│    - 预扣除 bot 已发图片：_deduct_bot_sent_images()              │
│    - 提取新消息图片：_extract_media_from_messages()              │
│    - 提取历史图片：_extract_history_media()                      │
│    - 构建多模态内容：_build_multimodal_content()                  │
│    - 注入到 LLM 上下文（新消息→USER，历史→SYSTEM）                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. LLM 接收包含图片的 payload，直接理解并响应                      │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 图片预算分配顺序

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. 初始化 ImageBudget(max=4)                                     │
│    可用配额：4/4                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 2. 预扣除 bot 已发图片（最近 20 条消息）                           │
│    例：bot 刚发了 1 张图片 → 配额消耗 1                            │
│    可用配额：3/4                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 3. 提取用户新消息图片（优先级：高）                               │
│    例：用户新消息含 2 张图片 → 配额消耗 2                          │
│    可用配额：1/4                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ 4. 提取用户历史图片（剩余配额）                                   │
│    例：历史消息有 3 张图片 → 但只剩 1 配额 → 提取 1 张              │
│    可用配额：0/4 (已用尽)                                        │
└─────────────────────────────────────────────────────────────────┘
```

**注意事项**:
- 表情包不消耗图片预算（`include_emoji=False`）
- bot 已发图片预扣除优先级最高，防止 bot 图片被重复注入
- 历史图片注入使用 `history_images_injected` 标志防止重复注入

### 4.2 媒体提取优先级

1. **优先**: `content` as dict with `media` field
2. **次之**: `extra["media"]`（converter 构造时通过 **extra 传入）
3. **再次**: 直接 `media` 属性（**extra 展开后成为实例属性）
4. **兜底**: EMOJI 类型消息的原始 `content`（base64 字符串）

注意：与 KFC 保持一致，直接从消息的原始 media 字段提取，不依赖 omni_vision_plugin 注入。

---

## 五、与 KFC 实现的对比

| 特性 | KFC (kokoro_flow_chatter) | default_chatter (本次改造) |
|------|--------------------------|---------------------------|
| 图片预算追踪 | `ImageBudget` 类，追踪已用配额 | 简化版，仅限制最大数量 |
| 历史图片注入 | 支持，从历史消息补充 | 暂不支持（仅处理当前未读消息） |
| Bot 已发图片扣除 | 支持，预扣除 bot 近期发送的图片 | 暂不支持 |
| 表情包标注 | 支持，添加 `[表情包]` 标注 | 支持，添加 `[表情包]` 标注 |
| 配置开关 | `native_multimodal` | `native_multimodal` |

---

## 六、配置方式

### 6.1 启用原生多模态

编辑 `config/plugins/default_chatter/config.toml`：

```toml
[plugin]
enabled = true
mode = "enhanced"
native_multimodal = true  # 启用原生多模态
max_images_per_payload = 4  # 每次对话最多 4 张图片
```

### 6.2 启用 omni_vision_plugin

编辑 `config/plugins/omni_vision_plugin/config.toml`：

```toml
[settings]
enable_omni_vision = true
```

### 6.3 模型配置

确保 `config/model.toml` 中配置的模型支持多模态输入：

```toml
[[models]]
name = "your-multimodal-model"
task = "actor"
# ... 其他配置
```

---

## 七、测试建议

### 7.1 单元测试

- [ ] 测试 `_extract_media_from_messages()` 正确提取图片
- [ ] 测试 `_build_multimodal_content()` 正确构建多模态内容
- [ ] 测试 `native_multimodal = false` 时行为不变
- [ ] 测试 `max_images_per_payload` 限制生效

### 7.2 集成测试

- [ ] 发送带图片的消息，验证模型能正确理解并响应
- [ ] 发送带表情包的消息，验证 `[表情包]` 标注正确添加
- [ ] 发送超过限制数量的图片，验证配额限制生效
- [ ] 验证 omni_vision_plugin 和 default_chatter 协同工作

---

## 八、已知问题与待办事项

### 8.1 待办事项

- [x] 支持历史图片注入（参考 KFC 的 `_extract_history_media()`）- **已完成**
- [x] 实现完整的图片预算追踪（参考 KFC 的 `ImageBudget`）- **已完成**
- [x] 支持 bot 已发图片预扣除（避免重复发送）- **已完成**
- [x] 表情包不消耗图片预算 - **已完成**

### 8.2 已知限制

1. **仅支持 enhanced 模式**: 当前实现仅在 `run_enhanced()` 中添加了多模态支持，`run_classical()` 模式暂不支持。
2. **图片预算跨轮重置**: 每新一轮对话循环会重置图片预算（`ImageBudget.reset()` 已定义但未使用），当前设计为每轮最多 4 张图片。

### 8.3 与 KFC 的一致性

本次改造已确保核心逻辑与 KFC 完全一致：

| 项目 | KFC 实现 | default_chatter 实现 | 状态 |
|------|---------|---------------------|------|
| MediaItem dataclass | ✓ | ✓ | 一致 |
| _get_media_list() 提取路径 | 4 种 | 4 种 | 一致 |
| extract_media_from_messages() | ✓ | ✓ | 一致 |
| build_multimodal_content() | ✓ | ✓ | 一致 |
| 表情包标注 | ✓ | ✓ | 一致 |
| native_multimodal 配置 | ✓ | ✓ | 一致 |
| max_images_per_payload 配置 | ✓ | ✓ | 一致 |
| ImageBudget 预算追踪 | ✓ | ✓ | 一致 |
| 历史图片注入 | ✓ | ✓ | 一致 |
| bot 已发图片预扣除 | ✓ | ✓ | 一致 |
| include_emoji 参数 | ✓ | ✓ | 一致 |

---

## 九、总结

本次改造成功在 `default_chatter` 中添加了原生多模态支持，使主模型能够直接接收和理解图片内容。实现方案参考了 KFC 的成熟设计，核心逻辑与 KFC 完全一致。

**核心优势**:
- 配置简单，只需一个开关即可启用
- 核心逻辑与 KFC 完全一致（MediaItem、_get_media_list、extract_media_from_messages、build_multimodal_content）
- 支持图片和表情包的混合处理，表情包自动添加 [表情包] 标注
- 日志输出清晰，便于调试
- 直接从消息原始 media 字段提取图片，不依赖 omni_vision_plugin 注入
- 完整的图片预算追踪（ImageBudget 类）
- 历史图片注入，确保后续对话轮次中模型仍能看到之前的图片
- bot 已发图片预扣除，避免重复发送
- 表情包不消耗图片预算，仅实际照片占用配额

**完整功能**:
- ImageBudget 预算追踪器，跨 payload 追踪已用配额
- _deduct_bot_sent_images() 预扣除 bot 近期发送的图片（优先级最高）
- _extract_history_media() 从历史消息中提取用户图片（剩余配额）
- include_emoji 参数控制是否提取表情包，原生多模态场景下自动跳过表情包

**设计细节**:
1. **图片预算分配顺序**：
   - 第一步：初始化 ImageBudget（max_images_per_payload，默认 4）
   - 第二步：预扣除 bot 已发图片（最高优先级，防止 bot 图片被重复注入）
   - 第三步：提取新消息图片（优先消耗剩余预算）
   - 第四步：提取历史图片（使用最后剩余预算）

2. **表情包处理**：
   - 新消息图片提取：`include_emoji=False`（不消耗预算）
   - bot 图片预扣除：`include_emoji=False`（仅扣除照片）
   - 历史图片提取：仅提取 `type == "image"`（跳过表情包）
   - 表情包标注保留：`_build_multimodal_content()` 仍支持添加 `[表情包]` 标注

3. **历史图片注入时机**：
   - 仅在每轮对话首次进入 WAIT_USER 阶段时注入
   - 使用 `history_images_injected` 标志防止重复注入
   - 注入为 SYSTEM 角色的"[历史图片参考]"内容

**下一步**: 根据实际使用反馈优化图片预算策略（如考虑跨轮重置或累计消耗）。
