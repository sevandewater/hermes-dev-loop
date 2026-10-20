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
`create_branch`, `push_files`, `create_pull_request`, `repository_ruleset_read`;
startup verifies the actual tool set. The ruleset read tool is used internally
for authenticated policy checks and is not exposed to Hermes. Agent-controlled arguments cannot choose the owner, repository,
credential, upstream command or API URL. Six tools are exposed to Hermes:

| Tool | Boundary |
| --- | --- |
| `broker_status` | Read required-review settings and App bypass permission; no remote writes |
| `read_project_file` | Read normalized paths from `main` or `hermes/` branches |
| `read_pull_request` | Read project PR metadata, diff, files, comments, reviews or status |
| `create_proposal_branch` | Create only `hermes/` branches, based on `main` |
| `commit_proposal_files` | Write at most 50 text files / 1 MB to a proposal branch |
| `open_proposal_pull_request` | Open a draft PR against fixed base `main` |

All three write tools recheck GitHub protection before their operation. Protected
paths, key files, traversal and workflow writes are rejected. PEM private-key
content is rejected from commits. There is no merge, approval, auto-merge,
branch-delete, workflow, administrative, generic API or command-execution tool.
Backend errors are sanitized; upstream stderr is not forwarded to agent logs.

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
hostname; possession of that endpoint grants only its six bounded operations.
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
Metadata read; Administration, Actions and Workflows are absent. Issues write
is unused. Keep the installation restricted to this repository.
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
