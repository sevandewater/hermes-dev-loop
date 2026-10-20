"""Injected over stdin by the human-run test harness; no test mount into Hermes."""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import urllib.error
import urllib.request


def dns(address, protocol, name="pypi.org"):
    query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    query += b"".join(bytes([len(label)]) + label.encode() for label in name.split("."))
    query += b"\x00\x00\x01\x00\x01"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM) as client:
        client.settimeout(2)
        if protocol == "udp":
            client.sendto(query, (address, 53))
            response = client.recv(4096)
        else:
            client.connect((address, 53))
            client.sendall(struct.pack("!H", len(query)) + query)
            size = int.from_bytes(client.recv(2), "big")
            response = client.recv(size)
        assert response[:2] == query[:2], "DNS transaction mismatch"
        return response[3] & 15


def connect(address, port):
    with socket.create_connection((address, int(port)), timeout=2) as client:
        client.sendall(b"GET / HTTP/1.0\r\n\r\n")
        response = b""
        while len(response) < 4096:
            chunk = client.recv(4096 - len(response))
            if not chunk:
                break
            response += chunk
        return response.decode()


def denied_connect(address, port):
    try:
        client = socket.create_connection((address, int(port)), timeout=2)
    except OSError:
        return
    client.close()
    raise AssertionError(f"Unexpected direct connectivity: {address}:{port}")


def proxy_status(authority, method="CONNECT", headers=""):
    with socket.create_connection(("10.237.80.2", 3128), timeout=5) as client:
        client.sendall(f"{method} {authority} HTTP/1.1\r\nHost: {authority}\r\n{headers}\r\n".encode())
        return int(client.recv(4096).split(b" ", 2)[1])


