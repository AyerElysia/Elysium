# 梦境系统改进方案

> 基于 `/root/Elysia/Neo-MoFox/notion/dream_diagnosis.md` 的诊断结果
> 目标文件：`/root/Elysia/Neo-MoFox/plugins/life_engine/dream.py`
> 设计原则：**仿生优先** — 不真正像人类做梦的机制就没有意义

---

## 改动总览

本方案包含 7 项改进。核心理念：**删除 fallback 模板（违背仿生原则）**，转而修复 LLM 调用的可靠性；让种子选择从确定性管道变为噪声驱动的概率采样；让整个记忆图谱都成为做梦素材的来源。

---

## P0 — 消除重复根因

### 改进 1：删除 Fallback 模板，修复 LLM 可靠性

**问题**：`_build_fallback_scene()` 用硬编码模板伪造梦境，严重违背仿生原则。同时 LLM 调用失败率高（约 50%），疑似超时或 JSON 解析问题。

**改动**：

1. **删除 `_build_fallback_scene()` 方法**，不要有任何模板替代品
2. **`_build_dream_scene()` 增加重试机制**（最多 3 次）
3. **大幅增加超时**：从 300s → 600s（LLM 可能返回慢）
4. **失败时正式记录为"做梦失败"**，不伪造结果

```python
_DREAM_SCENE_TIMEOUT_SECONDS = 600.0  # 从 300 → 600，给 LLM 足够时间
_DREAM_SCENE_MAX_RETRIES = 3

async def _build_dream_scene(
    self,
    *,
    seeds: list[DreamSeed],
    rem_report: REMReport,
    event_history: list[Any],
) -> tuple[DreamTrace, str, DreamResidue] | None:
    """用 LLM 将入梦种子变形成梦境。失败时返回 None（不伪造梦境）。"""
    last_error: Exception | None = None
    
    for attempt in range(_DREAM_SCENE_MAX_RETRIES):
        try:
            payload = await self._generate_scene_payload(
                seeds=seeds,
                rem_report=rem_report,
                event_history=event_history,
            )
            trace = _trace_from_payload(payload.get("dream_trace"))
            dream_text = _clean_text(payload.get("dream_text"))
            residue = _residue_from_payload(payload.get("dream_residue"))
            if dream_text and residue.summary:
                return trace, dream_text, residue
            logger.warning(
                f"DreamSceneBuilder 第{attempt+1}次返回空文本，重试中..."
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                f"DreamSceneBuilder 第{attempt+1}次异常：{exc}，重试中..."
            )
            await asyncio.sleep(2)  # 短暂等待后重试
    
    logger.error(
        f"做梦失败：DreamSceneBuilder {_DREAM_SCENE_MAX_RETRIES}次尝试均失败。"
        f"最后错误：{last_error}"
    )
    return None  # 明确返回 None，不伪造
```

5. **`run_dream_cycle()` 处理 None**：做梦失败就是失败，这次不产出梦境

```python
# 在 run_dream_cycle() 中（约第 290 行）：
result = await self._build_dream_scene(
    seeds=report.seed_report,
    rem_report=report.rem,
    event_history=event_history,
)
if result is None:
    # 做梦失败 — NREM 和 REM 的脑活动已经完成，但没有形成清晰的梦
    # 这在人类身上也会发生（很多睡眠周期不产生可回忆的梦）
    report.dream_text = ""
    report.narrative = ""
    logger.info(f"🌙 Dream [{report.dream_id}] 本次未形成清晰梦境（类似无梦睡眠）")
    # 仍然正常进入醒来阶段
else:
    trace, dream_text, residue = result
    report.dream_trace = trace
    report.dream_text = dream_text
    report.narrative = dream_text
    report.dream_residue = residue
    report.archive_path = await self._archive_dream(report)
    # ... 后续正常流程
```

---

## P1 — 仿生化种子选择

### 改进 2：神经噪声 — Temperature Scoring

**仿生依据**：人类做梦时前额叶皮层活动降低，神经元随机放电增加。这种"噪声"是梦境多样性的关键来源。

**改动位置**：`_select_seed_candidates()`（第 1047-1069 行）

