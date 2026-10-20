# GitHub App credentials and proposed MCP broker

The human registered `hermes-dev-loop-bot` and supplied installation settings URL
`https://github.com/settings/installations/173550314`. The installation ID is
therefore `173550314`; it is not the App ID.

## Current local setup

The supplied repository-root `.github-private.key` contained a SHA-256 public-key
fingerprint, not private-key PEM data. It was untracked and no commit history
referenced that path. Its contents were not printed. The fingerprint was moved
to `~/.config/hermes-dev-loop/github-app.fingerprint`, with mode `0600` inside an
owner-only `0700` directory. The installation ID is saved locally in
`github-app.json`. No actual App key has been imported, no App ID is known, and
no App authentication or remote publication has run.

Run `./devloop` and choose **github**, then the configure/import action. The direct
equivalent is `./devloop github --setup`. The guided setup asks for:

1. The App ID or client ID from the App registration settings.
2. The installation ID or installation settings URL; an existing value is kept
   when Enter is pressed.
3. The local path to the actual downloaded private-key PEM. Enter keeps an
   existing key; key contents must never be pasted into the prompt or chat.

The App registration page is
`https://github.com/settings/apps/hermes-dev-loop-bot`. Use its private-key
generation action to download the PEM. GitHub stores the public portion; the
fingerprint alone cannot sign requests or recover the private key.

The importer uses host OpenSSL to validate an unencrypted RSA private key. It
restricts the downloaded source file to mode `0600` and stores a copy at
`~/.config/hermes-dev-loop/github-app.pem` using an atomic, owner-only write.
The source download is preserved with restricted permissions. Another existing
key is not overwritten; rotation requires a separate deliberate action. Invalid
input does not replace saved configuration or credentials. Unknown JSON metadata
is preserved. Host OpenSSL is required only for key import validation.

Current services do not mount this host configuration directory. The eventual
broker should receive only its key file as a read-only mount; Hermes should
receive MCP results, with neither the key nor installation tokens exposed.
The root Git ignore rules cover private-key filenames, and source export rejects
`.key`/`.pem` paths, `secrets/`, and private-key PEM headers under ordinary names.
These checks supplement keeping credentials out of Git; they do not identify
every possible secret or remove an already committed secret from history.

Owner-only filesystem permissions and a read-only container mount do not encrypt
the key at rest. Host root and Docker administrators remain trusted. If stronger
key custody becomes necessary, GitHub recommends a vault with sign-only access;
that is a later integration rather than a service required for this local setup.

## Existing component to reuse

[GitHub's official MCP server already supports GitHub App authentication](https://github.com/github/github-mcp-server/blob/main/docs/github-app-auth.md):
its stdio mode signs a JWT, obtains installation tokens and refreshes them. Its
HTTP mode does not directly support this App authentication configuration.
Use the maintained authentication implementation, with a pinned version verified
to include it, rather than writing a new token issuer for Hermes.

The deployment still needs an isolated transport boundary between Hermes and
the trusted stdio process. A maintained stdio/HTTP adapter is one option. This
must use fixed administrator-controlled process arguments, an internal service
name such as `github-broker`, no host-published listener, and no host command
runner or workspace execution in the broker. Token vending is not part of the
required interface. No such broker service has been deployed yet.

The [official server configuration](https://github.com/github/github-mcp-server/blob/main/docs/server-configuration.md)
supports explicit tool allowlists. Start with the read operations and proposal
creation operations needed by this project. Merge, review approval, branch
deletion, settings administration, Actions/workflow mutation and generic API or
execution facilities must remain unavailable. Tool filtering alone does not
restrict writable branch arguments or establish human-only merging.

## GitHub enforcement required before enabling writes

The supplied App permission screenshot has Contents and Pull requests read/write,
Issues read/write, Metadata read, and no Administration/Actions/Workflows access.
Issues write is unnecessary unless issue management becomes an explicit goal.
Install only on the selected project repositories.

[GitHub's merge API requires Contents write](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request).
Consequently, Contents write plus omission of the MCP merge tool does not prove
that the App cannot merge or update the base branch. Requiring a human review
also does not remove its merge permission after approval.

Before enabling proposal writes, choose and verify one of these boundaries:

- Restrict GitHub updates to protected branches to the human administrator,
  without App bypass, while allowing proposal branches such as `hermes/**`.
  The broker's fixed tool policy must also reject protected-branch writes.
- Use a proposal repository/fork and separate downscoped credentials so that
  target-repository operations lack Contents write. The complete cross-repository
  token and PR behavior must be validated before selecting this design.

The recommended first option fits the current single-repository App installation:
proposal branches under `hermes/**`, with human-only updates to every other branch
and no App bypass. This still requires verification before enabling the broker.

[Rulesets can restrict updates and configure bypass actors](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).
The read-only investigation on 2026-10-20 found no repository rulesets. The current
connector could not read legacy branch protection (GitHub returned integration
permission error 403), so legacy protections are unknown. No repository rules
were changed and no merge restriction has been claimed as verified.

Changes to Compose networks, Squid policy and Hermes MCP configuration require
their own integration tests. Broker activation must fail closed until the App,
installation scope, key isolation, tool policy, credential redaction and blocked
base-branch updates have been checked. Test negative operations with disposable
targets; never attempt a real PR merge as a permission probe.

## Validation for the local credential preparation

The local unit suite covers valid RSA import with disposable keys, owner-only
permissions, fingerprint and malformed-key rejection, preserved configuration,
idempotent import, rejected key replacement, symlink rejection and disabled
publication. It makes no GitHub requests with a key. Source-export tests cover
forced commits of protected key paths and PEM data under ordinary filenames.
No real App key, token minting, broker deployment, authenticated GitHub operation
or merge-denial test has run.

On 2026-10-20 all **52 local unit tests passed**. The interactive GitHub status
menu, help, syntax checks, Git ignore rules and committed-source export also
passed. Compose, images and service networking were unchanged by this credential
preparation, so the earlier 25-group container boundary run was not repeated.
