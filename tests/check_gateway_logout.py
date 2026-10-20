#!/usr/bin/env python3
"""Test real upstream logout and launcher using dummy state in a disposable container.

No networks, host mounts, account credentials or provider calls. Run from the repo
root with: python3 tests/check_gateway_logout.py
"""
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.devloop import LOGOUT_CLEANUP, PROBE

PROGRAM = r'''
import http.server, json, os, subprocess, sys, threading
from pathlib import Path
from types import SimpleNamespace
from hermes_cli.auth import _save_auth_store, _load_auth_store, logout_command
from hermes_cli.auth_nous import _write_shared_nous_state, _nous_shared_store_path
from hermes_cli.proxy.adapters.nous_portal import NousPortalAdapter

cleanup = sys.argv[1]
home = Path(os.environ['HERMES_HOME'])
home.mkdir(parents=True, exist_ok=True)
Path(os.environ['HERMES_SHARED_AUTH_DIR']).mkdir(parents=True, exist_ok=True)
dummy = {'agent_key': 'dummy-not-a-provider-key', 'access_token': 'dummy-access', 'refresh_token': 'dummy-refresh'}
other = {'api_key': 'dummy-other-provider'}
for orphan in (False, True):
    providers = {'other': other}
    pool = {'other': []}
    if not orphan:
        providers['nous'] = dummy
        pool['nous'] = []
    _save_auth_store({'version': 1, 'providers': providers, 'credential_pool': pool})
    _write_shared_nous_state(dummy)
    assert _nous_shared_store_path().exists()
    if not orphan:
        assert NousPortalAdapter().is_authenticated()
    logout_command(SimpleNamespace(provider='nous'))
    exec(cleanup)
    state = _load_auth_store()
    assert 'nous' not in state.get('providers', {})
    assert 'nous' not in state.get('credential_pool', {})
    assert state['providers']['other'] == other
    assert not _nous_shared_store_path().exists()
    assert (home / 'auth.json').exists(), 'Exercise the surviving empty auth store'
    assert not NousPortalAdapter().is_authenticated()

launcher = subprocess.Popen(['/usr/local/bin/gateway-entrypoint'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    launcher.communicate(timeout=3)
    raise AssertionError('Logged-out gateway exited instead of waiting for login')
except subprocess.TimeoutExpired:
    launcher.terminate()
    output, errors = launcher.communicate(timeout=5)
    assert 'awaiting human Nous OAuth login' in output, (output, errors)
print('PASS: upstream logout, orphaned shared-store cleanup, other-provider preservation and launcher waiting state')

class Responses(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/unauthorized':
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"access_token":"dummy-must-not-escape"}')
            return
        self.send_response(200)
        self.end_headers()
        payloads = {
            '/health': b'{"status":"ok","authenticated":true}',
            '/models': b'{"data":[{"id":"nous/model-fixture"}]}',
            '/fixture': b'{"data":[],"fixture":true}',
            '/malformed': b'not JSON',
            '/oversize': b'x' * 2_000_001,
        }
        self.wfile.write(payloads[self.path])
    def log_message(self, *args):
        pass

server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Responses)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    for path, kind, expected in (
        ('/health','health',{'ok':True,'authenticated':True}),
        ('/models','models',{'ok':True,'models':['nous/model-fixture']}),
        ('/unauthorized','models',{'ok':False,'http_status':401}),
        ('/fixture','models',{'ok':False}),
        ('/malformed','models',{'ok':False}),
        ('/oversize','models',{'ok':False}),
    ):
        response = subprocess.run(['/usr/local/bin/python3','-I','-',f'http://127.0.0.1:{server.server_port}{path}',kind],
                                  input=sys.argv[2], text=True, capture_output=True, check=True, timeout=10)
        assert json.loads(response.stdout) == expected
        assert 'dummy-must-not-escape' not in response.stdout + response.stderr
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
print('PASS: isolated gateway probes validate health/models, hide error bodies and reject fixture/malformed/oversized responses')
'''

result = subprocess.run([
    "docker", "run", "--rm", "--network", "none", "--user", "1000:1000",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--read-only",
    "--memory", "512m", "--pids-limit", "64",
    "--tmpfs", "/var/lib/gateway:rw,nosuid,nodev,noexec,size=33554432,uid=1000,gid=1000,mode=0700",
    "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=33554432,uid=1000,gid=1000,mode=1777",
    "--env", "HOME=/var/lib/gateway", "--env", "HERMES_HOME=/var/lib/gateway/hermes",
    "--env", "HERMES_SHARED_AUTH_DIR=/var/lib/gateway/shared",
    "-i", "hermes-dev-loop-gateway:local", "python3", "-", LOGOUT_CLEANUP, PROBE,
], input=PROGRAM, text=True, timeout=60)
raise SystemExit(result.returncode)
