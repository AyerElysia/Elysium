"""人物关系管理路由组件

提供人物关系管理的 API 接口，支持：
- 平台列表查询（按平台分组统计用户数）
- 用户列表查询（分页、平台筛选、排序）
- 用户详情查看（含印象、记忆点）
- 关系信息更新（分数、描述）
- 印象更新（长期印象、简短印象、偏好关键词、关系阶段）
- 记忆点更新（增删改）
- 用户搜索
- 关系统计

API 路径前缀：/webui/api/relationship

前端对应文件：
- forward/mofox-webui/src/api/relationship.ts (API定义)
- forward/mofox-webui/src/views/RelationshipView.vue (页面)
"""

import json
import time
from typing import Any

from fastapi import HTTPException, Query
from pydantic import BaseModel

from src.kernel.logger import get_logger
from src.core.components.base.router import BaseRouter
from src.core.utils.security import VerifiedDep
from src.kernel.db import CRUDBase
from src.core.models.sql_alchemy import PersonInfo

logger = get_logger(name="RelationshipRouter", color="#F2CDCD")


# ==================== API Models ====================


class PersonBasicInfo(BaseModel):
    """用户基础信息"""
    person_id: str
    person_name: str
    nickname: str | None = None
    know_times: int = 0
    know_since: str | None = None
    last_know: str | None = None
    attitude: str | None = None


class PersonCard(BaseModel):
    """用户卡片信息（列表展示）"""
    person_id: str
    person_name: str
    nickname: str | None = None
    relationship_score: float = 0.5
    relationship_text: str | None = None
    relationship_stage: str | None = None
    short_impression: str | None = None
    preference_keywords: str | None = None
    know_times: int = 0
    last_know: str | None = None


class PersonListResponse(BaseModel):
    """用户列表响应"""
    persons: list[PersonCard]
    total: int
    page: int
    page_size: int
    total_pages: int


class PersonRelationship(BaseModel):
    """用户关系信息"""
    person_id: str
    person_name: str
    relationship_score: float
    relationship_text: str | None = None
    impression_text: str | None = None
    preference_keywords: str | None = None
    relationship_stage: str | None = None
    first_met_time: float | None = None
    last_impression_update: float | None = None


class MemoryPoint(BaseModel):
    """记忆点"""
    content: str
    weight: float = 1.0
    timestamp: str


class PersonDetail(BaseModel):
    """用户详情"""
    basic_info: PersonBasicInfo
    relationship: PersonRelationship
    impression: str
    short_impression: str
    memory_points: list[MemoryPoint]


class PlatformInfo(BaseModel):
    """平台信息"""
    platform: str
    count: int


class PlatformsResponse(BaseModel):
    """平台列表响应"""
    platforms: list[PlatformInfo]


class UpdateRelationshipRequest(BaseModel):
    """更新关系请求"""
    relationship_score: float
    relationship_text: str | None = None


class UpdateImpressionRequest(BaseModel):
    """更新印象请求"""
    impression_text: str | None = None
    preference_keywords: str | None = None
    relationship_stage: str | None = None


class RelationshipStats(BaseModel):
    """关系统计"""
    total_count: int
    by_platform: dict[str, int]
    by_attitude_level: dict[str, int]
    average_attitude: float


# ==================== Helper Functions ====================


def _format_timestamp(ts: float | None) -> str | None:
    """格式化时间戳为字符串"""
    if ts is None:
        return None
    return str(ts)


def _get_attitude_level(attitude: int | None) -> str:
    """获取态度等级文本"""
    if attitude is None:
        return "未知"
    if attitude >= 80:
        return "亲密"
    if attitude >= 60:
        return "友好"
    if attitude >= 40:
        return "一般"
    if attitude >= 20:
        return "疏远"
    return "陌生"


