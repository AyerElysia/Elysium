"""飞书 action 执行层。

将飞书开放平台 REST API 封装为 action 路由表，
由统一工具 tool-platform_action 通过 adapter.execute_action() 调用。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from src.kernel.logger import get_logger

if TYPE_CHECKING:
    from .adapter import FeishuAdapter

logger = get_logger("feishu_adapter.actions")

# 禁止通过本层执行的危险操作
BLOCKED_ACTIONS: frozenset[str] = frozenset({
    "delete_chat",
    "remove_all_members",
})


class FeishuActionExecutor:
    """飞书 API action 路由器。"""

    def __init__(self, adapter: "FeishuAdapter") -> None:
        self._adapter = adapter

    async def execute(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """执行一个飞书 action，返回结果字典。"""
        if action in BLOCKED_ACTIONS:
            return {"status": "blocked", "message": f"操作 '{action}' 已被安全策略禁止"}

        handler = _ACTION_TABLE.get(action)
        if handler is None:
            return {"status": "error", "message": f"未知 action: {action}"}

        try:
            result = await handler(self, params)
            return {"status": "ok", "data": result}
        except Exception as exc:
            logger.error(f"[feishu_action] {action} 执行失败: {exc}")
            return {"status": "error", "message": str(exc)}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._adapter._post_json(path, body)

    async def _get(self, path: str) -> dict[str, Any]:
        return await self._adapter._get_json(path)

    async def _delete(self, path: str) -> dict[str, Any]:
        """DELETE 请求。"""
        token = await self._adapter._get_tenant_access_token()
        url = self._adapter._api_url(path)
        resp = await self._adapter._request_with_retry(
            "DELETE", url, timeout=15.0,
            headers={"Authorization": f"Bearer {token}"},
        )
        return self._adapter._decode_response(resp)

    async def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """PATCH 请求。"""
        token = await self._adapter._get_tenant_access_token()
        url = self._adapter._api_url(path)
        resp = await self._adapter._request_with_retry(
            "PATCH", url, timeout=15.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
        )
        return self._adapter._decode_response(resp)

    async def _put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """PUT 请求。"""
        token = await self._adapter._get_tenant_access_token()
        url = self._adapter._api_url(path)
        resp = await self._adapter._request_with_retry(
            "PUT", url, timeout=15.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
        )
        return self._adapter._decode_response(resp)


# ======================================================================
# Action 处理函数
# ======================================================================

# --- 消息操作 ---

async def _send_text(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """发送文本消息。params: {chat_id?, open_id?, text}"""
    text = str(params.get("text") or "")
    if not text:
        raise ValueError("text 不能为空")
    chat_id = str(params.get("chat_id") or "")
    open_id = str(params.get("open_id") or "")
    if chat_id:
        return await executor._adapter._send_text("chat_id", chat_id, text)
    if open_id:
        return await executor._adapter._send_text("open_id", open_id, text)
    raise ValueError("需要 chat_id 或 open_id")


async def _reply_text(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """引用回复。params: {message_id, text}"""
    message_id = str(params.get("message_id") or "")
    text = str(params.get("text") or "")
    if not message_id or not text:
        raise ValueError("需要 message_id 和 text")
    return await executor._adapter._reply_text(message_id, text)


async def _edit_message(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """编辑消息。params: {message_id, text}"""
    message_id = str(params.get("message_id") or "")
    text = str(params.get("text") or "")
    if not message_id or not text:
        raise ValueError("需要 message_id 和 text")
    return await executor._patch(
        f"/open-apis/im/v1/messages/{message_id}",
        {"content": json.dumps({"text": text}, ensure_ascii=False)},
    )


async def _delete_message(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """撤回消息。params: {message_id}"""
    message_id = str(params.get("message_id") or "")
    if not message_id:
        raise ValueError("需要 message_id")
    return await executor._delete(f"/open-apis/im/v1/messages/{message_id}")


async def _pin_message(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """置顶消息。params: {message_id}"""
    message_id = str(params.get("message_id") or "")
    if not message_id:
        raise ValueError("需要 message_id")
    return await executor._post("/open-apis/im/v1/pins", {"message_id": message_id})


async def _unpin_message(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """取消置顶。params: {message_id}"""
    message_id = str(params.get("message_id") or "")
    if not message_id:
        raise ValueError("需要 message_id")
    return await executor._delete(f"/open-apis/im/v1/pins/{message_id}")


async def _add_reaction(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """添加表情回应。params: {message_id, emoji_type}"""
    message_id = str(params.get("message_id") or "")
    emoji_type = str(params.get("emoji_type") or "THUMBSUP")
    if not message_id:
        raise ValueError("需要 message_id")
    return await executor._post(
        f"/open-apis/im/v1/messages/{message_id}/reactions",
        {"reaction_type": {"emoji_type": emoji_type}},
    )


async def _get_message_list(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """获取会话历史消息。params: {container_id, container_type?, page_size?, start_time?, end_time?}"""
    container_id = str(params.get("container_id") or "")
    if not container_id:
        raise ValueError("需要 container_id（即 chat_id）")
    container_type = str(params.get("container_type") or "chat")
    page_size = int(params.get("page_size") or 20)
    query = f"container_id_type={container_type}&container_id={container_id}&page_size={page_size}"
    start_time = params.get("start_time")
    end_time = params.get("end_time")
    if start_time:
        query += f"&start_time={start_time}"
    if end_time:
        query += f"&end_time={end_time}"
    return await executor._get(f"/open-apis/im/v1/messages?{query}")


async def _get_read_users(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """获取消息已读列表。params: {message_id, page_size?}"""
    message_id = str(params.get("message_id") or "")
    if not message_id:
        raise ValueError("需要 message_id")
    page_size = int(params.get("page_size") or 50)
    return await executor._get(
        f"/open-apis/im/v1/messages/{message_id}/read_users?page_size={page_size}"
    )


# --- 群组/会话管理 ---

async def _get_chat_list(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """获取我加入的群列表。params: {page_size?}"""
    page_size = int(params.get("page_size") or 50)
    return await executor._get(f"/open-apis/im/v1/chats?page_size={page_size}")


async def _get_chat_info(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """获取群信息。params: {chat_id}"""
    chat_id = str(params.get("chat_id") or "")
    if not chat_id:
        raise ValueError("需要 chat_id")
    return await executor._get(f"/open-apis/im/v1/chats/{chat_id}")


async def _get_chat_members(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """获取群成员列表。params: {chat_id, page_size?}"""
    chat_id = str(params.get("chat_id") or "")
    if not chat_id:
        raise ValueError("需要 chat_id")
    page_size = int(params.get("page_size") or 100)
    return await executor._get(
        f"/open-apis/im/v1/chats/{chat_id}/members?member_id_type=open_id&page_size={page_size}"
    )


async def _add_chat_members(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """邀请成员入群。params: {chat_id, user_ids: list[str]}"""
    chat_id = str(params.get("chat_id") or "")
    user_ids = params.get("user_ids") or []
    if not chat_id or not user_ids:
        raise ValueError("需要 chat_id 和 user_ids")
    id_list = [{"member_id_type": "open_id", "member_id": uid} for uid in user_ids]
    return await executor._post(
        f"/open-apis/im/v1/chats/{chat_id}/members",
        {"id_list": [str(uid) for uid in user_ids]},
    )


async def _remove_chat_members(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """移出群成员。params: {chat_id, user_ids: list[str]}"""
    chat_id = str(params.get("chat_id") or "")
    user_ids = params.get("user_ids") or []
    if not chat_id or not user_ids:
        raise ValueError("需要 chat_id 和 user_ids")
    return await executor._delete(
        f"/open-apis/im/v1/chats/{chat_id}/members?member_id_type=open_id"
    )


async def _create_chat(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """创建群。params: {name, description?, user_ids?: list[str]}"""
    name = str(params.get("name") or "")
    if not name:
        raise ValueError("需要 name")
    body: dict[str, Any] = {"name": name}
    if params.get("description"):
        body["description"] = str(params["description"])
    if params.get("user_ids"):
        body["user_id_list"] = list(params["user_ids"])
    return await executor._post("/open-apis/im/v1/chats?set_bot_manager=true", body)


async def _update_chat(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """修改群信息。params: {chat_id, name?, description?, avatar?}"""
    chat_id = str(params.get("chat_id") or "")
    if not chat_id:
        raise ValueError("需要 chat_id")
    body: dict[str, Any] = {}
    if params.get("name"):
        body["name"] = str(params["name"])
    if params.get("description"):
        body["description"] = str(params["description"])
    if not body:
        raise ValueError("至少需要 name 或 description")
    return await executor._put(f"/open-apis/im/v1/chats/{chat_id}", body)


# --- 文件/图片 ---

async def _upload_image(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """上传图片。params: {image_path} 本地路径，返回 image_key。"""
    import httpx
    from pathlib import Path

    image_path = str(params.get("image_path") or "")
    if not image_path:
        raise ValueError("需要 image_path")
    path = Path(image_path)
    if not path.is_file():
        raise ValueError(f"文件不存在: {image_path}")

    token = await executor._adapter._get_tenant_access_token()
    url = executor._adapter._api_url("/open-apis/im/v1/images")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": (path.name, path.read_bytes(), "application/octet-stream")},
        )
    data = executor._adapter._decode_response(resp)
    if int(data.get("code", 0)) != 0:
        raise RuntimeError(f"上传图片失败: {data}")
    return data.get("data", {})


async def _upload_file(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """上传文件。params: {file_path, file_type?, file_name?}"""
    import httpx
    from pathlib import Path

    file_path = str(params.get("file_path") or "")
    if not file_path:
        raise ValueError("需要 file_path")
    path = Path(file_path)
    if not path.is_file():
        raise ValueError(f"文件不存在: {file_path}")

    file_type = str(params.get("file_type") or "stream")
    file_name = str(params.get("file_name") or path.name)

    token = await executor._adapter._get_tenant_access_token()
    url = executor._adapter._api_url("/open-apis/im/v1/files")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            data={"file_type": file_type, "file_name": file_name},
            files={"file": (file_name, path.read_bytes(), "application/octet-stream")},
        )
    data = executor._adapter._decode_response(resp)
    if int(data.get("code", 0)) != 0:
        raise RuntimeError(f"上传文件失败: {data}")
    return data.get("data", {})


async def _download_resource(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """下载消息中的资源（图片/文件）。params: {message_id, file_key, type?}"""
    message_id = str(params.get("message_id") or "")
    file_key = str(params.get("file_key") or "")
    resource_type = str(params.get("type") or "image")
    if not message_id or not file_key:
        raise ValueError("需要 message_id 和 file_key")
    b64 = await executor._adapter._download_message_resource_as_base64(
        message_id=message_id,
        resource_key=file_key,
        resource_type=resource_type,
    )
    return {"base64": b64, "type": resource_type}


# --- 通讯录 ---

async def _get_user_info(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """获取用户信息。params: {open_id}"""
    open_id = str(params.get("open_id") or "")
    if not open_id:
        raise ValueError("需要 open_id")
    return await executor._get(
        f"/open-apis/contact/v3/users/{open_id}?user_id_type=open_id"
    )


async def _search_user(executor: FeishuActionExecutor, params: dict[str, Any]) -> Any:
    """搜索用户。params: {query, page_size?}"""
    query = str(params.get("query") or "")
    if not query:
        raise ValueError("需要 query")
    page_size = int(params.get("page_size") or 20)
    return await executor._post(
        "/open-apis/search/v1/user?page_size=" + str(page_size),
        {"query": query},
    )


# ======================================================================
# Action 路由表
# ======================================================================

_ACTION_TABLE: dict[str, Any] = {
    # 消息
    "send_text": _send_text,
    "reply_text": _reply_text,
    "edit_message": _edit_message,
    "delete_message": _delete_message,
    "pin_message": _pin_message,
    "unpin_message": _unpin_message,
    "add_reaction": _add_reaction,
    "get_message_list": _get_message_list,
    "get_read_users": _get_read_users,
    # 群组
    "get_chat_list": _get_chat_list,
    "get_chat_info": _get_chat_info,
    "get_chat_members": _get_chat_members,
    "add_chat_members": _add_chat_members,
    "remove_chat_members": _remove_chat_members,
    "create_chat": _create_chat,
    "update_chat": _update_chat,
    # 文件/图片
    "upload_image": _upload_image,
    "upload_file": _upload_file,
    "download_resource": _download_resource,
    # 通讯录
    "get_user_info": _get_user_info,
    "search_user": _search_user,
}
