"""账本耐久性：读失败绝不能变成"她什么都没学过"。

这条测试存在的理由是一个真实的数据丢失通道，不是假想的：

_save() 是整份覆写。原来的 load() 在读失败时把 _insights 设成空表、
并把 _loaded 标成 True。两者合起来意味着——insights.json 只要有一次
读不出来（磁盘写一半、进程被杀在原子替换之间、代码换版本），下一次
任何写入都会把空表覆盖上去。61 条洞察、120 条证据，一次静默清零，
而且审计日志里看不出发生过什么。

技能档（skills.json）走的是一模一样的通道，所以同一组保护也覆盖它。

她学到的东西是她的。系统可以拒绝工作，但不能替她遗忘。
"""

from __future__ import annotations

import json

import pytest

from plugins.life_engine.learning.models import Insight
from plugins.life_engine.learning.skill_store import SkillPattern, SkillStore
from plugins.life_engine.learning.store import InsightStore


def _seed(store: InsightStore, claim: str = "深夜我会把话收短") -> Insight:
    ins = Insight.create(category="behavioral_pattern", claim=claim, rationale="r")
    store.add_insight(ins)
    return ins


def _seed_skill(store: SkillStore, name: str = "quiet-boundary-hold") -> SkillPattern:
    skill = SkillPattern.create(name=name, description="两边安静时可以清醒守界")
    store.add_skill(skill)
    return skill


