# Shared local/CI verification

From the repository root, on a disposable **Linux amd64** Docker host:

```bash
python3 scripts/verify.py
```

This is the full verification command. It requires Python 3 on the host, Docker
Engine, Docker Compose 2.17+ with BuildKit/additional build contexts support,
internet access for the pinned upstream source/dependencies/base images and
GitHub broker binary, and enough disk/time for the development image. The broker
Dockerfile deliberately requires x86_64; do not substitute an emulated or ARM
success for native amd64 qualification.

The command first runs the repository's actual Compose build for `hermes`
(development target), `llm-gateway` (gateway target), `squid`, and `github-broker`,
including the `github` profile. The broker's `base: service:llm-gateway`
`additional_contexts` wiring is preserved; there is no hand-built replacement
Dockerfile, synthetic context or separate image-tag approximation. Any build
failure stops verification before unit execution. No image push occurs.

It then runs **every `tests/test_*.py` test** in the freshly built development
image using `/opt/hermes/.venv/bin/python`, with ResourceWarning promoted to an
error. Dependencies come from the source pin and frozen dependency installation
in `infrastructure/hermes/Dockerfile`; there is no independent pip install or
floating test-only MCP SDK. The development image supplies Git and OpenSSL for
the repository/key fixtures. The PR #5 broker `.dockerignore`/COPY packaging
regression remains in normal discovery; it complements, not replaces, real builds.

Unit execution has a read-only checkout, read-only root, temporary `/tmp`, dropped
capabilities, no-new-privileges and no external network. It does not mount the
Docker socket, App key, host home, runtime state or provider credentials. Git
repositories, dummy credentials and disposable RSA keys are fixture-local. No
Compose services are started, no OAuth/login occurs, no GitHub mutations are sent,
and no paid inference is requested. Builds do have network access and execute
untrusted Dockerfiles: only use an ephemeral host with no sensitive state.

## Exit and skip policy

Zero requires all four image builds and a nonempty successful unit suite with
**zero skipped tests**. All skips are unexpected and fail the gate, including
missing OpenSSL or the pinned MCP SDK. There is no skip allowlist or CI-only
exception. A unittest `OK (skipped=...)` footer does not mean the command passed:
inspect its process exit status. Test failures/import errors and unexpected
successes also fail. Docker/Compose failures propagate nonzero; absent Docker
produces an actionable error instead of falling back to fixtures.

For an already provisioned pinned environment, the explicitly partial command is:

```bash
/opt/hermes/.venv/bin/python -W error::ResourceWarning scripts/verify.py --units-only
```

`--units-only` runs the same discovery/skip policy with the current interpreter;
it neither provisions nor certifies that interpreter and **does not build images**.
It is useful for agent work without Docker, never a replacement for the full CI
gate. The full command can also be invoked by absolute path from another directory.

## Human activation gates (not performed by this change)

A local workflow example is not publication or activation. Before an owner
publishes/enables any Actions workflow or makes its check required, the owner must
inspect repository, organization and enterprise policy and record approval:

- Run untrusted PR code only on fresh ephemeral GitHub-hosted Linux amd64 runners,
  not self-hosted/persistent runners or privileged host/network bridges. Docker
  access belongs to the disposable runner only. Never run this checkout on the
  native deployment host with its credentials or host mounts.
- Use a `pull_request`-style untrusted-code context, not `pull_request_target` or a
  privileged `workflow_run` handoff. Review all callers, reusable workflows,
  downloaded artifacts and cache paths so no untrusted output enters privileged
  execution. Do not share writable caches with privileged jobs.
- Verify **all secret scopes**: repository, organization and environment secrets,
  variables holding sensitive values, GitHub App/private keys and provider
  credentials. Do not inject or inherit any into this job. Scope GITHUB_TOKEN to
  read-only checkout access, disable credential persistence, and grant no write
  permissions. Review any automatic token availability to PR jobs.
- Grant no `id-token: write`; verify cloud OIDC trust cannot accept this job or PR
  identity. Attach no deployment environment, deployment credential or approval
  bypass. Review downstream deployment/artifact publishing triggers separately.
- Pin reviewed Actions by full commit SHA; confirm Actions policy allows them,
  forks cannot gain privileged execution, and the job has a bounded timeout.
- Before requiring the check, run the full command on the intended runner, record
  actual build/unit logs and verify the exact check name **and producer App/owner
  identity** against active rulesets/classic protection. Preserve required PR and
  code-owner reviews, stale-approval dismissal and no bypass. Hidden/incomplete
  rule visibility is not proof of protection. Settings changes remain human-owned.

## Proposal-branch operating assumption

The owner accepts that concurrent edits are outside this change's protection:
no other writer edits an agent proposal branch during publication. The broker
validates the complete proposed workflow set, but pinned upstream `push_files`
re-reads the mutable branch parent and provides no expected-head/CAS input.
Its process-local serialization does not constrain external writers. Another
authorized writer could therefore invalidate the inventory between validation
and publication. No atomic-write adapter or additional writer restrictions are
required under this accepted assumption.

This acceptance does not waive the human activation gates above. Leave
`BROKER_WORKFLOW_PUBLICATION` unset until the owner has verified those gates and
activated the reviewed broker. Only then set it to `verified` on the host.
No live workflow is included in the bootstrap proposal.

## Evidence boundaries

Orchestration fixture tests prove command selection, failure handling and strict
unit discovery only. They do **not** establish that Dockerfiles build. Real Docker
builds must run on the intended runner before claiming the full gate passes.
The implementing agent had no Docker executable/access, so actual image builds
and the containerized unit run were not executed there.

Even successful builds plus fixture units do not qualify native-host isolation,
routes/firewall/DNS, live broker/App access, gateway logout, persistence or paid
inference. Keep the separate supervised checks in [tests](../tests/README.md),
[security validation](security-validation.md) and
[broker validation](github-broker-validation.md); do not reinterpret CI success
as deployment/security-boundary qualification.
