"""Amazon Bedrock AgentCore entrypoint for the SiteTrace FastAPI service.

The application itself lives in :mod:`app.main`.  This module performs the
small amount of bootstrapping that must happen *before* that module is imported:

* hydrate provider credentials from Secrets Manager when secret IDs are set;
* preserve explicitly supplied process environment variables for local use;
* attach the AgentCore runtime session ID to OpenTelemetry baggage; and
* expose the same FastAPI ``app`` on port 8080.

No secret value is logged or returned by a health endpoint.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from collections.abc import Mapping
from typing import Any


logger = logging.getLogger("sitetrace.agentcore")

_SECRET_ENVIRONMENT_MAP: dict[str, tuple[str, ...]] = {
    "SITETRACE_OPENAI_SECRET_ID": ("OPENAI_API_KEY",),
    "SITETRACE_TWELVELABS_SECRET_ID": ("TWELVE_LABS_API_KEY",),
    "SITETRACE_NEO4J_SECRET_ID": (
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NEO4J_DATABASE",
    ),
}

_ALLOWED_RUNTIME_SECRET_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "TWELVE_LABS_API_KEY",
        "NEO4J_URI",
        "NEO4J_USERNAME",
        "NEO4J_PASSWORD",
        "NEO4J_DATABASE",
    }
)

_COMMON_SECRET_ALIASES: dict[str, tuple[str, ...]] = {
    "OPENAI_API_KEY": ("openai_api_key", "api_key", "token", "value"),
    "TWELVE_LABS_API_KEY": (
        "twelve_labs_api_key",
        "twelvelabs_api_key",
        "api_key",
        "token",
        "value",
    ),
    "NEO4J_URI": ("neo4j_uri", "uri"),
    "NEO4J_USERNAME": ("neo4j_username", "username", "user"),
    "NEO4J_PASSWORD": ("neo4j_password", "password"),
    "NEO4J_DATABASE": ("neo4j_database", "database"),
}


def _secret_text(response: Mapping[str, Any]) -> str:
    """Return a Secrets Manager response body without logging its value."""

    secret_string = response.get("SecretString")
    if isinstance(secret_string, str):
        return secret_string

    secret_binary = response.get("SecretBinary")
    if isinstance(secret_binary, bytes):
        return secret_binary.decode("utf-8")
    if isinstance(secret_binary, str):
        return base64.b64decode(secret_binary).decode("utf-8")
    raise RuntimeError("Secrets Manager returned no SecretString or SecretBinary")


def _parse_secret(secret_text: str) -> dict[str, str] | str:
    try:
        parsed = json.loads(secret_text)
    except json.JSONDecodeError:
        return secret_text
    if not isinstance(parsed, Mapping):
        return secret_text
    return {
        str(key): str(value)
        for key, value in parsed.items()
        if value is not None and not isinstance(value, (dict, list))
    }


def _value_for_key(
    payload: dict[str, str] | str,
    environment_key: str,
    *,
    allow_scalar: bool,
) -> str | None:
    if isinstance(payload, str):
        return payload if allow_scalar and payload else None

    candidates = (environment_key, *_COMMON_SECRET_ALIASES[environment_key])
    lowered = {key.casefold(): value for key, value in payload.items()}
    for candidate in candidates:
        value = lowered.get(candidate.casefold())
        if value:
            return value
    return None


def load_runtime_secrets() -> tuple[str, ...]:
    """Hydrate missing provider environment variables from Secrets Manager.

    Direct process environment values always win.  This makes local ``.env``
    development possible while ensuring deployed containers receive only
    non-secret secret identifiers in their AgentCore configuration.
    """

    combined_secret_id = os.getenv("SITETRACE_RUNTIME_SECRET_ID", "").strip()
    configured_secret_ids = {
        selector: os.getenv(selector, "").strip()
        for selector in _SECRET_ENVIRONMENT_MAP
    }
    if not combined_secret_id and not any(configured_secret_ids.values()):
        return ()

    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - deployment dependency guard.
        raise RuntimeError(
            "boto3 is required when SiteTrace secret IDs are configured"
        ) from exc

    region = (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "us-east-1"
    )
    client = boto3.client("secretsmanager", region_name=region)
    loaded: set[str] = set()

    if combined_secret_id:
        response = client.get_secret_value(SecretId=combined_secret_id)
        payload = _parse_secret(_secret_text(response))
        if not isinstance(payload, dict):
            raise RuntimeError(
                "SITETRACE_RUNTIME_SECRET_ID must reference a JSON object"
            )
        for key in _ALLOWED_RUNTIME_SECRET_KEYS:
            if os.getenv(key):
                continue
            value = _value_for_key(payload, key, allow_scalar=False)
            if value:
                os.environ[key] = value
                loaded.add(key)

    for selector, environment_keys in _SECRET_ENVIRONMENT_MAP.items():
        secret_id = configured_secret_ids[selector]
        missing_keys = [key for key in environment_keys if not os.getenv(key)]
        if not secret_id or not missing_keys:
            continue
        response = client.get_secret_value(SecretId=secret_id)
        payload = _parse_secret(_secret_text(response))
        for environment_key in missing_keys:
            value = _value_for_key(
                payload,
                environment_key,
                allow_scalar=len(environment_keys) == 1,
            )
            if value:
                os.environ[environment_key] = value
                loaded.add(environment_key)

    logger.info(
        "Hydrated %d SiteTrace runtime setting(s) from Secrets Manager",
        len(loaded),
    )
    return tuple(sorted(loaded))


# This must run before app.main imports its environment-backed Settings object
# and constructs the sponsor clients.
load_runtime_secrets()

from app.main import app  # noqa: E402  (intentional post-secret import)


@app.middleware("http")
async def bind_agentcore_session(request: Any, call_next: Any) -> Any:
    """Propagate the AgentCore session header into request state and traces."""

    session_id = request.headers.get(
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
    )
    request.state.agentcore_session_id = session_id
    if not session_id:
        return await call_next(request)

    token = None
    otel_context = None
    try:
        from opentelemetry import baggage, context

        otel_context = context
        token = context.attach(baggage.set_baggage("session.id", session_id))
    except Exception:  # pragma: no cover - telemetry is intentionally optional.
        token = None

    try:
        return await call_next(request)
    finally:
        if token is not None and otel_context is not None:
            otel_context.detach(token)


if __name__ == "__main__":  # pragma: no cover - local container smoke test.
    import uvicorn

    uvicorn.run(
        "agentcore_entrypoint:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        reload=False,
    )


__all__ = ["app", "load_runtime_secrets"]
