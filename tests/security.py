#!/usr/bin/env python3
"""Host-run boundary checks. Recreates Hermes only with --persistence.

Uses disposable network/HTTP/DNS fixtures, never real credentials or inference.
--authenticated additionally requests the real gateway's provider model list.
"""
import argparse
from datetime import datetime, timezone
import http.server
import json
from pathlib import Path
import socketserver
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "--project-directory", str(ROOT), "-f", str(ROOT / "compose.yaml")]
CHECKS = (ROOT / "tests/container_checks.py").read_text()
RESULTS = []


def command(args, *, data=None, timeout=60):
    result = subprocess.run(args, input=data, text=True, capture_output=True, timeout=timeout, cwd=ROOT)
    if result.returncode:
        # Do not dump gateway logs/environment, which may contain auth failures.
        raise RuntimeError(f"Command exited {result.returncode}: {' '.join(args[:5])}; {result.stderr[-1500:]}")
    return result.stdout.strip()


def inside(service, task, *args):
    output = command(COMPOSE + ["exec", "-T", service, "python3", "-", task, *map(str, args)], data=CHECKS)
    assert json.loads(output.splitlines()[-1])["result"] == "PASS", output[-1500:]


def record(name, function=None, *, status="PASS", detail=""):
    if function:
        try:
            function()
        except Exception as error:
            status, detail = "FAIL", str(error)
    result = {"name": name, "status": status, "detail": detail}
    RESULTS.append(result)
    print(json.dumps(result), flush=True)
    return status == "PASS"


def inspect_container(service):
    identifier = command(COMPOSE + ["ps", "-q", service])
    assert identifier, f"Service not running: {service}"
    return json.loads(command(["docker", "inspect", identifier]))[0]


def validate_configuration(containers):
    for name, container in containers.items():
        host = container["HostConfig"]
        assert container["State"]["Running"], name
        assert container["Config"]["User"] == "1000:1000", name
        assert host["ReadonlyRootfs"] and not host["Privileged"], name
        assert host["CapDrop"] == ["ALL"] and not host["CapAdd"], name
        assert "no-new-privileges:true" in host["SecurityOpt"], name
        assert host["Memory"] > 0 and host["NanoCpus"] > 0 and host["PidsLimit"] > 0, name
        assert not host["PortBindings"], name
        assert host["NetworkMode"] != "host" and host["PidMode"] != "host", name
        assert not any("docker.sock" in mount["Source"] for mount in container["Mounts"]), name
    hermes = containers["hermes"]
    writable = {mount["Destination"]: str(Path(mount["Source"]).resolve())
                for mount in hermes["Mounts"] if mount["Type"] == "bind" and mount["RW"]}
    expected = {
        "/home/hermes": "home", "/workspace": "workspace",
        "/home/hermes/.hermes/memories": "memory",
        "/home/hermes/.hermes/skills": "skills", "/home/hermes/tools": "tools",
    }
    assert writable == {dest: str(ROOT / "runtime/hermes" / source) for dest, source in expected.items()}, writable
    assert len([mount for mount in hermes["Mounts"] if mount["Type"] == "bind"]) == len(expected) + 1
    reference = next(mount for mount in hermes["Mounts"] if mount["Destination"] == "/reference")
    assert reference["Source"] == str(ROOT / "runtime/reference") and not reference["RW"], reference
    gateway_mounts = containers["llm-gateway"]["Mounts"]
    assert any(mount["Source"] == str(ROOT / "runtime/gateway") and mount["Destination"] == "/var/lib/gateway" for mount in gateway_mounts)
    assert not any("runtime/hermes" in mount["Source"] for mount in gateway_mounts)
    assert all(not mount["RW"] for mount in containers["squid"]["Mounts"] if mount["Type"] == "bind")
    if "github-broker" in containers:
        broker = containers["github-broker"]
        assert all(not mount["RW"] for mount in broker["Mounts"] if mount["Type"] == "bind")
        assert {mount["Destination"] for mount in broker["Mounts"] if mount["Type"] == "bind"} == {
            "/broker-secrets/github-app.pem", "/broker-secrets/github-app.json"}
        assert len(broker["NetworkSettings"]["Networks"]) == 1


def network_config(containers):
    hermes_networks = containers["hermes"]["NetworkSettings"]["Networks"]
    assert len(hermes_networks) == 2, hermes_networks
    for name in hermes_networks:
        network = json.loads(command(["docker", "network", "inspect", name]))[0]
        assert network["Internal"] and not network["EnableIPv6"], name
        assert network["Options"]["com.docker.network.bridge.gateway_mode_ipv4"] == "isolated", name
        bridge = "br-" + network["Id"][:12]
        addresses = json.loads(command(["ip", "-j", "address", "show", "dev", bridge]))
        assert all(not entry.get("addr_info") for entry in addresses), f"Host bridge has an address: {bridge}"
        peers = {entry["Name"] for entry in network["Containers"].values()}
        expected_service = "squid" if name.endswith("_development") else "llm-gateway"
        expected = {containers["hermes"]["Name"].lstrip("/"), containers[expected_service]["Name"].lstrip("/")}
        if expected_service == "squid" and "github-broker" in containers:
            expected.add(containers["github-broker"]["Name"].lstrip("/"))
        assert peers == expected, peers


