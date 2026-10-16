# 2026-10-08 Claude expression trial

## Change

Expression route is now:

1. claude-sonnet-5-5
2. claude-opus-4-6
3. claude-sonnet-5
4. gpt-6.1-sol
5. gpt-6-sol
6. gpt-6-luna

The A6 upstream model directory confirms the exact hyphenated Sonnet 5.5 id claude-sonnet-5-5. Both Claude expression candidates are non-streaming. Sonnet thinking is disabled for expression tool rounds; other tasks retain their existing thinking settings.

## Channel repair

The local New API database was backed up before adding claude-sonnet-5-5 and claude-opus-4-6 to A6 channel 30. The model catalog and abilities rows were added, then the elysium-new-api container was reloaded.

## Verification

- A6 upstream directory: claude-sonnet-5-5 and claude-opus-4-6 are advertised.
- New API catalog: both ids are visible after reload.
- claude-opus-4-6 through A6: HTTP 200, about 3.4 seconds for plain text.
- claude-sonnet-5-5 through A6: still HTTP 503 smart_route_all_candidates_failed in both stream and non-stream probes. This is an A6 personal-pool failure, not an Elysium route or stream setting failure.
- claude-sonnet-5 and claude-opus-4-6 tool calls work through the previously healthy relay path.
- claude-fable-5: HTTP 200 but about 46.5 seconds, so it is not in the expression route.
- gpt-6.1-sol: existing synthetic tool roundtrip passed and remains a fallback.

All probes were synthetic no-op tests; they did not execute real Aili tools or write subject memory.

## Activation boundary

The configuration passed deployment_schema_check.py and ModelsConfig.get_task(expression) validation. Elysium was not restarted by this operation; the user must manually start it before the new expression route is loaded.
