"""Isolated broker-source Squid qualification; no live policy writes."""
from contextlib import contextmanager
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

LOADED = Path('/run/github-log-hosts.txt')
CONFIG = Path('/etc/squid/devloop/squid.conf')


class CanaryFailure(RuntimeError, ValueError):
    def __init__(self, category):
        self.category = category if category in ('bind', 'acl-mismatch', 'policy', 'input-changed') else 'runtime'
        super().__init__('Temporary broker ACL status/canary count mismatch'
                         if self.category == 'acl-mismatch' else self.category)


def select_host(text):
    hosts = [line for line in text.splitlines() if line and not line.startswith('#')]
    if not hosts or any(not re.fullmatch(r'productionresultssa[0-9]+\.blob\.core\.windows\.net', h)
                        or len(h) > 253 or any(len(label) > 63 for label in h.split('.'))
                        for h in hosts):
        raise ValueError('Loaded storage ACL is empty or invalid; enable policy first')
    return sorted(set(hosts))[0]


def make_policy(config, root, port, *, empty, protected):
    access_chain = [
        'http_access deny !hermes !github_broker',
        'http_access deny !CONNECT',
        'http_access deny !TLS_port',
        'http_access deny hermes !approved',
        'http_access deny github_broker !github_api !github_logs',
        'http_access deny prohibited',
        'http_access allow github_broker github_api',
        'http_access allow github_broker github_logs',
        'http_access allow hermes approved',
        'http_access deny all',
    ]
    actual = [line.strip() for line in config.splitlines()
              if line.strip().split()[:1] == ['http_access']]
    if actual != access_chain:
        raise CanaryFailure('policy')
    replacements = {
        'http_port 10.237.80.2:3128': f'http_port 127.0.0.1:{port}',
        'acl github_broker src 10.237.80.4/32': 'acl github_broker src 127.0.0.1/32',
        'pid_filename /run/squid.pid': f'pid_filename {root}/squid.pid',
        'cache_log /dev/stderr': f'cache_log {root}/cache.log',
        'access_log stdio:/dev/stdout devloop': 'access_log none',
    }
    for required in ('http_access deny prohibited', 'acl hermes src 10.237.80.3/32',
                     'acl github_logs dstdomain -n "/run/github-log-hosts.txt"'):
        if config.splitlines().count(required) != 1:
            raise CanaryFailure('policy')
    for old, new in replacements.items():
        if config.splitlines().count(old) != 1:
            raise CanaryFailure('policy')
        config = config.replace(old, new)
    acl_file = 'empty-hosts' if empty else 'snapshot-hosts'
    config = config.replace('"/run/github-log-hosts.txt"', f'"{root}/{acl_file}"')
    if not protected:
        config = config.replace('http_access deny prohibited', '# Counterfactual: private deny removed')
    return config + f'\nhosts_file {root}/hosts\n'


class Hits(list):
    """Probe-local counts with a bounded positive-control handshake."""

    def __init__(self):
        super().__init__()
        self.condition = threading.Condition()

    def record(self):
        with self.condition:
            self.append(1)
            self.condition.notify_all()

    def wait_for_hit(self, timeout=2):
        with self.condition:
            return self.condition.wait_for(lambda: len(self) >= 1, timeout=timeout)


@contextmanager
def listener(port=443, address='127.0.0.2'):
    hits = Hits()
    stop = threading.Event()
    # A separate loopback address avoids the existing pypi canary on 127.0.0.1.
    with socket.socket() as target:
        target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            target.bind((address, port))
        except OSError:
            raise CanaryFailure('bind') from None
        target.listen(8)
        target.settimeout(0.05)

        def accept():
            while True:
                try:
                    client, _ = target.accept()
                except socket.timeout:
                    if stop.is_set():
                        return
                    continue
                with client:
                    hits.record()

        thread = threading.Thread(target=accept)
        thread.start()
        try:
            yield target.getsockname(), hits
        finally:
            stop.set()
            thread.join(timeout=1)
            if thread.is_alive():
                raise RuntimeError('Canary thread failed to stop')


def wait_ready(process, port):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Temporary Squid exited')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError('Temporary Squid readiness deadline exceeded')


