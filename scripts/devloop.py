"""Standard-library CLI for the human administrator, never mounted into Hermes."""
import argparse
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

from scripts.project_reference import PROJECT_REPO, ReferenceError, publish as publish_reference
from scripts.github_app import AppError, AppSettings

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "hermes-dev-loop"
SERVICES = ("hermes", "squid", "llm-gateway")
OPTIONAL_SERVICES = ("github-broker",)
ACTIONS = {
    "setup": "Prepare, build and start DevLoop (safe to rerun)",
    "start": "Start services",
    "stop": "Stop services; keep data and login",
    "status": "Show service, login and model status",
    "login": "Sign in to Nous in your browser",
    "logout": "Clear the gateway's Nous login",
    "models": "List models and select a default",
    "chat": "Start an interactive Hermes session",
    "shell": "Open the development shell",
    "logs": "View service logs",
    "doctor": "Diagnose setup and connectivity",
    "test": "Run security checks",
    "rebuild": "Rebuild pinned images and recreate services",
    "project": "Inspect or refresh the read-only project reference",
    "github": "Configure, start or test the isolated GitHub MCP broker",
    "help": "Show commands and examples",
}

# Only bounded, nonsensitive results leave the gateway over this probe.
PROBE = r'''
import json, sys, urllib.error, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    request = urllib.request.Request(sys.argv[1], headers={"Authorization": "Bearer unused"})
    with opener.open(request, timeout=15) as response:
        payload = response.read(2_000_001)
        if len(payload) > 2_000_000:
            raise ValueError("Response too large")
        data = json.loads(payload)
    if sys.argv[2] == "health":
        result = {"ok": data.get("status") == "ok", "authenticated": data.get("authenticated") is True}
    else:
        rows = data.get("data")
        if not isinstance(rows, list) or data.get("fixture"):
            raise ValueError("Not a provider model list")
        result = {"ok": True, "models": [r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)]}
except urllib.error.HTTPError as error:
    result = {"ok": False, "http_status": error.code}
except Exception:
    result = {"ok": False}
print(json.dumps(result))
'''

LOGOUT_CLEANUP = (
    "from hermes_cli.auth import clear_provider_auth; "
    "from hermes_cli.auth_nous import _clear_shared_nous_state, _nous_shared_store_path; "
    "from hermes_cli.proxy.adapters.nous_portal import NousPortalAdapter; "
    "clear_provider_auth('nous'); _clear_shared_nous_state('devloop_logout'); "
    "assert not NousPortalAdapter().is_authenticated() and not _nous_shared_store_path().exists()"
)

PROJECT_CHECKOUT = r'''
import pathlib, subprocess, sys
target = pathlib.Path('/workspace/projects/hermes-dev-loop')
target.parent.mkdir(parents=True, exist_ok=True)
if target.exists() or target.is_symlink():
    raise SystemExit('Checkout already exists; keep its work and inspect it from Hermes.')
target.mkdir()
def git(*args):
    subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-C', str(target), *args], check=True)
git('init', '--quiet', '--template=')
git('remote', 'add', 'upstream', 'https://github.com/sevandewater/hermes-dev-loop.git')
git('fetch', '--depth=1', 'upstream', sys.argv[1])
git('checkout', '-b', 'hermes/improvement', 'FETCH_HEAD')
git('config', 'user.name', 'Hermes')
git('config', 'user.email', 'hermes@users.noreply.github.com')
print('Writable checkout prepared at /workspace/projects/hermes-dev-loop. No GitHub credentials installed.')
'''

GITHUB_MCP_CONFIGURE = r'''
import pathlib, sys
sys.path.insert(0, '/opt/hermes')
from hermes_cli.config import load_config
from hermes_cli.mcp_config import _save_mcp_server
path = pathlib.Path('/home/hermes/.hermes/config.yaml')
if path.is_symlink() or path.parent.is_symlink():
    raise SystemExit('Refusing symlinked Hermes configuration.')
config = load_config()
if not isinstance(config, dict):
    raise SystemExit('Hermes configuration is not a mapping; it was preserved.')
servers = config.setdefault('mcp_servers', {})
if not isinstance(servers, dict):
    raise SystemExit('MCP server configuration is not a mapping; it was preserved.')
entry = servers.get('github-devloop', {})
if not isinstance(entry, dict) or entry.get('url', 'http://github-broker:8000/mcp') != 'http://github-broker:8000/mcp':
    raise SystemExit('Another github-devloop endpoint already exists; configuration was preserved.')
entry.update({'url': 'http://github-broker:8000/mcp', 'connect_timeout': 20})
if not _save_mcp_server('github-devloop', entry):
    raise SystemExit('Hermes rejected the MCP registration; existing configuration was preserved.')
print('Registered github-devloop MCP at http://github-broker:8000/mcp. Start a new Hermes chat to discover its tools.')
'''