def main():
    task, *args = sys.argv[1:]
    if task == "positive":
        assert "canary" in connect(args[0], 443)
        assert dns(args[0], "udp") == 0
        assert dns(args[0], "tcp") == 0
    elif task == "host-positive":
        assert "canary" in connect(args[0], int(args[1]))
    elif task == "tcp-positive":
        with socket.create_connection((args[0], int(args[1])), timeout=5):
            pass
    elif task == "external-denied":
        for port in (53, 80, 443):
            denied_connect(args[0], port)
        try:
            dns(args[0], "udp")
        except OSError:
            pass
        else:
            raise AssertionError("Direct UDP DNS egress succeeded")
    elif task == "embedded-dns":
        for protocol in ("udp", "tcp"):
            assert dns("127.0.0.11", protocol) == 2, "Embedded DNS forwarded an external query"
        try:
            socket.getaddrinfo("pypi.org", 443)
        except socket.gaierror:
            pass
        else:
            raise AssertionError("System resolver resolves external names")
    elif task == "routes":
        routes = json.loads(subprocess.check_output(["ip", "-j", "route"]))
        assert routes and all(route.get("dst") != "default" for route in routes), routes
        assert not json.loads(subprocess.check_output(["ip", "-j", "-6", "route"])), "IPv6 routes exist"
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            try:
                client.connect(("2606:4700:4700::1111", 443))
            except OSError:
                pass
            else:
                raise AssertionError("Direct IPv6 connection succeeded")
        assert Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() == "0"
        status = Path("/proc/self/status").read_text()
        assert "CapEff:\t0000000000000000" in status
        assert "NoNewPrivs:\t1" in status
        assert "Seccomp:\t2" in status
        try:
            socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        except PermissionError:
            pass
        else:
            raise AssertionError("Raw sockets available")
        result = subprocess.run(["ip", "route", "add", "default", "via", "10.237.80.2"], capture_output=True)
        assert result.returncode != 0, "Agent can change routes"
    elif task == "direct":
        denied_connect(args[0], args[1])
    elif task == "peer-forwarding":
        assert Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() == "0"
    elif task == "approved":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": "http://10.237.80.2:3128"}))
        with opener.open("https://pypi.org/simple/pip/", timeout=20) as response:
            assert response.status == 200
            assert b"pip" in response.read(4096).lower()
    elif task == "proxy-denied":
        targets = ["example.com:443", "inference-api.nousresearch.com:443", "1.1.1.1:443", "pypi.org:80", "pypi.org:22"]
        targets += [f"{ip}:443" for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "172.16.0.1", "192.168.1.1", "[::1]")]
        for target in targets:
            assert proxy_status(target) == 403, target
        assert proxy_status("http://pypi.org/", "GET") == 403
        assert proxy_status("example.com:443", headers="X-Forwarded-For: 10.237.80.3\r\n") == 403
    elif task == "protected":
        for name in ("/var/run/docker.sock", "/run/docker.sock", "/var/lib/gateway", "/etc/squid/devloop", *args):
            assert not Path(name).exists(), name
        for name in ("/compose.yaml", "/infrastructure-canary", "/opt/hermes/devloop-write-canary"):
            try:
                Path(name).write_text("must fail")
            except OSError:
                pass
            else:
                raise AssertionError(f"Protected write succeeded: {name}")
    elif task == "development":
        token = args[0]
        roots = ("/workspace", "/home/hermes/.hermes/memories", "/home/hermes/.hermes/skills", "/home/hermes/tools", "/home/hermes")
        for root in roots:
            Path(root, token).write_text(token)
        tool = Path("/home/hermes/tools", token + ".py")
        tool.write_text("#!/usr/bin/env python3\nprint('development-tool-ok')\n")
        tool.chmod(0o700)
        assert subprocess.check_output([str(tool)], text=True).strip() == "development-tool-ok"
        assert subprocess.check_output(["node", "-e", "console.log('node-ok')"], text=True).strip() == "node-ok"
        source = Path("/workspace", token + ".c")
        binary = source.with_suffix(".bin")
        source.write_text('int main(void) { return 0; }\n')
        subprocess.run(["cc", str(source), "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True)
        subprocess.run(["python3", "-m", "venv", "/workspace/" + token + "-venv"], check=True)
        assert subprocess.check_output(["/workspace/" + token + "-venv/bin/pip", "--version"], text=True).startswith("pip ")
        subprocess.run(["hermes", "--help"], check=True, stdout=subprocess.DEVNULL)
    elif task == "persistence":
        token = args[0]
        for root in ("/workspace", "/home/hermes/.hermes/memories", "/home/hermes/.hermes/skills", "/home/hermes/tools", "/home/hermes"):
            assert Path(root, token).read_text() == token
        assert subprocess.check_output(["/home/hermes/tools/" + token + ".py"], text=True).strip() == "development-tool-ok"
    elif task == "gateway":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://10.237.81.2:8645/health", timeout=5) as response:
            result = json.load(response)
            assert result["status"] == "ok"
        if args and args[0] == "fixture":
            request = urllib.request.Request("http://10.237.81.2:8645/v1/models", headers={"Authorization": "Bearer dummy-client"})
            with opener.open(request, timeout=5) as response:
                data = json.load(response)
                assert data["fixture"] and data["credential_attached"], data
    elif task == "provider":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # No auth or inference. Establish only direct TLS/provider reachability.
        try:
            with opener.open("https://inference-api.nousresearch.com/v1/models", timeout=20) as response:
                assert response.status == 200
        except urllib.error.HTTPError as error:
            assert error.code in (401, 403), error.code
    elif task == "authenticated":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request("http://10.237.81.2:8645/v1/models", headers={"Authorization": "Bearer unused"})
        with opener.open(request, timeout=30) as response:
            data = json.load(response)
            assert isinstance(data.get("data"), list) and not data.get("fixture"), "Not a real provider model list"
    else:
        raise AssertionError(f"Unknown task: {task}")
    print(json.dumps({"task": task, "result": "PASS"}))


main()
