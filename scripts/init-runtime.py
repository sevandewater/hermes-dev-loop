#!/usr/bin/env python3
"""Create only the ignored, host-administered runtime layout; never copy secrets."""
import os
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if os.getuid() != 1000:
    raise SystemExit("Run as the host UID 1000 used by Compose; do not run as root")
runtime = root / "runtime"
paths = [
    runtime,
    runtime / "hermes",
    runtime / "hermes/home",
    runtime / "hermes/home/.hermes",
    runtime / "hermes/home/.hermes/memories",
    runtime / "hermes/home/.hermes/skills",
    runtime / "hermes/workspace",
    runtime / "hermes/memory",
    runtime / "hermes/skills",
    runtime / "hermes/tools",
    runtime / "gateway",
]
for path in paths:
    if path.is_symlink():
        raise SystemExit(f"Refusing symlink: {path}")
    path.mkdir(mode=0o700, exist_ok=True)
    if path.stat().st_uid != 1000:
        raise SystemExit(f"Unexpected owner: {path}")
    path.chmod(0o700)
print("Runtime directories ready (UID 1000, mode 0700); no credentials created")