class Error(Exception):
    pass


def terminal():
    return sys.stdin.isatty() and sys.stdout.isatty()


def require_terminal(action):
    if not terminal():
        raise Error(f"{action} needs an interactive terminal. Run ./devloop {action} in your terminal.")


def redact(line):
    """Best-effort display filtering, not a guarantee about arbitrary upstream logs."""
    line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
    line = re.sub(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", line)
    line = re.sub(r'''(?ix)((?:access_token|refresh_token|id_token|agent_key|api_key|client_secret|password)["']?\s*[:=]\s*["']?)[^\s,"'&}]+''', r"\1[redacted]", line)
    line = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted-jwt]", line)
    line = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[redacted-key]", line)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", line)


class Runner:
    def run(self, args, *, capture=True, data=None, timeout=30, label="Command", interactive=False, failure_messages=None, env=None):
        try:
            if interactive:
                # Leave Ctrl-C with Docker/the interactive application while it owns the terminal.
                child = subprocess.Popen(args, env=env)
                previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
                try:
                    code = child.wait()
                finally:
                    signal.signal(signal.SIGINT, previous)
                if code:
                    raise Error(f"{label} exited with status {code}.")
                return ""
            result = subprocess.run(args, input=data, text=True, capture_output=capture, timeout=timeout, env=env)
        except FileNotFoundError:
            raise Error(f"{args[0]} is not installed or not on PATH.") from None
        except subprocess.TimeoutExpired:
            raise Error(f"{label} timed out. Run ./devloop doctor to inspect the current state.") from None
        if result.returncode:
            if failure_messages and result.returncode in failure_messages:
                raise Error(failure_messages[result.returncode])
            problem = (result.stderr or "").lower()
            if args[0] == "docker" and "permission denied" in problem:
                raise Error("Docker access denied for this user. Check access to the Docker daemon socket.")
            if args[0] == "docker" and ("cannot connect" in problem or "is the docker daemon running" in problem):
                raise Error("Docker daemon is unavailable. Start Docker and try again.")
            raise Error(f"{label} failed (exit {result.returncode}). Run ./devloop doctor; use ./devloop logs for service errors.")
        return result.stdout.strip() if capture else ""

    def logs(self, args):
        child = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for line in child.stdout:
                print(redact(line), end="", flush=True)
            code = child.wait()
            if code:
                raise Error(f"Log viewer exited with status {code}.")
        except KeyboardInterrupt:
            child.send_signal(signal.SIGINT)
        finally:
            if child.poll() is None:
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            child.stdout.close()


def decoded(output, what):
    try:
        return json.loads(output)
    except (ValueError, TypeError):
        raise Error(f"{what} returned an unreadable response.") from None


def model_id(value):
    if not isinstance(value, str) or not 0 < len(value) <= 256 or value.startswith("-") or any(c.isspace() or not c.isprintable() for c in value):
        raise Error("Invalid model ID. Choose an ID from ./devloop models.")
    return value


