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
`create_branch`, `push_files`, `create_pull_request`; startup verifies the actual
tool set. Agent-controlled arguments cannot choose the owner, repository,
credential, upstream command or API URL. Six tools are exposed to Hermes:

| Tool | Boundary |
| --- | --- |
| `broker_status` | Read current human-only update protection; no remote writes |
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

## GitHub must enforce human-only updates

The installed App has Contents and Pull requests read/write, Issues read/write,
and Metadata read; Administration, Actions and Workflows are absent. Issues
write is unused and can be removed. Keep the installation restricted to this
repository. [GitHub's merge API requires Contents write](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request),
so omitting a merge tool or requiring review is insufficient by itself.

Configure the existing [no-direct-main ruleset](https://github.com/sevandewater/hermes-dev-loop/rules/25510073):

1. Keep enforcement **Active** and target **Default branch (`main`)**.
2. Under Rules, enable **Restrict updates**. Retain the PR/review protections.
3. Add **Repository admin** to the bypass list, choosing **For pull requests
   only**. Do not add the GitHub App or the Write role. Save the ruleset.

The broker accepts an effective active `update` rule only when its bypass list
is nonempty and consists exclusively of RepositoryRole ID 5 (administrators),
with `pull_request` or `always` mode. On this personal repository, that role is
the human administrator, not the App. The check uses GitHub's effective branch
rules and then the originating ruleset. Disabled or unrelated rulesets cannot
enable writes. Empty bypass lists would block the human too, so are rejected.
[Restrict updates permits updates only by bypass actors](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).

Rules are read anonymously for this public repository. Network errors, rate
limits and unavailable rule data fail closed; retry later after investigating
connectivity or GitHub's API limits. Private repositories need a separate
reviewed policy-read mechanism. This check is not a GitHub administrator: it
cannot repair protections. Ruleset removal after a check is an external
administrative race; keep the human-owned protections in place.

On 2026-10-20 GitHub reported `no-direct-main` **active**, but without an `update`
rule or bypass actors. Authenticated reads pass; proposal writes remain blocked.
No draft canary has been created. After completing the steps above, rerun the
explicit canary test. It creates a disposable `hermes/integration-*` branch,
one text-file commit and a draft PR marked **Do not merge**. Close that PR after
verification. The test never attempts a real merge.

See [executed validation](github-broker-validation.md) and the
[human-reviewed workflow](hermes-project-workflow.md). Token refresh across an
installation-token expiry has not been exercised; that behavior is delegated
to the maintained upstream implementation.
