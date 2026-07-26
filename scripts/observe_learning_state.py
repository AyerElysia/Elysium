#!/usr/bin/env python3
"""观察学习系统的真实状态

用途：在决定下一步开发前，先了解她的真实情况
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from plugins.life_engine.learning.store import InsightStore
from plugins.life_engine.learning.models import InsightStatus

def observe_learning_state(workspace_path: str):
    """观察学习系统状态"""
    workspace = Path(workspace_path).resolve()
    
    print("=" * 60)
    print("学习系统状态观察")
    print("=" * 60)
    
    # 检查学习目录是否存在
    learning_dir = workspace / ".life_learning"
    if not learning_dir.exists():
        print("\n❌ 学习系统尚未初始化")
        print("   .life_learning 目录不存在")
        print("\n结论：她还没有开始使用学习系统")
        return
    
    print(f"\n📁 工作空间: {workspace}")
    print(f"📁 学习目录: {learning_dir}")
    
    # 加载数据
    store = InsightStore(workspace)
    store.load()
    
    insights = store.list_all()
    
    print(f"\n📊 洞察统计")
    print(f"   总数: {len(insights)}")
    
    if len(insights) == 0:
        print("\n❌ 没有任何洞察")
        print("   结论：学习系统是空的，她还没有形成任何认知")
        return
    
    # 按状态分类
    stats = store.get_stats()
    print(f"\n   按状态分类:")
    for status, count in stats['by_status'].items():
        print(f"      {status}: {count}")
    
    print(f"\n   验证率: {stats['validation_rate']:.1%}")
    
    # 最近的洞察
    recent_insights = sorted(insights, key=lambda i: i.born_at, reverse=True)[:5]
    print(f"\n📝 最近5条洞察:")
    for ins in recent_insights:
        print(f"   [{ins.status[:4]}] {ins.category}: {ins.claim[:50]}...")
        print(f"          证据数: {len(ins.evidence)}, 创建于: {ins.born_at[:10]}")
    
    # 自我认知文档
    knowledge = store.read_current_knowledge()
    if knowledge:
        manifest = store.load_knowledge_manifest()
        version = manifest.get('current_version', 0)
        print(f"\n📖 自我认知文档: v{version}")
        print(f"   长度: {len(knowledge)} 字符")
        print(f"   预览: {knowledge[:200]}...")
    else:
        print(f"\n📖 自我认知文档: （尚未形成）")
    
    # 验证实验
    store._load_experiments()
    pending_exps = store.list_pending_experiments()
    completed_exps = store.list_completed_experiments()
    
    print(f"\n🧪 验证实验:")
    print(f"   待验证: {len(pending_exps)}")
    print(f"   已完成: {len(completed_exps)}")
    
    # 审计日志
    if (learning_dir / "insights_audit.jsonl").exists():
        with open(learning_dir / "insights_audit.jsonl") as f:
            audit_count = sum(1 for _ in f)
        print(f"\n📋 审计日志: {audit_count} 条记录")
    
    print("\n" + "=" * 60)
    print("观察总结")
    print("=" * 60)
    
    if len(insights) == 0:
        print("✋ 学习系统是空的，她还没有开始使用")
        print("   建议：等她开始使用，再决定优化方向")
    elif len(insights) < 10:
        print("🌱 学习系统刚起步，洞察较少")
        print("   建议：让她继续积累，观察遇到什么问题")
    else:
        print(f"🌳 学习系统已有 {len(insights)} 条洞察")
        print("   可以分析具体问题：")
        print("   - 洞察质量如何？")
        print("   - 她是否感到困惑？")
        print("   - 验证率低的原因？")
    
    print("\n💡 下一步:")
    print("   1. 如果她还没用 → 等她使用")
    print("   2. 如果洞察很少 → 让她积累经历")
    print("   3. 如果有具体问题 → 针对性解决")
    print("   4. 避免过度工程 → 基于真实需求")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="观察学习系统状态")
    parser.add_argument(
        "workspace", 
        nargs="?",
        default="data/life_engine_workspace",
        help="工作空间路径（默认: data/life_engine_workspace）"
    )
    args = parser.parse_args()
    
    observe_learning_state(args.workspace)
