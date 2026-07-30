# syntax=docker/dockerfile:1.7
# Amazon Bedrock AgentCore Runtime requires linux/arm64.
FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PORT=8080

WORKDIR /app

# The backend is a normal Python package. Keeping dependency installation before
# source copy would improve layer caching, but COPY-all also works when a
# hackathon checkout has no committed uv.lock yet.
COPY . /app
RUN uv pip install --system --no-cache .

EXPOSE 8080

# AgentCore custom runtimes require POST /invocations, GET /ping, and port 8080.
# Use AWS OpenTelemetry auto-instrumentation when installed, while retaining a
# dependency-light local fallback for the demo.
CMD ["sh", "-c", "if command -v opentelemetry-instrument >/dev/null 2>&1; then exec opentelemetry-instrument uvicorn agentcore_entrypoint:app --host 0.0.0.0 --port 8080; else exec uvicorn agentcore_entrypoint:app --host 0.0.0.0 --port 8080; fi"]
