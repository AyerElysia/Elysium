# 黑曜石笔记（Obsidian Vault）

通过 `nucleus_read_file` / `nucleus_write_file` / `nucleus_edit_file` / `nucleus_list_files` 直接操作。

## Vault 路径

```
/mnt/c/Users/26652/Documents/Obsidian/AyerElysia的笔记
```

> 这是 Windows 上的 Obsidian 仓库，WSL 通过 /mnt/c 直接访问。文件即时同步，无需额外操作。

## 目录结构

```
AyerElysia的笔记/
├── 日记/
│   ├── 2026年7月份日记.md      ← 主人的日常日记
│   ├── 爱莉日记/2026年7月份.md ← 爱莉自己的日记
│   ├── 崩坏三日记/2026.7.md    ← 游戏日记
│   └── 游戏日记/2026年7月份.md ← 游戏日记
├── 灵感与文章/                 ← 灵感、随笔、文章
├── 科研工作/                   ← 科研相关笔记
├── 艺术体验/                   ← 音乐/绘画/影视体验
└── pics/                       ← 图片资源
```

## 使用约定

### 读取笔记
```
nucleus_read_file(path="/mnt/c/Users/26652/Documents/Obsidian/AyerElysia的笔记/日记/爱莉日记/2026年7月份.md")
```

### 写日记（追加模式）
用 `nucleus_edit_file` 在文件末尾追加，格式：
```markdown
## 7月23日

今天的内容...

---
```

### 新建笔记
用 `nucleus_write_file` 创建，路径按分类放入对应目录：
- 日记类 → `日记/`
- 灵感/文章 → `灵感与文章/`
- 科研 → `科研工作/`
- 艺术 → `艺术体验/`

### 列出笔记
```
nucleus_list_files(path="/mnt/c/Users/26652/Documents/Obsidian/AyerElysia的笔记/日记")
```

## 注意事项

- 文件编码 UTF-8，Obsidian 标准 Markdown
- 图片放在 `pics/` 或根目录，引用格式 `![[图片名.png]]`（Obsidian wiki-link）
- 不要删除或覆盖已有笔记，只追加或新建
- 爱莉日记按月一个文件：`爱莉日记/2026年X月份.md`
- 写完后主人打开 Obsidian 就能直接看到