def _parse_memory_points(points_json: str | None) -> list[MemoryPoint]:
    """解析记忆点 JSON"""
    if not points_json:
        return []
    try:
        data = json.loads(points_json)
        if isinstance(data, list):
            return [
                MemoryPoint(
                    content=str(p.get("content", "")),
                    weight=float(p.get("weight", 1.0)),
                    timestamp=str(p.get("timestamp", "")),
                )
                for p in data
                if isinstance(p, dict) and p.get("content")
            ]
    except Exception as e:
        logger.warning(f"解析记忆点 JSON 失败: {e}")
    return []


def _serialize_memory_points(points: list[MemoryPoint]) -> str:
    """序列化记忆点为 JSON"""
    return json.dumps([
        {"content": p.content, "weight": p.weight, "timestamp": p.timestamp}
        for p in points
    ])


def _person_to_card(record: PersonInfo) -> PersonCard:
    """PersonInfo → PersonCard"""
    nickname = record.nickname or record.cardname
    relationship_score = (record.attitude or 50) / 100.0

    # 尝试从 info_list 解析 preference_keywords
    preference_keywords = None
    if record.info_list:
        try:
            info_data = json.loads(record.info_list)
            if isinstance(info_data, dict):
                preference_keywords = info_data.get("preference_keywords")
        except Exception:
            pass

    return PersonCard(
        person_id=record.person_id,
        person_name=nickname or record.user_id,
        nickname=nickname,
        relationship_score=relationship_score,
        relationship_text=None,  # 数据库无此字段，使用默认
        relationship_stage="acquaintance",  # 默认值
        short_impression=record.short_impression,
        preference_keywords=preference_keywords,
        know_times=record.interaction_count,
        last_know=_format_timestamp(record.last_interaction),
    )


def _person_to_detail(record: PersonInfo) -> PersonDetail:
    """PersonInfo → PersonDetail"""
    nickname = record.nickname or record.cardname
    relationship_score = (record.attitude or 50) / 100.0

    # 尝试从 info_list 解析额外字段
    preference_keywords = None
    impression_text = None
    if record.info_list:
        try:
            info_data = json.loads(record.info_list)
            if isinstance(info_data, dict):
                preference_keywords = info_data.get("preference_keywords")
                impression_text = info_data.get("impression_text")
        except Exception:
            pass

    return PersonDetail(
        basic_info=PersonBasicInfo(
            person_id=record.person_id,
            person_name=nickname or record.user_id,
            nickname=nickname,
            know_times=record.interaction_count,
            know_since=_format_timestamp(record.first_interaction),
            last_know=_format_timestamp(record.last_interaction),
            attitude=_get_attitude_level(record.attitude),
        ),
        relationship=PersonRelationship(
            person_id=record.person_id,
            person_name=nickname or record.user_id,
            relationship_score=relationship_score,
            relationship_text=None,
            impression_text=impression_text or record.impression,
            preference_keywords=preference_keywords,
            relationship_stage="acquaintance",
            first_met_time=record.first_interaction,
            last_impression_update=record.updated_at,
        ),
        impression=record.impression or "",
        short_impression=record.short_impression or "",
        memory_points=_parse_memory_points(record.points),
    )


# ==================== Router ====================


