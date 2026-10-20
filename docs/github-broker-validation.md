# GitHub broker integration validation

Executed 2026-10-20 on native Ubuntu 22.04.5 / kernel 5.15.0-105-generic,
Docker Engine 29.8.1 and Compose 5.5.1. The final boundary report completed at
22:57:07 UTC (18:57 America/Toronto). This extends the historical foundation and
CLI runs; it does not replace their recorded scope.

## Executed results

| Check | Result |
| --- | --- |
| Local unit suite, including credential storage, reference export, CLI lifecycle and broker policy | 62 tests PASS |
| App registration, installation and selected repository authenticated using corrected App ID 5402740 | PASS |
| Official GitHub MCP v2.0.2 starts with native App authentication through Squid | PASS |
| Hermes MCP client connects to `github-devloop` by service name and discovers exactly six tools | PASS |
| Real `LICENSE` read and PR #1 metadata through Hermes → broker → official GitHub MCP → GitHub | PASS |
| Exact tool surface; unregistered merge call rejected locally, without any GitHub merge request | PASS |
| Main branch writes, workflow writes and broker-key path reads denied | PASS |
| GitHub effective human-only update protection | NOT READY: active ruleset lacks Restrict updates and human-admin bypass |
| Explicit draft-PR canary | SKIP: protection gate blocked creation; no proposal created |
| `./devloop test --persistence`, with all four services running | 28 groups PASS, one account group SKIP, one accepted gateway exception |
| Whitespace and source syntax checks | PASS |

The five read/denial groups in `./devloop github --check` passed after the final
broker rebuild and container persistence run. The actual `hermes mcp test
github-devloop` command connected and listed all six tools. No inference request
was needed. The explicit proposal test repeated the checks and reported
`writes_enabled: false`, `proposal_created: false`; its underlying script exits
2 for that blocked outcome (the host CLI explains the missing rule and returns nonzero).

## Boundary and persistence coverage

The extended container suite inspected the four running containers, not only
Compose configuration. It checked read-only roots, UID, capabilities, seccomp,
no-new-privileges, CPU/memory/PID limits, unpublished ports, actual bind mounts,
network peers and unnumbered internal host bridges. Broker-only credential binds
were read-only; Hermes could not read `/broker-secrets` or the host key.

The broker had no IPv4 default route, configured IPv6 address or usable IPv6
route, and no forwarding/raw-socket capability. Linux's unreachable IPv6 rejection
entries are explicitly distinguished from usable routes. Its proxy CONNECT to
GitHub succeeded while PyPI, Nous, arbitrary hosts and private destinations were
denied. Direct TCP and UDP/TCP DNS attempts were blocked against an external
canary that Squid had first reached as a positive control. The existing Hermes
network, DNS, host/private-network and proxy-destination checks passed too.
A subsequent targeted proxy-denial run also confirmed that Hermes cannot use
`api.github.com` through its own Squid policy; the broker's separate policy can.

The gateway API uses `llm-gateway-api`, a DNS alias scoped only to its inference
network. Both the Hermes client and trusted gateway self-probes use this name.
Binding the unmodified upstream server by that alias also satisfies its strict
Host-header policy; an ordinary dual-network service name resolves the wrong
self-interface, and an IP-bound upstream rejects foreign hostname headers. The
repeated boundary check confirms API access through the alias, rejection of a
foreign Host name, and no listener on the external interface. No upstream
transport or Host-header validation patch was introduced.

Hermes' workspace, home, memory, skills and executable tools, and the gateway's
private storage survived recreation. The read-only project reference remained
readable and rejected writes. The suite removed its own canaries and helper
containers. Generated evidence stays in ignored `runtime/security-results.json`.
The build/start and suite logs were captured under `/tmp/` without key or token
output.

The local tests use disposable RSA keys and temporary Git repositories. They
cover App-ID correction without key rotation, symlink/key replacement refusal,
private modes, source exclusions, the proposal branch namespace, protected file
paths, private-key content, commit limits and the human-admin-only ruleset policy.
CLI tests distinguish broker startup, read checks and explicit canary creation,
and include the enabled broker in lifecycle actions.

## Current GitHub protection and remaining acceptance

GitHub reports ruleset `25510073` (`no-direct-main`) **active**, targeting the
default branch, with PR/review, deletion, force-push, linear-history, signature
and status-check rules. It has **no `update` rule and an empty bypass list**.
Review approval alone does not reserve merging for the human. Proposal writes
therefore remain disabled, independently of successful App authentication.

Complete [the ruleset steps](github-app-broker.md#github-must-enforce-human-only-updates),
then rerun the explicit canary to validate branch creation, commit publication,
App attribution and draft PR creation. Close the canary afterward; no real merge
is part of the test. No GitHub rules were changed by this implementation, and no
App-created proposal branch/commit/PR is claimed as validated yet. Deliberate
installation-token expiry/refresh and real-credential error/redaction scenarios
also remain untested. Authentication and refresh are delegated to upstream.

The existing human-authenticated LLM gateway was checked locally for health;
this run did not repeat browser OAuth, request paid inference or validate
provider refresh/streaming. Its separately approved direct-egress exception and
lack of independent per-agent inference budgets remain unchanged.

## Final local image identities

- Hermes: `sha256:de23f9daa161d432f7a79b39fa23efb1f31bd524782691fa5ce0736d08b9d341`
- Squid: `sha256:d6cae5be515ddff05d6d0818e93fa2a5f3cdf8dabf68508030379a167480c31d`
- LLM gateway: `sha256:adb00f2ea1e009e6bbdd68db34ea3bb7baafa8a7935ad696aa264634f915785c`
- GitHub broker: `sha256:ac80494da5d0ef9a74089258e806960945ff3af17e73457d600ba0314bfef80a`

These are observed local build identities, not published image references. The
GitHub release archive is checksum-pinned; OS package repositories remain time
dependent through the shared foundation build.
