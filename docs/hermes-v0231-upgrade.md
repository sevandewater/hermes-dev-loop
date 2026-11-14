# Hermes v0.23.1 upgrade proposal — issue #24

Refs #24 (non-closing). Implementation is authorized; publication, human merge
and host activation are separate gates. This document does not authorize downtime.

## Identities and bounded change

- DevLoop base: `f5595345077d961bff7c22ed529660b8d9be4fd5`.
- Old Hermes: `e55c2662f5ebfbd3a474ebf5e0e0c51187e70e39`.
- Proposed stable v0.23.1: `3b421578588243f920886e6fe79a6faa87ea026e`.
- Fetch, asserted HEAD and runtime revision label move together. Python 3.14,
  digest-pinned base, uv 0.12.3 and `uv sync --frozen --no-default-groups
  --no-install-project --extra mcp --extra pty --extra sms` are retained.
  The source lock revision changes 3 → 5; no hand-edited lock or transport patch.
- Both development and credential gateway use this source/dependency layer;
  GitHub broker inherits the gateway layer through Compose `additional_contexts`.
- Dummy shared-auth seeding supplies keyword-only `renewal=False` (new identity,
  not refresh). Production cleanup is unchanged. The diagnostic requires aiohttp
  3.14.5 and still guards proxy source blob
  `49575154482db3d336b00501dc74dfb5b8711a89` (identical at both pins).

