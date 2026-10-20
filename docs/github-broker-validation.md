# GitHub broker integration validation

Executed 2026-10-20 on Ubuntu 22.04.5 / kernel 5.15.0-105-generic,
Docker Engine 29.8.1 and Compose 5.5.1. The human chose required code-owner
approval without bypass, including no administrator bypass. No GitHub rules or
permissions were modified by this integration.

## Current review-policy results

| Check | Result |
| --- | --- |
| Local unit suite, including credential storage, source exports, CLI lifecycle and review policy | 65 tests PASS |
| Official GitHub MCP v2.0.2 App authentication | PASS |
| Exact six-tool Hermes surface; no merge, approval or generic execution tool | PASS |
| Actual repository file and PR reads through Hermes → broker → official GitHub MCP → GitHub | PASS |
| Main writes, workflow writes and key-path reads rejected | PASS |
| Unregistered merge tool rejected locally; no GitHub merge request sent | PASS |
| Active PR rule with one approval, code-owner review and stale-approval dismissal | PASS |
| Authenticated App bypass permission | PASS: `current_user_can_bypass: never` |
| Complete bypass list, inspected through the human GitHub connection | PASS: empty |
| App visibility of complete bypass list | Not visible; status reports this rather than assuming an empty list |
| App-authored branch, commit and draft PR | PASS: disposable PR #2 |
| Canary cleanup | PASS: closed unmerged; disposable branch deleted by host administrator |
| CODEOWNERS active on main | PENDING: ownership file included in foundation PR #1; main still contains only LICENSE |

The explicit `./devloop github --proposal-test` exited zero. It created
`hermes/integration-f0dc7641e423`, committed one test text file and opened
[draft PR #2](https://github.com/sevandewater/hermes-dev-loop/pull/2). GitHub
confirmed the author was `hermes-dev-loop-bot[bot]`, the base was `main`, and the
PR was draft and unmerged. Commit
`ba4160ed0c947c42d976d026c574cf7cbf950d47` was App-attributed and GitHub reported
its signature verified. The PR was then closed and only that disposable branch
was deleted. No proposal was approved or merged.

The official MCP `repository_ruleset_read` tool now supplies the authenticated
policy reads internally; it is not exposed to Hermes. Public rule reads omit
bypass information. The pinned MCP server serializes branch rules in go-github's
grouped format, which the policy normalizes and tests. The App's own inability
to bypass must be explicit; missing or permissive actor data fails closed. If
the complete bypass list is visible and nonempty, the policy rejects it too.
The full list can be hidden from the App, so the broker does not claim to
continuously verify hidden actors. The human connection verified an empty list.

The unit tests reject disabled or missing rules, zero approvals, disabled
code-owner review, stale approvals that survive new commits, visible bypass
actors (including administrators), and missing/permissive App bypass results.
Existing branch, path, content, key-storage and CLI checks also passed.

## CODEOWNERS and bootstrap

`.github/CODEOWNERS` assigns all files, including itself, to `@sevandewater`.
GitHub uses the ownership file on the PR base branch. At this check, main had no
ownership file, so the configured code-owner checkbox did not yet select a
specific reviewer. The generic one-review requirement already applies; proposal
creation is permitted during bootstrap. Owner-specific enforcement starts after
the file reaches main. Drafts do not automatically request code-owner reviews.

The original foundation PR #1 is authored by `sevandewater`. GitHub does not
allow authors to approve their own PR, so it needs another eligible reviewer
under the no-bypass rule. App-authored proposals can be reviewed by the human
owner. This change did not replace, re-author or merge the foundation PR.

Required review gates acceptance; it does not remove the App's underlying
Contents-write ability to merge an approved PR. The broker offers no merge,
approval or auto-merge tool, and tokens remain inside the trusted official MCP
process. The human explicitly selected this approval boundary.

## Container and persistence evidence

The earlier four-service `./devloop test --persistence` completed at 22:57:07 UTC
(18:57 America/Toronto) with **28 groups PASS**, one real-account group SKIP, and
one accepted direct-gateway-egress exception. It inspected actual mounts,
permissions, limits, capabilities, seccomp, network peers, unnumbered bridges,
routes, DNS and listeners, and used reachable positive-control canaries for
direct TCP/DNS and host/private-network denial. It verified the read-only source
reference and persistence of all agent/gateway storage across recreation.

The inference-only `llm-gateway-api` alias worked for both clients and gateway
self-probes. Foreign Host headers and the gateway's external listener were
rejected. A targeted proxy check also denied Hermes direct GitHub API access,
while the broker's GitHub policy worked. Linux IPv6 rejection entries were
distinguished from usable routes; the broker had no IPv6 address or usable route.

The review-policy update changes broker application code, not mounts, networks,
Squid policy or gateway configuration. The full persistence suite was not
repeated for that policy change. The rebuilt broker's network/capability and
GitHub-only proxy checks were repeated and passed. Generated boundary evidence
remains in ignored `runtime/security-results.json`; temporary logs are under
`/tmp/`. Credentials and runtime state are not committed.

## Scope and local image identities

The boundary run used these local images:

- Hermes: `sha256:de23f9daa161d432f7a79b39fa23efb1f31bd524782691fa5ce0736d08b9d341`
- Squid: `sha256:d6cae5be515ddff05d6d0818e93fa2a5f3cdf8dabf68508030379a167480c31d`
- LLM gateway: `sha256:adb00f2ea1e009e6bbdd68db34ea3bb7baafa8a7935ad696aa264634f915785c`
- Earlier broker: `sha256:ac80494da5d0ef9a74089258e806960945ff3af17e73457d600ba0314bfef80a`
- Broker used for successful review-policy canary: `sha256:07ac301e4660933d90566182ddfaa5e62666046424741eff06557a6bd6691ea9`

These are local build identities, not published image references. The GitHub
release archive is checksum-pinned. OS package repositories remain time dependent.
The previous stricter update/bypass policy blocked the earlier canary; it was
superseded by the human's review-only decision and is not an installation step.

These checks did not run paid inference, deliberate provider refresh/streaming,
installation-token expiry/refresh, a real approval or a merge. Real-credential
error/log behavior remains unverified. The approved gateway direct-egress
exception and lack of independent inference budgets are unchanged.