```python
_SEED_SCORE_TEMPERATURE = 0.15  # 高斯噪声标准差

def _select_seed_candidates(self, candidates: list[DreamSeed]) -> list[DreamSeed]:
    """按类型优先 + 神经噪声选择最终种子。"""
    if not candidates:
        return []
    
    # 给每个种子加入神经噪声（模拟前额叶抑制下的随机激活）
    scored = []
    for seed in candidates:
        noise = random.gauss(0, _SEED_SCORE_TEMPERATURE)
        effective = max(0.01, seed.score + noise)
        scored.append((effective, seed))
    
    # 按类型分组，每组取最高有效分
    by_type: dict[str, tuple[float, DreamSeed]] = {}
    for eff_score, seed in sorted(scored, key=lambda x: x[0], reverse=True):
        by_type.setdefault(seed.seed_type, (eff_score, seed))
    
    selected = [seed for _, seed in sorted(by_type.values(), key=lambda x: x[0], reverse=True)]
    
    if len(selected) >= _MAX_DREAM_SEEDS:
        return selected[:_MAX_DREAM_SEEDS]
    
    # 剩余名额：加权随机采样（不是 top-K）
    existing_ids = {s.seed_id for s in selected}
    remaining = [(eff, s) for eff, s in scored if s.seed_id not in existing_ids]
    if remaining:
        weights = [eff for eff, _ in remaining]
        total = sum(weights)
        if total > 0:
            extra_count = min(_MAX_DREAM_SEEDS - len(selected), len(remaining))
            extras = random.choices(
                [s for _, s in remaining],
                weights=[w/total for w in weights],
                k=extra_count,
            )
            selected.extend(extras)
    
    return selected[:_MAX_DREAM_SEEDS]
```

### 改进 3：重复抑制 — 海马体新鲜度衰减

**仿生依据**：海马体对重复刺激的响应会自然衰减（habituation）。反复出现的记忆在短期内的激活阈值会升高。

**新增数据结构**：

```python
_DREAM_HISTORY_WINDOW = 5
_REPETITION_DECAY = 0.3  # 每次重复扣 0.3

# DreamScheduler.__init__ 新增：
self._recent_seed_titles: deque[set[str]] = deque(maxlen=_DREAM_HISTORY_WINDOW)
```

**在 `_generate_dream_seeds()` 中应用**：

```python
# 收集完所有候选后，应用重复衰减
recent_titles = set()
for title_set in self._recent_seed_titles:
    recent_titles.update(title_set)

for seed in candidates:
    if seed.title in recent_titles:
        seed.score = max(0.05, seed.score - _REPETITION_DECAY)

selected = self._select_seed_candidates(candidates)

# 记录本次选中的种子
if selected:
    self._recent_seed_titles.append({s.title for s in selected})
```

在 `serialize()` / `deserialize()` 中持久化 `_recent_seed_titles`。

### 改进 4：全图谱记忆候选 — 自由联想

**仿生依据**：人类做梦时，大脑不只访问"重要"记忆 — 任何记忆都可能被随机激活。海马体-新皮层对话不受前额叶的"重要性"滤波约束。

**改动**：`_load_memory_candidates()` 不再只取 top-12，而是从全图谱随机游走采样。

```python
async def _load_memory_candidates(self) -> list[dict[str, Any]]:
    """从整个记忆图谱中采样候选节点 — 模拟海马体自由联想。
    
    策略：
    - 保留 top-5 高重要性节点（核心记忆更容易被激活）
    - 从全图谱随机采样 15 个节点（任何记忆都可能闪回）
    - 合并去重，总共 ~20 个候选
    """
    if self._memory is None:
        return []
    
    candidates: list[dict[str, Any]] = []
    
    # 1. 高重要性核心节点（模拟高激活强度记忆）
    getter = getattr(self._memory, "list_dream_candidate_nodes", None)
    if callable(getter):
        try:
            top_nodes = await getter(limit=5)
            if isinstance(top_nodes, list):
                candidates.extend(item for item in top_nodes if isinstance(item, dict))
        except Exception as exc:
            logger.debug(f"读取 top memory candidates 失败：{exc}")
    
    # 2. 全图谱随机采样（模拟海马体自由放电）
    random_getter = getattr(self._memory, "list_dream_candidate_nodes", None)
    if callable(random_getter):
        try:
            # 取大量候选然后随机采样
            all_nodes = await random_getter(limit=200)
            if isinstance(all_nodes, list):
                valid = [item for item in all_nodes if isinstance(item, dict)]
                existing_ids = {c.get("node_id") for c in candidates}
                remaining = [n for n in valid if n.get("node_id") not in existing_ids]
                if remaining:
                    sample_size = min(15, len(remaining))
                    sampled = random.sample(remaining, sample_size)
                    candidates.extend(sampled)
        except Exception as exc:
            logger.debug(f"随机采样 memory candidates 失败：{exc}")
    
    random.shuffle(candidates)
    return candidates
```

**注意**：`list_dream_candidate_nodes` 的 `limit` 参数需要传大值（200），或者在 memory_service.py 中新增一个 `list_random_file_nodes()` 方法用 `ORDER BY RANDOM() LIMIT ?` 做真正的随机采样。推荐后者更高效：

```python
# memory_service.py 新增方法：
async def list_random_file_nodes(self, *, limit: int = 15) -> List[Dict[str, Any]]:
    """随机采样文件节点 — 供做梦系统自由联想使用。"""
    if not self._initialized or not self._db:
        return []
    cursor = self._db.cursor()
    cursor.execute(
        """
        SELECT node_id, file_path, title, activation_strength, access_count,
               emotional_valence, emotional_arousal, importance, updated_at
        FROM memory_nodes
        WHERE node_type = ?
        ORDER BY RANDOM()
        LIMIT ?
        """,
        (NodeType.FILE.value, max(1, int(limit))),
    )
    return [dict(row) for row in cursor.fetchall()]
```

