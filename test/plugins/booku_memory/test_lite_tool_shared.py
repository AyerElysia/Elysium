"""Booku Memory 轻量工作流共享工具测试。"""

from plugins.booku_memory.lite_tool.shared import parse_json_object


def test_parse_json_object_reads_valid_object() -> None:
    """解析合法 JSON 对象。"""
    assert parse_json_object('{"content": "hello"}') == {"content": "hello"}


def test_parse_json_object_repairs_common_json_error() -> None:
    """解析可修复的 JSON 字符串。"""
    assert parse_json_object('{"content": "hello",}') == {"content": "hello"}


def test_parse_json_object_rejects_non_object() -> None:
    """非对象 JSON 不作为有效结果。"""
    assert parse_json_object('["hello"]') == {}


def test_parse_json_object_rejects_invalid_content() -> None:
    """无法修复的内容返回空字典。"""
    assert parse_json_object("not json") == {}
