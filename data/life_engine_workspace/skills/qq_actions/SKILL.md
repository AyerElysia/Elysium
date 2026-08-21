# QQ 操作能力清单

通过 `tool-platform_action` 调用，固定 `platform='qq'`。格式：`action` + `params`。

---

## 群公告

| action | 说明 | params |
|--------|------|--------|
| `_get_group_notice` | 获取群公告列表 | `{group_id}` |
| `_send_group_notice` | 发送群公告 | `{group_id, content, image?}` |
| `_del_group_notice` | 删除群公告 | `{group_id, notice_id}` |

## 群信息查询

| action | 说明 | params |
|--------|------|--------|
| `get_group_info` | 群基本信息 | `{group_id, no_cache?}` |
| `get_group_list` | 我加入的所有群 | `{no_cache?}` |
| `get_group_info_ex` | 群额外信息 | `{group_id}` |
| `get_group_member_info` | 群成员信息 | `{group_id, user_id, no_cache?}` |
| `get_group_member_list` | 群成员列表 | `{group_id, no_cache?}` |
| `get_group_honor_info` | 群荣誉（龙王/群聊之火等） | `{group_id, type?}` type: all/talkative/performer/legend/strong_new_king/emotion |
| `get_group_shut_list` | 被禁言列表 | `{group_id}` |
| `get_group_at_all_remain` | @全体 剩余次数 | `{group_id}` |

## 群管理操作

| action | 说明 | params |
|--------|------|--------|
| `set_group_ban` | 禁言某人（duration=0 解除） | `{group_id, user_id, duration?}` 秒 |
| `set_group_whole_ban` | 全员禁言 | `{group_id, enable?}` |
| `set_group_admin` | 设/取消管理员 | `{group_id, user_id, enable?}` |
| `set_group_card` | 设置群名片 | `{group_id, user_id, card?}` card空=删除 |
| `set_group_name` | 修改群名 | `{group_id, group_name}` |
| `set_group_special_title` | 设置专属头衔 | `{group_id, user_id, special_title?, duration?}` -1=永久 |
| `set_group_portrait` | 设置群头像 | `{group_id, file}` 路径或URL |

## 群签到

| action | 说明 | params |
|--------|------|--------|
| `set_group_sign` | 群签到 | `{group_id}` |
| `send_group_sign` | 群打卡（同上） | `{group_id}` |

## 精华消息

| action | 说明 | params |
|--------|------|--------|
| `set_essence_msg` | 设为精华 | `{message_id}` |
| `delete_essence_msg` | 移出精华 | `{message_id}` |
| `get_essence_msg_list` | 精华列表 | `{group_id}` |

## 消息操作

| action | 说明 | params |
|--------|------|--------|
| `send_group_msg` | 发群消息 | `{group_id, message}` message: 字符串或消息段数组 |
| `send_private_msg` | 发私聊消息 | `{user_id, message}` |
| `delete_msg` | 撤回消息 | `{message_id}` |
| `get_msg` | 获取消息详情 | `{message_id}` |
| `get_forward_msg` | 获取合并转发内容 | `{message_id}` |
| `get_group_msg_history` | 群消息历史 | `{group_id, count?, message_seq?}` |
| `get_friend_msg_history` | 私聊历史 | `{user_id, count?, message_seq?}` |
| `mark_msg_as_read` | 标记已读 | `{message_id}` |
| `mark_group_msg_as_read` | 群聊已读 | `{group_id}` |
| `mark_private_msg_as_read` | 私聊已读 | `{user_id}` |

## 转发消息

| action | 说明 | params |
|--------|------|--------|
| `send_group_forward_msg` | 群聊合并转发 | `{group_id, messages}` |
| `send_private_forward_msg` | 私聊合并转发 | `{user_id, messages}` |
| `forward_friend_single_msg` | 转发单条到私聊 | `{message_id, user_id}` |
| `forward_group_single_msg` | 转发单条到群聊 | `{message_id, group_id}` |

## 互动

