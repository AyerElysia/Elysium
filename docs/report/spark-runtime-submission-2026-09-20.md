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

## Operator acceptance (2026-09-21, Spark CST)

The operator restarted the running instance. The live process started at
2026-09-21 17:47:49 CST from `/home/ayerelysia/Elysia/Elysium`, loaded all 15
plugins, and reported `Elysium 已苏醒`. Its console then exercised the installed
command handler:

1. `/model` listed the configured task routes.
2. `/model core MiMo-V2.5-Pro` returned `generation=1` and stated that new
   requests use the selected model while in-flight requests remain unchanged.
3. `/model core` immediately reported `MiMo-V2.5-Pro` as active and retained the
   original candidate fallback list.
4. `/model core clear` restored `qwen3.8-flash-next` and left no runtime override.

The request inspector remained empty during the observation window, so this is
live startup and routing-command acceptance, not proof of a completed upstream
model response. No synthetic chat message was injected, no QQ/KOOK message was
sent, and no subject or diary content was changed. The runtime override is
process-local and is now cleared.

The live process was PID 2201942, owned by the `tmux -L elysium` session,
not `elysium.service` (which was inactive). Service status alone would have
incorrectly implied that Elysium was stopped. A NapCat heartbeat timeout was
visible at 18:07:05; this check did not establish subsequent QQ recovery and
must not be cited as acceptance of overall messaging health.
