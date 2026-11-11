# Python Breeze client bounded shared-service contract

This is the engineering investigation and isolated verification for the shared
Breeze service change. It contains no subject text, voice reference transcript,
credentials, or synthesis output.

## Observed deployment and cause

Read-only inspection found the Python Elysium process running `main.py` from the
parent repository. Its corresponding ignored TTS configuration selected
`breeze_tts2` at `http://127.0.0.1:7862`, with a 1200-second request timeout,
transport splitting enabled, and `auto_start = false`. One Breeze container
published that endpoint; no additional listeners were found at the historical
9880/8092 TTS ports. Legacy GPT startup fields remained configured but were
inactive. External service ownership meant the plugin's owned-process idle
timer did not shut down this container.

The old client retried HTTP 409 every 0.5 seconds until each segment's independent
deadline. The serial segment loop also continued after a failed segment and only
rejected the incomplete expression after all remaining segments had run. Together
these paths could turn one busy backend into repeated 20-minute waits. The local
expression lock then held later callers behind that unsuccessful expression.
This is a timeout/retry amplification path, not evidence of a periodic
20-minute shutdown.

## Change and compatibility

- Breeze retains its multipart fields, trusted reference voice and instruction,
  PCM validation, segmentation order and single returned outward expression.
- One configured timeout now covers local lane waiting, reference reads, all
  segments, PCM receipt and final processing. Caller cancellation propagates.
- Compatibility handling for HTTP 409 stops after three attempts. HTTP 503 or
  504 fails explicitly without automatic replay.
- The first failed serial segment stops later requests. Partial audio is never
  delivered as a complete expression.
- `X-Breeze-Client: elysium-python` identifies the engineering caller without
  granting authority. `X-Breeze-Timeout-Ms` sends the remaining expression budget;
  the shared service may only shorten its own queue/prepare/inference cap.
- The client does not adopt or restart an external service, and does not depend
  on explicit request-cancellation endpoints. A cancelled aiohttp response closes
  the transport normally.

The agreed shared backend keeps one active worker and eight waiting slots, a
30-second queue deadline and a 300-second server job cap. Deployment owns lowering
the ignored Python timeout from 1200 to 300 seconds. That active configuration,
backend implementation and all running services were outside this client's edits.
Each expression now runs in a managed child task owned by its service instance.
Stop marks that instance closed, cancels and awaits only its own active/queued
children, and then reclaims its idle timer and owned process. It neither cancels
external caller/chat tasks nor another instance. A stop-induced child cancellation
returns failure to an uncancelled caller; caller-initiated cancellation propagates
after its child response is closed. Startup failure only cleans up the process it
started, avoiding recursive stop while expression children are being drained.

The previously loaded service class did not track its in-flight expressions and
could not gain this ownership retroactively from a disk edit. The handoff therefore
required the old process to exit rather than treating plugin hot reload as proof
that an old 20-minute operation was cancelled. The original process subsequently
exited; an externally started replacement was verified and reused, then the native
plugin reload confirmed the final configuration. The completed deployment and real
acceptance are recorded in [the shared-service report](2026-11-10-shared-breeze-service.md).

## Verification and rollback

The focused shared-client tests passed before the two complete TTS test files
were run. The complete isolated run passed 72 tests, including eleven added cases
covering permanent busy, queue rejection, remaining-budget headers, first-segment
failure, cross-segment deadline, local lane timeout/recovery, caller cancellation
and truncated PCM, plus stopped-service rejection, active/queued child cleanup,
concurrent stop, caller survival and isolation between service instances. Tests used temporary reference fixtures and mocked HTTP; they
did not synthesize speech, invoke a model, send platform messages, or control
services.

Pre-edit engineering files were copied intact to
`/tmp/breeze-python-shared-client-ujzxhcx6`. Rollback is limited to the exact
reviewed code/documentation hunks; the snapshot does not include private active
configuration or subject files. Deployment and real shared-service acceptance
remain owned by the root operator task.

Ruff F/E9 passed for changed Python files. Full Ruff has 29 existing diagnostics; comparison against the pre-edit files found 0 added diagnostics. The only pytest warning was the dependency deprecation of websockets.legacy. Targeted diff whitespace checking passed.

## Original operator handoff (pre-deployment snapshot)

At inspection the app was PID 2302442, parent 2302441, uid 1000 (ayerelysia),
cwd /home/ayerelysia/Elysia/Elysium, argv [.venv/bin/python, main.py], with fd 0/1/2
all attached to /dev/pts/0. The parent was the dedicated tmux server started with
socket name elysium, session elysium, pane %0. The systemd unit was inactive with
MainPID=0 and Restart=no; starting that unit alongside tmux would create a second
instance.

The runtime console has a native single-plugin reload command and cleans its
module namespace. This handoff originally prepared a normal full restart because
old in-flight TTS calls were not service-owned. Its PID and restart instructions
are historical inspection metadata, not current authorization to signal a process.
Signals are handled by src/app/runtime/signal_handler.py; any later lifecycle work
must first recheck the current exact PID/owner. The one-shot run_elysium.sh entry
execs .venv/bin/python main.py and contains no restart loop. The final deployment
reused the externally restarted process and performed one native plugin reload,
as recorded in the shared-service report above.

The prepared handoff allowed capturing only that verified app's environment into
an operator-private 0700 directory and 0600 file, without printing values or
staging it. A one-shot tmux launch would restore that environment while discarding
only old TMUX/TMUX_PANE bookkeeping. This is historical preparation, not a request
to read an obsolete PID. The launcher and main entry retain their canonical
private env-file loading.

The isolated acceptance runner is prepared at
/tmp/elysia-python-breeze-shared-check.py. It imports only the new TTSService and
configuration schema, reads the active engineering configuration without writing
it, requires breeze_tts2 with auto_start=false, and uses a fixed operations-only
short sentence. Root can run it from the repository with
/home/ayerelysia/.local/bin/uv run --no-sync --group dev python
/tmp/elysia-python-breeze-shared-check.py.
It has a bounded lifetime, writes audio/metadata under /tmp with 0600 permissions,
and never boots a runtime, agent or model conversation, publishes audio or sends a
platform message. At this handoff stage the runner was syntax-checked only. Root later executed it
in the successful three-client concurrent acceptance; real HTTP disconnect
cancellation and a subsequent complete WAV also passed. Those one-time results
are recorded in the shared-service report above and do not guarantee future uptime.