class RelationshipRouter(BaseRouter):
    """人物关系管理路由组件

    提供与前端完全匹配的 API 端点，支持人物关系的完整生命周期管理。

    API 端点列表：

    **查询接口**
    - GET  /platforms
      获取平台列表（含各平台用户数）
      前端对应: getPlatforms() in api/relationship.ts

    - GET  /list
      获取用户分页列表（支持平台筛选、排序）
      参数: page, page_size, platform
      前端对应: getPersonList() in api/relationship.ts

    - GET  /person/{person_id}
      获取用户详情（含印象、记忆点）
      前端对应: getPersonDetail() in api/relationship.ts

    - GET  /search
      搜索用户（nickname/person_id 模糊匹配）
      前端对应: searchPerson() in api/relationship.ts

    - GET  /stats
      获取关系统计信息
      前端对应: getRelationshipStats() in api/relationship.ts

    **更新接口**
    - PUT  /person/{person_id}
      更新关系分数（对应 attitude 字段）
      前端对应: updatePersonRelationship() in api/relationship.ts

    - PUT  /person/{person_id}/impression
      更新印象信息
      前端对应: updatePersonImpression() in api/relationship.ts

    - PUT  /person/{person_id}/points
      更新记忆点
      前端对应: updatePersonPoints() in api/relationship.ts

    **其他接口**
    - GET  /person/{person_id}/report
      获取关系报告（基于 impression 生成的摘要）
      前端对应: getRelationshipReport() in api/relationship.ts

    - POST /cache/clear
      清理关系缓存（预留接口，当前无缓存机制）
      前端对应: clearRelationshipCache() in api/relationship.ts
    """

    router_name = "RelationshipRouter"
    router_description = "人物关系管理接口"

    custom_route_path = "/webui/api/relationship"
    cors_origins = []

    def register_endpoints(self) -> None:
        """注册所有 HTTP 端点"""

        # ==================== 查询接口 ====================

        @self.app.get("/platforms", summary="获取平台列表")
        async def get_platforms(_=VerifiedDep) -> dict[str, Any]:
            """获取平台列表（含各平台用户数统计）"""
            try:
                crud = CRUDBase(PersonInfo)
                all_records = await crud.get_multi()

                # 按 platform 分组计数
                platform_counts: dict[str, int] = {}
                for record in all_records:
                    platform = record.platform
                    platform_counts[platform] = platform_counts.get(platform, 0) + 1

                platforms = [
                    PlatformInfo(platform=platform, count=count)
                    for platform, count in sorted(
                        platform_counts.items(),
                        key=lambda x: x[1],
                        reverse=True
                    )
                ]

                return {
                    "success": True,
                    "data": PlatformsResponse(platforms=platforms)
                }

            except Exception as e:
                logger.error(f"获取平台列表失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/list", summary="获取用户列表")
        async def get_person_list(
            page: int = Query(1, ge=1, description="页码"),
            page_size: int = Query(20, ge=1, le=100, description="每页数量"),
            platform: str | None = Query(None, description="平台筛选"),
            _=VerifiedDep
        ) -> dict[str, Any]:
            """获取用户分页列表"""
            try:
                crud = CRUDBase(PersonInfo)

                # 构建查询条件
                filters = {}
                if platform:
                    filters["platform"] = platform

                # 查询所有符合条件的记录
                all_records = await crud.get_multi(**filters)

                # 按 last_interaction 排序（最新活跃优先）
                all_records = sorted(
                    all_records,
                    key=lambda r: r.last_interaction or 0,
                    reverse=True
                )

                # 分页
                total = len(all_records)
                total_pages = (total + page_size - 1) // page_size if page_size > 0 else 1
                start = (page - 1) * page_size
                end = start + page_size
                page_records = all_records[start:end]

                # 转换为响应格式
                persons = [_person_to_card(r) for r in page_records]

                return {
                    "success": True,
                    "data": PersonListResponse(
                        persons=persons,
                        total=total,
                        page=page,
                        page_size=page_size,
                        total_pages=total_pages,
                    )
                }

            except Exception as e:
                logger.error(f"获取用户列表失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/person/{person_id}", summary="获取用户详情")
        async def get_person_detail(person_id: str, _=VerifiedDep) -> dict[str, Any]:
            """获取用户详情（含印象、记忆点）"""
            try:
                crud = CRUDBase(PersonInfo)
                record = await crud.get_by(person_id=person_id)

                if not record:
                    return {"success": False, "error": f"用户 {person_id} 不存在"}

                return {
                    "success": True,
                    "data": _person_to_detail(record)
                }

            except Exception as e:
                logger.error(f"获取用户详情失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/search", summary="搜索用户")
        async def search_person(
            query: str = Query(..., min_length=1, description="搜索关键词"),
            _=VerifiedDep
        ) -> dict[str, Any]:
            """搜索用户（nickname/person_id 模糊匹配）"""
            try:
                crud = CRUDBase(PersonInfo)
                all_records = await crud.get_multi()

                query_lower = query.lower()

                # 模糊匹配
                matched = [
                    r for r in all_records
                    if (
                        (r.nickname and query_lower in r.nickname.lower())
                        or (r.cardname and query_lower in r.cardname.lower())
                        or query_lower in r.person_id.lower()
                        or query_lower in r.user_id.lower()
                    )
                ]

                if not matched:
                    return {"success": False, "error": "未找到匹配的用户"}

                # 返回第一个匹配结果
                record = matched[0]

                return {
                    "success": True,
                    "data": PersonBasicInfo(
                        person_id=record.person_id,
                        person_name=record.nickname or record.user_id,
                        nickname=record.nickname,
                        know_times=record.interaction_count,
                        know_since=_format_timestamp(record.first_interaction),
                        last_know=_format_timestamp(record.last_interaction),
                        attitude=_get_attitude_level(record.attitude),
                    )
                }

            except Exception as e:
                logger.error(f"搜索用户失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.get("/stats", summary="获取关系统计")
        async def get_relationship_stats(_=VerifiedDep) -> dict[str, Any]:
            """获取关系统计信息"""
            try:
                crud = CRUDBase(PersonInfo)
                all_records = await crud.get_multi()

                total_count = len(all_records)

                # 按平台统计
                by_platform: dict[str, int] = {}
                for record in all_records:
                    platform = record.platform
                    by_platform[platform] = by_platform.get(platform, 0) + 1

                # 按态度等级统计
                by_attitude_level: dict[str, int] = {}
                for record in all_records:
                    level = _get_attitude_level(record.attitude)
                    by_attitude_level[level] = by_attitude_level.get(level, 0) + 1

                # 计算平均态度
                attitudes = [r.attitude for r in all_records if r.attitude is not None]
                average_attitude = sum(attitudes) / len(attitudes) if attitudes else 50.0

                return {
                    "success": True,
                    "data": RelationshipStats(
                        total_count=total_count,
                        by_platform=by_platform,
                        by_attitude_level=by_attitude_level,
                        average_attitude=average_attitude,
                    )
                }

            except Exception as e:
                logger.error(f"获取关系统计失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        # ==================== 更新接口 ====================

        @self.app.put("/person/{person_id}", summary="更新关系信息")
        async def update_person_relationship(
            person_id: str,
            data: UpdateRelationshipRequest,
            _=VerifiedDep
        ) -> dict[str, Any]:
            """更新关系分数（对应 attitude 字段）"""
            try:
                crud = CRUDBase(PersonInfo)
                record = await crud.get_by(person_id=person_id)

                if not record:
                    return {"success": False, "error": f"用户 {person_id} 不存在"}

                # 转换 relationship_score (0-1) 到 attitude (0-100)
                attitude = int(data.relationship_score * 100)
                attitude = max(0, min(100, attitude))  # 确保范围有效

                # 更新字段
                updates = {
                    "attitude": attitude,
                    "updated_at": time.time(),
                }

                await crud.update(record.id, **updates)

                return {"success": True, "message": "关系信息已更新"}

            except Exception as e:
                logger.error(f"更新关系信息失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.put("/person/{person_id}/impression", summary="更新印象信息")
        async def update_person_impression(
            person_id: str,
            data: UpdateImpressionRequest,
            _=VerifiedDep
        ) -> dict[str, Any]:
            """更新印象信息（长期印象、偏好关键词等）"""
            try:
                crud = CRUDBase(PersonInfo)
                record = await crud.get_by(person_id=person_id)

                if not record:
                    return {"success": False, "error": f"用户 {person_id} 不存在"}

                updates = {"updated_at": time.time()}

                # 更新长期印象（存储到 info_list JSON）
                if data.impression_text is not None:
                    info_data = {}
                    if record.info_list:
                        try:
                            info_data = json.loads(record.info_list)
                        except Exception:
                            info_data = {}
                    info_data["impression_text"] = data.impression_text
                    updates["info_list"] = json.dumps(info_data)

                # 更新偏好关键词（存储到 info_list JSON）
                if data.preference_keywords is not None:
                    info_data = {}
                    if record.info_list:
                        try:
                            info_data = json.loads(record.info_list)
                        except Exception:
                            info_data = {}
                    info_data["preference_keywords"] = data.preference_keywords
                    updates["info_list"] = json.dumps(info_data)

                # 关系阶段（存储到 info_list JSON）
                if data.relationship_stage is not None:
                    info_data = {}
                    if record.info_list:
                        try:
                            info_data = json.loads(record.info_list)
                        except Exception:
                            info_data = {}
                    info_data["relationship_stage"] = data.relationship_stage
                    updates["info_list"] = json.dumps(info_data)

                await crud.update(record.id, **updates)

                return {"success": True, "message": "印象信息已更新"}

            except Exception as e:
                logger.error(f"更新印象信息失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.put("/person/{person_id}/points", summary="更新记忆点")
        async def update_person_points(
            person_id: str,
            data: list[MemoryPoint],
            _=VerifiedDep
        ) -> dict[str, Any]:
            """更新记忆点（全量替换）"""
            try:
                crud = CRUDBase(PersonInfo)
                record = await crud.get_by(person_id=person_id)

                if not record:
                    return {"success": False, "error": f"用户 {person_id} 不存在"}

                # 序列化记忆点为 JSON
                points_json = _serialize_memory_points(data)

                updates = {
                    "points": points_json,
                    "updated_at": time.time(),
                }

                await crud.update(record.id, **updates)

                return {"success": True, "message": "记忆点已更新"}

            except Exception as e:
                logger.error(f"更新记忆点失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        # ==================== 其他接口 ====================

        @self.app.get("/person/{person_id}/report", summary="获取关系报告")
        async def get_relationship_report(person_id: str, _=VerifiedDep) -> dict[str, Any]:
            """获取关系报告（基于 impression 生成的摘要）"""
            try:
                crud = CRUDBase(PersonInfo)
                record = await crud.get_by(person_id=person_id)

                if not record:
                    return {"success": False, "error": f"用户 {person_id} 不存在"}

                # 生成简单报告（基于现有数据）
                nickname = record.nickname or record.user_id
                attitude = record.attitude or 50
                impression = record.impression or "暂无印象"

                report = f"【{nickname} 关系报告】\n"
                report += f"关系分数: {attitude}/100 ({_get_attitude_level(attitude)})\n"
                report += f"交互次数: {record.interaction_count} 次\n"
                report += f"首次交互: {_format_timestamp(record.first_interaction) or '未知'}\n"
                report += f"最后交互: {_format_timestamp(record.last_interaction) or '未知'}\n"
                report += f"\n印象摘要:\n{impression[:500]}..."

                return {
                    "success": True,
                    "data": {
                        "person_id": person_id,
                        "report": report,
                    }
                }

            except Exception as e:
                logger.error(f"获取关系报告失败: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

        @self.app.post("/cache/clear", summary="清理关系缓存")
        async def clear_relationship_cache(
            data: dict[str, Any] | None = None,
            _=VerifiedDep
        ) -> dict[str, Any]:
            """清理关系缓存（预留接口，当前无缓存机制）"""
            # 当前 PersonInfo 直接从数据库读取，无缓存机制
            # 预留接口以便后续添加缓存功能
            person_id = data.get("person_id") if data else None

            if person_id:
                logger.info(f"收到清理缓存请求: person_id={person_id}")
            else:
                logger.info("收到清理全部缓存请求")

            return {"success": True, "message": "缓存清理请求已记录（当前无缓存机制）"}

    async def startup(self) -> None:
        """路由启动钩子"""
        logger.info(f"人物关系管理路由已启动，路径: {self.custom_route_path}")

    async def shutdown(self) -> None:
        """路由关闭钩子"""
        logger.info("人物关系管理路由已关闭")