Delegation completion/resume, compaction, MCP reliability, redaction and approval/
URL hardening are the intended gains. Unified gateway ownership and main/auxiliary
inference hooks already existed. No optional UI/routing/telemetry is enabled; no
new listener, service, credential route, permission, profile route or policy is
introduced. The approved unrestricted gateway-egress exception remains unresolved.
This does not satisfy inference custody (#19), funding attribution (#7), or PR
feedback operations (#17).

## Migration and restrictions review

Config 51 → 53 removes `browser.backend` and `browser.extension_control` and
adopts applicable `AGENT_BROWSER_*` settings into browser config without changing
`.env`. Review relative browser-profile paths in the intended mounted home. Old
`browser.backend: off` chose built-in browser tools rather than prohibiting all
browsing: retain actual toolset restrictions and network policy, not that obsolete
key. Migration changes an explicit old-default `agent.clarify_timeout: 3600` to
90 seconds; accept that upstream default, do not restore an hour-long wait.

Session schema 32 → 33 repairs delegated-child markers. Opening writable stores
also runs bounded legacy refusal-history repair and FTS layout/backfill/CJK
retirement, even though FTS storage version remains 3. These are writes, not
read-only compatibility checks. No blanket `optimize-storage` or VACUUM is required;
do not run either against live state as upgrade preparation. SQLite's protective
DELETE fallback is not proof of WAL qualification on the final image.

Privately inventory required tools/plugins/skills in every used home/profile,
including enabled/excluded toolsets, custom MCPs, terminal backend, provider,
browser engine/profile and moved-from-core integrations. Compare to target
`hermes_cli/left_core_migration.py` and the plugin catalog. Classic local terminal,
Nous subscription proxy and this broker must not depend on an optional lazy
installation. Preserve user-curated skills, config and existing denials; never
silently install/update plugins to make a smoke test pass. If a required moved
feature is absent, stop for separate reviewed provisioning. Restricted egress is
not an installation strategy. Use `HERMES_DISABLE_LAZY_INSTALLS=1` during isolated
qualification, retain `security.allow_lazy_installs: false` wherever configured,
and inspect any proposed config delta before host activation. No live config or
private plugin/skill inventory was accessed for this proposal.

## Local evidence — 2026-11-14

All execution below was UID 1000 in scratch/source environments, not Docker.
Local logs and dummy fixtures stay outside Git. Historical foundation reports
remain unchanged and must not be read as tests of the proposed image.

- Strict installed-pin interpreter unit discovery: **206 passed**, no skips,
  `-W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py' -q`.
- Actual `/opt/hermes/.venv/bin/python -W error::ResourceWarning
  scripts/verify.py --units-only`: **206 passed**, no skips.
- Actual same `scripts/verify.py --units-only` in the isolated frozen target-deps
  environment: **206 passed**, no skips, including SDK dispatch/denial tests.
  Python 3.14.8; aiohttp 3.14.5, MCP 2.3.0, httpx2 2.15.1, Starlette 1.9.0,
  Pydantic 2.14.1, OpenAI 2.54.0, nemo-relay 0.11.0. Earlier assessment installed
  83 packages using retained uv 0.12.3/frozen extras; this is not an image build.
- Test-first regressions: old recipe fails the target fetch assertion; old dummy
  call fails the keyword-only renewal contract; old diagnostic rejects target
  aiohttp before reaching its source guard. Each failed before its minimal fix,
  then all three passed. A separate scratch sabotage run restored exactly the
  three base integration files and all three regressions failed again; the proposal
  was untouched. Source mismatch and old aiohttp rejection remain tested.
- Executed the actual proposal's dummy-auth fixture prefix against exact target
  source/deps, with isolated temporary auth homes and socket connections denied:
  ordinary/orphaned logout and cleanup preserved the other dummy provider. This
  did **not** run the container launcher or real OAuth.
- Actual updated loopback diagnostic: `LIMITATION_REPRODUCED`, aiohttp 3.14.5,
  direct=1/proxy=0 and positive-control proxy=1, security_boundaries_verified=false.
- Re-executed scratch assertion runners against exact target source: config
  51 → 53/90-second timeout/lazy-install denial, delegate markers, header
  redaction, explicit read-only DB byte preservation, CJK retirement/integrity,
  selected FTS layout/backfill assertions and five refusal-repair cases passed.
  These directly execute selected upstream assertions, **not full upstream pytest**
  or private-state tests. Scratch SQLite 3.46.1 selected protective DELETE journal.
- Exact target CLI `python -m hermes_cli.main --help` passed using target deps
  and a fresh synthetic home; this is not the installed container launcher.
- Full `python3 scripts/verify.py` was attempted and exited 1: Docker executable
  absent. No image build, containerized unit, native boundary or live claim.

## Operator activation and rollback plan (review before execution)

Use only a trusted host checkout of the reviewed revision. Never execute the
following against the development checkout's runtime mounts. Keep a private
change record of exact Git SHA, image IDs/digests/labels, commands and outcomes;
publish only sanitized conclusions. Do not use `hermes update` in an image.

1. **Prepare without activation.** Agree supervised downtime and rollback owner.
   Record current checkout/image IDs and tag or export all old Hermes, gateway,
   broker and Squid images under immutable rollback names before a build overwrites
   `:local`. Record current Compose/service and saved workflow/log-host settings,
   mounts/ownership and broker named-volume identity. Keep `.env` and App metadata/
   key custody private. Review required plugins as above. Do not broaden policy.
2. **Stop all writers before snapshot.** End agent chats, delegates, shells/tasks
   and any external writer; `./devloop stop` stops managed services including the
   enabled broker. Confirm no old containers/processes retain writable mounts.
   Do not run old/new credential gateways against the same auth store, even for
   a test. Preserve the broker request journal; never `docker compose down -v`.
3. **Take consistent private backups.** With writers stopped, use an owner-only
   directory outside the repository/runtime exports (`umask 077`), encrypted at
   rest where available. Archive all `runtime/hermes/` components (home including
   config, `.env`, auth, sessions/state.db and SQLite sidecars, memories, skills,
   tools, workspace), `runtime/gateway/` separately as credentials, and
   `runtime/devloop/` settings/policy. Snapshot the stopped broker named volume
   too. Preserve UID/GID/modes. Do not copy just an actively written `state.db` or
   omit WAL/SHM; use stopped whole directories or SQLite backup API for databases.
   Record private checksums and verify archives can be read/extracted. Never put
   backup contents, runtime logs, session titles or credentials into Git/PRs.
4. **Build/qualify without private state.** Run `python3 scripts/verify.py` on a
   disposable supported Linux amd64 Docker host: all four Compose images build
   with frozen dependencies and units run in the built development image, no
   skips. Run `python3 tests/check_gateway_logout.py` for dummy logout/orphan/
   awaiting-login/probe integration; launcher `hermes --help` and `hermes proxy
   --help`, imports, UID 1000/read-only roots/writable scratch homes. Record actual
   image IDs and `devloop.hermes.revision` labels for Hermes/gateway/inherited
   broker. Labels are intent; inspect selected source/lock bytes too.
5. **Isolated restore/migration rehearsal before activation.** Extract a second
   private copy, not the backup originals, preserving separate bind-mount topology
   and user ownership. Mount only that copy into the candidate development image
   as UID 1000 with read-only root, dropped capabilities, writable temporary home
   paths, `--network none`, no App keys/provider auth or host socket; disable lazy
   installs. Never start an authenticated proxy on copied rotating credentials.
   Use the target config migration and `SessionDB(db_path=<copied path>)` on each
   copied profile/store, not the live path. Record SQLite version/journal mode,
   schema 33/FTS 3, `PRAGMA integrity_check`, session/message counts, representative
   history and delegate parent/child resume selection, search (including existing
   CJK data where applicable), index repair/backfill completion and reopen behavior.
   Compare config delta/restrictions and `.env` bytes with baseline; preserve skills.
   Exercise CLI history/resume selection without submitting a prompt/inference.
   Recreate the isolated container with the same copied mounts to prove persistence.
   Read-only-open checks alone do not prove writable migration. Failed/incomplete
   search, repair, imports or plugin inventory blocks activation. Keep exact paths,
   titles, search phrases and result content private. This rehearsal has **not run**.
6. **Separately approve activation.** Only after the preceding evidence and
   rollback review, use `./devloop rebuild` from the trusted approved checkout to
   build/recreate with production mounts; recheck no old writers exist first.
   No concurrent host administration. Run `./devloop status`, `./devloop doctor`,
   `./devloop github --check` (read/discovery/denial, not proposal canary),
   `./devloop test`, and during downtime `./devloop test --persistence`.
   Verify awaiting-login if logged out, alias/Host/routing controls, unchanged
   mounts/egress/listeners, saved/live policy and broker-journal persistence.
   After owner login if needed, `./devloop test --authenticated` verifies provider
   model-list access without paid inference. Inspect existing history/search and
   resume selection without sending a turn. Unknown billing exposure requires
   explicit approval; never use a chat completion as a smoke test.
7. **Rollback on a failed gate.** Stop every candidate writer first, retain the
   failed candidate state privately for diagnosis, and restore the complete
   consistent pre-upgrade application-state snapshot plus broker journal/settings
   and old reviewed checkout/immutable images. Verify ownership/mounts before
   starting the old version. Do not point old code at a migrated database or assume
   changing a source pin reverses repairs. New writes since backup are not in that
   snapshot: reconcile privately, do not merge SQLite files blindly. OAuth refresh
   tokens rotate; the snapshot may contain a consumed token. Restore credential
   state only under owner supervision and expect human re-login if needed; do not
   promise transparent credential rollback or run both versions to compare auth.

No dependency on cloud backup issue #6 is introduced. Keep #24 open until real
image, copied-private-state and approved host/live checks are recorded; a locally
complete proposal or merged PR is not operational completion.

## Sources

- [Authoritative issue #24](https://github.com/sevandewater/hermes-dev-loop/issues/24)
- [Stable release](https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.1)
- [Exact target source](https://github.com/NousResearch/hermes-agent/tree/3b421578588243f920886e6fe79a6faa87ea026e)
- [Upstream update documentation](https://hermes-agent.nousresearch.com/docs/getting-started/updating)

Exact source and executed checks, rather than current documentation alone,
establish the compatibility evidence above. Independent fresh-context review
is required before publication and is not claimed by this worker report.
