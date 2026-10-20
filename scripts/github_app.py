"""Host-only GitHub App credential storage. Never returns a key or mints a token."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


class AppError(Exception):
    pass


class AppSettings:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else Path.home() / ".config/hermes-dev-loop"
        self.config = self.directory / "github-app.json"
        self.key = self.directory / "github-app.pem"

    def secure_directory(self):
        for path in (self.directory.parent, self.directory):
            if path.is_symlink():
                raise AppError("Refusing a symlink in GitHub App configuration storage.")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.stat().st_uid != os.getuid():
                raise AppError("GitHub App configuration storage has an unexpected owner.")
        self.directory.chmod(0o700)

    def load(self):
        if self.directory.is_symlink() or self.config.is_symlink():
            raise AppError("Refusing symlinked GitHub App configuration.")
        if not self.config.exists():
            return {}
        if self.config.stat().st_uid != os.getuid() or self.config.stat().st_mode & 0o077:
            raise AppError("GitHub App configuration must be owner-only (mode 0600).")
        try:
            data = json.loads(self.config.read_text())
        except (ValueError, OSError):
            raise AppError("GitHub App configuration is unreadable; it was not replaced.") from None
        if not isinstance(data, dict):
            raise AppError("GitHub App configuration must contain a JSON object.")
        return data

    def atomic_write(self, target, content):
        if target.is_symlink():
            raise AppError("Refusing a symlink for a GitHub App configuration file.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory, mode="wb", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def import_key(self, source):
        source = Path(source).expanduser()
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise AppError("Cannot open the downloaded key as a regular, nonsymlink file.") from None
        with os.fdopen(descriptor, "rb") as handle:
            details = os.fstat(handle.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
                raise AppError("The downloaded key must be a regular file owned by this host user.")
            os.fchmod(handle.fileno(), 0o600)
            contents = handle.read(65_537)
        if len(contents) > 65_536:
            raise AppError("The downloaded key exceeds the expected PEM size.")
        if re.fullmatch(rb"SHA256:[A-Za-z0-9+/=]+\s*", contents):
            raise AppError("That file contains a public fingerprint. Download the actual private-key PEM from GitHub App settings.")
        if not contents.startswith((b"-----BEGIN RSA PRIVATE KEY-----", b"-----BEGIN PRIVATE KEY-----")):
            raise AppError("Expected the downloaded, unencrypted RSA private-key PEM. Key contents were not displayed or saved.")
        # Parse the exact bytes being imported; never pass key contents in argv or print errors.
        try:
            check = subprocess.run(["openssl", "rsa", "-check", "-noout", "-passin", "pass:"],
                                   input=contents, capture_output=True, timeout=10)
        except FileNotFoundError:
            raise AppError("Host OpenSSL is required to validate the GitHub App key.") from None
        except subprocess.TimeoutExpired:
            raise AppError("GitHub App key validation timed out.") from None
        if check.returncode:
            raise AppError("The file is not a valid unencrypted RSA private key. Existing credentials were preserved.")
        self.secure_directory()
        if self.key.exists() or self.key.is_symlink():
            if self.key.is_symlink() or self.key.read_bytes() != contents:
                raise AppError("A different key already exists. Review key rotation separately; it was not overwritten.")
            self.key.chmod(0o600)
        else:
            self.atomic_write(self.key, contents)

    def configure(self, *, app_id=None, installation_id=None, key_file=None):
        data = self.load()
        if app_id is not None:
            value = str(app_id).strip()
            if not re.fullmatch(r"(?:[1-9][0-9]*|Iv[0-9A-Za-z.]+)", value):
                raise AppError("Enter the numeric App ID or client ID from the App settings page.")
            if data.get("app_id") and data["app_id"] != value and self.key.exists():
                raise AppError("Changing the App identity with an installed key requires a separate rotation review.")
            data["app_id"] = value
        if installation_id is not None:
            value = str(installation_id).strip()
            value = value.removeprefix("https://github.com/settings/installations/").rstrip("/")
            if not re.fullmatch(r"[1-9][0-9]*", value):
                raise AppError("Enter a positive installation ID or its github.com installation settings URL.")
            data["installation_id"] = int(value)
        # Check configuration before touching a credential; invalid input preserves existing state.
        if key_file:
            self.import_key(key_file)
        self.secure_directory()
        self.atomic_write(self.config, (json.dumps(data, indent=2) + "\n").encode())
        return data

    def status(self):
        data = self.load()
        if self.directory.exists() and self.directory.stat().st_mode & 0o077:
            raise AppError("GitHub App configuration directory must be owner-only (mode 0700).")
        if self.key.is_symlink():
            raise AppError("Refusing a symlink for the GitHub App key.")
        key_present = self.key.is_file()
        if key_present and (self.key.stat().st_uid != os.getuid() or self.key.stat().st_mode & 0o077):
            raise AppError("GitHub App key must be owner-only (mode 0600).")
        return {"app_id": data.get("app_id"), "installation_id": data.get("installation_id"),
                "key_present": key_present, "key_path": str(self.key), "publishing_enabled": False}
