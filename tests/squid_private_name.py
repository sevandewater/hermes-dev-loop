"""Exercise the production dst ACL with an approved hostname mapped to loopback.

Uses separate temporary Squid processes bound only to container loopback. A
counterfactual control removes that one deny rule to prove the canary is reachable
and the remaining ACLs permit the request. The live listener/policy is unchanged.
"""
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time


def main():
    count = []
    target = socket.socket()
    target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    target.bind(("127.0.0.1", 443))
    target.listen()
    target.settimeout(0.2)
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                client, _ = target.accept()
            except socket.timeout:
                continue
            count.append(1)
            client.close()

    thread = threading.Thread(target=accept)
    thread.start()
    config = Path("/etc/squid/devloop/squid.conf").read_text()
    assert config.count("http_access deny prohibited") == 1
    assert config.count("http_port 10.237.80.2:3128") == 1
    try:
        with tempfile.TemporaryDirectory(prefix="devloop-squid-") as directory:
            root = Path(directory)
            hosts = root / "hosts"
            hosts.write_text("127.0.0.1 pypi.org\n")
            for port, protected in ((3130, True), (3131, False)):
                policy = config.replace("http_port 10.237.80.2:3128", f"http_port 127.0.0.1:{port}")
                policy = policy.replace("acl hermes src 10.237.80.3/32", "acl hermes src 127.0.0.1/32")
                policy = policy.replace("pid_filename /run/squid.pid", f"pid_filename {root}/squid-{port}.pid")
                policy += f"\nhosts_file {hosts}\n"
                if not protected:
                    policy = policy.replace("http_access deny prohibited", "# Counterfactual control: private-address deny removed")
                path = root / f"squid-{port}.conf"
                path.write_text(policy)
                process = subprocess.Popen(["squid", "-N", "-f", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    for attempt in range(50):
                        assert process.poll() is None, "Temporary Squid exited"
                        try:
                            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                                break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        raise AssertionError("Temporary Squid never became ready")
                    before = len(count)
                    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                        client.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n")
                        status = int(client.recv(4096).split(b" ", 2)[1])
                    time.sleep(0.2)
                    if protected:
                        assert status == 403 and len(count) == before, (status, count)
                    else:
                        assert status == 200 and len(count) == before + 1, (status, count)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    finally:
        stop.set()
        thread.join(timeout=2)
        target.close()
    print("Approved-name private destination denied; counterfactual control reached canary")


main()
