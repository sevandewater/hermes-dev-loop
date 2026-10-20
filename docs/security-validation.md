# Executed validation and security review

Run completed 2026-10-20 at 14:44 America/Toronto (18:44 UTC).

The subsequent [DevLoop CLI validation](devloop-validation.md) records the updated
gateway launcher, CLI checks and a repeated boundary/persistence run.

## Environment and result

Native Linux host: Ubuntu 22.04.5 LTS, kernel 5.15.0-105-generic. Docker Engine
29.8.1, Compose 5.5.1. Images contain Python 3.14.8; development tools include
Node 20.19.2, npm 9.2.0 and GCC 14.2.0. Squid is Debian's 6.13 package.
Hermes source pin: `e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39`.

Executed on the final policy:

```bash
docker compose config --quiet
docker compose build
docker compose exec -T squid squid -k parse -f /etc/squid/devloop/squid.conf
docker compose up -d --wait --wait-timeout 30 squid hermes
python3 tests/security.py --persistence
```

Compose validation, all three image builds, Squid parsing, and the final security
suite exited 0. Squid emits warnings about its disabled IPv6 loopback; IPv6 is
intentionally disabled and its actual absence is covered by the suite. Python
syntax checks, Git whitespace checks, and runtime/environment ignore checks also
passed. No real provider credentials were used during validation.

The suite recorded **24 passing check groups, one skipped account-validation
group, and one accepted security exception**. PASS includes executed runtime
behavior or inspection of actual running containers, not just Compose settings.
Some groups contain multiple attempts, so these counts are not assertion counts.

## Boundary results

| Check | Result |
| --- | --- |
| Container permissions, resources, ports and actual mount isolation | PASS |
| Actual internal isolated networks and unnumbered host bridges | PASS |
| No default/IPv6 routes, raw sockets, capabilities or route changes | PASS |
| Squid and gateway kernel forwarding disabled | PASS |
| Squid does not listen on its external interface | PASS |
| Infrastructure/config/credential files and Docker socket unavailable to Hermes | PASS |
| Workspace, memory, skills, tools, executable Python/Node/C and Hermes CLI | PASS |
| External TCP and UDP/TCP DNS canaries reachable from Squid (positive control) | PASS |
| Hermes direct TCP internet path, alternate ports and UDP/TCP DNS blocked | PASS |
| No blocked Hermes requests received by reachable external canaries | PASS |
| Approved public destination via Squid with end-to-end TLS | PASS |
| Public internet TCP target reachable from Squid (positive control) | PASS |
| Hermes direct public-IP internet connection blocked with proxy settings bypassed | PASS |
| Disallowed hosts, provider bypass, IP literals, private destinations and methods denied by Squid | PASS |
| Approved hostname resolving private denied by production dst ACL (with counterfactual control) | PASS |
| Embedded Docker DNS refuses external UDP/TCP queries; system DNS unavailable | PASS |
| Host/private-network HTTP canary reachable from Squid (positive control) | PASS |
| Hermes cannot reach a live host/private-network service | PASS |
| Gateway direct provider TLS reachability (approved temporary egress exception) | PASS |
| Designated internal gateway and real upstream forwarding with fake adapter | PASS |
| Gateway does not listen on its external interface | PASS |
| Hermes cannot use gateway's external interface as an alternate path | PASS |
| Nous OAuth login/refresh and authenticated provider inference | SKIP |
| Gateway independent provider destination restriction | ACCEPTED_EXCEPTION |
| Hermes persistent workspace/memory/skills/tools/home survive container recreation | PASS |
| Gateway private storage survives container recreation | PASS |

The external canary served TCP 53/80/443 and UDP 53 on Squid's egress network.
Squid reached the targets first. Hermes' attempted connections were denied and
the canary received no additional requests. A real public IPv4 endpoint was also
positively reached from Squid and denied from Hermes using raw sockets that
bypass proxy settings. Approved HTTPS through Squid performed real TLS validation
and fetched PyPI content. A reachable host HTTP fixture provided the positive
control for host/private-network denial.

Both UDP and TCP queries to Docker's embedded resolver returned SERVFAIL for an
external, resolvable name. Runtime route/capability checks and host bridge address
inspection additionally verified no default route, no IPv6 route, no bridge host
address, no effective capabilities, active seccomp, and denied route changes.

The private-name test ran temporary loopback-only Squid instances using the
production policy with fixture listener/source addresses and a hosts-file entry.
An allowlisted name mapped to loopback was denied before contacting the canary.
A counterfactual control removed only the private-destination deny and reached
that same canary. The production Squid listener and configuration were not weakened.

Protected-file checks combined actual mount inspection with host-created
infrastructure and gateway canaries, attempted paths/writes from Hermes, an
unavailable Docker socket, and an actually read-only root filesystem. Persistence
checks wrote UUID canaries into every agent data mount and gateway-only storage,
recreated the two containers, and verified the data and executable tool remained.
Only test-owned files and helper containers were removed afterward.

The gateway check used the **actual upstream Subscription Proxy implementation**
with a fake adapter and loopback origin. Hermes reached the designated internal
API and the origin confirmed replacement of the dummy client bearer with the
dummy fixture bearer. This does not verify a Nous account, real credential
refresh, provider streaming, or inference. A separate unauthenticated TLS request
confirmed the gateway could reach the intended provider endpoint directly.

## Image identities for this run

- `hermes`: `sha256:de23f9daa161d432f7a79b39fa23efb1f31bd524782691fa5ce0736d08b9d341`
- `squid`: `sha256:d6cae5be515ddff05d6d0818e93fa2a5f3cdf8dabf68508030379a167480c31d`
- `llm-gateway`: `sha256:207786029c2afdd8353416b6e7b83d8482cb0db9e08a545b5220c7196eba0dff`

These are local build identities, not published image references. The source and
Python base are pinned; OS package repositories remain time-dependent.

## Review findings and remaining work

| Severity / disposition | Finding and practical implication |
| --- | --- |
| High, human-approved exception | The gateway has direct outbound access without an independent provider destination filter. A gateway compromise can reach other public destinations and potentially host/LAN services. Hermes retains its own verified isolation. Removing this exception awaits reviewed upstream proxy support in a later change. |
| High, known milestone limitation | The gateway has no private inference-client authentication or independent per-agent request/token/spending limits. A compromised authorized agent can consume the subscription quota. Its API is internal-only; unattended agent execution remains out of scope. |
| Pending account acceptance | Human Nous OAuth login, entitlement checks, token refresh/revocation, real inference/streaming, and real-credential error/log redaction have not run. Follow the README login procedure and run `--authenticated` for the real model-list check; deliberate inference/refresh validation is still separate. |
| Scoped qualification | This evidence qualifies the tested native Linux/Docker environment. It does not qualify Docker Desktop/rootless networking, a changed firewall backend, future upstream releases, or modified allowlists/subnets. |

The implementation review checked the network, mount, process and credential
boundaries against actual deployment behavior. No privileged services, Docker
socket mounts, host-network containers, custom authentication broker, upstream
transport patch or autonomous workflow were introduced. The accepted exception
and pending account tests remain explicit; green mechanical checks do not close
them. All other requested handoff boundaries have an executed check above.

After testing, Hermes remains idle and the unauthenticated gateway awaits human
login. The credential-free gateway fixture and external canary are stopped.
Runtime state and the machine-readable report remain Git-ignored. Reproduce the
checks using the README rather than treating this dated snapshot as current
verification after future changes.