class DevLoop:
    def __init__(self, root=ROOT, runner=None):
        self.root = Path(root).resolve()
        self.runner = runner or Runner()
        self.compose = ["docker", "compose", "--project-directory", str(self.root), "--file", str(self.root / "compose.yaml"), "--project-name", PROJECT]
        self.control = self.root / "runtime/devloop"
        self.settings = self.control / "settings.json"
        self._config = None

    def dc(self, *args, **kwargs):
        environment = dict(os.environ)
        environment['BROKER_WORKFLOW_PUBLICATION'] = 'verified' if self.host_settings().get('workflow_publication', False) else ''
        return self.runner.run(self.compose + list(args), label="Docker Compose", env=environment, **kwargs)

    def config(self):
        if self._config is None:
            value = decoded(self.dc("config", "--format", "json"), "Compose configuration")
            if not isinstance(value, dict) or not all(service in value.get("services", {}) for service in SERVICES):
                raise Error("Compose configuration is missing a required service.")
            environment = value["services"]["hermes"].get("environment", {})
            if not isinstance(environment, dict) or not isinstance(environment.get("OPENAI_BASE_URL"), str):
                raise Error("Compose configuration is missing Hermes' gateway endpoint.")
            self._config = value
        return self._config

    def private_control(self):
        if os.getuid() != 1000:
            raise Error("Run DevLoop as host UID 1000, not root (the container ownership configured for this project).")
        for path in (self.root / "runtime", self.control):
            if path.is_symlink():
                raise Error(f"Refusing symlink: {path}")
            path.mkdir(mode=0o700, exist_ok=True)
            if path.stat().st_uid != os.getuid():
                raise Error(f"Unexpected owner: {path}")
            path.chmod(0o700)

    @contextmanager
    def administration(self):
        self.private_control()
        path = self.control / "admin.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Error("Another DevLoop administration action is running. Try again after it finishes.") from None
            yield
        finally:
            os.close(descriptor)

    def host_settings(self):
        for path in (self.root / "runtime", self.control, self.settings):
            if path.is_symlink():
                raise Error(f"Refusing symlink: {path}")
        if not self.settings.exists():
            return {}
        settings = decoded(self.settings.read_text(), "Host settings")
        if not isinstance(settings, dict):
            raise Error("Host settings must contain a JSON object.")
        if 'workflow_publication' in settings and type(settings['workflow_publication']) is not bool:
            raise Error("Host workflow_publication setting must be a boolean.")
        return settings

    def selected_model(self):
        settings = self.host_settings()
        return model_id(settings["model"]) if settings.get("model") else None

    def save_model(self, value):
        value = model_id(value)
        self.save_settings({'model': value})
        print(f"Default model saved: {value}")

    def save_settings(self, updates):
        self.private_control()
        # Preserve unknown settings, and refuse to replace unreadable existing configuration.
        settings = self.host_settings()
        settings.update({'version': 1, **updates})
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.control, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(settings, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.settings)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        self._config = None

    def services(self):
        output = self.dc("ps", "--all", "--format", "json")
        if not output:
            return {}
        if output.lstrip().startswith("["):
            rows = decoded(output, "Service status")
        else:
            rows = [decoded(line, "Service status") for line in output.splitlines() if line.strip()]
        return {row["Service"]: row for row in rows if row.get("Service") in SERVICES + OPTIONAL_SERVICES}

    def require_running(self, *services):
        rows = self.services()
        missing = [name for name in services if rows.get(name, {}).get("State") != "running"]
        if missing:
            raise Error(f"Services not running: {', '.join(missing)}. Run ./devloop start (or setup on first use).")

    def endpoint(self, kind):
        base = self.config()["services"]["hermes"]["environment"]["OPENAI_BASE_URL"]
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise Error("Compose has an invalid gateway endpoint.")
        return urlunsplit((parts.scheme, parts.netloc, "/health", "", "")) if kind == "health" else base.rstrip("/") + "/models"

    def probe(self, kind):
        try:
            # Read trusted API state from the gateway, never an agent-owned
            # interpreter or Python user-site package in Hermes' writable home.
            result = decoded(self.dc("exec", "-T", "llm-gateway", "/usr/local/bin/python3", "-I", "-", self.endpoint(kind), kind,
                                     data=PROBE, timeout=25), "Gateway")
            return result if isinstance(result, dict) else {"ok": False}
        except Error:
            return {"ok": False}

    def stored_login(self):
        # Local-only upstream check; credentials are never serialized to the host CLI.
        code = "from hermes_cli.proxy.adapters.nous_portal import NousPortalAdapter; print('yes' if NousPortalAdapter().is_authenticated() else 'no')"
        return self.dc("exec", "-T", "llm-gateway", "python3", "-c", code, timeout=15).splitlines()[-1] == "yes"

    def status(self):
        rows = self.services()
        for name in SERVICES + tuple(name for name in OPTIONAL_SERVICES if name in rows):
            row = rows.get(name, {})
            print(f"{name}: {row.get('State', 'not created')}" + (f" ({row['Health']})" if row.get("Health") else ""))
        selected = self.selected_model()
        print(f"Selected model: {selected or 'none'}")
        if any(rows.get(name, {}).get("State") != "running" for name in SERVICES):
            print("Next: ./devloop setup on first use, or ./devloop start.")
            return
        health = self.probe("health")
        if health.get("ok") and health.get("authenticated"):
            print("Gateway responding; credentials stored. Provider access has not been checked by status.")
            print("Next: ./devloop chat" if selected else "Next: ./devloop models to choose a model.")
        elif not self.stored_login():
            print("Nous login required. Next: ./devloop login")
        else:
            print("Credentials stored, but gateway is not ready. Run ./devloop doctor or ./devloop logs llm-gateway.")

    def prerequisites(self):
        if sys.platform != "linux" or os.getuid() != 1000:
            raise Error("This foundation requires native Linux and host UID 1000. Do not run as root.")
        version = decoded(self.runner.run(["docker", "version", "--format", "{{json .Server}}"], label="Docker daemon"), "Docker version")
        try:
            major = int(version["Version"].split(".")[0])
        except (KeyError, ValueError, TypeError):
            raise Error("Unable to determine Docker Engine version.") from None
        if major < 28 or version.get("Os") != "linux":
            raise Error("Linux Docker Engine 28 or newer is required for isolated bridge mode.")
        options = decoded(self.runner.run(["docker", "info", "--format", "{{json .SecurityOptions}}"], label="Docker security mode"), "Docker security mode")
        if any("rootless" in option for option in options):
            raise Error("Rootless Docker is not qualified for this foundation.")
        compose = self.dc("version", "--short").lstrip("v")
        try:
            compose_version = tuple(int(part) for part in compose.split(".")[:2])
        except ValueError:
            raise Error("Unable to determine Docker Compose version.") from None
        if compose_version < (2, 17):
            raise Error("Docker Compose 2.17 or newer is required for service build contexts.")
        self.config()
        print(f"Docker Engine {version['Version']}; Compose {compose}; configuration valid.")

    def network_check(self):
        if not shutil.which("ip"):
            raise Error("Host iproute2 is required to check LAN/VPN routes and run security tests.")
        planned = [ipaddress.ip_network(item["subnet"]) for network in self.config()["networks"].values()
                   if network.get("internal") for item in network.get("ipam", {}).get("config", [])]
        identifiers = self.runner.run(["docker", "network", "ls", "-q"], label="Docker networks").split()
        networks = decoded(self.runner.run(["docker", "network", "inspect", *identifiers], label="Docker networks"), "Docker networks") if identifiers else []
        own_bridges = set()
        for network in networks:
            if (network.get("Labels") or {}).get("com.docker.compose.project") == PROJECT:
                own_bridges.add("br-" + network["Id"][:12])
                continue
            for item in network.get("IPAM", {}).get("Config") or []:
                existing = ipaddress.ip_network(item["Subnet"]) if item.get("Subnet") else None
                if existing and any(existing.version == subnet.version and existing.overlaps(subnet) for subnet in planned):
                    raise Error(f"Internal subnet overlaps another Docker network ({item['Subnet']}). Review the fixed subnets before setup.")
        routes = decoded(self.runner.run(["ip", "-j", "route"], label="Host routes"), "Host routes")
        for route in routes:
            if route.get("dst", "default") == "default" or route.get("dev") in own_bridges:
                continue
            destination = ipaddress.ip_network(route["dst"], strict=False)
            if any(destination.version == subnet.version and destination.overlaps(subnet) for subnet in planned):
                raise Error(f"Internal subnet overlaps host/LAN/VPN route {destination}. Review the fixed subnets before setup.")
        print("Internal subnets have no detected Docker/LAN/VPN conflicts.")

    def runtime_check(self):
        for name in ("home", "workspace", "memory", "skills", "tools"):
            path = self.root / "runtime/hermes" / name
            for parent in (self.root / "runtime", self.root / "runtime/hermes", path):
                if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != 1000:
                    raise Error(f"Runtime directory unavailable or unsafe: {parent}. Run ./devloop setup.")
        gateway = self.root / "runtime/gateway"
        if gateway.is_symlink() or not gateway.is_dir() or gateway.stat().st_uid != 1000 or stat.S_IMODE(gateway.stat().st_mode) & 0o077:
            raise Error("Gateway storage must be a UID-1000 directory with mode 0700. Run ./devloop setup.")
        print("Runtime directories and private gateway permissions verified.")

    def prepare(self):
        env = self.root / ".env"
        if env.is_symlink():
            raise Error("Refusing a symlink for .env; review your infrastructure configuration.")
        try:
            descriptor = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            print("Keeping existing .env.")
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write((self.root / ".env.example").read_bytes())
            print("Created .env from the example.")
        self.runner.run([sys.executable, str(self.root / "scripts/init-runtime.py")], capture=False, label="Runtime initialization")
        self._config = None
        self.config()
        self.runtime_check()
        self.refresh_reference()

    def refresh_reference(self):
        try:
            revision = publish_reference(self.root)
        except ReferenceError as error:
            raise Error(str(error)) from None
        print(f"Read-only project reference prepared from committed HEAD {revision[:12]}.")

    def project(self, refresh=False, checkout=False):
        if not refresh and not checkout and terminal():
            print("1. Show reference status\n2. Refresh from committed host source\n3. Prepare a new writable checkout")
            choice = input("Project action [1]: ").strip() or "1"
            if choice not in ("1", "2", "3"):
                raise Error("Choose one of the listed project actions.")
            refresh, checkout = choice == "2", choice == "3"
        if refresh:
            with self.administration():
                self.refresh_reference()
        manifest = self.root / "runtime/reference/hermes-dev-loop/.devloop-reference.json"
        if not manifest.is_file():
            raise Error("Project reference unavailable. Run ./devloop project --refresh, then ./devloop start.")
        info = decoded(manifest.read_text(), "Project reference")
        if info.get("repository") != PROJECT_REPO or not re.fullmatch(r"[0-9a-f]{40}", info.get("revision", "")):
            raise Error("Invalid project reference identity. Run ./devloop project --refresh.")
        print(f"Repository: {info['repository']}")
        print(f"Reference commit: {info['revision']}")
        print("Hermes can read /reference/hermes-dev-loop; changes belong in a separate /workspace checkout.")
        print("GitHub proposals use the isolated App broker; check live access with ./devloop github.")
        if checkout:
            self.require_running("hermes", "squid")
            with self.administration():
                exists = self.dc("exec", "-T", "hermes", "/usr/local/bin/python3", "-I", "-c",
                                 "from pathlib import Path; p=Path('/workspace/projects/hermes-dev-loop'); print('yes' if p.exists() or p.is_symlink() else 'no')")
                if exists == "yes":
                    raise Error("Writable project checkout already exists; existing work was preserved.")
                self.dc("exec", "-T", "hermes", "/usr/local/bin/python3", "-I", "-", info["revision"],
                        data=PROJECT_CHECKOUT, capture=False, timeout=120)

    def workflow_publication(self, action):
        if action != 'status':
            with self.administration():
                self.save_settings({'workflow_publication': action == 'enable'})
                try:
                    self.require_running('github-broker')
                    self.dc('--profile', 'github', 'up', '-d', '--no-deps', '--force-recreate', '--wait', '--wait-timeout', '60', 'github-broker', capture=False, timeout=90)
                    live = self.live_workflow_publication()
                    if live != ('enabled' if action == 'enable' else 'disabled'):
                        raise Error('Running broker setting does not match the configured setting.')
                except Error as error:
                    raise Error(f'Workflow setting saved, but live application was not verified: {error} Run ./devloop github --workflows status; retry the command after recovery.') from None
        if action == 'status':
            live = self.live_workflow_publication()
        configured = 'enabled' if self.host_settings().get('workflow_publication', False) else 'disabled'
        print(f'Workflow publication configured: {configured}')
        print(f'Workflow publication live: {live}')

    def live_workflow_publication(self):
        try:
            self.require_running('github-broker')
            # Read only this setting, never the broker credential environment.
            value = self.dc('exec', '-T', 'github-broker', '/usr/local/bin/python3', '-I', '-c',
                            "import os; print('enabled' if os.environ.get('BROKER_WORKFLOW_PUBLICATION') == 'verified' else 'disabled')")
            return value if value in ('enabled', 'disabled') else 'unavailable'
        except Error:
            return 'unavailable'

    def github(self, setup=False, start=False, check=False, proposal_test=False, workflows=None):
        if workflows is not None:
            return self.workflow_publication(workflows)
        store = AppSettings()
        try:
            if not any((setup, start, check, proposal_test)) and terminal():
                print("1. Show GitHub App setup status\n2. Configure App IDs and import a downloaded PEM\n3. Start broker and register Hermes MCP\n4. Check real MCP reads and denied operations\n5. Test draft PR creation (creates a canary only when protection passes)\n6. Workflow publication (host setting only)")
                choice = input("GitHub action [1]: ").strip() or "1"
                if choice not in ("1", "2", "3", "4", "5", "6"):
                    raise Error("Choose one of the listed GitHub actions.")
                if choice == '6':
                    print('1. Enable for authorized workflow-file changes\n2. Disable workflow publication\n3. Show configured and live status')
                    choice = input('Workflow action [3]: ').strip() or '3'
                    if choice not in ('1', '2', '3'):
                        raise Error('Choose one of the listed workflow actions.')
                    return self.workflow_publication({'1': 'enable', '2': 'disable', '3': 'status'}[choice])
                setup = choice == "2"
                start, check, proposal_test = choice == "3", choice == "4", choice == "5"
            if setup:
                require_terminal("github")
                previous = store.load()
                print("Use the App settings page for IDs. Enter only the downloaded PEM's file path, never key contents.")
                app_id = input(f"App ID or client ID [{previous.get('app_id', '')}]: ").strip() or previous.get("app_id")
                installation_id = input(f"Installation ID or settings URL [{previous.get('installation_id', '')}]: ").strip() or previous.get("installation_id")
                key_file = input("Downloaded private-key PEM path (Enter keeps existing key): ").strip()
                if not app_id or not installation_id:
                    raise Error("Both App ID and installation ID are required. Existing configuration was preserved.")
                with self.administration():
                    store.configure(app_id=app_id, installation_id=installation_id, key_file=key_file)
            info = store.status()
        except AppError as error:
            raise Error(str(error)) from None
        print(f"App ID: {info['app_id'] or 'not configured'}")
        print(f"Installation ID: {info['installation_id'] or 'not configured'}")
        print(f"Private key: {'stored locally' if info['key_present'] else 'not imported'} ({info['key_path']})")
        if start:
            if not all((info['app_id'], info['installation_id'], info['key_present'])):
                raise Error("Complete GitHub App credential setup before starting the broker.")
            self.prerequisites()
            self.network_check()
            self.require_running('hermes', 'squid')
            with self.administration():
                self.dc('--profile', 'github', 'build', 'github-broker', capture=False, timeout=None)
                self.dc('--profile', 'github', 'up', '-d', '--wait', '--wait-timeout', '60', 'github-broker', capture=False, timeout=90)
                self.dc('exec', '-T', 'hermes', '/opt/hermes/.venv/bin/python', '-I', '-', data=GITHUB_MCP_CONFIGURE)
                (self.control / 'github-enabled').write_text('enabled\n')
            print('Broker started and registered. Start a new Hermes chat to discover its tools. Writes require active PR/code-owner review rules that the App cannot bypass; use Check integration.')
        elif check or proposal_test:
            self.require_running('hermes', 'github-broker')
            args = [sys.executable, str(self.root / 'tests/check_github_broker.py')]
            if proposal_test:
                args.append('--proposal')
            with self.administration():
                self.runner.run(args, capture=False, timeout=150, label='GitHub broker integration', failure_messages={
                    2: 'Proposal test blocked: main must require PR/code-owner review, dismiss stale approvals, and prevent App bypass.'})
        else:
            print('Credential storage alone does not enable PR publishing. Start the broker, then check its live policy.')

    def start_services(self):
        self.runtime_check()
        if not (self.root / "runtime/reference/hermes-dev-loop/.devloop-reference.json").is_file():
            self.refresh_reference()
        self.dc("up", "-d", capture=False, timeout=120)
        # The unauthenticated gateway is intentionally unhealthy, so wait only
        # for the development pair here. Login separately verifies the gateway.
        self.dc("up", "-d", "--wait", "--wait-timeout", "60", "squid", "hermes", capture=False, timeout=90)
        if (self.control / 'github-enabled').is_file():
            self.dc('--profile', 'github', 'up', '-d', '--wait', '--wait-timeout', '60', 'github-broker', capture=False, timeout=90)

    def setup(self):
        self.prerequisites()
        self.network_check()
        with self.administration():
            self.prepare()
            self.dc("build", capture=False, timeout=None)
            self.start_services()
        print("Setup complete. No agent session has been started.")
        self.status()

    def start(self):
        self.prerequisites()
        self.network_check()
        with self.administration():
            self.start_services()
        self.status()

    def stop(self):
        with self.administration():
            options = ('--profile', 'github', 'stop') if (self.control / 'github-enabled').is_file() else ('stop',)
            self.dc(*options, capture=False, timeout=90)
        print("Services stopped. Workspace, login and model preference preserved.")

    def models_available(self):
        self.require_running("hermes", "llm-gateway")
        response = self.probe("models")
        if not response.get("ok"):
            if response.get("http_status") in (401, 403):
                raise Error("Nous credentials or account access need attention. Run ./devloop login.")
            raise Error("The gateway could not list provider models. Run ./devloop login if unsigned in, or ./devloop doctor.")
        models = sorted({model_id(value) for value in response.get("models", [])})
        if not models:
            raise Error("Nous returned no available models. Check your Portal account entitlements.")
        return models

    def choose_model(self, models):
        require_terminal("models")
        selected = self.selected_model()
        if selected not in models:
            selected = None
        for index, name in enumerate(models, 1):
            print(f"{index:3}. {name}" + (" [selected]" if name == selected else ""))
        prompt = "Enter keeps the current choice" if selected else "Enter cancels"
        answer = input(f"Choose a model number or exact ID ({prompt}): ").strip()
        if not answer:
            return selected
        value = models[int(answer) - 1] if answer.isdigit() and 1 <= int(answer) <= len(models) else answer
        if value not in models:
            raise Error("That model is not in the current provider list. Run ./devloop models to choose again.")
        with self.administration():
            self.save_model(value)
        return value

    def models(self, select=None, list_only=False):
        models = self.models_available()
        if select is not None:
            if model_id(select) not in models:
                raise Error("That model is not available from your current Nous account.")
            with self.administration():
                self.save_model(select)
        elif terminal() and not list_only:
            self.choose_model(models)
        else:
            selected = self.selected_model()
            for value in models:
                print(value + (" [selected]" if value == selected else ""))
        print("Next: ./devloop chat")

    def login(self):
        require_terminal("login")
        self.prerequisites()
        with self.administration():
            self.start_services()
            print("Open the verification URL in your browser and approve the displayed device code.", flush=True)
            self.dc("exec", "llm-gateway", "hermes", "auth", "add", "nous", "--type", "oauth", "--no-browser",
                    interactive=True)
            self.dc("restart", "llm-gateway", capture=False, timeout=60)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if self.probe("health").get("authenticated"):
                    break
                time.sleep(1)
            else:
                raise Error("Login command finished, but the gateway is not ready. Run ./devloop doctor.")
            models = self.models_available()
        print("Nous login verified through the gateway model list. No chat completion was requested.")
        if self.selected_model() not in models:
            self.choose_model(models)
        print("Next: ./devloop chat")

    def logout(self):
        self.prerequisites()
        self.runtime_check()
        with self.administration():
            self.dc("up", "-d", "--no-deps", "llm-gateway", capture=False, timeout=90)
            self.dc("exec", "-T", "llm-gateway", "hermes", "auth", "logout", "nous", timeout=30)
            # Upstream's public logout can return early for a guest login or an
            # orphaned shared store. Use its locked cleanup owners for those cases.
            self.dc("exec", "-T", "llm-gateway", "python3", "-c", LOGOUT_CLEANUP, timeout=30)
            if self.stored_login():
                raise Error("Upstream logout did not clear the login. The gateway was not reported as logged out.")
            self.dc("restart", "llm-gateway", capture=False, timeout=60)
        print("Nous login cleared locally; gateway is awaiting login. Workspace and model preference preserved.")

    def chat(self, model=None):
        require_terminal("chat")
        models = self.models_available()
        selected = model_id(model) if model else self.selected_model()
        if selected not in models:
            if model:
                raise Error("Requested model is not available from your current Nous account.")
            print("Choose an available model before starting Hermes.")
            selected = self.choose_model(models)
        if selected not in models:
            raise Error("No model selected. Run ./devloop models.")
        base = self.config()["services"]["hermes"]["environment"]["OPENAI_BASE_URL"]
        print(f"Starting interactive Hermes with {selected}.", flush=True)
        self.dc("exec", "hermes", "hermes", "chat", "--cli", "--provider", "custom", "--base-url", base,
                "--api-key", "unused", "--model", selected, interactive=True)

    def shell(self):
        require_terminal("shell")
        self.require_running("hermes")
        self.dc("exec", "hermes", "bash", interactive=True)

    def logs(self, service=None, follow=False, tail=100):
        if service is None and terminal():
            choices = ("all", *SERVICES, *OPTIONAL_SERVICES)
            for index, name in enumerate(choices, 1):
                print(f"{index}. {name}")
            answer = input("Service [1]: ").strip() or "1"
            if not answer.isdigit() or not 1 <= int(answer) <= len(choices):
                raise Error("Choose one of the listed service numbers.")
            service = choices[int(answer) - 1]
        args = ["logs", "--no-color", "--tail", str(tail)]
        if follow:
            args.append("--follow")
        if service and service != "all":
            args.append(service)
        self.runner.logs(self.compose + args)

    def doctor(self):
        failed = False
        for name, check in (("Docker and Compose", self.prerequisites), ("Subnet conflicts", self.network_check), ("Storage permissions", self.runtime_check)):
            try:
                check()
                print(f"PASS: {name}")
            except (Error, OSError) as error:
                print(f"FAIL: {name}: {redact(str(error))}")
                failed = True
        try:
            self.require_running(*SERVICES)
            health = self.probe("health")
            if not health.get("ok"):
                if not self.stored_login():
                    print("ACTION: Nous login required. Run ./devloop login.")
                else:
                    raise Error("Stored credentials exist, but the gateway does not respond.")
            elif not health.get("authenticated"):
                print("ACTION: Gateway responds, but Nous login is required. Run ./devloop login.")
            else:
                models = self.models_available()
                print(f"PASS: Gateway verified a provider model list ({len(models)} models); no inference requested.")
                selected = self.selected_model()
                if selected not in models:
                    print("ACTION: Choose a current model with ./devloop models.")
        except Error as error:
            print(f"FAIL: Gateway connectivity: {redact(str(error))}")
            failed = True
        return int(failed)

    def test(self, persistence=False, authenticated=False):
        self.prerequisites()
        self.runtime_check()
        self.require_running(*SERVICES)
        if persistence:
            print("Persistence testing will recreate Hermes and the gateway. Run during supervised downtime.", flush=True)
        args = [sys.executable, str(self.root / "tests/security.py")]
        if persistence:
            args.append("--persistence")
        if authenticated:
            args.append("--authenticated")
        with self.administration():
            self.runner.run(args, capture=False, timeout=None, label="Security suite")

    def rebuild(self):
        self.prerequisites()
        self.network_check()
        self.runtime_check()
        with self.administration():
            self.refresh_reference()
            self.dc("build", capture=False, timeout=None)
            self.dc("up", "-d", "--force-recreate", capture=False, timeout=120)
            self.dc("up", "-d", "--wait", "--wait-timeout", "60", "squid", "hermes", capture=False, timeout=90)
            if (self.control / 'github-enabled').is_file():
                self.dc('--profile', 'github', 'build', 'github-broker', capture=False, timeout=None)
                self.dc('--profile', 'github', 'up', '-d', '--force-recreate', '--wait', '--wait-timeout', '60', 'github-broker', capture=False, timeout=90)
        print("Pinned images rebuilt; services recreated. Runtime data and model preference preserved.")
        self.status()


