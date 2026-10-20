"""Publish committed project source without exposing the host checkout or its secrets."""
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

PROJECT_REPO = "sevandewater/hermes-dev-loop"
REFERENCE_NAME = "hermes-dev-loop"
PRIVATE_KEY_HEADER = re.compile(rb"(?m)^-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\r?$")


class ReferenceError(Exception):
    pass


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise ReferenceError("Cannot read the committed project source. Check the host Git checkout.")
    return result.stdout


def safe_member(member):
    name = PurePosixPath(member.name)
    blocked = {".git", "runtime", "secrets", ".ssh", ".aws", ".gnupg", ".netrc"}
    if name.is_absolute() or not name.parts or any(part in ("..", ".") or part.lower() in blocked for part in name.parts):
        raise ReferenceError("The committed source contains a protected path; reference was not updated.")
    if any((part == ".env" or part.startswith(".env.")) and part != ".env.example" for part in name.parts):
        raise ReferenceError("The committed source contains a private environment path; reference was not updated.")
    if name.suffix.lower() in (".key", ".pem"):
        raise ReferenceError("The committed source contains a key file; reference was not updated.")
    if not member.isfile() and not member.isdir():
        raise ReferenceError("The committed source contains a link or special file; reference was not updated.")
    return name


def publish(root):
    """Caller holds the DevLoop administration lock. Export HEAD, never worktree contents."""
    root = Path(root).resolve()
    reference = root / "runtime/reference"
    revisions = reference / "revisions"
    for path in (root / "runtime", reference, revisions):
        if path.is_symlink():
            raise ReferenceError("Refusing a symlink in reference storage.")
        path.mkdir(mode=0o700, exist_ok=True)
        if path.stat().st_uid != os.getuid():
            raise ReferenceError("Reference storage has an unexpected owner.")
        path.chmod(0o700)
    revision = git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ReferenceError("Unexpected project commit identity.")
    # Re-export rather than trusting previously generated files as a source.
    archive = git(root, "archive", "--format=tar", revision)
    if len(archive) > 100_000_000:
        raise ReferenceError("Project reference exceeds the 100 MB export limit.")
    temporary = Path(tempfile.mkdtemp(prefix=revision + "-", dir=revisions))
    pointer = reference / ".next-reference"
    try:
        with tarfile.open(fileobj=BytesIO(archive), mode="r:") as source:
            members = source.getmembers()
            if len(members) > 10_000:
                raise ReferenceError("Project reference exceeds the 10,000 file export limit.")
            # Check the complete archive before copying any file; never extract links.
            for member in members:
                safe_member(member)
            for member in members:
                path = temporary.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with source.extractfile(member) as content, path.open("xb") as target:
                        tail = b""
                        while chunk := content.read(1_048_576):
                            if PRIVATE_KEY_HEADER.search(tail + chunk):
                                raise ReferenceError("The committed source contains private-key PEM data; reference was not updated.")
                            target.write(chunk)
                            tail = chunk[-128:]
                    path.chmod(0o700 if member.mode & 0o111 else 0o600)
        (temporary / ".devloop-reference.json").write_text(json.dumps({
            "repository": PROJECT_REPO, "revision": revision,
            "source": "committed host HEAD; excludes uncommitted and untracked files",
        }, indent=2) + "\n")
        if pointer.exists() or pointer.is_symlink():
            pointer.unlink()
        pointer.symlink_to(temporary.relative_to(reference), target_is_directory=True)
        current = reference / REFERENCE_NAME
        if current.exists() and not current.is_symlink():
            raise ReferenceError("Reference pointer was replaced by a directory; review host reference storage.")
        os.replace(pointer, current)
    except BaseException:
        pointer.unlink(missing_ok=True)
        shutil.rmtree(temporary)
        raise
    # Leave previous snapshots alone: an active reader may still be using one.
    return revision
