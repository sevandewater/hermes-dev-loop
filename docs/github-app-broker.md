# GitHub App and isolated MCP broker

Hermes reads and proposes changes to `sevandewater/hermes-dev-loop` through
`hermes-dev-loop-bot`. The host's human GitHub identity is not reused. App ID
**5402740** identifies the registration; installation ID **173550314** identifies
its installation. These are different public identifiers.

## Administration

Use `./devloop`, then **github**. The submenu supports credential setup,
broker startup/registration, read and denial checks, and an explicit draft-PR
canary. Equivalent commands:

```bash
./devloop github --setup
./devloop github --start
./devloop github --check
./devloop github --proposal-test
```

Setup asks for the App ID, installation ID or installation URL, and the path to
an actual unencrypted RSA private-key PEM. Enter preserves existing values.
Use numeric App ID `5402740` for this broker; startup restricts it to the approved
App. Never paste the key into chat or enter its contents in the path prompt. A
`SHA256:...` fingerprint is not a private key. Host OpenSSL validates the import;
invalid input does not replace saved credentials. Importing a different key is
refused, so rotation is a separate administration task.

The importer stores metadata and the key at
`~/.config/hermes-dev-loop/github-app.json` and `github-app.pem`. The directory
is mode `0700`; files are `0600`. It rejects symlinks and restricts the source
download to `0600` too. The original download remains on the host. Key names,
`secrets/`, and private-key PEM content are excluded from source exports, and
key filenames are Git-ignored. These guards are not a general secret scanner.
The key is not encrypted at rest; host root and Docker administrators are trusted.

Startup builds the optional Compose `github` profile, waits for the broker,
and preserves existing Hermes configuration while registering MCP server
`github-devloop` at `http://github-broker:8000/mcp` using Hermes' maintained
configuration writer. A new Hermes chat discovers the tools. Existing MCP
entries are preserved; a conflicting `github-devloop` URL is refused. Subsequent
DevLoop start/stop/rebuild actions include the enabled broker. No agent session
starts automatically.

## Maintained authentication, narrow operations

