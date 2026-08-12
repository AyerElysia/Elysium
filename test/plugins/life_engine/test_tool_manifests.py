"""意识工具清单契约测试（chat manifest 与静态文案对齐，防"未知的工具"回归）。

真实缺陷（2026-08-12）：chatter._build_primary_tool_guide 静态文案列出
`nucleus_bash`/`nucleus_grep_file`/`nucleus_view_screen`/`tool-inspect_media` 等，
但 chat 意识 manifest 漏配这些工具 → 过滤后 usable_map 无此名 → 模型照文案调用
报"未知的工具"（tool-nucleus_grep_file / tool-nucleus_bash）。
本契约：文案中出现的核心工具必须存在于 chat manifest。
"""
import pytest

from plugins.life_engine.service.tool_manifests import get_tool_manifest


# 与 chatter.py `_build_primary_tool_guide` 核心工具文案对齐（注册名 = schema 名）
_CHAT_GUIDE_TOOLS = {
    "tool-nucleus_bash",  # 查看或操作电脑终端
    "tool-nucleus_view_screen",  # 查看 Ayer 当前屏幕
    "tool-nucleus_manage_todo",  # 创建 TODO
    "tool-inner_dialogue",  # 把念头沉进心里慢慢想
    "tool-inspect_media",  # 把图片/视频/语音提升为原生多模态输入
    "action-life_send_text",  # 发送文字给用户
    "action-life_pass_and_wait",  # 结束本轮
}


def test_chat_manifest_contains_guide_tools() -> None:
    """文案中列出的核心工具必须都在 chat 意识清单里（否则照文案调用会"未知的工具"）。"""
    chat = set(get_tool_manifest("chat"))
    missing = _CHAT_GUIDE_TOOLS - chat
    assert not missing, f"chat manifest 缺少文案中列出的工具: {sorted(missing)}"


def test_chat_manifest_contains_file_tools() -> None:
    """文件读写/搜索类基础能力必须在 chat 清单（读日记/搜索 workspace 依赖它们）。"""
    chat = set(get_tool_manifest("chat"))
    required = {
        "tool-nucleus_grep_file",
        "tool-nucleus_read_file",
        "tool-nucleus_list_files",
        "tool-nucleus_edit_file",
        "tool-nucleus_write_file",
        "tool-nucleus_mkdir",
    }
    missing = required - chat
    assert not missing, f"chat manifest 缺少文件类工具: {sorted(missing)}"


@pytest.mark.parametrize("kind", ["chat", "minecraft", "livestream", "voice_live", "memory_witness"])
def test_manifest_kind_declared(kind: str) -> None:
    """已声明的意识类型必须能取到清单（未声明的 kind 抛 KeyError 是设计）。"""
    manifest = get_tool_manifest(kind)
    assert isinstance(manifest, list)
    assert all(name.startswith(("tool-", "action-")) for name in manifest)
