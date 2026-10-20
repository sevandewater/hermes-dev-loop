FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates build-essential pkg-config libffi-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/hermes
# Human-maintained source pin. No provider credentials or runtime state in build.
RUN git init . && git remote add origin https://github.com/NousResearch/hermes-agent.git \
    && git fetch --depth=1 origin e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39 \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39 \
    && rm -rf .git
RUN python -m pip install --no-cache-dir uv==0.12.3 \
    && uv sync --frozen --no-default-groups --no-install-project --extra mcp --extra pty --extra sms

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime
LABEL org.opencontainers.image.source="https://github.com/sevandewater/hermes-dev-loop" \
      devloop.hermes.revision="e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39"
RUN groupadd --gid 1000 hermes && useradd --uid 1000 --gid 1000 --create-home hermes
COPY --from=build /opt/hermes /opt/hermes
COPY hermes /usr/local/bin/hermes
ENV PYTHONPATH=/opt/hermes \
    PATH=/home/hermes/tools:/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
USER 1000:1000
WORKDIR /home/hermes

FROM runtime AS gateway
COPY gateway-entrypoint /usr/local/bin/gateway-entrypoint
ENTRYPOINT ["/usr/local/bin/gateway-entrypoint"]

FROM runtime AS development
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential pkg-config nodejs npm sqlite3 \
    iproute2 dnsutils netcat-openbsd ripgrep \
    && rm -rf /var/lib/apt/lists/*
USER 1000:1000
WORKDIR /workspace
CMD ["sleep", "infinity"]
