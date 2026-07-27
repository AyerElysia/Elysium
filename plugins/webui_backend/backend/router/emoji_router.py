"""表情包管理路由组件

提供表情包管理接口，支持：
- 分页列表查询（支持搜索、排序、筛选）
- 详情查看（含 base64 图片）
- 编辑描述和禁用状态
- 删除（数据库 + 文件）
- 上传（自动 VLM 识别）
- 批量操作（删除/禁用/启用）
- 统计信息

API 路径前缀：/webui/api/emoji

前端对应文件：
- forward/mofox-webui/src/api/emoji.ts (API定义)
- forward/mofox-webui/src/stores/emojiStore.ts (状态管理)
- forward/mofox-webui/src/components/emoji/EmojiManager.vue (管理页面)
- forward/mofox-webui/src/components/emoji/EmojiDetailDialog.vue (详情对话框)
- forward/mofox-webui/src/components/emoji/EmojiUploadDialog.vue (上传对话框)
"""

import base64
import hashlib
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from src.kernel.logger import get_logger
from src.core.components.base.router import BaseRouter
from src.core.utils.security import VerifiedDep
from src.kernel.db import CRUDBase
from src.core.models.sql_alchemy import Images

logger = get_logger(name="EmojiRouter", color="cyan")

# 表情包存储路径
EMOJI_FOLDER = Path("data/media_cache/emojis")


# ==================== API Models ====================

class EmojiItem(BaseModel):
    """表情包列表项 - 匹配前端 EmojiItem 接口"""
    id: int
    hash: str
    description: str = ""
    format: str = "png"
    is_registered: bool = False
    is_banned: bool = False
    usage_count: int = 0
    query_count: int = 0
    record_time: float = 0
    thumbnail: str | None = None


class EmojiDetail(BaseModel):
    """表情包详情 - 匹配前端 EmojiDetail 接口"""
    id: int
    hash: str
    description: str = ""
    format: str = "png"
    full_path: str = ""
    is_registered: bool = False
    is_banned: bool = False
    usage_count: int = 0
    query_count: int = 0
    last_used_time: float | None = None
    record_time: float = 0
    register_time: float | None = None
    full_image: str | None = None
    emotions: list[str] = Field(default_factory=list)


