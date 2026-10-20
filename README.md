# Hermes DevLoop

Infrastructure foundation for a human-administered Hermes development environment.

**Status: architectural review required; no deployable stack has been created.**
The handoff requires stopping when a functional integration conflicts with a
security requirement. Investigation found that the preferred Subscription Proxy
does not honor outbound proxy environment settings for inference requests. A
Squid-only gateway deployment therefore needs a transport change or a separately
enforced gateway egress policy. Neither approach has been selected or deployed.

See [the architectural review](docs/infrastructure-review.md) for pinned source
evidence, the exact proposed transport change, alternatives, credential-storage
requirements, and validation status. See
[the diagnostic instructions](tests/README.md) to reproduce the transport finding.

The intended foundation has three services: a nonprivileged Hermes development
container, default-deny Squid, and the existing Nous Subscription Proxy. The
development container will own only its persistent workspace, memory, skills,
and tools. Infrastructure, network controls, and gateway credential storage
remain exclusively human-administered.

No autonomous agent execution is enabled. Runtime security tests, image builds,
storage persistence, and authenticated inference remain pending. This repository
must not yet be treated as an isolated or functional development environment.
