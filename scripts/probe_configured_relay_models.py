#!/usr/bin/env python3
"""Bounded synthetic tool roundtrips against the already configured local relay.

Never imports subject/runtime code, writes config/data, executes returned tools,
or prints credentials, response bodies, headers, URL queries or exceptions.
Only the four existing expression candidates below and the existing NexusAI
credential are used. Each HTTP phase has a 45-second total deadline; at most
two candidates run concurrently. No retries, redirects or credential rotation.
"""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
import os
import re
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

NAMES = ("gpt-5.6-terra", "MiMo-V2.5", "gemini-3.7-flash", "gpt-6-astra")
TOOL_NAME = "diagnostic-noop"
BYTE_LIMIT = 131072
SAFE_MODEL = re.compile(r"[A-Za-z0-9._:/+ -]{1,100}\Z")


def emit(value):
    print(json.dumps(value, ensure_ascii=True, sort_keys=True), flush=True)


def utc_now():
    return datetime.now(UTC).isoformat()


def actual_model(value):
    return value if isinstance(value, str) and SAFE_MODEL.fullmatch(value) else None


async def phase(client, url, headers, body):
    started = time.monotonic()
    result = {"started_utc": utc_now(), "http_status": None}
    message = {"role": "assistant", "content": "", "tool_calls": []}
    tool_parts = {}
    models = set()
    try:
        async with asyncio.timeout(45):
            async with client.stream("POST", url, headers=headers, json=body) as response:
                result["http_status"] = response.status_code
                if response.status_code != 200:
                    result["classification"] = {
                        401: "authentication_rejected", 402: "quota_rejected",
                        403: "access_rejected", 404: "route_missing",
                        429: "rate_or_quota_limited",
                    }.get(response.status_code, "upstream_http_error")
                    return result, None
                if body["stream"]:
                    total = 0
                    async for line in response.aiter_lines():
                        total += len(line.encode("utf-8"))
                        if total > BYTE_LIMIT:
                            raise ValueError("response_size_limit")
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        packet = json.loads(data)
                        if packet.get("error"):
                            result["classification"] = "provider_error_payload"
                            return result, None
                        safe_model = actual_model(packet.get("model"))
                        if safe_model:
                            models.add(safe_model)
                        choices = packet.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        for key in ("content", "reasoning_content"):
                            if isinstance(delta.get(key), str):
                                message[key] = message.get(key, "") + delta[key]
                        for call in delta.get("tool_calls") or []:
                            item = tool_parts.setdefault(call.get("index", 0), {
                                "id": "", "type": "function",
                                "function": {"name": "", "arguments": ""},
                            })
                            if call.get("id"):
                                item["id"] = call["id"]
                            function = call.get("function") or {}
                            for key in ("name", "arguments"):
                                if isinstance(function.get(key), str):
                                    item["function"][key] += function[key]
                        finish = choice.get("finish_reason")
                        if finish in {"stop", "tool_calls", "length", "content_filter"}:
                            result["finish_reason"] = finish
                    message["tool_calls"] = list(tool_parts.values())
                else:
                    chunks = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > BYTE_LIMIT:
                            raise ValueError("response_size_limit")
                        chunks.append(chunk)
                    packet = json.loads(b"".join(chunks))
                    if packet.get("error"):
                        result["classification"] = "provider_error_payload"
                        return result, None
                    safe_model = actual_model(packet.get("model"))
                    if safe_model:
                        models.add(safe_model)
                    choice = (packet.get("choices") or [{}])[0]
                    original = choice.get("message") or {}
                    message.update({key: original[key] for key in (
                        "role", "content", "reasoning_content", "tool_calls",
                    ) if key in original})
                    finish = choice.get("finish_reason")
                    if finish in {"stop", "tool_calls", "length", "content_filter"}:
                        result["finish_reason"] = finish
                result["classification"] = "http_ok_parsed"
                return result, message
    except (TimeoutError, httpx.TimeoutException):
        result["classification"] = "deadline_exceeded"
    except httpx.RequestError:
        result["classification"] = "transport_error"
    except (ValueError, TypeError, KeyError):
        result["classification"] = "invalid_or_oversized_response"
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["actual_models"] = sorted(models)
    return result, None


