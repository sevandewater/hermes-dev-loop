"""Credential-free fixtures; actual Squid qualification remains a host check."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ImportSafetyTests(unittest.TestCase):
    def test_import_does_not_open_listener_or_run_live_policy(self):
        with patch('socket.socket', side_effect=AssertionError('import opened listener')):
            module = load('private_name', 'tests/squid_private_name.py')
        self.assertTrue(callable(module.main))


class BrokerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / 'tests/squid_broker_canary.py').is_file(),
                        'Missing broker-source active-policy canary')
        self.canary = load('broker_canary', 'tests/squid_broker_canary.py')

    def test_select_requires_real_exact_numbered_member(self):
        host = 'productionresultssa7.blob.core.windows.net'
        self.assertEqual(self.canary.select_host('# comment\n' + host + '\n'), host)
        for text in ('# empty\n', '.blob.core.windows.net', host.upper(),
                     host + '.evil', 'productionresultssa.blob.core.windows.net',
                     'productionresultssa' + '1' * 50 + '.blob.core.windows.net'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.canary.select_host(text)

    def test_policy_classifies_broker_not_hermes_and_changes_only_isolated_inputs(self):
        config = (ROOT / 'infrastructure/squid/squid.conf').read_text()
        policy = self.canary.make_policy(config, Path('/tmp/canary'), 12345,
                                         empty=False, protected=True)
        self.assertIn('acl github_broker src 127.0.0.1/32', policy)
        self.assertIn('acl hermes src 10.237.80.3/32', policy)
        self.assertIn('http_access deny prohibited', policy)
        self.assertIn('http_port 127.0.0.1:12345', policy)
        self.assertIn('"/tmp/canary/snapshot-hosts"', policy)
        control = self.canary.make_policy(config, Path('/tmp/canary'), 12345,
                                          empty=False, protected=False)
        self.assertEqual(control, policy.replace('http_access deny prohibited',
                         '# Counterfactual: private deny removed'))
        empty = self.canary.make_policy(config, Path('/tmp/canary'), 12345,
                                        empty=True, protected=False)
        self.assertIn('"/tmp/canary/empty-hosts"', empty)
        self.assertNotIn('"/run/github-log-hosts.txt"', empty)
        self.assertEqual((ROOT / 'infrastructure/squid/squid.conf').read_text(), config)

    def test_policy_rejects_reordered_chain_and_extra_permissive_rules(self):
        config = (ROOT / 'infrastructure/squid/squid.conf').read_text()
        mutations = (
            config.replace('http_access deny prohibited\n', '').replace(
                'http_access deny all', 'http_access deny prohibited\nhttp_access deny all'),
            config.replace('http_access deny prohibited',
                           'http_access allow all\nhttp_access deny prohibited'),
            config + '\nhttp_access allow all\n',
        )
        for changed in mutations:
            with self.subTest(policy=mutations.index(changed)), self.assertRaises(ValueError):
                self.canary.make_policy(changed, Path('/tmp/canary'), 12345,
                                        empty=False, protected=True)

    def test_policy_drift_rejected_instead_of_silent_substitution(self):
        config = (ROOT / 'infrastructure/squid/squid.conf').read_text()
        with self.assertRaises(ValueError):
            self.canary.make_policy(config.replace('http_access deny prohibited', ''),
                                    Path('/tmp/canary'), 12345, empty=False, protected=True)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.canary = load('broker_canary', 'tests/squid_broker_canary.py')

    def test_pass_labels_are_isolated_snapshot_not_production_enforcement(self):
        import io
        import tempfile
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as directory:
            loaded = Path(directory) / 'loaded'
            config = Path(directory) / 'config'
            loaded.write_text('productionresultssa7.blob.core.windows.net\n')
            config.write_text('snapshot')
            with patch.object(self.canary, 'LOADED', loaded), \
                    patch.object(self.canary, 'CONFIG', config), \
                    patch.object(self.canary, 'listener') as target, \
                    patch.object(self.canary, 'qualify'), redirect_stdout(io.StringIO()) as output:
                target.return_value.__enter__.return_value = (('127.0.0.2', 443), [])
                self.canary.main()
        lines = output.getvalue().splitlines()
        self.assertTrue(all(line.startswith('PASS ISOLATED ') for line in lines if line.startswith('PASS')))
        self.assertIn('production runtime correlation/enforcement pending', output.getvalue())

    def test_fixed_failure_categories_do_not_disclose_exception_details(self):
        import io
        import subprocess
        from contextlib import redirect_stdout
        self.assertTrue(callable(getattr(self.canary, 'cli_main', None)),
                        'Missing sanitized category boundary')
        failures = (
            (ValueError('secret membership'), 'membership'),
            (subprocess.CalledProcessError(1, 'secret parse'), 'parse'),
            (TimeoutError('secret deadline'), 'deadline'),
            (self.canary.CanaryFailure('bind'), 'bind'),
            (self.canary.CanaryFailure('acl-mismatch'), 'acl-mismatch'),
        )
        for error, category in failures:
            with self.subTest(category=category), \
                    patch.object(self.canary, 'main', side_effect=error), \
                    redirect_stdout(io.StringIO()) as output:
                self.assertEqual(self.canary.cli_main(), 1)
            self.assertEqual(output.getvalue(), f'FAIL ISOLATED {category}\n')

    def test_new_execution_helpers_present(self):
        for name in ('listener', 'running_squid', 'qualify', 'main'):
            self.assertTrue(callable(getattr(self.canary, name, None)), name)

    def test_listener_counts_real_connections_and_closes_on_failure(self):
        import socket
        import time
        self.assertTrue(hasattr(self.canary, 'listener'), 'Missing bounded listener')
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            with self.canary.listener(port=0) as target:
                address, hits = target
                with socket.create_connection(address, timeout=1):
                    pass
                for _ in range(50):
                    if len(hits) == 1:
                        break
                    time.sleep(0.01)
                self.assertEqual(len(hits), 1)
                raise RuntimeError('injected')
        with self.assertRaises(OSError):
            socket.create_connection(address, timeout=0.1)

    def test_listener_rebinds_immediately_after_server_active_close(self):
        import socket

        with self.canary.listener(port=0) as (address, hits):
            with socket.create_connection(address, timeout=1) as client:
                # The server must close first: receiving EOF before this client
                # closes puts the listener's local address into TIME_WAIT.
                self.assertEqual(client.recv(1), b'')
            self.assertEqual(hits, [1])
        with self.canary.listener(port=address[1]) as (rebound, hits):
            self.assertEqual(rebound, address)
            with socket.create_connection(rebound, timeout=1) as client:
                self.assertEqual(client.recv(1), b'')
            self.assertEqual(hits, [1])

    def test_squid_cleanup_kills_after_terminate_timeout_even_on_readiness_failure(self):
        import subprocess
        import tempfile
        from unittest.mock import Mock
        self.assertTrue(hasattr(self.canary, 'running_squid'), 'Missing process cleanup')
        child = Mock()
        child.wait.side_effect = [subprocess.TimeoutExpired('squid', 2), 0]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.canary.subprocess, 'run') as parse, \
                patch.object(self.canary.subprocess, 'Popen', return_value=child), \
                patch.object(self.canary, 'wait_ready', side_effect=RuntimeError('startup')):
            with self.assertRaisesRegex(RuntimeError, 'startup'):
                with self.canary.running_squid(Path(directory) / 'squid.conf', 1234):
                    self.fail('startup failure ignored')
            self.assertEqual(parse.call_args.kwargs['timeout'], 5)
            self.assertTrue(parse.call_args.kwargs['check'])
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)

    def test_deadline_during_cleanup_kills_and_reaps_real_term_resistant_child(self):
        self.check_deadline_during_cleanup()

    def test_deadline_during_cleanup_preserves_original_failure(self):
        self.check_deadline_during_cleanup(RuntimeError('original probe failure'))

    def check_deadline_during_cleanup(self, original_error=None):
        import signal
        import subprocess
        import sys
        import time

        # This is a real process, not a mock wait/kill sequence. The handshake
        # ensures SIGTERM is ignored before the helper takes ownership.
        child = subprocess.Popen(
            [sys.executable, '-c',
             'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); '
             'print("ready", flush=True); time.sleep(30)'],
            stdout=subprocess.PIPE, text=True)
        deadline = TimeoutError('injected cleanup deadline')

        def expired(signum, frame):
            raise deadline

        previous = signal.signal(signal.SIGALRM, expired)
        try:
            self.assertEqual(child.stdout.readline(), 'ready\n')
            child.stdout.close()
            started = time.monotonic()
            with self.canary.listener(port=0), \
                    patch.object(self.canary.subprocess, 'run'), \
                    patch.object(self.canary.subprocess, 'Popen', return_value=child), \
                    patch.object(self.canary, 'wait_ready'):
                with self.assertRaises(type(original_error or deadline)) as caught:
                    with self.canary.running_squid(Path('unused.conf'), 1234):
                        signal.setitimer(signal.ITIMER_REAL, 0.1)
                        if original_error is not None:
                            raise original_error
            self.assertIs(caught.exception, original_error or deadline)
            self.assertEqual(child.returncode, -signal.SIGKILL,
                             'deadline escaped before the child was killed and reaped')
            self.assertLess(time.monotonic() - started, 5, 'cleanup exceeded its budget')
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            child.stdout.close()

    def test_main_uses_loaded_membership_and_removes_files_on_failure(self):
        import tempfile
        from contextlib import contextmanager
        self.assertTrue(hasattr(self.canary, 'main'))
        with tempfile.TemporaryDirectory() as directory:
            loaded = Path(directory) / 'loaded'
            config = Path(directory) / 'production'
            host = 'productionresultssa42.blob.core.windows.net'
            loaded.write_text(host + '\n')
            config.write_text('original')
            seen = []

            @contextmanager
            def target():
                yield ('127.0.0.2', 443), []

            def fail(policy, chosen, root):
                self.assertEqual(chosen, host)
                seen.append(root)
                (root / 'evidence').write_text('temporary')
                raise RuntimeError('injected')

            with patch.object(self.canary, 'LOADED', loaded), \
                    patch.object(self.canary, 'CONFIG', config), \
                    patch.object(self.canary, 'listener', target), \
                    patch.object(self.canary, 'qualify', fail):
                with self.assertRaisesRegex(RuntimeError, 'injected'):
                    self.canary.main()
            self.assertFalse(seen[0].exists())
            self.assertEqual(loaded.read_text(), host + '\n')
            self.assertEqual(config.read_text(), 'original')
            loaded.write_text('# disabled\n')
            with patch.object(self.canary, 'LOADED', loaded), \
                    patch.object(self.canary, 'CONFIG', config), \
                    patch.object(self.canary, 'listener') as listener:
                with self.assertRaises(ValueError):
                    self.canary.main()
                listener.assert_not_called()

    def test_delayed_hits_wait_for_event_and_use_probe_local_listeners(self):
        import socket
        import tempfile
        import threading
        from contextlib import contextmanager
        addresses = []
        workers = []
        probes = []

        @contextmanager
        def squid(path, port):
            root = path.parent
            address = (root.joinpath('hosts').read_text().split()[0], 443)
            addresses.append(address)
            try:
                yield
            finally:
                for worker in workers:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())

        def probe(port, host):
            probes.append(host)
            if len(probes) in (2, 4):
                address = addresses[-1]

                def delayed():
                    threading.Event().wait(0.3)
                    try:
                        with socket.create_connection(address, timeout=1) as client:
                            client.recv(1)
                    except OSError:
                        pass  # Legacy implementation has no per-probe listener.

                worker = threading.Thread(target=delayed)
                workers.append(worker)
                worker.start()
                return 200
            return 403

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.canary, 'running_squid', squid), \
                patch.object(self.canary, 'probe', probe):
            self.canary.qualify((ROOT / 'infrastructure/squid/squid.conf').read_text(),
                                'productionresultssa7.blob.core.windows.net', Path(directory))
        self.assertEqual(len(addresses), 4)
        self.assertEqual(len(set(addresses)), 4)

    def test_late_denied_connection_during_producer_shutdown_is_not_missed(self):
        import socket
        import tempfile
        from contextlib import contextmanager

        @contextmanager
        def squid(path, port):
            yield
            address = (path.parent.joinpath('hosts').read_text().split()[0], 443)
            # Arrives after the 403 probe returns, before producer reap finishes.
            with socket.create_connection(address, timeout=1) as client:
                self.assertEqual(client.recv(1), b'')

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.canary, 'running_squid', squid), \
                patch.object(self.canary, 'probe', return_value=403):
            with self.assertRaisesRegex(RuntimeError, 'status/canary count'):
                self.canary.qualify((ROOT / 'infrastructure/squid/squid.conf').read_text(),
                                    'productionresultssa7.blob.core.windows.net', Path(directory))

    def test_closed_probe_address_cannot_charge_next_listener(self):
        import socket
        with self.canary.listener(port=0, address='127.0.0.2') as (old, _):
            pass
        with self.canary.listener(port=old[1], address='127.0.0.3') as (current, hits):
            with self.assertRaises(OSError):
                socket.create_connection(old, timeout=0.1)
            with socket.create_connection(current, timeout=1) as client:
                self.assertEqual(client.recv(1), b'')
        self.assertEqual(hits, [1])

    def test_qualify_rejects_denial_without_reachable_control(self):
        import tempfile
        from contextlib import contextmanager
        self.assertTrue(hasattr(self.canary, 'qualify'))

        @contextmanager
        def squid(path, port):
            yield

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.canary, 'running_squid', squid), \
                patch.object(self.canary, 'probe', return_value=403):
            with self.assertRaisesRegex(RuntimeError, 'status/canary count'):
                self.canary.qualify((ROOT / 'infrastructure/squid/squid.conf').read_text(),
                                    'productionresultssa7.blob.core.windows.net', Path(directory))

    def test_qualify_empty_policy_has_reachable_api_control(self):
        import tempfile
        from contextlib import contextmanager
        self.assertTrue(hasattr(self.canary, 'qualify'), 'Missing empty policy qualification')
        config = (ROOT / 'infrastructure/squid/squid.conf').read_text()
        policies = []
        probes = []
        addresses = []

        @contextmanager
        def squid(path, port):
            policies.append(path.read_text())
            addresses.append((path.parent.joinpath('hosts').read_text().split()[0], 443))
            yield

        def probe(port, host):
            probes.append(host)
            if len(probes) in (2, 4):
                import socket
                with socket.create_connection(addresses[-1], timeout=1) as client:
                    self.assertEqual(client.recv(1), b'')
                return 200
            return 403

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.canary, 'running_squid', squid), \
                patch.object(self.canary, 'probe', probe):
            root = Path(directory)
            self.canary.qualify(config, 'productionresultssa7.blob.core.windows.net', root)
            self.assertEqual((root / 'empty-hosts').read_text(), '# deliberately empty\n')
            self.assertIn('127.0.0.5 api.github.com', (root / 'hosts').read_text())
        self.assertEqual(probes, ['productionresultssa7.blob.core.windows.net'] * 3 + ['api.github.com'])
        self.assertEqual(len(policies), 4)
        self.assertIn('http_access deny prohibited', policies[0])
        self.assertNotIn('http_access deny prohibited', policies[1])
        self.assertIn('/empty-hosts', policies[2])


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / 'scripts/check_network_canary.py'
        self.assertTrue(self.path.is_file(), 'Missing one-command host wrapper')
        self.wrapper = load('network_wrapper', 'scripts/check_network_canary.py')

    def test_only_exec_running_squid_with_source_stdin(self):
        import subprocess
        with patch.object(self.wrapper.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(self.wrapper.main([]), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['docker', 'compose', '--project-directory', str(ROOT),
                                  '-f', str(ROOT / 'compose.yaml'), 'exec', '-T',
                                  'squid', 'python3', '-'])
        self.assertEqual(kwargs['input'], (ROOT / 'tests/squid_broker_canary.py').read_text())
        self.assertEqual(kwargs['timeout'], 60)
        self.assertNotIn('env', kwargs)
        self.assertNotIn('shell', kwargs)

    def test_command_failures_are_nonzero_and_sanitized(self):
        import io
        import subprocess
        from contextlib import redirect_stderr
        for error in (OSError('private detail'), subprocess.TimeoutExpired('private detail', 60),
                      subprocess.CalledProcessError(1, 'private detail')):
            with patch.object(self.wrapper.subprocess, 'run', side_effect=error), \
                    redirect_stderr(io.StringIO()) as output:
                self.assertEqual(self.wrapper.main([]), 1)
            self.assertNotIn('private detail', output.getvalue())
            category = ('command-deadline' if isinstance(error, subprocess.TimeoutExpired)
                        else 'command-unavailable' if isinstance(error, OSError)
                        else 'command-failed')
            self.assertEqual(output.getvalue(), f'FAIL ISOLATED {category}\n')

    def test_unknown_arguments_rejected_without_docker(self):
        import io
        from contextlib import redirect_stderr
        with patch.object(self.wrapper.subprocess, 'run') as run, \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.wrapper.main(['--enable'])
        run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