| action | 说明 | params |
|--------|------|--------|
| `send_poke` | 戳一戳 | `{user_id, group_id?}` |
| `send_like` | 点赞（名片赞） | `{user_id, times?}` |
| `set_msg_emoji_like` | 消息表情回应 | `{message_id, emoji_id, set?}` |

## QQ 内置角色语音

这里的接口属于 QQ 平台自带角色音色，不是爱莉自己的本地 IndexTTS 音色。发送爱莉自己的普通语音到当前 QQ 私聊或群聊，应使用 `life_send_voice`；它会走本地 TTS 或已有音频，再由 OneBot `record` 消息发送。只有主体明确选择 QQ 内置角色音色时，才使用下列接口，且 `character` 必须来自当前群真实返回的角色列表。

| action | 说明 | params |
|--------|------|--------|
| `get_ai_characters` | 查询 QQ 内置语音角色列表 | `{group_id?}` |
| `send_group_ai_record` | 使用 QQ 内置角色向群聊发语音；不是本地 TTS | `{group_id, character, text}` |

## 好友与账号

| action | 说明 | params |
|--------|------|--------|
| `get_login_info` | 我的信息 | `{}` |
| `get_friend_list` | 好友列表 | `{}` |
| `get_stranger_info` | 陌生人信息 | `{user_id, no_cache?}` |
| `set_qq_profile` | 修改我的资料 | `{nickname?, company?, email?, college?, personal_note?}` |
| `set_self_longnick` | 修改个性签名 | `{longNick}` |
| `set_online_status` | 设置在线状态 | `{status}` 10在线/30离开/40隐身/50忙碌/60Q我吧/70请勿打扰 |
| `set_input_status` | 输入状态 | `{user_id, event_type}` 0停止/1正在输入 |
| `nc_get_user_status` | 查他人在线状态 | `{user_id}` |

## 文件

| action | 说明 | params |
|--------|------|--------|
| `upload_group_file` | 上传群文件 | `{group_id, file, name, folder?}` |
| `upload_private_file` | 上传私聊文件 | `{user_id, file, name}` |
| `get_group_root_files` | 群根目录文件 | `{group_id}` |
| `get_group_files_by_folder` | 群子目录文件 | `{group_id, folder_id?}` |
| `get_group_file_url` | 获取文件链接 | `{group_id, file_id, busid?}` |
| `delete_group_file` | 删除群文件 | `{group_id, file_id, busid?}` |
| `create_group_file_folder` | 创建群文件夹 | `{group_id, name, parent_id?}` |
| `download_file` | 下载文件到缓存 | `{url?, base64?, name?}` |

## 收藏与工具

| action | 说明 | params |
|--------|------|--------|
| `create_collection` | 创建文本收藏 | `{brief, content}` |
| `get_collection_list` | 收藏列表 | `{}` |
| `fetch_custom_face` | 收藏表情 | `{count?}` |
| `ocr_image` | 图片OCR | `{image}` URL或base64 |

## 请求处理

| action | 说明 | params |
|--------|------|--------|
| `set_friend_add_request` | 处理好友请求 | `{flag, approve?, remark?}` |
| `set_group_add_request` | 处理加群请求 | `{flag, approve?, reason?}` |
| `get_group_system_msg` | 群系统消息 | `{}` |

---

## 消息段格式

发送消息时 `message` 支持：
- 纯文本字符串：`"你好呀"`
- 消息段数组：
```json
[
  {"type": "text", "data": {"text": "你好 "}},
  {"type": "at", "data": {"qq": "123456"}},
  {"type": "image", "data": {"file": "https://..."}}
]
```

常用消息段类型：`text`、`at`、`image`、`face`、`reply`、`record`(语音)、`video`、`json`、`xml`

---

## 安全限制

以下操作被禁止直接调用（需专门决策流程）：
- `set_group_leave`（退群/解散）
- `delete_friend`（删好友）
- `set_group_kick`（踢人）
- `get_cookies` / `get_csrf_token` / `get_credentials`（凭证）
