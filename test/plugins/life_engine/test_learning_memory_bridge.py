"""反思环 → 记忆修正的落地，以及自动演化链的源文件推断。

这两条路径此前都是"看起来接好了、实际上永远不触发"：
- 反思出的"我之前理解错了"没有写进 memory_corrections，检索时看不见；
- 自动演化链用 RRF 分去比绝对阈值 0.6（RRF 最大约 0.033），永远不成立。
"""

from __future__ import annotations

from typing import Any

import pytest

from plugins.life_engine.learning.models import Evidence, EvidenceKind, Insight
from plugins.life_engine.learning.reflection import ReflectionEngine
from plugins.life_engine.learning.store import InsightStore
from plugins.life_engine.memory.edges import EdgeType
from plugins.life_engine.memory.search import SearchResult
from plugins.life_engine.tools.file_tools import (
    _build_lineage_query,
    _extract_new_material,
    _find_similar_source_file,
)


def _hit(path: str, relevance: float, source: str = "direct") -> SearchResult:
    return SearchResult(
        file_path=path,
        title=path,
        snippet="",
        relevance=relevance,
        source=source,
    )


class _FakeMemoryService:
    """只实现反思环与演化链推断真正调用到的两个方法。"""

    def __init__(
        self,
        *,
        search_results: list[SearchResult] | None = None,
        reject_paths: bool = False,
    ) -> None:
        self.search_results = search_results or []
        self.reject_paths = reject_paths
        self.corrections: list[dict[str, Any]] = []
        self.search_queries: list[str] = []

    async def search_memory(self, **kwargs: Any) -> list[SearchResult]:
        self.search_queries.append(str(kwargs.get("query", "")))
        return list(self.search_results)

    async def record_memory_correction(self, **kwargs: Any) -> list[Any]:
        if self.reject_paths and kwargs.get("related_paths"):
            raise ValueError("不支持索引的记忆文档路径: 测试")
        self.corrections.append(kwargs)
        return []


def _engine(tmp_path, memory_service: Any | None) -> ReflectionEngine:
    return ReflectionEngine(
        store=InsightStore(tmp_path),
        workspace_path=tmp_path,
        memory_service=memory_service,
    )


def _insight(
    claim: str,
    *,
    rationale: str = "",
    topic_key: str = "",
    category: str = "自我认知",
) -> Insight:
    return Insight.create(
        category=category,
        claim=claim,
        rationale=rationale,
        topic_key=topic_key,
        initial_evidence=[
            Evidence.create(
                kind=EvidenceKind.SELF_OBSERVATION,
                description="测试证据",
            )
        ],
    )


# ── 修正型洞察的识别 ────────────────────────────────────────


def test_extract_correction_message_requires_correction_marker(tmp_path) -> None:
    """没有"我之前理解错了"语气的洞察，不该被当成记忆修正。"""
    engine = _engine(tmp_path, _FakeMemoryService())

    plain = _insight("先回应情绪再给建议，效果更好")
    assert engine._extract_correction_message(plain) == ""


def test_extract_correction_message_detects_marker_and_appends_rationale(tmp_path) -> None:
    """命中修正标记时，把依据一并写进修正说明。"""
    engine = _engine(tmp_path, _FakeMemoryService())

    insight = _insight(
        "他沉默不是在生气，而是在整理想法",
        rationale="之前以为沉默等于不满，这次问了才知道",
    )
    message = engine._extract_correction_message(insight)

    assert "沉默不是在生气" in message
    assert "依据：" in message


def test_extract_correction_message_skips_too_short_claim(tmp_path) -> None:
    """过短的陈述没有可追溯价值。"""
    engine = _engine(tmp_path, _FakeMemoryService())
    assert engine._extract_correction_message(_insight("不是")) == ""


# ── 修正记录的落盘与绑定 ────────────────────────────────────


async def test_auto_record_corrections_binds_to_retrieved_files(tmp_path) -> None:
    """修正要挂到真实记忆文件上，否则检索该文件时看不见它已被修正。"""
    service = _FakeMemoryService(
        search_results=[_hit("USER.md", 0.03), _hit("diaries/2026-07-26.md", 0.02)]
    )
    engine = _engine(tmp_path, service)

    await engine._auto_record_corrections(
        [_insight("他沉默不是在生气，其实是在整理想法", topic_key="情绪解读")],
        reflection_type="interaction",
    )

    assert len(service.corrections) == 1
    recorded = service.corrections[0]
    assert recorded["topic"] == "情绪解读"
    assert recorded["source"] == "reflection"
    assert recorded["related_paths"] == ["USER.md", "diaries/2026-07-26.md"]


async def test_auto_record_corrections_ignores_non_correction_insights(tmp_path) -> None:
    service = _FakeMemoryService(search_results=[_hit("USER.md", 0.03)])
    engine = _engine(tmp_path, service)

    await engine._auto_record_corrections(
        [_insight("先回应情绪再给建议，效果更好")],
        reflection_type="interaction",
    )

    assert service.corrections == []