@contextmanager
def running_squid(path, port):
    # Parse the actual Squid binary, including the comment-only empty ACL.
    subprocess.run(['squid', '-k', 'parse', '-f', str(path)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=5, check=True)
    process = subprocess.Popen(['squid', '-N', '-f', str(path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_ready(process, port)
        yield
    finally:
        # Defer the outer deadline while terminating/reaping: a signal raised
        # inside wait() must not abandon a SIGTERM-resistant child. Both waits
        # stay bounded, so cleanup adds at most four seconds to the deadline.
        # A process-directed alarm can land on the listener thread, so blocking
        # SIGALRM on this thread alone would not protect Python's main thread.
        original_error = sys.exception()
        pending = []

        def defer_deadline(signum, frame):
            pending.append((signum, frame))

        previous = signal.signal(signal.SIGALRM, defer_deadline)
        try:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        finally:
            cleanup_error = sys.exception()
            signal.signal(signal.SIGALRM, previous)
            if pending and original_error is None and cleanup_error is None:
                # Deliver only after reaping; preserve any earlier failure.
                if callable(previous):
                    previous(*pending[0])
                elif previous != signal.SIG_IGN:
                    raise TimeoutError('Network canary deadline exceeded')


def probe(port, host):
    with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
        client.settimeout(2)
        client.sendall(f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n'.encode('ascii'))
        response = client.recv(4096)
    try:
        return int(response.split(b' ', 2)[1])
    except (IndexError, ValueError):
        raise RuntimeError('Invalid temporary Squid response') from None


def qualify(config, host, root):
    (root / 'empty-hosts').write_text('# deliberately empty\n')
    checks = ((False, True, host, 403), (False, False, host, 200),
              (True, False, host, 403), (True, False, 'api.github.com', 200))
    for index, (empty, protected, name, expected) in enumerate(checks):
        # A distinct loopback destination per probe: even a late connection
        # cannot contaminate the next probe. Never recycle an address here.
        address = f'127.0.0.{index + 2}'
        with listener(address=address) as (_, hits):
            (root / 'hosts').write_text(f'{address} {host}\n{address} api.github.com\n')
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            path = root / 'squid.conf'
            path.write_text(make_policy(config, root, port, empty=empty, protected=protected))
            with running_squid(path, port):
                status = probe(port, name)
                if status != expected or (expected == 200 and not hits.wait_for_hit()):
                    raise CanaryFailure('acl-mismatch')
            # First stop/reap the sole producer, then drain accepted/queued
            # connections before checking counts. No fixed post-probe sleep.
        if len(hits) != (1 if expected == 200 else 0):
            raise CanaryFailure('acl-mismatch')


def main():
    # Outer deadline also interrupts blocking calls and unwinds all cleanup paths.
    def expired(signum, frame):
        raise TimeoutError('Network canary deadline exceeded')

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(45)
    try:
        loaded = LOADED.read_text()
        config = CONFIG.read_text()
        host = select_host(loaded)  # No fixture/fallback when policy is disabled.
        with tempfile.TemporaryDirectory(prefix='devloop-broker-canary-') as directory:
            (Path(directory) / 'snapshot-hosts').write_text(loaded)
            qualify(config, host, Path(directory))
        if LOADED.read_text() != loaded or CONFIG.read_text() != config:
            raise CanaryFailure('input-changed')
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    print('PASS ISOLATED on-disk startup snapshot: broker-classified storage private deny 403/0 hits; counterfactual 200/1 hit')
    print('PASS ISOLATED actual Squid empty ACL parse; storage 403/0 hits; mapped API ACL control 200/1 hit')
    print('API control removes private deny in isolation; not public TLS or live broker evidence')
    print('On-disk inputs unchanged; production runtime correlation/enforcement pending')


def cli_main():
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        if isinstance(error, CanaryFailure):
            category = error.category
        elif isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            category = 'deadline'
        elif isinstance(error, subprocess.CalledProcessError):
            category = 'parse'
        elif isinstance(error, ValueError):
            category = 'membership'
        elif isinstance(error, OSError):
            category = 'io'
        else:
            category = 'runtime'
        print(f'FAIL ISOLATED {category}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(cli_main())
