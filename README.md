# Hermes DevLoop

Infrastructure foundation for a human-administered Hermes development environment.

**Status: waiting for upstream proxy support; no deployable stack has been created.**
The handoff requires stopping when a functional integration conflicts with a
security requirement. Investigation found that the preferred Subscription Proxy
does not honor outbound proxy environment settings for inference requests. A
Squid-only gateway deployment therefore needs upstream transport support or a
separate enforcement approach. The human decision on 2026-10-20 is to wait for
upstream support. A local transport patch and host-managed filtering alternative
are not approved for implementation.

See [the architectural review](docs/infrastructure-review.md) for pinned source
evidence, the deferred transport proposal, the decision and resumption criteria,
credential-storage requirements, and validation status. See
[the diagnostic instructions](tests/README.md) to reproduce the transport finding.

The intended foundation has three services: a nonprivileged Hermes development
container, default-deny Squid, and the existing Nous Subscription Proxy. The
development container will own only its persistent workspace, memory, skills,
and tools. Infrastructure, network controls, and gateway credential storage
remain exclusively human-administered.

No autonomous agent execution is enabled. Runtime security tests, image builds,
storage persistence, and authenticated inference remain pending. This repository
must not yet be treated as an isolated or functional development environment.
