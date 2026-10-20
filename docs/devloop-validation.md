# DevLoop CLI validation

Executed on 2026-10-20 against the pinned foundation, using native Linux,
Docker Engine 29.8.1 and Compose 5.5.1. No real provider credentials were used.

## Executed checks

- **31 CLI unit tests passed.** Coverage includes repeatable setup, configuration
  preservation, private model preferences, administration locking, subnet
  conflicts, headless operation, model selection and cancellation, login/logout
  orchestration, lifecycle commands, diagnostic output and log filtering.
- **The isolated gateway integration check passed.** It exercised actual pinned
  upstream logout and orphaned shared-store cleanup, preserved another provider's
  authentication, and verified that the launcher waits after logout even when
  an empty `auth.json` remains. Loopback HTTP fixtures verified the CLI's isolated
  health/model probes, hidden HTTP error bodies, and rejection of fixture,
  malformed and oversized responses. This used dummy state in a disposable
  container with no external network or host state mounts.
- **Live operational smoke checks passed:** setup, pinned rebuild, stop/start,
  status, doctor, gateway logs, interactive menu/status/quit, and an interactive
  shell confirming `/workspace`. Missing login was reported as the required next
  action. No agent session was started.
- **`./devloop test --persistence` passed:** 24 boundary/persistence check groups,
  one skipped real-account group and one accepted gateway egress exception.
  The completed report records `2026-10-20T19:44:24.012697+00:00`. The checks and
  exception are described in [the foundation validation](security-validation.md).

Commands for the focused regression checks:

```bash
python3 -m unittest discover -s tests -p test_devloop.py -v
python3 tests/check_gateway_logout.py
./devloop test --persistence
```

The isolated gateway check requires the already-built Compose gateway image;
it does not modify the running gateway or its persistent state. The persistence
suite recreates running containers and should run during supervised downtime.

## Image identities for the CLI boundary run

- `hermes`: `sha256:de23f9daa161d432f7a79b39fa23efb1f31bd524782691fa5ce0736d08b9d341`
- `squid`: `sha256:d6cae5be515ddff05d6d0818e93fa2a5f3cdf8dabf68508030379a167480c31d`
- `llm-gateway`: `sha256:c7b28692da3bdc0becc0d9f9df7fbcdabf376d595ecfdf66fdcf8e521f49be80`

These are local build identities, not published references. The gateway differs
from the earlier foundation run because its launcher now checks usable upstream
authentication rather than the mere presence of a nonempty authentication file.
The upstream transport remains unmodified.

## Account acceptance still pending

Browser approval, actual account model entitlements, credential refresh, real
chat/streaming, and real-credential error/log behavior require human acceptance.
Mock CLI responses and dummy gateway state do not verify those outcomes.
Run `./devloop login` in a terminal, approve the device sign-in in your browser,
select an available model, and use `./devloop chat` for a supervised session.

The previously approved temporary direct gateway egress exception remains;
Hermes retains the tested isolation boundaries. Services were left running,
with Hermes idle and the gateway waiting for human login.
