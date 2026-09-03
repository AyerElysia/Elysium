# learning

工程操作说明书。不是第一人称自我叙事，也不是任务。

心跳把到期事实放在机会页邀请栏。是否动手由当前意识决定。忽略、安静结束、不调用工具都完整，不等于拒绝。系统不会因为到期而自动 `get_skill`、自动调用本 skill，或把「本轮没注入某张 schema」解释成主体不想用。

MEMORY.md 不走本 skill 的主体文档复盘入口。长期记忆读取、短文字整理、索引和决定只使用常驻工具 `nucleus_memory_continuity_review`。

## 入口

`nucleus_learn` 是跟随本 skill 的执行门，不是第二套学习子系统。

- `action=help`：读取本文件。
- 其它 `action`：下面的操作名；也接受旧工具全名（如 `nucleus_reflect_now`）。
- 操作参数放在 `arguments` 对象里，字段以本节为准。带内部 `action` 的操作，把那个字段放在 `arguments.action`。

## 操作

### 反思与洞察账本

- `reflect_now`：`reflection_text`（要反思的内容）；可选 `reflection_type=introspection|interaction`。
- `list_insights`：可选 `status_filter=candidate|validated|rejected|all`，`limit`。
- `challenge_insight`：`insight_id`，`challenge`（反面证据/质疑理由）。
- `reconsider_insight`：`insight_id`；可选 `reason`。
- `observe_stale_insights`：可选 `threshold_days`，`max_results`。只观察，不强制改变。
- `list_validation_experiments`：可选 `status=pending|completed|all`，`max_results`。
- `complete_validation_experiment`：`experiment_id`，`actual_outcome`，`result_type=confirmed|contradicted|inconclusive`；可选 `notes`。

### 学习派生观察（非主体权威）

- `view_knowledge`：可选 `show_stats`。
- `knowledge_candidates`：`arguments.action=list|diff|decide`。`decide` 时 `decision=accept|decline`，可选 `version`、`reason`、`limit`。

### SOUL.md / USER.md

- `review_subject_document`：`arguments.action=status|unchanged|snooze|propose`。`target_path=SOUL.md|USER.md`。`propose` 提交完整新版本候选，不会自动接受。`status` 可带 `offset_bytes` / `max_bytes`。写操作需要 `reason`、`expected_subject_revision`、`reviewed_content_sha256`；`propose` 另需 `proposed_content`；`snooze` 可带 `snooze_hours`。
- `list_subject_candidates`：可选 `status`，`limit`。
- `read_subject_candidate`：`candidate_id`；可选 `offset_bytes`，`max_bytes`。
- `decide_subject_candidate`：`candidate_id`，`candidate_revision`，`candidate_sha256`，`expected_subject_revision`，`decision=accepted|rejected|kept_open`，`reason`；`accepted` 时完整 `accepted_content`。

任何 `target_path=MEMORY.md` 的通用候选只可审计，不能在这里接受。

### 程序性技能候选（蒸馏出的做事方式，仍须亲自决定）

- `list_skill_candidates`：可选 `status`，`limit`。
- `read_skill_candidate`：`candidate_id`；可选 `offset_bytes`，`max_bytes`。
- `decide_skill_candidate`：`candidate_id`，`candidate_revision`，`candidate_sha256`，`expected_subject_revision`，`decision=accepted|rejected|kept_open`，`reason`；接受时可选完整 `accepted_name` / `accepted_description` / `accepted_instructions`。

这与 `nucleus_skill`（管理已经存在的程序性记忆）不是同一扇门。