class HostCanary(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"canary")

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--persistence", action="store_true")
    parser.add_argument("--authenticated", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / "runtime/security-results.json")
    args = parser.parse_args()
    args.report.write_text(json.dumps({"completed": False, "results": []}) + "\n")
    token = "devloop-check-" + uuid.uuid4().hex
    canary_name = "devloop-canary-" + uuid.uuid4().hex[:12]
    fixture_started = False
    helper_started = False
    files = [ROOT / "infrastructure" / token, ROOT / "runtime/gateway" / token]
    containers = {service: inspect_container(service) for service in ("hermes", "squid", "llm-gateway")}
    if command(COMPOSE + ["--profile", "github", "ps", "-q", "github-broker"]):
        containers["github-broker"] = inspect_container("github-broker")
    record("Container permissions, resources, ports and actual mount isolation", lambda: validate_configuration(containers))
    record("Actual internal isolated networks and unnumbered host bridges", lambda: network_config(containers))
    record("No default/IPv6 routes, raw sockets, capabilities or route changes", lambda: inside("hermes", "routes"))
    record("Squid and gateway kernel forwarding disabled", lambda: [inside(service, "peer-forwarding") for service in ("squid", "llm-gateway")])
    if "github-broker" in containers:
        record("GitHub broker has no default/IPv6 route or network-administration capabilities", lambda: inside("github-broker", "broker-network"))
        record("Broker-specific Squid policy permits GitHub and denies other destinations", lambda: inside("github-broker", "broker-egress"))
    external_network = next(name for name in containers["squid"]["NetworkSettings"]["Networks"] if name.endswith("_proxy_egress"))
    external = json.loads(command(["docker", "network", "inspect", external_network]))[0]
    host_address = external["IPAM"]["Config"][0]["Gateway"]
    squid_external = containers["squid"]["NetworkSettings"]["Networks"][external_network]["IPAddress"]
    record("Squid does not listen on its external interface", lambda: inside("squid", "direct", squid_external, 3128))
    try:
        for path in files:
            path.write_text(token)
            assert path.read_text() == token
        record("Infrastructure/config/credential files and Docker socket unavailable to Hermes",
               lambda: inside("hermes", "protected", *map(str, files)))
        record("Project reference readable, read-only and excludes host Git/config/runtime state",
               lambda: inside("hermes", "reference"))
        record("Workspace, memory, skills, tools, executable Python/Node/C and Hermes CLI",
               lambda: inside("hermes", "development", token))
        command(["docker", "run", "-d", "--name", canary_name, "--network", external_network,
                 "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                 "--read-only", "--memory", "128m", "--pids-limit", "32", "--entrypoint", "python3",
                 containers["squid"]["Image"], "-u", "-c", (ROOT / "tests/network_canary.py").read_text()])
        helper_started = True
        for attempt in range(50):
            if "READY" in command(["docker", "logs", canary_name]):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Network canary did not become ready")
        helper = json.loads(command(["docker", "inspect", canary_name]))[0]
        target = helper["NetworkSettings"]["Networks"][external_network]["IPAddress"]
        if record("External TCP and UDP/TCP DNS canaries reachable from Squid (positive control)", lambda: inside("squid", "positive", target)):
            before = command(["docker", "logs", canary_name])
            record("Hermes direct TCP internet path, alternate ports and UDP/TCP DNS blocked",
                   lambda: inside("hermes", "external-denied", target))
            if "github-broker" in containers:
                record("Broker direct TCP internet path and UDP/TCP DNS blocked with reachable canary",
                       lambda: inside("github-broker", "external-denied", target))
            after = command(["docker", "logs", canary_name])
            record("No blocked Hermes requests received by reachable external canaries",
                   lambda: require(before == after, "Canary received Hermes traffic"))
        else:
            record("Hermes direct egress canary checks", status="SKIP", detail="Positive control failed")
        record("Approved public destination via Squid with end-to-end TLS", lambda: inside("hermes", "approved"))
        public_address = command(COMPOSE + ["exec", "-T", "squid", "python3", "-c",
                                 "import socket; print(socket.getaddrinfo('pypi.org',443,socket.AF_INET,socket.SOCK_STREAM)[0][4][0])"])
        if record("Public internet TCP target reachable from Squid (positive control)",
                  lambda: inside("squid", "tcp-positive", public_address, 443)):
            record("Hermes direct public-IP internet connection blocked with proxy settings bypassed",
                   lambda: inside("hermes", "direct", public_address, 443))
        else:
            record("Direct public internet denial", status="SKIP", detail="Positive control failed")
        record("Disallowed hosts, provider bypass, IP literals, private destinations and methods denied by Squid",
               lambda: inside("hermes", "proxy-denied"))
        record("Approved hostname resolving private denied by production dst ACL (with counterfactual control)",
               lambda: command(COMPOSE + ["exec", "-T", "squid", "python3", "-"],
                               data=(ROOT / "tests/squid_private_name.py").read_text()))
        record("Embedded Docker DNS refuses external UDP/TCP queries; system DNS unavailable",
               lambda: inside("hermes", "embedded-dns"))
        with socketserver.ThreadingTCPServer(("0.0.0.0", 0), HostCanary) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                if record("Host/private-network HTTP canary reachable from Squid (positive control)",
                          lambda: inside("squid", "host-positive", host_address, port)):
                    record("Hermes cannot reach a live host/private-network service",
                           lambda: inside("hermes", "direct", host_address, port))
                else:
                    record("Host/private-network denial", status="SKIP", detail="Positive control failed")
            finally:
                server.shutdown()
                thread.join(timeout=3)
        record("Gateway direct provider TLS reachability (approved temporary egress exception)", lambda: inside("llm-gateway", "provider"))
        auth_exists = command(COMPOSE + ["exec", "-T", "llm-gateway", "python3", "-c",
                              "from pathlib import Path; print(Path('/var/lib/gateway/hermes/auth.json').exists())"])
        if auth_exists == "False":
            fixture = (ROOT / "tests/gateway_fixture.py").read_text()
            command(COMPOSE + ["exec", "-T", "llm-gateway", "python3", "-c",
                               "import sys; from pathlib import Path; Path('/tmp/devloop-gateway-fixture.py').write_text(sys.stdin.read())"], data=fixture)
            command(COMPOSE + ["exec", "-T", "-d", "llm-gateway", "/opt/hermes/.venv/bin/python", "/tmp/devloop-gateway-fixture.py"])
            fixture_started = True
            for attempt in range(30):
                ready = subprocess.run(COMPOSE + ["exec", "-T", "llm-gateway", "python3", "-c",
                                "import socket; socket.create_connection(('10.237.81.2',8645),1).close()"], capture_output=True, timeout=5)
                if ready.returncode == 0:
                    break
                time.sleep(0.2)
            record("Designated internal gateway and real upstream forwarding with fake adapter",
                   lambda: inside("hermes", "gateway", "fixture"))
        else:
            record("Designated gateway health (existing human authentication)", lambda: inside("hermes", "gateway"))
        gateway_external = next(value["IPAddress"] for name, value in containers["llm-gateway"]["NetworkSettings"]["Networks"].items() if name.endswith("_gateway_egress"))
        record("Gateway does not listen on its external interface", lambda: inside("llm-gateway", "direct", gateway_external, 8645))
        record("Hermes cannot use gateway's external interface as an alternate path", lambda: inside("hermes", "direct", gateway_external, 8645))
        if args.authenticated and not fixture_started:
            record("Real Nous model-list connectivity through gateway", lambda: inside("hermes", "authenticated"))
        else:
            record("Nous OAuth login/refresh and authenticated provider inference", status="SKIP",
                   detail="Requires human login and deliberate account validation; fixture does not verify OAuth")
        record("Gateway independent provider destination restriction", status="ACCEPTED_EXCEPTION",
               detail="Human approved direct gateway egress pending upstream PR #110083; Hermes egress remains restricted")
        if args.persistence:
            gateway_token = ROOT / "runtime/gateway" / token
            command(COMPOSE + ["up", "-d", "--no-deps", "--force-recreate", "hermes", "llm-gateway"], timeout=60)
            fixture_started = False
            record("Hermes persistent workspace/memory/skills/tools/home survive container recreation",
                   lambda: inside("hermes", "persistence", token))
            record("Gateway private storage survives container recreation",
                   lambda: require(command(COMPOSE + ["exec", "-T", "llm-gateway", "python3", "-c",
                                   f"from pathlib import Path; print(Path('/var/lib/gateway/{token}').read_text())"]) == token,
                                   "Gateway persistence mismatch"))
        else:
            record("Persistence across container recreation", status="SKIP", detail="Run with --persistence")
    finally:
        if fixture_started:
            subprocess.run(COMPOSE + ["exec", "-T", "llm-gateway", "python3", "-c",
                "import os,signal; from pathlib import Path; p=Path('/tmp/devloop-gateway-fixture.pid'); os.kill(int(p.read_text()),signal.SIGTERM) if p.exists() else None"], capture_output=True)
        if helper_started:
            subprocess.run(["docker", "rm", "-f", canary_name], capture_output=True)
        for path in files:
            path.unlink(missing_ok=True)
        # Remove only test-owned UUID files; preserve all pre-existing runtime data.
        for root in (ROOT / "runtime/hermes").iterdir():
            for path in root.rglob(token + "*"):
                if path.is_dir() and not path.is_symlink():
                    import shutil
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
    report = {
        "completed": True,
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "docker_engine": command(["docker", "version", "--format", "{{.Server.Version}}"]),
        "compose": command(["docker", "compose", "version", "--short"]),
        "images": {service: container["Image"] for service, container in containers.items()},
        "results": RESULTS,
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {args.report}", flush=True)
    return int(any(result["status"] == "FAIL" for result in RESULTS))


def require(condition, message):
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    raise SystemExit(main())
