# Reuse the pinned Hermes dependency environment, including MCP SDK 2.3.0.
FROM base
USER root
RUN python3 - <<'PY'
import hashlib, io, pathlib, platform, tarfile, urllib.request
assert platform.machine() == 'x86_64', 'This pinned broker artifact requires Linux amd64.'
url = 'https://github.com/github/github-mcp-server/releases/download/v2.0.2/github-mcp-server_Linux_x86_64.tar.gz'
data = urllib.request.urlopen(url, timeout=90).read()
assert hashlib.sha256(data).hexdigest() == '815e27d931c796af0c97f24fd211e14f8a4607d4528377719a5ad2352086b0b0'
with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
    member = next(m for m in archive if pathlib.PurePosixPath(m.name).name == 'github-mcp-server' and m.isfile())
    pathlib.Path('/usr/local/bin/github-mcp-server').write_bytes(archive.extractfile(member).read())
pathlib.Path('/usr/local/bin/github-mcp-server').chmod(0o755)
PY
COPY server.py policy.py issue_management.py actions.py job_logs.py workflow_policy.py workflow_proposals.py /opt/github-broker/
RUN mkdir /broker-state && chown 1000:1000 /broker-state && chmod 700 /broker-state
USER 1000:1000
WORKDIR /tmp
ENTRYPOINT ["/opt/hermes/.venv/bin/python", "/opt/github-broker/server.py"]
