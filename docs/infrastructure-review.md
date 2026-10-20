# Infrastructure decision: temporary direct gateway egress

Human decision on 2026-10-20: proceed with the infrastructure foundation using a
workaround, reducing isolation where necessary, and defer outbound Subscription
Proxy support until upstream provides it. This supersedes the earlier decision
to hold the entire implementation.

The exception is limited to **the trusted LLM gateway's outbound network**.
Hermes still has no direct internet, external DNS, host-management, or private-LAN
route. Infrastructure and credentials remain outside its mounts. No provider
credentials, privileged containers, Docker socket, or unattended agent loop are
introduced into Hermes. Independent gateway destination enforcement is an
accepted exception, not a passing security control.

## Upstream investigation

Hermes source is pinned to
[`e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39`](https://github.com/NousResearch/hermes-agent/tree/e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39).
The reviewed `hermes_cli/proxy/server.py` Git blob is
`49575154482db3d336b00501dc74dfb5b8711a89`.

| Question | Finding and evidence |
| --- | --- |
| Standalone operation | [`proxy/cli.py`](https://github.com/NousResearch/hermes-agent/blob/e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39/hermes_cli/proxy/cli.py) starts an HTTP proxy without the agent loop. |
| Nous authentication | [`adapters/nous_portal.py`](https://github.com/NousResearch/hermes-agent/blob/e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39/hermes_cli/proxy/adapters/nous_portal.py) uses the existing Hermes Nous credential resolver and refresh/retry behavior. Account compatibility is unverified until human login. |
| OpenAI-compatible interface | [`proxy/server.py`](https://github.com/NousResearch/hermes-agent/blob/e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39/hermes_cli/proxy/server.py) serves chat/completions, completions, embeddings, and model-list routes under `/v1`, with streaming support. |
| Credential persistence | [`auth.py`](https://github.com/NousResearch/hermes-agent/blob/e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39/hermes_cli/auth.py) and [`auth_nous.py`](https://github.com/NousResearch/hermes-agent/blob/e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39/hermes_cli/auth_nous.py) manage the private auth store and shared Nous store with locking and atomic writes. Both paths are explicitly pinned inside the gateway mount. |
| Outbound proxy routing | The inference session does not read proxy environment variables. The [executed loopback diagnostic](../tests/README.md) reproduced that behavior with the actual upstream transport. OAuth uses separate HTTPX code. |
| Client authentication and usage control | The proxy replaces an arbitrary client bearer with its own provider credential. It has no independently enforced per-agent request, token, or spending ceiling. |

[PR #110083](https://github.com/NousResearch/hermes-agent/pull/110083), opened
September 13, 2026, was still open and unmerged when checked on October 20. Its
head was `87349a46890d8d0e2ebb0d21a1eb0b12d36490a9`; the API reported no conversation
comments and no update since its creation. That is an observed status, not a
prediction about maintainer attention or a merge date.

The PR proposes explicit proxy selection using Hermes' shared resolver and
transport helpers. It also identifies a `.netrc` authentication conflict with
simply setting `trust_env=True`. The earlier one-line recommendation in this
repository did not account for that interaction and is withdrawn. The local
loopback diagnostic is evidence of the routing limitation, not qualification of
that patch or the upstream PR's other behavior.

## Current architecture

Compose contains three primary services and an optional GitHub broker. Only Squid and the gateway
join their separate external NAT networks. Hermes joins two isolated internal
bridges without host addresses or default routes. Both network-facing services
have kernel IP forwarding disabled and no network-administration capabilities.
Their listeners bind to the intended internal addresses, not their egress
interfaces. No ports are published.

Hermes' normal HTTPS traffic passes through Squid's exact-name allowlist. Provider
hosts are absent, preventing direct provider use through the development proxy.
The gateway's stock Nous adapter selects the upstream inference endpoint and
attaches the provider bearer, with redirects disabled by the proxy transport.
That application behavior is not independent network enforcement. A compromised
gateway may contact other public destinations or reachable host/private services.
The human explicitly allowed this temporary relaxation so infrastructure work
could continue; all other boundaries remain in scope for runtime verification.

See the [README](../README.md) for the diagram, mount ownership, setup, OAuth,
lifecycle, allowlist, log-review and test procedures.

## Deferred work

### Subsequent project-reference change

On 2026-10-20 the human requested read-only access to this project's source so
Hermes can propose improvements through PRs, with human-only merge and local
activation. This explicitly allows inspection of tracked infrastructure source.
The live host checkout, credentials and administration files remain unmounted.
Only a committed-source export at `/reference` is readable, and the Docker mount
denies writes. A separate workspace Git checkout supports edits. A separate GitHub App and isolated MCP broker provide proposal operations; no
administrator credentials are made available to Hermes. Proposal writes stay
blocked until GitHub enforces human-only updates to `main`. See [the workflow](hermes-project-workflow.md).

### Gateway and account work

This slice will not patch or adopt an unmerged implementation of outbound
Subscription Proxy support. In a later change, inspect the merged upstream
implementation, pin it, qualify proxy routing including refresh/retry and failure
behavior, remove the gateway's external network, introduce an independently
enforced provider-only egress policy, and rerun the full suite. Merely updating
Hermes or exporting `HTTPS_PROXY` does not close the exception.

Real OAuth login, token refresh/revocation, provider entitlements, streaming
inference, and credential redaction need deliberate account validation. The
current fixture proves forwarding with dummy credentials; it does not establish
any of these account-level outcomes. Autonomous execution and independently
enforced inference budgets remain outside this milestone.

### GitHub broker integration

The human requested the App/broker integration and its live test. The optional
`github-broker` service isolates the imported host App key from Hermes. It reuses
the pinned Hermes MCP SDK and the checksum-pinned official GitHub MCP server
v2.0.2, with native App authentication. A narrow policy adapter fixes the project,
proposal branch namespace and draft PR base; no token issuer, merge operation
or generic host runner was added. It has only the internal development network,
no default route, and GitHub-only CONNECT egress through Squid. Client URLs now
use service names. The gateway uses an inference-only `llm-gateway-api` DNS alias
for its stock bind option and clients, preserving upstream Host-header validation
and avoiding self-resolution to the egress interface. The existing gateway egress exception is unchanged.

An active GitHub Restrict updates rule with human-admin-only bypass is required
before every proposal write. This extends the isolation boundary to GitHub;
review approval alone would leave the App able to merge with Contents write.
See [design and setup](github-app-broker.md) and [validation](github-broker-validation.md).
