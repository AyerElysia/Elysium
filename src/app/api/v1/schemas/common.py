"""API v1 通用协议 schema。"""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

API_VERSION = "v1"
SCHEMA_VERSION = 1


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""

    return datetime.now(UTC)


class StrictModel(BaseModel):
    """拒绝未知字段的公共协议模型。"""

    model_config = ConfigDict(extra="forbid")


class VersionedModel(StrictModel):
    """携带稳定技术 schema 版本的公共模型。"""

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: int) -> int:
        """P3-01 仅接受当前 v1 schema。"""

        if value != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {value}")
        return value


class TimestampedModel(StrictModel):
    """统一将时间输出为带时区的 ISO 8601。"""

    @field_serializer("*", when_used="json", check_fields=False)
    def serialize_datetime(self, value: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError("naive datetime is not allowed")
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return value
