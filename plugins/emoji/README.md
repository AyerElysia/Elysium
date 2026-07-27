# emoji 插件

爱莉的表情包能力：收藏、检索、发送，以及现场生成。

## 两个子包

- **sender/** — 表情包的收藏、检索、发送
- **generated/** — 现场生成表情包（NovelAI，默认禁用）

## 纯视觉检索

检索不再是"匹配 VLM 写的文字描述"，而是**纯视觉语义匹配**：

```
想表达的意图（文本） ──embed──┐
                              ├─ 同一多模态语义空间 ── cosine 匹配 ── 最贴合的表情包
表情包图像 ────────embed──────┘
```

用 **Qwen3-VL-Embedding-2B** 把文本意图和表情包图像映射到同一空间，直接按视觉语义检索。
这样匹配的是"图本身在表达什么"，而不是"VLM 怎么形容这张图"，更贴近人选表情包的真实体感。

视觉服务不可用时自动回退到旧的文本描述检索（`visual.embed_enabled` 可关闭）。

### 视觉嵌入服务

独立服务，位于 `services/visual_embedding/`（独立 venv，与主项目隔离）：

```bash
cd services/visual_embedding
./start.sh                 # 建 venv + 装 torch CUDA + 下载模型 + 起服务（默认端口 8848）
./start.sh --setup-only    # 只装环境
python smoke_test.py --image <图> --match "俏皮卖萌" --mismatch "商务报表"  # 冒烟验证
```

模型：`Qwen/Qwen3-VL-Embedding-2B`（约 4GB，modelscope 优先下载），输出 2048 维。

## 仿生收藏

收藏表情包是爱莉的**自主行为**，不是后台替她决定的任务：

```
聊天收到图片
   ↓
[感知层·后台自动] 轻量 VLM 认出"这是表情包" → 登记到候选池（前注意，只登记不决策）
   ↓
[意识层·好奇心驱动] 好奇心上下文感知"有 N 张没看过的表情包"（提醒，不是任务）
   ↓
[浏览·她主动] nucleus_browse_memes —— 翻看候选
   ↓
[收藏·她决定] nucleus_collect_meme —— 收藏喜欢的（可附"为什么喜欢"），视觉 embed 入库
   ↓
[使用] 收藏后即可被纯视觉检索到
```

- **感知**是前注意的后台行为（定时扫描 media cache，认出有哪些表情包），不替她做收藏决定。
- **收藏与否完全是她的选择**：通过 `nucleus_browse_memes` / `nucleus_collect_meme` / `nucleus_dismiss_meme` 自主操作。
- **去重**：hash 去重 + 视觉去重（cosine ≥ 0.95 视为近似重复）。

## 存储

图片、向量、候选状态分离存储，用 id 关联：

| 存什么 | 存哪 |
|---|---|
| 视觉向量 + 元数据 | ChromaDB（集合 `emoji_sender_visual`） |
| 图片二进制 | 文件系统 `data/emoji/memes/` |
| 候选池 + 浏览/收藏状态 + 溯源 | SQLite `data/emoji/meme_candidates.db` |

## 存量迁移

把旧的已收藏表情包视觉化入库：

```bash
.venv/bin/python scripts/migrate_emoji_to_visual.py
```

## 配置

`config/plugins/emoji/config.toml` 的 `[sender.visual]` 与 `[sender.collection]` 段：

- `visual.embed_endpoint` — 视觉嵌入服务地址
- `visual.embed_enabled` — 纯视觉检索开关（回退用）
- `visual.match_min_cosine` — 视觉检索最低 cosine 阈值
- `collection.meme_db_path` / `meme_image_dir` / `media_cache_dir` — 存储路径
- `collection.visual_dedup_threshold` — 视觉去重阈值
