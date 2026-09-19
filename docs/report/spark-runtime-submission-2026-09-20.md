# Spark runtime consolidation and model switching

## Scope and operating contract

This batch preserves the existing Spark changes to heartbeat checkpoint tooling,
maintenance budgets, KOOK message replay/delivery, and model response handling.
It also adds process-local task-model selection in the console:

```text
/model
/model core
/model core MiMo-V2.5-Pro
/model core clear
```

Only an already configured candidate of the selected task can be promoted.
Other candidates remain in the fallback list. Existing request model sets are
unchanged; subsequently constructed requests resolve the override. Configuration
files, subject identity, memories, and diaries are not rewritten. Overrides are
not persisted and disappear on process restart. The displayed active model means
the preferred route, not proof of which model eventually answers after fallback.
These commands belong to the operator console, not a public QQ chat command.

## Consolidation evidence (2026-09-20, Spark CST)

- Read repository instructions and inspected tracked/untracked changes before
  staging. No runtime database, credentials, subject files, or logs are included.
- Moved 27 untracked `.bak-*` files and one local Qwen evaluation report outside
  the repository, preserving their relative paths under
  `/home/ayerelysia/Elysia/Elysium-backups-8fdpvp3q`. No contents were deleted.
  Restore individual files from that directory only after checking the current
  destination; do not overwrite later edits. The report uses production request
  material and was deliberately not included in the source submission.
- Corrected newly introduced CRLF-only diff noise in `request.py` while retaining
  existing unchanged lines and all functional changes.
- Ayla's deployed checkout and its local package-lock edit remain untouched.
  Remote Elysium commits advance the Ayla gitlink; these must not be replaced
  with Spark's older deployed gitlink.
- Prior isolated verification: 93 selected model-registry tests passed; loading
  the actual `config/models.toml`, promoting `core` to `MiMo-V2.5-Pro`, and clearing
  the override succeeded. Direct console-handler simulation also succeeded.
  These did not make a real upstream model request or exercise the live process.
- Consolidation regression: 350 tests passed, with one dependency deprecation
  warning, using `uv run --no-sync python -m pytest --no-cov -n 0 -q` over model
  registry/configuration, runtime switching, OpenAI client, request retry policy,
  KOOK gateway/upload, chat events, life-engine configuration, event-stream
  simulation, reachability, heartbeat compression/timeout and minimal checkpoint
  tests. Test data is isolated; no full-suite run competed with production.
- A broader lint check found existing unused imports in unchanged
  `media_capabilities.py` and `trajectory_types.py`; those files were not edited.

## Pending acceptance and push gate

The service was active with a start time of 2026-09-19 15:00:06 CST, predating the
runtime-switch changes. This is not startup acceptance for this batch. Earlier
conversation claims of a completed implementation mean code plus isolated tests,
not production activation or end-to-end model-response acceptance.

Per AGENTS.md, the operator must manually restart Elysium before push. The agent
must not restart it. After the operator restart, verify initialization and plugin
logs, run `/model core`, temporarily promote a candidate, inspect a new request's
route and successful response, and clear the override. Preserve the original
configuration and ensure QQ/KOOK connectivity is healthy. Record observed results
without copying private messages or credentials. Until then keep commits local;
do not represent the batch as production-accepted or pushed.
