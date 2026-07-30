"""NapCat 适配器工具层。"""

from .cache import get_cached, set_cached, GROUP_INFO_TTL, MEMBER_INFO_TTL, SELF_INFO_TTL, STRANGER_INFO_TTL
from .constants import ACCEPT_FORMAT, QQ_FACE, CommandType, NoticeType, RealMessageType, RequestType
from .media import convert_image_to_gif, download_image_base64, get_image_format

__all__ = [
    "get_cached", "set_cached",
    "GROUP_INFO_TTL", "MEMBER_INFO_TTL", "SELF_INFO_TTL", "STRANGER_INFO_TTL",
    "ACCEPT_FORMAT", "QQ_FACE", "CommandType", "NoticeType", "RealMessageType", "RequestType",
    "convert_image_to_gif", "download_image_base64", "get_image_format",
]