def parser():
    result = argparse.ArgumentParser(prog="./devloop", description="Supervised Hermes DevLoop administration. Run without arguments for an interactive menu.",
                                     epilog="First use: setup → login → models → chat. Examples: ./devloop status; ./devloop logs squid --follow; ./devloop test --persistence")
    commands = result.add_subparsers(dest="action")
    for name, description in ACTIONS.items():
        action = commands.add_parser(name, help=description, description=description)
        if name == "models":
            group = action.add_mutually_exclusive_group()
            group.add_argument("--select", metavar="MODEL_ID", help="Save an available model as the default")
            group.add_argument("--list", action="store_true", dest="list_only", help="List models without prompting")
        elif name == "chat":
            action.add_argument("--model", help="Use an available model for this session only")
        elif name == "logs":
            action.add_argument("service", nargs="?", choices=("all", *SERVICES, *OPTIONAL_SERVICES))
            action.add_argument("--follow", "-f", action="store_true")
            action.add_argument("--tail", type=int, default=100, choices=range(1, 10001), metavar="N")
        elif name == "test":
            action.add_argument("--persistence", action="store_true", help="Recreate containers to check persistent storage")
            action.add_argument("--authenticated", action="store_true", help="Check the real provider model list after human login")
        elif name == "project":
            action.add_argument("--refresh", action="store_true", help="Export committed host HEAD; uncommitted and untracked files stay private")
            action.add_argument("--checkout", action="store_true", help="Fetch the reference commit into a new writable /workspace checkout; preserves existing work")
        elif name == "github":
            group = action.add_mutually_exclusive_group()
            group.add_argument("--workflows", choices=('enable', 'disable', 'status'), help="Host-only workflow publication control; no remote GitHub changes")
            group.add_argument("--setup", action="store_true", help="Guide local App ID and PEM-file import; no tokens or remote writes")
            group.add_argument("--start", action="store_true", help="Build/start the isolated broker and register its MCP endpoint in Hermes")
            group.add_argument("--check", action="store_true", help="Test real MCP reads and denial paths without creating a proposal")
            group.add_argument("--proposal-test", action="store_true", help="Create a disposable draft PR only when required review rules pass")
    return result


def dispatch(app, args, cli):
    if args.action == "help":
        cli.print_help()
        return 0
    options = vars(args).copy()
    action = options.pop("action")
    app._config = None  # Re-read human configuration between interactive menu actions.
    return getattr(app, action)(**options) or 0


def menu(app, cli):
    names = list(ACTIONS)
    while True:
        print("\nHermes DevLoop — setup → login → models → chat")
        for index, name in enumerate(names, 1):
            print(f"{index:2}. {name:8} {ACTIONS[name]}")
        answer = input("Choose an action or number (q to quit): ").strip()
        if answer.lower() in ("q", "quit", "exit", ""):
            return 0
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            answer = names[int(answer) - 1]
        if answer not in ACTIONS:
            print("Choose a listed action or q.")
            continue
        try:
            dispatch(app, cli.parse_args([answer]), cli)
        except Error as error:
            print(f"Error: {redact(str(error))}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nBack to menu.")


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.action is None and not terminal():
        cli.print_help()
        return 0
    app = DevLoop()
    try:
        return menu(app, cli) if args.action is None else dispatch(app, args, cli)
    except (Error, OSError) as error:
        print(f"Error: {redact(str(error))}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130