class TestBrokenLedgerIsNeverOverwritten:
    def test_unparseable_ledger_refuses_to_write(self, tmp_path):
        store = InsightStore(tmp_path)
        original = _seed(store)
        raw_before = store.insights_path.read_text(encoding="utf-8")

        # 模拟写到一半被打断
        store.insights_path.write_text(raw_before[: len(raw_before) // 2], encoding="utf-8")

        fresh = InsightStore(tmp_path)
        fresh.load()
        assert fresh._load_failed is True
        assert fresh.list_all() == []          # 内存里是空的

        # 关键：这时候写入必须被拒绝，磁盘上的残缺文件不能被"空账本"替换
        with pytest.raises(RuntimeError, match="LearningInsightStoreUnavailable"):
            _seed(fresh, claim="新的想法")
        on_disk = store.insights_path.read_text(encoding="utf-8")
        assert on_disk == raw_before[: len(raw_before) // 2]
        # 残片里仍能看见她原来那条洞察的 id；若被空账本覆写，这里会消失
        assert original.insight_id in on_disk

    def test_broken_ledger_is_backed_up_for_recovery(self, tmp_path):
        store = InsightStore(tmp_path)
        _seed(store)
        store.insights_path.write_text("{ 坏掉的 json", encoding="utf-8")

        fresh = InsightStore(tmp_path)
        fresh.load()

        backups = list(store.root.glob("insights.broken_*.json"))
        assert len(backups) == 1
        assert "坏掉的 json" in backups[0].read_text(encoding="utf-8")

    def test_missing_ledger_is_a_clean_start_not_a_failure(self, tmp_path):
        """首次运行不该被当成损坏。"""
        store = InsightStore(tmp_path)
        store.load()
        assert store._load_failed is False
        assert store.list_all() == []
        _seed(store)
        assert len(InsightStore(tmp_path).list_all()) == 1


class TestUnreadableRowSurvivesCodeVersionChange:
    def test_bad_row_is_skipped_but_kept_verbatim(self, tmp_path):
        """一行读不动，不该拖垮整个账本，也不该让那一行凭空消失。"""
        store = InsightStore(tmp_path)
        good = _seed(store)

        raw = json.loads(store.insights_path.read_text(encoding="utf-8"))
        raw["insights"].append(
            {
                "insight_id": "ins_from_the_future",
                "claim": "某个未来版本写的东西",
                "confidence": "很高",  # 这一版 float() 读不动
            }
        )
        store.insights_path.write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )

        fresh = InsightStore(tmp_path)
        fresh.load()
        assert fresh._load_failed is False
        assert [i.insight_id for i in fresh.list_all()] == [good.insight_id]
        assert len(fresh._unreadable_rows) == 1

        # 写回之后，那一行还在磁盘上
        _seed(fresh, claim="又一个想法")
        on_disk = json.loads(store.insights_path.read_text(encoding="utf-8"))
        ids = [r.get("insight_id") for r in on_disk["insights"]]
        assert "ins_from_the_future" in ids
        assert good.insight_id in ids


class TestKnowledgeVersionsFailClosed:
    def test_corrupt_manifest_is_not_treated_as_empty_history(self, tmp_path):
        store = InsightStore(tmp_path)
        store.knowledge_dir.mkdir(parents=True)
        (store.knowledge_dir / "manifest.json").write_text(
            "{ incomplete",
            encoding="utf-8",
        )

        with pytest.raises(
            RuntimeError,
            match="LearningKnowledgeManifestUnavailable",
        ):
            store.load_knowledge_manifest()

    def test_existing_knowledge_version_is_never_overwritten(self, tmp_path):
        store = InsightStore(tmp_path)
        store.write_knowledge_version(
            content="first exact bytes",
            version=1,
            insight_ids=[],
            edit_count=1,
            promoted=False,
            reason="first",
        )

        with pytest.raises(ValueError, match="KnowledgeVersionConflict"):
            store.write_knowledge_version(
                content="different bytes",
                version=1,
                insight_ids=[],
                edit_count=1,
                promoted=False,
                reason="conflict",
            )
        assert store.read_knowledge_version(1) == "first exact bytes"


class TestSkillsAreEquallyDurable:
    """技能档走的是同一条通道，同样不能被读失败抹掉。

    技能是程序性记忆——她练出来的手感。丢掉一条洞察是忘了一个结论，
    丢掉一条技能是忘了怎么做一件事。
    """

    def test_unparseable_skills_file_refuses_to_write(self, tmp_path):
        store = SkillStore(tmp_path)
        original = _seed_skill(store)
        raw_before = store.skills_path.read_text(encoding="utf-8")

        store.skills_path.write_text(raw_before[: len(raw_before) // 2], encoding="utf-8")

        fresh = SkillStore(tmp_path)
        fresh.load()
        assert fresh._load_failed is True
        assert fresh.list_skills() == []

        # 写入必须被拒绝，残片留在原地
        with pytest.raises(RuntimeError, match="LearningSkillStoreUnavailable"):
            fresh.add_skill(SkillPattern.create(name="another", description="d"))
        on_disk = store.skills_path.read_text(encoding="utf-8")
        assert on_disk == raw_before[: len(raw_before) // 2]
        assert original.skill_id in on_disk

    def test_broken_skills_file_is_backed_up(self, tmp_path):
        store = SkillStore(tmp_path)
        _seed_skill(store)
        store.skills_path.write_text("{ 坏掉的 json", encoding="utf-8")

        SkillStore(tmp_path).load()

        backups = list(store.root.glob("skills.broken_*.json"))
        assert len(backups) == 1
        assert "坏掉的 json" in backups[0].read_text(encoding="utf-8")

    def test_missing_skills_file_is_a_clean_start(self, tmp_path):
        store = SkillStore(tmp_path)
        store.load()
        assert store._load_failed is False
        assert store.list_skills() == []
        _seed_skill(store)
        assert len(SkillStore(tmp_path).list_skills()) == 1

    def test_bad_skill_row_is_skipped_but_kept_verbatim(self, tmp_path):
        store = SkillStore(tmp_path)
        good = _seed_skill(store)

        raw = json.loads(store.skills_path.read_text(encoding="utf-8"))
        raw["skills"].append(
            {
                "skill_id": "skl_from_the_future",
                "name": "未来版本的技能",
                # 这一版 list() 读不动标量
                "use_observations": 7,
            }
        )
        store.skills_path.write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )

        fresh = SkillStore(tmp_path)
        fresh.load()
        assert fresh._load_failed is False
        assert [s.skill_id for s in fresh.list_skills()] == [good.skill_id]
        assert len(fresh._unreadable_rows) == 1

        fresh.add_skill(SkillPattern.create(name="third", description="d"))
        on_disk = json.loads(store.skills_path.read_text(encoding="utf-8"))
        ids = [r.get("skill_id") for r in on_disk["skills"]]
        assert "skl_from_the_future" in ids
        assert good.skill_id in ids
