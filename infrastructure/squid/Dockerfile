FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
RUN apt-get update && apt-get install -y --no-install-recommends squid \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 proxy-runtime \
    && useradd --uid 1000 --gid 1000 --no-create-home proxy-runtime
USER 1000:1000
CMD ["squid", "-N", "-f", "/etc/squid/devloop/squid.conf"]