The broker starts [GitHub's official MCP server v2.0.2](https://github.com/github/github-mcp-server/tree/v2.0.2)
in stdio mode. Its [native App authentication](https://github.com/github/github-mcp-server/blob/v2.0.2/docs/github-app-auth.md)
signs JWTs and obtains/refreshes installation tokens. There is no custom token
issuer or token-vending interface. The Linux amd64 release archive is checked
against SHA256 `815e27d931c796af0c97f24fd211e14f8a4607d4528377719a5ad2352086b0b0`.
The image reuses the pinned Hermes environment and its MCP SDK 2.3.0; it does
not install an additional floating Python dependency. Other CPU architectures
require a reviewed artifact/digest change.

A small policy adapter uses that SDK's Streamable HTTP server and stdio client.
Its fixed upstream allowlist is exactly `get_file_contents`, `pull_request_read`,
`create_branch`, `push_files`, `create_pull_request`, `repository_ruleset_read`,
`list_issues`, `issue_read`, `issue_write`, `list_label`, `add_issue_comment`;
startup verifies the actual tool set. The ruleset read tool is used internally
for authenticated policy checks and is not exposed to Hermes. Agent-controlled arguments cannot choose the owner, repository,
credential, upstream command or API URL. Thirteen tools are exposed to Hermes:

| Tool | Boundary |
| --- | --- |
| `broker_status` | Read required-review settings and App bypass permission; no remote writes |
| `read_project_file` | Read normalized paths from `main` or `hermes/` branches |
| `read_pull_request` | Read project PR metadata, diff, files, comments, reviews or status |
| `create_proposal_branch` | Create only `hermes/` branches, based on `main` |
| `commit_proposal_files` | Write at most 50 text files / 1 MB to a proposal branch |
| `open_proposal_pull_request` | Open a draft PR against fixed base `main` |
| `list_project_issues` | Cursor-paged issue-only list; optional literal title/body substring search |
| `read_project_issue` | Exact issue metadata and full explicitly paginated discussion; fails on truncation |
| `list_project_labels` | Complete existing label catalog (upstream maximum 100) |
| `create_project_issue` | Title/body only; durable request UUID and exact readback |
| `update_project_issue` | Only title/body; rejects PR targets and verifies readback |
| `change_project_issue_labels` | Existing-label add/remove deltas; preserves unrelated labels |
| `comment_on_project_issue` | Issue-only comment with durable request UUID and exact readback |

All three proposal write tools recheck GitHub protection before their operation. Protected
paths, key files, traversal and workflow writes are rejected. PEM private-key
content is rejected from commits. There is no merge, approval, auto-merge,
branch-delete, workflow, administrative, generic API or command-execution tool.
Backend errors are sanitized; upstream stderr is not forwarded to agent logs.

## Issue management and replay safety

Issue mutations are independent of the branch-review readiness check: they cannot
change code, PRs, review rules, issue state, repository labels or administration.
The original proposal checks are unchanged. Every targeted issue mutation first
reads metadata and requires the exact project `/issues/N` URL (upstream's minimal
issue response drops the REST `pull_request` marker, so absence of that field is
not sufficient). No close/delete, assignment, milestone or reaction argument is exposed.

`list_project_issues(query='', state='all', after='')` uses upstream GraphQL
`repository.issues`, which excludes PRs. Search is a case-insensitive **literal
substring of title/body**, not GitHub search syntax or comment search. Follow
`next_cursor` until null, even when a filtered page is empty. Pages contain up to
100 issues. `read_project_issue` fetches comment pages explicitly with `page=N`,
`perPage=100`, stopping on a short page. Duplicate IDs, failed pages, inconsistent
metadata counts and the 100-page safety ceiling fail rather than report partial
discussion as complete. Reads are not transactional snapshots during concurrent edits.

Input limits: issue numbers are positive 32-bit integers; titles are 1–200
characters; body/comment input is at most 20,000 characters; queries 200;
cursors 2,000; each add/remove list at most 20 unique label names of at most 50
characters. Titles exclude HTML/entity/backtick markup and newlines. Text rejects
hidden/control characters and variation selectors to avoid upstream sanitization
changing exact readback (use LF, not CRLF). Bodies support Markdown and HTML.
Upstream v2.0.2 cannot clear an existing body using an empty string; the broker
rejects that operation rather than falsely claiming success. Omitted update
fields remain untouched. Comments must not be blank.

Create/comment require a caller-generated lowercase UUID `request_id`, retained
with the exact original payload. The broker appends a hidden HTML request marker
to the published body and durably reserves the UUID + payload hash in SQLite
**before dispatch**. Only the first invocation may send the non-idempotent write.
Every invocation scans/readbacks for the exact marked content and target; even a
returned upstream success is not proof. Lost responses are reconciled by reads,
not blind retries. Changed payload with an existing UUID is refused. A retry
using the same UUID can only reconcile, never send again, including after restart.
If failure occurred before dispatch, this conservative choice sacrifices automatic
progress: a human must establish the outcome before authorizing a new UUID.
Editing/removing the marker/content also makes replay unresolved, not a new write.
This is at-most-one dispatch per UUID while the journal is retained, **not**
end-to-end exactly-once GitHub delivery. Different UUIDs can create duplicates.

The broker-only named Docker volume `github-broker-state` at `/broker-state`
retains the journal across recreation. The directory is mode 0700, database 0600;
only request IDs and hashes are stored, not credentials or issue bodies. Do not
remove/restore an older journal and then retry uncertain requests; that loses
replay protection. Keep one broker instance. SQLite serializes reservations and
an in-process lock serializes issue mutations. There is no background retry loop,
controller, token store or backup service.

Title/body and label updates are idempotent desired-state operations: first read,
skip an already-satisfied request, otherwise write once, then exact readback even
on sanitized backend errors. Label deltas merge with current labels, validate
against the complete existing catalog, and re-read immediately before replacement.
The pinned official tools only support label replacement, not atomic deltas/CAS;
external edits in the final read/write window can still be overwritten. Coordinate
concurrent human label edits. A mismatch fails, never silently retries a stale set.
`list_label` is capped at 100 without pagination; incomplete catalogs block label
operations instead of guessing or creating labels.

### Human activation and acceptance (not executed by this implementation)

After independent review/merge, the human rebuilds/activates the broker with
`./devloop github --start`, opens a fresh Hermes session, and runs
`./devloop github --check` (discovery, real issue #3/full discussion, labels and PR
denials, no remote mutations). Source edits alone do not activate these tools.
Then, **only with separate explicit human authorization**, run on the host:

```bash
# Generate ONCE and retain the printed UUID before executing the canary:
python3 -c 'import uuid; print(uuid.uuid4())'
python3 tests/check_github_broker.py --issue-canary YOUR_RETAINED_LOWERCASE_UUID
```

The canary needs two existing repository labels; it never creates labels. It
creates one disposable issue, checks create replay, updates title/body, verifies
label add/remove preserves an unrelated label, posts/replays one comment, and
reads the exact issue/discussion. On failure retain the UUID and inspect the
issue before further action; do not blindly rerun with a new UUID. Once the canary
has edited its creation body/title, the original create replay intentionally
cannot reconcile, so a full rerun is not a recovery controller. Resolve later
steps using the existing issue and retained request IDs. The human closes the
canary afterward; the broker cannot close it. Re-run host security checks and
verify journal persistence across broker recreation. Docker/runtime networking,
App-authenticated reads/mutations and token refresh remain host/live checks.

### Verified upstream contract

The implementation inspected the actual v2.0.2 source and ran credential-free
`initialize` + `tools/list` on the SHA256-verified official Linux amd64 binary.
All eleven allowlisted upstream tools were discovered exactly; no GitHub API
read/write was sent by that discovery. Source references:

- [issues.go](https://github.com/github/github-mcp-server/blob/v2.0.2/pkg/github/issues.go): `IssueRead`, `ListIssues`, `IssueWrite`, comments pagination and empty-body limitation.
- [consolidated_issue_types.go](https://github.com/github/github-mcp-server/blob/v2.0.2/pkg/github/consolidated_issue_types.go): modern method-wrapped issue/comment outputs.
- [typed_output.go](https://github.com/github/github-mcp-server/blob/v2.0.2/pkg/inventory/typed_output.go): modern structured/text versus legacy text protocol contracts (both tested).
- [labels.go](https://github.com/github/github-mcp-server/blob/v2.0.2/pkg/github/labels.go): `list_label`, 100-label cap and `totalCount`.
- [minimal_types.go](https://github.com/github/github-mcp-server/blob/v2.0.2/pkg/github/minimal_types.go): issue URLs, string label arrays, numeric comment IDs and camelCase `pageInfo`.

Local deterministic tests require no credentials. Run `python3 -m unittest discover
-s tests -p 'test_*.py' -v`; SDK-dependent adapter registration tests skip when MCP
is absent. Run the same suite with `/opt/hermes/.venv/bin/python` in the pinned
Hermes environment to exercise the real MCP SDK 2.3.0 as well. No floating SDK
installation is needed.

## Runtime and credential boundary

The broker runs as UID 1000 with a read-only root, dropped capabilities,
no-new-privileges, default seccomp, resource limits and bounded logs. It has no
workspace, host checkout, Docker socket or gateway credential mount. Only its
App metadata and PEM are mounted read-only at `/broker-secrets/`. Hermes has
neither mount and receives only MCP results.

The broker joins only the isolated development bridge. There is no default
route, external DNS forwarding, IPv6 address, host port or external NAT network.
It reaches GitHub through `http://squid:3128`; Squid's broker-specific policy
permits only `api.github.com` and `raw.githubusercontent.com` on CONNECT 443,
with the existing private-destination denial. Hermes cannot directly use
`api.github.com` through its own Squid policy. The trusted broker does not run
agent scripts. Its internal HTTP endpoint accepts the `github-broker:8000`
hostname; possession of that endpoint grants only its eighteen bounded operations.
Hermes' writable local MCP settings do not grant access to additional host mounts.

## Required review without bypass

Human decision on 2026-10-20: code-owner approval is the acceptance gate; the
human owner must also follow the rules. No Restrict updates rule or administrator
bypass is required or added. The broker accepts an effective active PR rule on
`main` only when all of these hold:

- At least one approving review is required.
- Code-owner review is required.
- New commits dismiss stale approvals.
- The authenticated App is explicitly reported as unable to bypass the rule.
- Any visible bypass list is empty. The host-side inspection confirmed no human,
  App, writer or other bypass actor is configured.

The existing [no-direct-main ruleset](https://github.com/sevandewater/hermes-dev-loop/rules/25510073)
already satisfies these checks. Every proposal write rechecks the effective
branch rules and their originating ruleset through the official MCP server
authenticated as the App. Disabled/unrelated rulesets, missing review settings,
any App bypass permission, visible bypass entries, network failures and
unavailable rule data block writes. There is no custom authenticated HTTP client
or token issuer. API limits fail closed.

GitHub can omit `bypass_actors` from the App response. An omitted list is not
treated as an empty list: status reports `bypass_list_visible: false`. The
authenticated `current_user_can_bypass: never` result independently establishes
that the App cannot bypass. The complete empty bypass list was verified through
the human connection; the broker does not claim to continuously verify hidden
actors. No actor or bypass setting was added or changed.
The broker cannot edit GitHub rules. An external administrator can change them,
so maintain the review settings throughout the workflow.

[CODEOWNERS on the PR base branch determines the owners](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners).
The included `.github/CODEOWNERS` assigns every file, including itself, to
`@sevandewater`. It must reach `main` before code-owner-specific enforcement can
use it. At the live check, `main` contained only LICENSE, so this ownership file
was not yet effective. The generic one-review rule already applies; the broker
permits proposal creation during that bootstrap and verifies the configured
code-owner setting, not deployment of the ownership file. Do not confuse that
configuration check with confirmed owner selection on an individual PR.

The App has Contents and Pull requests read/write, Issues read/write, and
Metadata read. Actions diagnostics additionally need Actions **read**, not write.
Installation access is not established by configured grants; verify it after
approved activation. Administration and Workflows write are not requested.
Issues write is used only for bounded issue tools. Keep the installation restricted
to this repository. See [Actions diagnostics and activation](actions-diagnostics.md)
for the new tools, limits, exact-host default-deny storage policy and pending live checks.
[GitHub's merge API requires Contents write](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request),
so this chosen design guarantees required review rather than removal of the
App's underlying post-approval merge permission. The broker has no merge,
approval or auto-merge tool and never attempts a real merge as a test.

The explicit proposal test creates a disposable `hermes/integration-*` branch,
one text-file commit and a draft PR marked **Do not merge**. Close it after
verification. Drafts do not automatically request code-owner reviews; marking a
real proposal ready for review does. GitHub does not allow a PR author to approve
their own PR: the existing foundation PR #1 is authored by `sevandewater` and
needs another eligible reviewer under the no-bypass policy. Future App-authored
proposals can be reviewed by the human owner. No existing PR is merged,
re-authored or replaced by this change.

See [executed validation](github-broker-validation.md) and the
[human-reviewed workflow](hermes-project-workflow.md). Token refresh across an
installation-token expiry has not been exercised; it is delegated to upstream.