async def test_auto_record_corrections_caps_per_run(tmp_path) -> None:
    """一次反思不该刷满修正表。"""
    service = _FakeMemoryService(search_results=[_hit("USER.md", 0.03)])
    engine = _engine(tmp_path, service)

    await engine._auto_record_corrections(
        [
            _insight("A 其实并不像我以为的那样"),
            _insight("B 不是我原以为的原因"),
            _insight("C 需要修正之前的判断"),
        ],
        reflection_type="introspection",
    )

    assert len(service.corrections) == 2


async def test_auto_record_corrections_dedups_same_message(tmp_path) -> None:
    service = _FakeMemoryService(search_results=[_hit("USER.md", 0.03)])
    engine = _engine(tmp_path, service)

    duplicate = "他沉默不是在生气，其实是在整理想法"
    await engine._auto_record_corrections(
        [_insight(duplicate), _insight(duplicate)],
        reflection_type="interaction",
    )

    assert len(service.corrections) == 1


async def test_auto_record_corrections_falls_back_when_paths_rejected(tmp_path) -> None:
    """路径不可索引时降级为不绑定文件，而不是丢掉这次认知转折。"""
    service = _FakeMemoryService(
        search_results=[_hit("outside/notes.md", 0.03)],
        reject_paths=True,
    )
    engine = _engine(tmp_path, service)

    await engine._auto_record_corrections(
        [_insight("他沉默不是在生气，其实是在整理想法")],
        reflection_type="interaction",
    )

    assert len(service.corrections) == 1
    assert service.corrections[0]["related_paths"] is None


async def test_auto_record_corrections_noop_without_memory_service(tmp_path) -> None:
    """记忆服务缺失时静默跳过，不能让反思环因此报错。"""
    engine = _engine(tmp_path, None)
    await engine._auto_record_corrections(
        [_insight("他沉默不是在生气，其实是在整理想法")],
        reflection_type="interaction",
    )


async def test_auto_record_corrections_survives_search_failure(tmp_path) -> None:
    """检索失败也要把修正记下来，只是不绑定文件。"""

    class _BrokenSearch(_FakeMemoryService):
        async def search_memory(self, **kwargs: Any) -> list[SearchResult]:
            raise RuntimeError("检索挂了")

    service = _BrokenSearch()
    engine = _engine(tmp_path, service)

    await engine._auto_record_corrections(
        [_insight("他沉默不是在生气，其实是在整理想法")],
        reflection_type="interaction",
    )

    assert len(service.corrections) == 1
    assert service.corrections[0]["related_paths"] is None


# --------------------------------------------------------------------------
# 演化链源文件推断：新增内容的提取
# --------------------------------------------------------------------------


def test_extract_new_material_returns_only_added_lines() -> None:
    """编辑已有笔记时，只有新增的行能表达"我在延续什么"。"""
    before = "# 关于沉默\n他沉默的时候我以为是生气\n"
    after = "# 关于沉默\n他沉默的时候我以为是生气\n后来发现那是在整理想法\n"

    assert _extract_new_material(before, after) == "后来发现那是在整理想法"


def test_extract_new_material_uses_full_content_for_new_file() -> None:
    """新建文件没有 before，全文本身就是新增内容。"""
    after = "# 新笔记\n这是整理旧笔记后得到的结论\n"

    assert _extract_new_material("", after) == after.strip()
    assert _extract_new_material(None, after) == after.strip()


def test_extract_new_material_falls_back_when_nothing_added() -> None:
    """只调换顺序时没有新增行，退回全文而不是放弃推断。"""
    before = "第一行内容在这里\n第二行内容在这里"
    after = "第二行内容在这里\n第一行内容在这里"

    assert _extract_new_material(before, after) == after


def test_extract_new_material_returns_empty_for_blank_after() -> None:
    assert _extract_new_material("旧内容", "   \n  ") == ""
    assert _extract_new_material("旧内容", None) == ""


# --------------------------------------------------------------------------
# 演化链源文件推断：查询文本的构造
# --------------------------------------------------------------------------


def test_build_lineage_query_strips_markdown_prefixes() -> None:
    """标题符号本身不携带语义，去掉后才是可检索的内容。"""
    material = "# 关于沉默这件事的理解\n- 他不是在生气而是在整理\n> 引用了昨天的对话记录"

    query = _build_lineage_query(material)

    assert "#" not in query
    assert "-" not in query
    assert ">" not in query
    assert query.startswith("关于沉默这件事的理解")
    assert "他不是在生气而是在整理" in query
    assert "引用了昨天的对话记录" in query


def test_build_lineage_query_drops_lines_shorter_than_eight_chars() -> None:
    """信息量门槛是 8 字：短标题会被整行丢掉，这是有意的取舍。"""
    material = "# 关于沉默的理解\n他沉默的时候是在整理想法而不是生气"

    query = _build_lineage_query(material)

    assert "关于沉默的理解" not in query
    assert query == "他沉默的时候是在整理想法而不是生气"


def test_build_lineage_query_drops_short_lines() -> None:
    """过短的行（如"是的"）会污染检索，优先取有信息量的行。"""
    material = "好\n嗯\n他沉默的时候是在整理想法而不是生气\n对"

    assert _build_lineage_query(material) == "他沉默的时候是在整理想法而不是生气"


