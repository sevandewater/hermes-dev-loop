# DevLoop checks and Subscription Proxy diagnostic

The human administration CLI has standard-library regression tests:

```bash
python3 -m unittest discover -s tests -p test_devloop.py -v
python3 -m unittest discover -s tests -p test_project_reference.py -v
python3 tests/check_gateway_logout.py
```

The second command uses the built gateway image in a disposable container with
no external network or host state mounts. It verifies actual upstream logout,
orphaned shared-store cleanup, launcher waiting behavior and isolated API probes
using dummy credentials and loopback HTTP fixtures. It does not perform OAuth
or modify the running gateway. See [CLI validation](../docs/devloop-validation.md).

Project-reference tests use real temporary Git repositories to check committed
exports, private-file exclusion, executable modes, refresh behavior, and rejection
of tracked sensitive paths or links. The container suite additionally verifies
actual read access and read-only mount enforcement at `/reference`.

## Foundation boundary suite

For the implemented foundation's boundary suite, run `python3 tests/security.py`.
See the [main README](../README.md#security-validation) for fixtures, persistence
and authenticated-check options. The diagnostic below records the separate
upstream limitation that motivated the temporary gateway egress exception.

This diagnostic reproduces a gateway integration limitation. It does **not**
validate Docker network isolation or any of the requested foundation boundaries.
Exit status zero means the limitation was reproduced, not that the infrastructure
is secure.

The probe extracts the unmodified `_open_upstream` function and its header/error
helpers from the pinned upstream source using Python's AST. It checks the Git
blob hash before executing the extracted code. Two loopback HTTP listeners
represent the upstream and a proxy. With proxy environment variables set and
`NO_PROXY` empty, the upstream function reaches the direct listener. A separate
aiohttp session with `trust_env=True` reaches the proxy listener as a positive
control. It uses a dummy bearer and makes no Nous requests. It does not start the
full proxy server, perform OAuth, test HTTPS CONNECT, or apply the proposed patch.

## Reproduce

Requires Docker and an internet connection to obtain the diagnostic image and
Python dependencies. The upstream checkout and tests are mounted read-only. The
diagnostic container uses Docker's ordinary network for dependency installation;
this is deliberately not presented as the proposed Hermes deployment.

```bash
git clone https://github.com/NousResearch/hermes-agent.git /tmp/hermes-upstream-review
git -C /tmp/hermes-upstream-review checkout e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39
docker run --rm \
  --user 65532:65532 --cap-drop ALL \
  --security-opt no-new-privileges:true --read-only \
  --memory 256m --pids-limit 64 \
  --tmpfs /tmp:rw,nosuid,nodev,size=134217728 \
  --mount "type=bind,src=$PWD/tests,dst=/review,readonly" \
  --mount type=bind,src=/tmp/hermes-upstream-review,dst=/upstream,readonly \
  python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 sh -c \
  'python -m pip install --no-cache-dir --disable-pip-version-check --target /tmp/deps aiohttp==3.14.3 && PYTHONPATH=/tmp/deps python /review/probe_subscription_proxy.py /upstream'
```

The aiohttp version matches the reviewed upstream lockfile. The diagnostic image
digest above records the image resolved during the executed run. This disposable
dependency setup is not a production image definition or a fully locked build;
transitive Python dependencies are not pinned by this diagnostic.

## Result

Executed on 2026-10-20 using Docker Engine 29.8.1, the image digest above, and
aiohttp 3.14.3. The process exited with status 0 and printed:

```json
{"aiohttp": "3.14.3", "positive_control_proxy_hits": 1, "result": "LIMITATION_REPRODUCED", "security_boundaries_verified": false, "upstream_transport_hits": {"direct": 1, "proxy": 0}}
```

Observed: the actual upstream transport contacted the direct loopback listener,
while the positive control contacted the proxy listener. Inference transport
therefore ignores these environment settings at the reviewed source pin. This
was an HTTP loopback diagnostic, not a deployed gateway or HTTPS CONNECT test.
The diagnostic itself did not execute foundation security-boundary or persistence
tests. Subsequent [foundation validation](../docs/security-validation.md) and
[CLI validation](../docs/devloop-validation.md) record those separate runs.
