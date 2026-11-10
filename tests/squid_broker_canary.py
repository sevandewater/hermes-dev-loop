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


def select_host(text):
    hosts = [line for line in text.splitlines() if line and not line.startswith('#')]
    if not hosts or any(not re.fullmatch(r'productionresultssa[0-9]+\.blob\.core\.windows\.net', h)
                        or len(h) > 253 or any(len(label) > 63 for label in h.split('.'))
                        for h in hosts):
        raise ValueError('Loaded storage ACL is empty or invalid; enable policy first')
    return sorted(set(hosts))[0]


def make_policy(config, root, port, *, empty, protected):
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
            raise ValueError('Unexpected production Squid policy')
    for old, new in replacements.items():
        if config.splitlines().count(old) != 1:
            raise ValueError('Unexpected production Squid policy')
        config = config.replace(old, new)
    if empty:
        config = config.replace('"/run/github-log-hosts.txt"', f'"{root}/empty-hosts"')
    if not protected:
        config = config.replace('http_access deny prohibited', '# Counterfactual: private deny removed')
    return config + f'\nhosts_file {root}/hosts\n'


@contextmanager
def listener(port=443):
    hits = []
    stop = threading.Event()
    # A separate loopback address avoids the existing pypi canary on 127.0.0.1.
    with socket.socket() as target:
        target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        target.bind(('127.0.0.2', port))
        target.listen(8)
        target.settimeout(0.05)

        def accept():
            while not stop.is_set():
                try:
                    client, _ = target.accept()
                except socket.timeout:
                    continue
                with client:
                    hits.append(1)

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


def qualify(config, host, root, hits):
    (root / 'hosts').write_text(f'127.0.0.2 {host}\n127.0.0.2 api.github.com\n')
    (root / 'empty-hosts').write_text('# deliberately empty\n')
    for empty, protected in ((False, True), (False, False), (True, False)):
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        path = root / 'squid.conf'
        path.write_text(make_policy(config, root, port, empty=empty, protected=protected))
        with running_squid(path, port):
            checks = [(host, 403 if protected or empty else 200)]
            if empty:
                checks.append(('api.github.com', 200))
            for name, expected in checks:
                before = len(hits)
                status = probe(port, name)
                time.sleep(0.15)
                if status != expected or len(hits) - before != (1 if expected == 200 else 0):
                    raise RuntimeError('Temporary broker ACL status/canary count mismatch')


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
            with listener() as (_, hits):
                qualify(config, host, Path(directory), hits)
        if LOADED.read_text() != loaded or CONFIG.read_text() != config:
            raise RuntimeError('Live policy changed during qualification; evidence invalid')
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    print('PASS broker loaded-storage private deny: 403/0 hits; counterfactual: 200/1 hit')
    print('PASS actual Squid empty ACL parse; storage: 403/0 hits; mapped API ACL control: 200/1 hit')
    print('API control removes private deny in isolation; not public TLS or live broker evidence')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        print('FAIL isolated network qualification; check running Squid and enabled loaded storage policy')
        raise SystemExit(1)