class EmojiListResponse(BaseModel):
    """表情包列表响应 - 匹配前端 EmojiListResponse 接口"""
    items: list[EmojiItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class EmojiUpdateRequest(BaseModel):
    """表情包更新请求"""
    description: str | None = None
    is_banned: bool | None = None
    emotions: list[str] | None = None


class BatchOperationResult(BaseModel):
    """批量操作单项结果"""
    hash: str
    success: bool
    error: str | None = None


class BatchOperationResponse(BaseModel):
    """批量操作响应"""
    processed: int
    succeeded: int
    failed: int
    results: list[BatchOperationResult]


class BatchOperationRequest(BaseModel):
    """批量操作请求"""
    action: Literal["delete", "ban", "unban"]
    emoji_hashes: list[str]


class UploadResult(BaseModel):
    """上传单项结果"""
    filename: str
    success: bool
    hash: str | None = None
    message: str | None = None
    error: str | None = None


class UploadResponse(BaseModel):
    """上传响应"""
    uploaded: int
    failed: int
    results: list[UploadResult]


class EmojiStatsResponse(BaseModel):
    """表情包统计响应"""
    total_count: int
    registered_count: int
    banned_count: int
    total_usage: int
    top_used: list[dict[str, Any]] = Field(default_factory=list)


# ==================== Helper Functions ====================

def _parse_format(path: str) -> str:
    """从文件路径解析格式"""
    ext = Path(path).suffix.lower()
    format_map = {
        ".png": "png",
        ".jpg": "jpg",
        ".jpeg": "jpg",
        ".gif": "gif",
        ".webp": "webp",
    }
    return format_map.get(ext, "png")


def _parse_description_emotions(description: str) -> tuple[str, list[str]]:
    """解析联合格式的描述
    格式: "精炼描述 Keywords: [关键词] Desc: 详细描述"
    """
    import re

    if not description:
        return "", []

    keywords_match = re.search(r"Keywords:\s*\[(.*?)\]", description)
    refined_match = re.search(r"^(.*?)\s*Keywords:", description)

    refined = refined_match.group(1).strip() if refined_match else description.split("Keywords:")[0].strip() if "Keywords:" in description else description
    keywords = keywords_match.group(1).split(",") if keywords_match else []
    keywords = [k.strip() for k in keywords if k.strip()]

    return refined, keywords


def _build_description(refined: str, keywords: list[str], detailed: str) -> str:
    """构建联合格式的描述"""
    return f"{refined} Keywords: [{','.join(keywords)}] Desc: {detailed}"


def _image_to_emoji_item(record: Images, include_thumbnail: bool = False) -> EmojiItem:
    """将 Images 记录转换为 EmojiItem"""
    thumbnail = None
    if include_thumbnail and record.path:
        try:
            file_path = Path(record.path)
            if file_path.exists():
                file_bytes = file_path.read_bytes()
                thumbnail = f"data:image/{_parse_format(record.path)};base64,{base64.b64encode(file_bytes).decode()}"
        except Exception as e:
            logger.warning(f"生成缩略图失败: {e}")

    return EmojiItem(
        id=record.id,
        hash=record.image_id,
        description=record.description or "",
        format=_parse_format(record.path),
        is_registered=record.vlm_processed,
        is_banned=record.is_banned,
        usage_count=record.count,
        query_count=record.query_count,
        record_time=record.timestamp,
        thumbnail=thumbnail,
    )


def _image_to_emoji_detail(record: Images, include_image: bool = True) -> EmojiDetail:
    """将 Images 记录转换为 EmojiDetail"""
    full_image = None
    if include_image and record.path:
        try:
            file_path = Path(record.path)
            if file_path.exists():
                file_bytes = file_path.read_bytes()
                full_image = f"data:image/{_parse_format(record.path)};base64,{base64.b64encode(file_bytes).decode()}"
        except Exception as e:
            logger.warning(f"生成完整图片失败: {e}")

    _, emotions = _parse_description_emotions(record.description or "")

    return EmojiDetail(
        id=record.id,
        hash=record.image_id,
        description=record.description or "",
        format=_parse_format(record.path),
        full_path=record.path,
        is_registered=record.vlm_processed,
        is_banned=record.is_banned,
        usage_count=record.count,
        query_count=record.query_count,
        last_used_time=None,  # Images 表无此字段
        record_time=record.timestamp,
        register_time=record.timestamp if record.vlm_processed else None,
        full_image=full_image,
        emotions=emotions,
    )


# ==================== Router ====================

class EmojiRouter(BaseRouter):
    """表情包管理路由组件

    提供与前端完全匹配的 API 端点，支持表情包的完整生命周期管理。

    API 端点列表：

    **查询接口**
    - GET  /list
      获取表情包分页列表（支持搜索、排序、筛选）
      参数: page, page_size, search, sort_by, sort_order, is_registered, is_banned
      前端对应: getEmojiList() in api/emoji.ts

    - GET  /detail/{hash}
      获取表情包详情（含完整图片 base64）
      前端对应: getEmojiDetail() in api/emoji.ts

    - GET  /stats
      获取表情包统计信息
      前端对应: getEmojiStats() in api/emoji.ts

    **编辑接口**
    - PATCH /update/{hash}
      更新表情包信息（描述、禁用状态）
      前端对应: updateEmoji() in api/emoji.ts

    - DELETE /delete/{hash}
      删除表情包（数据库记录 + 物理文件）
      前端对应: deleteEmoji() in api/emoji.ts

    **上传接口**
    - POST /upload
      上传表情包（自动调用 VLM 识别）
      前端对应: uploadEmojis() in api/emoji.ts

    **批量操作**
    - POST /batch
      批量操作表情包（delete/ban/unban）
      前端对应: batchOperationEmojis() in api/emoji.ts
    """

    router_name = "EmojiRouter"
    router_description = "表情包管理接口"

    custom_route_path = "/webui/api/emoji"
    cors_origins = []

    def register_endpoints(self) -> None:
        """注册所有 HTTP 端点"""

        # ==================== 查询接口 ====================

        @self.app.get("/list", summary="获取表情包列表")
        async def get_emoji_list(
            page: int = 1,
            page_size: int = 50,
            search: str | None = None,
            sort_by: str = "record_time",
            sort_order: Literal["asc", "desc"] = "desc",
            is_registered: bool | None = None,
            is_banned: bool | None = None,
            _=VerifiedDep
        ) -> dict[str, Any]:
            """获取表情包分页列表

            支持功能：
            - 分页: page, page_size
            - 搜索: search（描述文本模糊匹配）
            - 排序: sort_by (record_time/usage_count/query_count/description)
            - 筛选: is_registered, is_banned
            """
            try:
                crud = CRUDBase(Images)

                # 构建查询条件
                filters = {"type": "emoji"}

                if is_registered is not None:
                    filters["vlm_processed"] = is_registered

                if is_banned is not None:
                    filters["is_banned"] = is_banned

                # 查询所有符合条件的记录
                # 注意: CRUDBase.get_multi 不支持 LIKE 搜索，需要手动过滤
                all_records = await crud.get_multi(limit=1000000, **filters)

                # 搜索过滤
                if search:
                    search_lower = search.lower()
                    all_records = [r for r in all_records if r.description and search_lower in r.description.lower()]

                # 排序
                sort_key_map = {
                    "record_time": "timestamp",
                    "usage_count": "count",
                    "query_count": "query_count",
                    "description": "description",
                }
                sort_key = sort_key_map.get(sort_by, "timestamp")
                reverse = sort_order == "desc"

                all_records = sorted(all_records, key=lambda r: getattr(r, sort_key) or 0, reverse=reverse)

                # 分页
                total = len(all_records)
                total_pages = (total + page_size - 1) // page_size if page_size > 0 else 1
                start = (page - 1) * page_size
                end = start + page_size
                page_records = all_records[start:end]

                # 转换为响应格式（包含缩略图，供管理页网格直接预览）
                items = [_image_to_emoji_item(r, include_thumbnail=True) for r in page_records]

                return {
                    "success": True,
                    "data": EmojiListResponse(
                        items=items,
                        total=total,
                        page=page,
                        page_size=page_size,
                        total_pages=total_pages,
                    )
                }

            except Exception as e:
                logger.error(f"获取表情包列表失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/detail/{hash}", summary="获取表情包详情")
        async def get_emoji_detail(hash: str, _=VerifiedDep) -> dict[str, Any]:
            """获取表情包详情（含完整图片 base64）"""
            try:
                crud = CRUDBase(Images)
                record = await crud.get_by(image_id=hash, type="emoji")

                if not record:
                    return {"success": False, "error": f"表情包 {hash} 不存在"}

                return {
                    "success": True,
                    "data": _image_to_emoji_detail(record, include_image=True)
                }

            except Exception as e:
                logger.error(f"获取表情包详情失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/stats", summary="获取表情包统计")
        async def get_emoji_stats(_=VerifiedDep) -> dict[str, Any]:
            """获取表情包统计信息"""
            try:
                crud = CRUDBase(Images)

                # 查询所有表情包
                all_records = await crud.get_multi(limit=1000000, type="emoji")

                total_count = len(all_records)
                registered_count = sum(1 for r in all_records if r.vlm_processed)
                banned_count = sum(1 for r in all_records if r.is_banned)
                total_usage = sum(r.count for r in all_records)

                # 使用次数最高的 5 个
                top_used_records = sorted(all_records, key=lambda r: r.count, reverse=True)[:5]
                top_used = [
                    {
                        "hash": r.image_id,
                        "description": r.description or "",
                        "usage_count": r.count,
                    }
                    for r in top_used_records
                ]

                return {
                    "success": True,
                    "data": EmojiStatsResponse(
                        total_count=total_count,
                        registered_count=registered_count,
                        banned_count=banned_count,
                        total_usage=total_usage,
                        top_used=top_used,
                    )
                }

            except Exception as e:
                logger.error(f"获取表情包统计失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        # ==================== 编辑接口 ====================

        @self.app.patch("/update/{hash}", summary="更新表情包信息")
        async def update_emoji(
            hash: str,
            data: EmojiUpdateRequest,
            _=VerifiedDep
        ) -> dict[str, Any]:
            """更新表情包描述和禁用状态

            支持更新：
            - description: 描述文本（前端联合格式）
            - is_banned: 禁用状态
            - emotions: 情感标签（会编码到 description 中）
            """
            try:
                crud = CRUDBase(Images)
                record = await crud.get_by(image_id=hash, type="emoji")

                if not record:
                    return {"success": False, "error": f"表情包 {hash} 不存在"}

                # 更新字段
                updates = {}

                if data.is_banned is not None:
                    updates["is_banned"] = data.is_banned

                if data.description is not None:
                    updates["description"] = data.description

                # 如果提供了 emotions，需要重新构建描述
                # 前端传来的 description 已经是联合格式，直接保存即可

                if updates:
                    await crud.update(record.id, updates)
                    # 重新获取更新后的记录
                    record = await crud.get_by(id=record.id)

                # 描述变更时同步更新向量库 metadata & embedding，避免检索到旧描述
                vdb_synced: bool | None = None
                if data.description is not None:
                    try:
                        from src.app.plugin_system.api.service_api import get_service
                        from plugins.emoji.sender.service import EmojiSenderService
                        svc = get_service("emoji:service:emoji_sender")
                        if svc is not None and isinstance(svc, EmojiSenderService):
                            vdb_synced = await svc.update_meme_description(hash, data.description)
                        else:
                            vdb_synced = None
                    except Exception as vdb_err:
                        logger.warning(f"同步向量库描述失败（不影响 SQLite 更新）: {vdb_err}")
                        vdb_synced = False

                return {
                    "success": True,
                    "data": _image_to_emoji_detail(record, include_image=False),
                    "vdb_synced": vdb_synced,
                }

            except Exception as e:
                logger.error(f"更新表情包失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.delete("/delete/{hash}", summary="删除表情包")
        async def delete_emoji(hash: str, _=VerifiedDep) -> dict[str, Any]:
            """删除表情包（数据库记录 + 物理文件）

            ⚠️ 此操作不可撤销
            """
            try:
                crud = CRUDBase(Images)
                record = await crud.get_by(image_id=hash, type="emoji")

                if not record:
                    return {"success": False, "error": f"表情包 {hash} 不存在"}

                # 删除物理文件
                file_path = Path(record.path)
                if file_path.exists():
                    try:
                        file_path.unlink()
                        logger.info(f"已删除表情包文件: {file_path}")
                    except Exception as e:
                        logger.warning(f"删除表情包文件失败: {e}")

                # 删除数据库记录
                await crud.delete(record.id)

                return {"success": True, "message": f"表情包 {hash} 已删除"}

            except Exception as e:
                logger.error(f"删除表情包失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        # ==================== 上传接口 ====================

        @self.app.post("/upload", summary="上传表情包")
        async def upload_emojis(
            files: list[UploadFile] = File(...),
            _=VerifiedDep
        ) -> dict[str, Any]:
            """上传表情包

            功能：
            - 保存文件到 data/media_cache/emojis/
            - 计算 SHA256 hash
            - 自动调用 VLM 识别生成描述
            - 创建 Images 数据库记录
            """
            try:
                # 确保 emoji 文件夹存在
                EMOJI_FOLDER.mkdir(parents=True, exist_ok=True)

                results: list[UploadResult] = []
                succeeded = 0
                failed = 0

                for upload_file in files:
                    try:
                        # 读取文件内容
                        content = await upload_file.read()

                        # 计算 hash
                        file_hash = hashlib.sha256(content).hexdigest()

                        ext = Path(upload_file.filename).suffix.lower() or ".png"

                        # 保存文件
                        filename = f"{file_hash[:16]}_emoji{ext}"
                        file_path = EMOJI_FOLDER / filename
                        file_path.write_bytes(content)

                        # 检查是否已存在
                        crud = CRUDBase(Images)
                        existing = await crud.get_by(image_id=file_hash, type="emoji")

                        if existing:
                            results.append(UploadResult(
                                filename=upload_file.filename,
                                success=True,
                                hash=file_hash,
                                message="表情包已存在，跳过",
                            ))
                            succeeded += 1
                            continue

                        # 调用 VLM 识别（可选）
                        description = ""
                        vlm_processed = False
                        try:
                            from src.core.managers import get_media_manager
                            media_manager = get_media_manager()

                            # 转 base64 调用 VLM
                            base64_data = base64.b64encode(content).decode()
                            description = await media_manager.recognize_media(
                                base64_data=base64_data,
                                media_type="emoji",
                                use_cache=True,
                            )
                            if description:
                                vlm_processed = True
                                logger.info(f"VLM 识别表情包: {upload_file.filename} -> {description[:50]}...")
                        except Exception as e:
                            logger.warning(f"VLM 识别失败: {e}")
                            description = f"表情包:{upload_file.filename}"

                        # 创建数据库记录
                        await crud.create({
                            "image_id": file_hash,
                            "description": description,
                            "path": str(file_path),
                            "count": 0,
                            "timestamp": time.time(),
                            "type": "emoji",
                            "vlm_processed": vlm_processed,
                            "is_banned": False,
                            "query_count": 0,
                        })

                        results.append(UploadResult(
                            filename=upload_file.filename,
                            success=True,
                            hash=file_hash,
                            message="上传成功",
                        ))
                        succeeded += 1

                    except Exception as e:
                        logger.error(f"上传文件 {upload_file.filename} 失败: {e}")
                        results.append(UploadResult(
                            filename=upload_file.filename,
                            success=False,
                            error=str(e),
                        ))
                        failed += 1

                return {
                    "success": True,
                    "data": UploadResponse(
                        uploaded=succeeded,
                        failed=failed,
                        results=results,
                    )
                }

            except Exception as e:
                logger.error(f"上传表情包失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        # ==================== 批量操作 ====================

        @self.app.post("/batch", summary="批量操作表情包")
        async def batch_operation(
            data: BatchOperationRequest,
            _=VerifiedDep
        ) -> dict[str, Any]:
            """批量操作表情包

            支持操作：
            - delete: 批量删除（数据库 + 文件）
            - ban: 批量禁用
            - unban: 批量启用
            """
            try:
                crud = CRUDBase(Images)
                results: list[BatchOperationResult] = []
                succeeded = 0
                failed = 0
                action = data.action
                emoji_hashes = data.emoji_hashes

                for hash_value in emoji_hashes:
                    try:
                        record = await crud.get_by(image_id=hash_value, type="emoji")

                        if not record:
                            results.append(BatchOperationResult(
                                hash=hash_value,
                                success=False,
                                error="表情包不存在",
                            ))
                            failed += 1
                            continue

                        if action == "delete":
                            # 删除物理文件
                            file_path = Path(record.path)
                            if file_path.exists():
                                try:
                                    file_path.unlink()
                                except Exception as e:
                                    logger.warning(f"删除文件失败: {e}")

                            # 删除数据库记录
                            await crud.delete(record.id)
                            results.append(BatchOperationResult(
                                hash=hash_value,
                                success=True,
                            ))

                        elif action == "ban":
                            await crud.update(record.id, {"is_banned": True})
                            results.append(BatchOperationResult(
                                hash=hash_value,
                                success=True,
                            ))

                        elif action == "unban":
                            await crud.update(record.id, {"is_banned": False})
                            results.append(BatchOperationResult(
                                hash=hash_value,
                                success=True,
                            ))

                        succeeded += 1

                    except Exception as e:
                        results.append(BatchOperationResult(
                            hash=hash_value,
                            success=False,
                            error=str(e),
                        ))
                        failed += 1

                return {
                    "success": True,
                    "data": BatchOperationResponse(
                        processed=len(emoji_hashes),
                        succeeded=succeeded,
                        failed=failed,
                        results=results,
                    )
                }

            except Exception as e:
                logger.error(f"批量操作失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

    async def startup(self) -> None:
        """路由启动钩子"""
        logger.info(f"表情包管理路由已启动，路径: {self.custom_route_path}")

    async def shutdown(self) -> None:
        """路由关闭钩子"""
        logger.info("表情包管理路由已关闭")
