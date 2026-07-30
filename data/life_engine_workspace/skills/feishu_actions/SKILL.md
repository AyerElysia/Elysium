# 飞书操作能力清单（lark-cli）

通过 `tool-platform_action` 调用，固定 `platform='feishu'`。

**action 格式**：lark-cli 命令字符串（不含 `lark-cli` 前缀），例如：
- `im +messages-send --chat-id oc_xxx --text 你好`
- `calendar +agenda`
- `docs +fetch --url https://xxx.feishu.cn/docx/xxx`

params 通常留空 `{}`，所有参数直接写在 action 字符串里。

---

## 快速参考

| 用途 | action 示例 |
|------|-------------|
| 查命令帮助 | `<domain> --help` |
| 查 API 参数 | `schema <service>.<resource>.<method>` |
| 预览请求（不执行） | 任意命令 + `--dry-run` |
| 过滤输出 | 任意命令 + `--jq '.items[].name'` |

---

## 即时通讯 (im)

| 命令 | 说明 |
|------|------|
| `im +messages-send --chat-id <oc_xxx> --text <内容>` | 发文本消息 |
| `im +messages-send --chat-id <oc_xxx> --markdown <md>` | 发 Markdown 消息 |
| `im +messages-send --user-id <ou_xxx> --text <内容>` | 给个人发消息 |
| `im +messages-reply --message-id <om_xxx> --text <内容>` | 引用回复 |
| `im +chat-messages-list --chat-id <oc_xxx>` | 查聊天历史 |
| `im +messages-search --query <关键词>` | 搜索消息 |
| `im +messages-mget --message-ids <om_1,om_2>` | 批量获取消息 |
| `im +chat-list` | 我加入的群列表 |
| `im +chat-search --query <群名>` | 搜索群 |
| `im +chat-members-list --chat-id <oc_xxx>` | 群成员列表 |
| `im +chat-create --name <群名> --user-ids <ou_1,ou_2>` | 建群 |
| `im +chat-update --chat-id <oc_xxx> --name <新名>` | 改群名 |
| `im +messages-resources-download --message-id <om_xxx> --file-key <key> --type image` | 下载消息中的图片/文件 |
| `im messages.delete --message-id <om_xxx>` | 撤回消息 |
| `im pins.create --chat-id <oc_xxx> --message-id <om_xxx>` | 置顶消息 |
| `im reactions.create --message-id <om_xxx> --reaction-type THUMBSUP` | 表情回应 |

## 云文档 (docs / markdown)

| 命令 | 说明 |
|------|------|
| `docs +fetch --url <文档URL>` | 读取文档内容 |
| `docs +fetch --url <URL> --doc-format im-markdown` | 以 Markdown 读取（适合转发） |
| `docs +create --title <标题> --markdown <内容>` | 创建文档 |
| `docs +update --url <URL> --markdown <新内容>` | 覆盖更新文档 |
| `docs +search --query <关键词>` | 搜索文档 |
| `markdown +create --title <标题> --content <md>` | 创建 Markdown 文件 |
| `markdown +fetch --token <file_token>` | 读取 Markdown 文件 |

## 云盘 (drive)

| 命令 | 说明 |
|------|------|
| `drive +list` | 列出文件 |
| `drive +upload --file <路径> --folder <folder_token>` | 上传文件 |
| `drive +download --token <file_token> --output <路径>` | 下载文件 |

## 多维表格 (base)

| 命令 | 说明 |
|------|------|
| `base +list-tables --app-token <token>` | 列出数据表 |
| `base +list-records --app-token <token> --table-id <id>` | 查记录 |
| `base +create-record --app-token <token> --table-id <id> --fields '<json>'` | 新增记录 |
| `base +update-record --app-token <token> --table-id <id> --record-id <id> --fields '<json>'` | 更新记录 |

## 电子表格 (sheets)

| 命令 | 说明 |
|------|------|
| `sheets +read --spreadsheet <token> --range <A1:D10>` | 读取单元格 |
| `sheets +write --spreadsheet <token> --range <A1> --values '<json>'` | 写入 |
| `sheets +append --spreadsheet <token> --range <A1> --values '<json>'` | 追加行 |

## 日历 (calendar)

| 命令 | 说明 |
|------|------|
| `calendar +agenda` | 今日/近期日程 |
| `calendar +create-event --summary <标题> --start <时间> --end <时间>` | 创建日程 |
| `calendar +freebusy --user-ids <ou_xxx> --start <时间> --end <时间>` | 查空闲 |

## 邮件 (mail)

| 命令 | 说明 |
|------|------|
| `mail +list` | 收件箱列表 |
| `mail +read --message-id <id>` | 读邮件 |
| `mail +send --to <邮箱> --subject <主题> --body <内容>` | 发邮件 |
| `mail +search --query <关键词>` | 搜索邮件 |

## 任务 (task)

| 命令 | 说明 |
|------|------|
| `task +list` | 任务列表 |
| `task +create --summary <标题> --due <截止时间>` | 创建任务 |
| `task +done --task-id <id>` | 完成任务 |

## 通讯录 (contact)

| 命令 | 说明 |
|------|------|
| `contact +search --query <姓名>` | 搜索用户 |
| `contact +get --user-id <ou_xxx>` | 查用户信息 |

## 知识库 (wiki)

| 命令 | 说明 |
|------|------|
| `wiki +list-spaces` | 知识空间列表 |
| `wiki +list-nodes --space-id <id>` | 节点列表 |
| `wiki +fetch --url <wiki_url>` | 读取 wiki 文档 |

## 审批 (approval)

| 命令 | 说明 |
|------|------|
| `approval +pending` | 待办审批 |
| `approval +approve --instance-id <id> --task-id <id>` | 同意 |
| `approval +reject --instance-id <id> --task-id <id> --comment <原因>` | 拒绝 |

## 其他域

| 域 | 说明 |
|----|------|
| `attendance` | 考勤打卡记录 |
| `okr` | OKR 目标/关键结果 |
| `vc` | 会议记录/纪要/录制 |
| `minutes` | 妙记内容 |
| `slides` | 幻灯片 |
| `whiteboard` | 画板 |
| `mindnotes` | 思维笔记 |

---

## 使用技巧

1. **不确定参数？** 先 `schema <service>.<resource>.<method>` 查看完整参数说明
2. **不确定命令？** 先 `<domain> --help` 查看该域所有命令
3. **预览不执行**：加 `--dry-run` 查看将要发送的请求
4. **过滤输出**：加 `--jq '.items[].name'` 只取需要的字段
5. **身份切换**：默认 bot 身份，加 `--as user` 用用户身份（需 user 登录）
6. **分页**：加 `--page-all` 自动翻页获取全部数据

## 安全限制

以下操作被工具层禁止：
- `--yes`（高危操作自动确认）
- `auth login` / `config set` / `config init`（认证/配置变更）
