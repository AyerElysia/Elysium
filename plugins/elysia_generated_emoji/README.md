# Elysia Generated Emoji

爱莉的现场生成表情包插件。这个插件只负责聊天表情包/贴纸/视觉表达，不负责通用画图。

特点：

- 不读取旧表情包数据库。
- 不依赖 `elysia_art_studio`；通用画图请走爱莉小画室能力。
- 直接调用 NovelAI Image Generation API。
- 生成失败时不回退旧图。
- 支持多个表达风格 preset。
- 图片文字由本地 Pillow 叠字，避免模型生成中文乱码。
- 只应在用户消息触发的会话轮次使用，不用于无人触发的主动心跳刷图。

## 配置

在 `config/plugins/elysia_generated_emoji/config.toml` 中启用：

```toml
[plugin]
enabled = true

[api]
api_keys = ["pst-..."]
```

也可以用环境变量：

```bash
export NOVELAI_API_KEY="pst-..."
```

## Action

`action-generate_emoji_meme`

参数：

- `intent`：表达意图。
- `scene`：画面描述。
- `style`：`chibi_sticker`、`soft_illustration`、`meme_reaction`、`sleepy_goodnight`、`angry_cute`、`workbench`。
- `caption`：本地叠字短句。
- `resolution`：`square`、`landscape`、`portrait`、`1024x1024` 等。
