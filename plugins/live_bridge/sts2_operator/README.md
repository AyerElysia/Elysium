# STS2 Operator Bridge

This bridge keeps Slay the Spire 2 execution tools out of Elysia's tool list.

Flow:

1. `sts2AITeammate` calls Neo-MoFox through the OpenAI-compatible `/v1/chat/completions` route.
2. `live_bridge` recognizes the STS2 decision contract and hands it to the operator.
3. The operator compresses the request into one game-decision message for Elysia.
4. Elysia replies with strict JSON choosing one legal action id.
5. The operator validates the action id and returns the STS2 mod contract.

Recommended STS2 teammate environment:

```bash
AITEAMMATE_BACKEND=openai
AITEAMMATE_API_BASE_URL=http://127.0.0.1:18000/v1
AITEAMMATE_API_KEY=local-elysia
AITEAMMATE_API_MODEL=elysia-sts2
AITEAMMATE_DECISION_TIMEOUT_MS=10000
AITEAMMATE_API_MAX_TOKENS=240
```

Restart Steam/the game after changing Windows environment variables. Adjust the
port if Neo-MoFox is running on a different HTTP port.