def test_build_lineage_query_keeps_short_lines_when_nothing_else() -> None:
    """全是短行时退让一步，避免直接放弃推断。"""
    assert _build_lineage_query("好的\n嗯嗯") == "好的 嗯嗯"


def test_build_lineage_query_limits_lines_and_chars() -> None:
    material = "\n".join(f"这是第{i}行足够长的内容用来测试行数限制" for i in range(10))

    query = _build_lineage_query(material)

    assert len(query) <= 300
    assert "这是第4行" not in query  # 只取前 4 行（索引 0-3）
    assert "这是第3行" in query


def test_build_lineage_query_returns_empty_for_blank() -> None:
    assert _build_lineage_query("") == ""
    assert _build_lineage_query("\n\n   \n") == ""


# --------------------------------------------------------------------------
# 演化链源文件推断：相对分数判定
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_similar_source_file_accepts_rrf_scale_scores(tmp_path) -> None:
    """核心回归：RRF 分只有 0.0x 量级，不能拿去比 0.6 这种绝对阈值。"""
    service = _FakeMemoryService(
        search_results=[_hit("memory/old_note.md", 0.0164), _hit("memory/other.md", 0.0100)]
    )

    found = await _find_similar_source_file(
        service,
        target_path="memory/new_note.md",
        target_content="他沉默的时候是在整理想法而不是生气",
        edge_type=EdgeType.CONTINUES,
    )

    assert found == "memory/old_note.md"


@pytest.mark.asyncio
async def test_find_similar_source_file_skips_self(tmp_path) -> None:
    """编辑自己时最高命中往往就是自己，不能连成自环。"""
    service = _FakeMemoryService(
        search_results=[_hit("memory/note.md", 0.0164), _hit("memory/source.md", 0.0160)]
    )

    found = await _find_similar_source_file(
        service,
        target_path="memory/note.md",
        target_content="足够长的内容用来触发检索逻辑",
        edge_type=EdgeType.CONTINUES,
    )

    assert found == "memory/source.md"


@pytest.mark.asyncio
async def test_find_similar_source_file_rejects_weak_candidate(tmp_path) -> None:
    """自己之外的候选明显不突出时，宁可不连线也不要连错。"""
    service = _FakeMemoryService(
        search_results=[_hit("memory/note.md", 0.0164), _hit("memory/weak.md", 0.0050)]
    )

    found = await _find_similar_source_file(
        service,
        target_path="memory/note.md",
        target_content="足够长的内容用来触发检索逻辑",
        edge_type=EdgeType.CONTINUES,
    )

    assert found is None


@pytest.mark.asyncio
async def test_find_similar_source_file_ignores_associated_hits(tmp_path) -> None:
    """联想扩散来的文件不足以作为演化源。"""
    service = _FakeMemoryService(
        search_results=[_hit("memory/assoc.md", 0.0164, source="associated")]
    )

    found = await _find_similar_source_file(
        service,
        target_path="memory/note.md",
        target_content="足够长的内容用来触发检索逻辑",
        edge_type=EdgeType.CONTINUES,
    )

    assert found is None


@pytest.mark.asyncio
async def test_find_similar_source_file_returns_none_on_empty_results(tmp_path) -> None:
    service = _FakeMemoryService(search_results=[])

    found = await _find_similar_source_file(
        service,
        target_path="memory/note.md",
        target_content="足够长的内容用来触发检索逻辑",
        edge_type=EdgeType.CONTINUES,
    )

    assert found is None


@pytest.mark.asyncio
async def test_find_similar_source_file_survives_search_failure(tmp_path) -> None:
    """推断失败不能影响本次写入操作本身。"""

    class _BrokenSearch(_FakeMemoryService):
        async def search_memory(self, **kwargs: Any) -> list[SearchResult]:
            raise RuntimeError("检索挂了")

    found = await _find_similar_source_file(
        _BrokenSearch(),
        target_path="memory/note.md",
        target_content="足够长的内容用来触发检索逻辑",
        edge_type=EdgeType.CONTINUES,
    )

    assert found is None


@pytest.mark.asyncio
@pytest.mark.parametrize("edge_type", [EdgeType.RENAMES, EdgeType.REINTERPRETS])
async def test_find_similar_source_file_refuses_to_guess_for_non_lineage_edges(
    edge_type,
) -> None:
    """改名和重新解释必须有明确的源，猜出来的就是伪造的血缘。

    continues/refines/corrects 猜错了只是关联弱一点；renames 猜错了等于宣称
    "这份笔记就是那份笔记"，会把两条独立的记忆合并成一条假的演化史。
    """
    service = _FakeMemoryService(search_results=[_hit("memory/old.md", 0.033)])

    found = await _find_similar_source_file(
        service,
        target_path="memory/note.md",
        target_content="足够长的内容用来触发检索逻辑",
        edge_type=edge_type,
    )

    assert found is None
    # 连检索都不该发起
    assert service.search_queries == []