然后 dream.py 中的 `_load_memory_candidates()` 可以同时调用 `list_dream_candidate_nodes(limit=5)` + `list_random_file_nodes(limit=15)`。

---

## P2 — 深化仿生性

### 改进 5：LLM Prompt 多样性指令 + 避重上下文

**仿生依据**：人类大脑在做梦时会将记忆片段重新组合、变形、象征化。不是复述白天，而是创造性地重构。

**改动位置**：`_generate_scene_payload()` 的 system prompt（第 527-554 行）

在 system prompt 末尾追加：

```python
diversity_lines = [
    "",
    "多样性约束（极重要）：",
    "- 每个梦的场景类型必须不同：不要总用走廊/房间/追赶，尝试水域、天空、声音、温度、光影、季节、时间折叠。",
    "- 情绪基调必须变化：怅然只是一种可能，还有好奇、微甜、荒诞、安宁、不安、恍惚、炽热等。",
    "- 叙事视角可以变化：第一人称、旁观者、片段式意识流、倒叙、多线交织。",
    "- 将 seeds 中的具体内容（TODO标题、文件名）变形为象征意象，不要直接引用原文。",
]
```

在 user prompt 中，如果有最近梦境记录，传入避重参考：

```python
# DreamScheduler 新增：
self._recent_dream_summaries: deque[str] = deque(maxlen=5)

# 在 brief dict 中追加：
if self._recent_dream_summaries:
    brief["avoid_recent_themes"] = list(self._recent_dream_summaries)
    # 在 user prompt 中追加：
    # "以下是最近的梦境摘要，本次梦境必须在主题、意象和情绪上与它们不同："
```

### 改进 6：REM 渐进式参数变化 — 模拟睡眠周期

**仿生依据**：人类一晚经历 4-6 个睡眠周期，后半夜的 REM 期更长、更vivid、更荒诞。

**改动位置**：`_run_rem()` 方法

```python
async def _run_rem(self, seed_node_ids: list[str]) -> REMReport:
    """REM 阶段：渐进式联想扩散 — 后半夜梦更深更wild。"""
    # 当晚第几个梦 → 控制 REM 强度
    n = self._dreams_since_sleep_start  # 需要在 DreamScheduler 中跟踪
    
    # 渐进参数：后半夜更深、更广、衰减更慢
    effective_depth = self._rem_max_depth + min(n, 3)
    effective_decay = min(self._rem_decay_factor + n * 0.05, 0.85)
    effective_seeds = self._rem_seeds_per_round + n
    effective_rounds = self._rem_walk_rounds + (1 if n >= 2 else 0)
    
    # ... 使用调整后的参数
```

新增 `self._dreams_since_sleep_start: int = 0`，在 `enter_sleep()` 时重置为 0，每次做梦后 +1。

### 改进 7：dream_lag 扩展 — 远期记忆闪回

**仿生依据**：人类做梦不只回溯一周内的记忆。研究表明梦中经常出现数月甚至数年前的内容。远期记忆闪回（flashback）是重要的做梦特征。

**改动位置**：`_collect_dream_lag()` 方法（第 961-1009 行）

```python
async def _collect_dream_lag(self, memory_candidates: list[dict]) -> list[DreamSeed]:
    """收集延迟记忆 — 经典梦滞后（4-8天）+ 远期闪回（14-90天）。"""
    
    # 70% 概率走经典 4-8 天窗口
    # 30% 概率走远期闪回窗口（14-90天）
    if random.random() < 0.3:
        min_age, max_age, optimal_age = 14, 90, 30
    else:
        min_age, max_age, optimal_age = 4, 8, 6
    
    # ... 使用选定窗口扫描文件，optimal_age 用于距离惩罚计算
```

---

## 实施顺序

```
第一步（P0）：改进 1
  → 删除 fallback，修复 LLM 重试 + 超时，失败 = 无梦（合理仿生）
  → 工作量：~30 分钟

第二步（P1）：改进 2 + 3 + 4
  → 神经噪声 + 重复衰减 + 全图谱采样
  → 工作量：~1.5 小时（含 memory_service 新方法 + 序列化）

第三步（P2）：改进 5 + 6 + 7
  → LLM 多样性 + REM 渐变 + 远期闪回
  → 工作量：~1 小时
```

---

## 不改动的部分

以下设计保持不变，因为它们是正确的仿生实现：

- NREM 事件重放 + SNN + 突触稳态缩放（SHY 假说）
- REM 记忆图谱随机游走 + Hebbian 学习 + 弱边修剪
- 四路种子模型（day_residue, dream_lag, unfinished_tension, self_theme）
- 梦境残余注入后续行为（24h TTL）
- 梦境归档 Markdown + YAML frontmatter
- 梦境节点集成到记忆图谱