async def probe(name, model, provider, semaphore):
    async with semaphore:
        started = time.monotonic()
        nonce = "synthetic_" + uuid4().hex
        report = {"model_name": name, "configured_id": model["id"],
                  "provider": "NexusAI", "started_utc": utc_now(),
                  "tool_roundtrip_ok": False, "synthetic_only": True}
        schema = {"type": "function", "function": {
            "name": TOOL_NAME,
            "description": "Synthetic diagnostic only; records no state and performs no action.",
            "parameters": {"type": "object", "properties": {
                "marker": {"type": "string"}, "count": {"type": "integer"},
            }, "required": ["marker", "count"], "additionalProperties": False},
        }}
        messages = [
            {"role": "system", "content": "You are a synthetic infrastructure diagnostic, not Elysia or any subject. No subject memory, persona or history is supplied. Follow the diagnostic user request."},
            {"role": "user", "content": f"Call {TOOL_NAME} exactly once with marker {nonce} and count 7. This is a simulated no-op tool capability test."},
        ]
        body = {"model": model["id"], "messages": messages, "tools": [schema],
                "tool_choice": "auto", "max_tokens": 1024,
                "stream": bool(model.get("stream", False))}
        body.update({key: value for key, value in model.get("extra", {}).items()
                     if key in {"reasoning_effort", "enable_thinking", "thinking"}})
        headers = {"Authorization": "Bearer " + provider["api_key"],
                   "Content-Type": "application/json"}
        emit({"event": "probe_started", "model_name": name, "started_utc": report["started_utc"]})
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                    timeout=httpx.Timeout(40, connect=5)) as client:
            first, assistant = await phase(client, provider["base_url"].rstrip("/") + "/chat/completions", headers, body)
            report["tool_phase"] = first
            calls = (assistant or {}).get("tool_calls") or []
            first["native_tool_call_count"] = len(calls)
            first["text_present"] = bool((assistant or {}).get("content"))
            matched = False
            if len(calls) == 1:
                call = calls[0]
                try:
                    arguments = json.loads(call.get("function", {}).get("arguments", ""))
                    matched = (call.get("type") == "function" and bool(call.get("id"))
                               and call.get("function", {}).get("name") == TOOL_NAME
                               and arguments == {"marker": nonce, "count": 7})
                except (ValueError, TypeError):
                    pass
            first["schema_nonce_call_id_match"] = matched
            if matched:
                tool_result = {"role": "tool", "tool_call_id": calls[0]["id"],
                               "content": json.dumps({"marker": nonce, "count": 7, "ok": True})}
                body["messages"] = messages + [assistant, tool_result, {
                    "role": "user", "content": "The simulated tool returned successfully. Reply with its exact marker in a short final text, and do not call another tool.",
                }]
                second, final = await phase(client, provider["base_url"].rstrip("/") + "/chat/completions", headers, body)
                report["text_phase"] = second
                text = (final or {}).get("content") or ""
                second["text_present"] = isinstance(text, str) and bool(text.strip())
                second["tool_result_nonce_returned"] = isinstance(text, str) and nonce in text
                second["native_tool_call_count"] = len((final or {}).get("tool_calls") or [])
                report["tool_roundtrip_ok"] = bool(second["http_status"] == 200 and second["tool_result_nonce_returned"] and second["native_tool_call_count"] == 0)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["finished_utc"] = utc_now()
        emit(report)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", help="one current expression candidate; at most four")
    args = parser.parse_args()
    names = tuple(args.model or NAMES)
    if not 1 <= len(names) <= 4 or len(set(names)) != len(names):
        raise ValueError("invalid_bounded_candidate_set")
    root = Path(__file__).resolve().parents[1]
    raw = (root / "config/models.toml").read_bytes()
    config = tomllib.loads(raw.decode("utf-8"))
    provider = dict(config["providers"]["NexusAI"])
    for key in ("base_url", "api_key"):
        value = provider[key]
        if not isinstance(value, str):
            raise TypeError("unsupported_credential_or_endpoint_shape")
        provider[key] = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda match: os.environ.get(match[1], match[0]), value)
        if "${" in provider[key]:
            raise ValueError("unresolved_environment")
    endpoint = urlsplit(provider["base_url"])
    if (endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1"}
            or endpoint.port != 3000 or endpoint.path.rstrip("/") != "/v1"
            or endpoint.query or endpoint.username or endpoint.password):
        raise ValueError("configured_relay_identity_mismatch")
    if not provider["api_key"] or provider.get("client_type", "openai") != "openai":
        raise ValueError("configured_relay_auth_or_protocol_missing")
    candidates = {name: config["models"][name] for name in names}
    expression = config["tasks"]["expression"]["models"]
    if any(model["provider"] != "NexusAI" or name not in expression for name, model in candidates.items()):
        raise ValueError("candidate_not_in_current_expression_route")
    emit({"event": "probe_scope", "started_utc": utc_now(), "endpoint_host": endpoint.hostname,
          "endpoint_port": 3000, "endpoint_path": "/v1", "provider": "NexusAI",
          "config_sha256": hashlib.sha256(raw).hexdigest(), "candidate_count": len(candidates),
          "phase_deadline_seconds": 45, "max_concurrency": 2, "max_tokens_per_phase": 1024})
    semaphore = asyncio.Semaphore(2)
    await asyncio.gather(*(probe(name, model, provider, semaphore) for name, model in candidates.items()))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 -- never print secret-bearing exceptions
        emit({"event": "probe_aborted", "exception_type": type(error).__name__})
        raise SystemExit(1) from None
