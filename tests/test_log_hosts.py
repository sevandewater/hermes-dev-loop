"""Host metadata policy tests; no credentials, Docker or live endpoints."""
import json
import unittest

from unittest.mock import patch
import subprocess
import sys

import tempfile
from pathlib import Path
import contextlib
import io

from scripts import devloop

HOST1 = 'productionresultssa1.blob.core.windows.net'
HOST2 = 'productionresultssa2.blob.core.windows.net'


def metadata(hosts):
    return json.dumps({'domains': {'actions_inbound': {'full_domains': hosts}}}).encode()


class MetadataTests(unittest.TestCase):
    def test_selects_only_full_exact_matches_sorted_and_deduplicated(self):
        self.assertTrue(hasattr(devloop, 'select_log_hosts'), 'host metadata selection is missing')
        self.assertEqual(devloop.select_log_hosts(metadata([
            HOST2, HOST1, HOST2, '*.blob.core.windows.net',
            '.' + HOST1, HOST1 + '.evil', HOST1.upper(),
            'https://' + HOST1, 'productionresultssa.blob.core.windows.net'])), [HOST1, HOST2])


    def test_rejects_malformed_empty_and_unsupported_shapes(self):
        for payload in (b'bad', b'[]', b'{}', b'{"domains": []}',
                        metadata([]), metadata(['example.com']), metadata([HOST1, 3]),
                        b'{"domains":{"actions_inbound":{"full_domains":"' + HOST1.encode() + b'"}}}'):
            with self.subTest(payload=payload), self.assertRaises(devloop.Error):
                devloop.select_log_hosts(payload)


    def test_fetch_is_isolated_bounded_and_does_not_forward_credentials(self):
        self.assertTrue(hasattr(devloop, 'fetch_log_hosts'), 'bounded metadata fetch is missing')
        with patch('scripts.devloop.subprocess.run', return_value=subprocess.CompletedProcess([], 0, metadata([HOST1]))) as run:
            self.assertEqual(devloop.fetch_log_hosts(), [HOST1])
        args, options = run.call_args
        self.assertEqual(args[0][:3], [sys.executable, '-I', '-c'])
        self.assertEqual(options['timeout'], 12)
        self.assertTrue(options['capture_output'])
        self.assertNotIn('github_app', args[0][-1])
        for response in (subprocess.TimeoutExpired('fetch', 12), OSError('offline')):
            with patch('scripts.devloop.subprocess.run', side_effect=response), self.assertRaises(devloop.Error):
                devloop.fetch_log_hosts()


    def test_fetch_worker_rejects_redirects_sizes_encoding_and_slow_reads(self):
        self.assertTrue(hasattr(devloop, 'METADATA_FETCH'))
        from unittest.mock import MagicMock
        import ssl
        import urllib.request
        for status, encoding, body in ((200, 'identity', metadata([HOST1])),
                                       (302, 'identity', b''),
                                       (200, 'gzip', b'bad'),
                                       (200, 'identity', b'x' * 2_000_001)):
            response = MagicMock()
            response.__enter__.return_value = response
            response.status = status
            response.headers = {'Content-Encoding': encoding}
            response.read.return_value = body
            opener = MagicMock()
            opener.open.return_value = response
            output = MagicMock()
            with patch('urllib.request.build_opener', return_value=opener) as build, \
                 patch('sys.stdout', output):
                if status == 200 and encoding == 'identity' and len(body) < 2_000_001:
                    exec(devloop.METADATA_FETCH, {})
                    output.buffer.write.assert_called_once_with(body)
                else:
                    with self.assertRaises(SystemExit):
                        exec(devloop.METADATA_FETCH, {})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.github.com/meta')
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 5)
            self.assertFalse({'authorization', 'cookie'} & {k.lower() for k in request.headers})
            handlers = build.call_args.args
            self.assertIsInstance(handlers[0], urllib.request.ProxyHandler)
            self.assertIsNone(handlers[1].redirect_request(None, None, None, None, None, None))
            self.assertEqual(handlers[2]._context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(handlers[2]._context.check_hostname)
            if status == 200 and encoding == 'identity':
                response.read.assert_called_once_with(2_000_001)
        with patch('scripts.devloop.subprocess.run', return_value=subprocess.CompletedProcess([], 1, b'')), self.assertRaises(devloop.Error):
            devloop.fetch_log_hosts()


    def test_overall_timeout_kills_and_reaps_a_real_slow_worker(self):
        import time
        real_run = subprocess.run
        def short_budget(args, **options):
            return real_run(args, **{**options, 'timeout': 0.05})
        started = time.monotonic()
        with patch('scripts.devloop.METADATA_FETCH', 'import time; time.sleep(30)'), \
             patch('scripts.devloop.subprocess.run', side_effect=short_budget), self.assertRaises(devloop.Error):
            devloop.fetch_log_hosts()
        self.assertLess(time.monotonic() - started, 3)


class HostPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = devloop.DevLoop(Path(self.directory.name))
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def test_enable_saves_one_snapshot_and_configures_both_layers(self):
        self.assertTrue(hasattr(self.app, 'log_hosts'), 'host log-policy controls are missing')
        with patch('scripts.devloop.fetch_log_hosts', return_value=[HOST1, HOST2]), \
             patch.object(self.app, 'apply_log_hosts', create=True) as apply:
            self.app.log_hosts('enable')
        settings = self.app.host_settings()
        self.assertIs(settings['log_hosts_enabled'], True)
        self.assertEqual(settings['log_snapshot']['hosts'], [HOST1, HOST2])
        self.assertGreater(settings['log_snapshot']['fetched_at'], 0)
        self.assertEqual(self.app.effective_log_hosts(), [HOST1, HOST2])
        apply.assert_called_once()


    def test_refresh_failure_keeps_lkg_warns_age_and_does_not_reenable_disabled(self):
        self.app.save_settings({'log_hosts_enabled': True,
                                'log_snapshot': {'hosts': [HOST1], 'fetched_at': 1}})
        with patch('scripts.devloop.fetch_log_hosts', side_effect=devloop.Error('offline')), \
             patch.object(self.app, 'apply_log_hosts', create=True):
            self.app.log_hosts('refresh')
        self.assertEqual(self.app.effective_log_hosts(), [HOST1])
        self.assertEqual(self.app.host_settings()['log_snapshot']['fetched_at'], 1)
        self.assertIn('age', self.output.getvalue())
        self.assertIn('failed', self.app.host_settings()['log_fetch'])
        with patch('scripts.devloop.fetch_log_hosts') as fetch, \
             patch.object(self.app, 'apply_log_hosts', create=True):
            self.app.log_hosts('disable')
            with self.assertRaisesRegex(devloop.Error, 'enable'):
                self.app.log_hosts('refresh')
        fetch.assert_not_called()
        self.assertEqual(self.app.effective_log_hosts(), [])
        self.assertEqual(self.app.host_settings()['log_snapshot']['hosts'], [HOST1])


    def test_invalid_dns_hosts_preserve_lkg_and_are_denied_in_cache(self):
        for digits in (300, 64 - len('productionresultssa')):
            host = 'productionresultssa' + '1' * digits + '.blob.core.windows.net'
            with self.subTest(digits=digits):
                self.app.save_settings({'log_hosts_enabled': True,
                                        'log_snapshot': {'hosts': [HOST1], 'fetched_at': 1}})
                with patch('scripts.devloop.subprocess.run', return_value=
                           subprocess.CompletedProcess([], 0, metadata([HOST1, host]))):
                    self.app.refresh_log_hosts()
                self.assertEqual(self.app.effective_log_hosts(), [HOST1])
                self.assertEqual(self.app.host_settings()['log_snapshot']['fetched_at'], 1)
                self.assertIn('failed', self.app.host_settings()['log_fetch'])
                self.app.save_settings({'log_snapshot': {'hosts': [host], 'fetched_at': 1}})
                self.assertEqual(self.app.effective_log_hosts(), [])
        longest = 'productionresultssa' + '1' * (63 - len('productionresultssa')) + '.blob.core.windows.net'
        self.assertEqual(devloop.select_log_hosts(metadata([longest])), [longest])
        self.app.save_settings({'log_snapshot': {'hosts': [longest], 'fetched_at': 1}})
        self.assertEqual(self.app.effective_log_hosts(), [longest])

    def test_oversized_host_set_startup_keeps_lkg_or_denies(self):
        from test_devloop import DockerDouble
        hosts = sorted(f'productionresultssa{i}.blob.core.windows.net' for i in range(4000))
        payload = metadata(hosts)
        self.assertGreater(len(','.join(hosts).encode()), 131_072)
        self.assertLess(len(payload), 2_000_000)
        for cached in (False, True):
            with self.subTest(cached=cached):
                runner = DockerDouble()
                runner.running = False
                self.app.runner = runner
                snapshot = {'hosts': [HOST1], 'fetched_at': 1} if cached else None
                self.app.save_settings({'log_hosts_enabled': True, 'log_snapshot': snapshot})
                with patch('scripts.devloop.subprocess.run', return_value=
                           subprocess.CompletedProcess([], 0, payload)):
                    self.app.prepare_log_start()
                self.assertEqual(self.app.host_settings()['log_snapshot'], snapshot)
                self.assertEqual(self.app.effective_log_hosts(), [HOST1] if cached else [])
                self.assertIn('failed', self.app.host_settings()['log_fetch'])
                self.assertTrue(runner.calls)
                for _, options in runner.calls:
                    self.assertLessEqual(len(options['env']['BROKER_LOG_HOSTS'].encode()), 65_536)

    def test_oversized_cache_is_denied_before_every_compose_call(self):
        from test_devloop import DockerDouble
        hosts = sorted(f'productionresultssa{i}.blob.core.windows.net' for i in range(4000))
        self.app.save_settings({'log_hosts_enabled': True,
                                'log_snapshot': {'hosts': hosts, 'fetched_at': 1}})
        self.app.runner = DockerDouble()
        self.assertEqual(self.app.effective_log_hosts(), [])
        self.app.dc('config')
        self.assertEqual(self.app.runner.calls[-1][1]['env']['BROKER_LOG_HOSTS'], '')

    def test_invalid_cache_is_denied_without_losing_opt_in(self):
        for snapshot in ({'hosts': ['evil.test'], 'fetched_at': 1},
                         {'hosts': [HOST1], 'fetched_at': 'yesterday'},
                         {'hosts': [HOST2, HOST1], 'fetched_at': 1}):
            self.app.save_settings({'log_hosts_enabled': True, 'log_snapshot': snapshot})
            self.assertEqual(self.app.effective_log_hosts(), [])
        self.app.settings.write_text('{"log_hosts_enabled": "false"}')
        with self.assertRaises(devloop.Error):
            self.app.effective_log_hosts()

    def test_compose_ignores_ambient_manual_hosts_and_uses_only_valid_snapshot(self):
        from test_devloop import DockerDouble
        runner = DockerDouble()
        self.app.runner = runner
        with patch.dict('os.environ', {'BROKER_LOG_HOSTS': 'evil.test'}):
            self.app.dc('config')
        self.assertEqual(runner.calls[-1][1]['env']['BROKER_LOG_HOSTS'], '')
        self.app.save_settings({'log_hosts_enabled': True,
                                'log_snapshot': {'hosts': [HOST1], 'fetched_at': 1}})
        self.app.dc('config')
        self.assertEqual(runner.calls[-1][1]['env']['BROKER_LOG_HOSTS'], HOST1)


    def test_apply_recreates_both_layers_only_after_parse_and_stops_on_failure(self):
        self.assertTrue(hasattr(self.app, 'apply_log_hosts'), 'coordinated log-host application is missing')
        self.app.save_settings({'log_hosts_enabled': True,
                                'log_snapshot': {'hosts': [HOST1], 'fetched_at': 1}})
        calls = []
        def dc(*args, **options):
            calls.append(args)
            return ''
        with patch.object(self.app, 'dc', side_effect=dc), \
             patch.object(self.app, 'services', return_value={'squid': {'State': 'running'}, 'github-broker': {'State': 'running'}}), \
             patch.object(self.app, 'live_log_hosts', return_value={'squid': [HOST1], 'github-broker': [HOST1]}, create=True):
            self.app.apply_log_hosts()
        acl = self.app.control / 'log-policy/github-log-hosts.txt'
        self.assertEqual(acl.read_text(), HOST1 + '\n')
        parse = next(i for i, c in enumerate(calls) if 'parse' in c)
        up = [i for i, c in enumerate(calls) if 'up' in c]
        self.assertTrue(up)
        self.assertTrue(all(parse < i for i in up))
        self.assertEqual(calls[0], ('--profile', 'github', 'stop', 'github-broker'))
        self.assertTrue(any('--force-recreate' in c and c[-1] == 'squid' for c in calls))
        for failure in ('parse', 'squid', 'github-broker', 'mismatch'):
            calls.clear()
            def broken(*args, **options):
                calls.append(args)
                if (failure == 'parse' and 'parse' in args) or ('up' in args and args[-1] == failure):
                    raise devloop.Error('fixture apply failed')
                return ''
            with self.subTest(failure=failure), patch.object(self.app, 'dc', side_effect=broken), \
                 patch.object(self.app, 'services', return_value={'squid': {'State': 'running'}, 'github-broker': {'State': 'running'}}), \
                 patch.object(self.app, 'live_log_hosts', return_value={'squid': [], 'github-broker': []}, create=True):
                with self.assertRaisesRegex(devloop.Error, 'saved.*not verified'):
                    self.app.apply_log_hosts()
            self.assertIn(('--profile', 'github', 'stop', 'github-broker', 'squid'), calls)
            self.assertEqual(self.app.effective_log_hosts(), [HOST1])


    def test_squid_uses_private_directory_mount_and_immutable_running_copy(self):
        root = devloop.ROOT
        compose = (root / 'compose.yaml').read_text()
        self.assertIn('source: ./runtime/devloop/log-policy', compose)
        self.assertIn('target: /etc/squid/log-policy', compose)
        self.assertNotIn('log-policy', compose.split('  squid:\n')[0])
        squid = (root / 'infrastructure/squid/squid.conf').read_text()
        self.assertIn('"/run/github-log-hosts.txt"', squid)
        entry = root / 'infrastructure/squid/entrypoint.sh'
        self.assertTrue(entry.exists())
        self.assertIn('cp /etc/squid/log-policy/github-log-hosts.txt /run/github-log-hosts.txt', entry.read_text())
        dockerfile = (root / 'infrastructure/squid/Dockerfile').read_text()
        self.assertIn('ENTRYPOINT ["/bin/sh", "/etc/squid/devloop/entrypoint.sh"]', dockerfile)


    def test_status_reads_live_layers_without_mutation_and_shows_discrepancies(self):
        self.assertTrue(hasattr(self.app, 'live_log_hosts'), 'live policy readback is missing')
        self.app.save_settings({'log_hosts_enabled': True,
                                'log_snapshot': {'hosts': [HOST1], 'fetched_at': 1}, 'log_fetch': 'failed'})
        before = self.app.settings.read_bytes()
        calls = []
        def dc(*args, **options):
            calls.append(args)
            return json.dumps([HOST2] if 'squid' in args else [])
        with patch.object(self.app, 'services', return_value={'squid': {'State': 'running'}, 'github-broker': {'State': 'running'}}), \
             patch.object(self.app, 'dc', side_effect=dc):
            self.app.log_hosts('status')
        self.assertEqual(self.app.settings.read_bytes(), before)
        self.assertTrue(all(c[0] == 'exec' for c in calls))
        output = self.output.getvalue()
        for text in ('configured: enabled', 'age', 'failed', 'squid live: differs', 'github-broker live: differs'):
            self.assertIn(text, output)
        self.assertIn('/run/github-log-hosts.txt', str(calls))
        self.assertIn('BROKER_LOG_HOSTS', str(calls))


    def test_startup_opt_in_only_refresh_and_offline_deny_or_lkg(self):
        self.assertTrue(hasattr(self.app, 'prepare_log_start'), 'startup log policy hook is missing')
        with patch('scripts.devloop.fetch_log_hosts') as fetch, patch.object(self.app, 'apply_log_hosts'):
            self.app.prepare_log_start()
        fetch.assert_not_called()
        for cached in (False, True):
            settings = {'log_hosts_enabled': True}
            if cached:
                settings['log_snapshot'] = {'hosts': [HOST1], 'fetched_at': 1}
            self.app.save_settings(settings)
            with patch('scripts.devloop.fetch_log_hosts', side_effect=devloop.Error('offline')), \
                 patch.object(self.app, 'apply_log_hosts') as apply:
                self.app.prepare_log_start()
            apply.assert_called_once()
            self.assertEqual(self.app.effective_log_hosts(), [HOST1] if cached else [])

    def test_deep_metadata_startup_preserves_lkg_or_starts_denied(self):
        from test_devloop import DockerDouble
        payload = b'[' * 100_000 + b'0' + b']' * 100_000
        for cached in (False, True):
            with self.subTest(cached=cached):
                runner = DockerDouble()
                runner.running = False
                self.app.runner = runner
                self.app.save_settings({'log_hosts_enabled': True, 'log_snapshot':
                                        {'hosts': [HOST1], 'fetched_at': 1} if cached else None})
                with patch('scripts.devloop.subprocess.run', return_value=
                           subprocess.CompletedProcess([], 0, payload)), \
                     patch.object(self.app, 'runtime_check'), \
                     patch.object(self.app, 'refresh_reference'):
                    self.app.start_services()
                self.assertEqual(self.app.effective_log_hosts(), [HOST1] if cached else [])
                self.assertIn('failed', self.app.host_settings()['log_fetch'])
                self.assertTrue(any('up' in args[8:] for args, _ in runner.calls))
                self.assertEqual((self.app.control / 'log-policy/github-log-hosts.txt').read_text(),
                                 HOST1 + '\n' if cached else '# No log hosts enabled\n')

    def test_lifecycle_and_cli_use_host_policy_control(self):
        cli = devloop.parser()
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                args = cli.parse_args(['github', '--log-hosts', 'status'])
            except SystemExit:
                self.fail('--log-hosts is missing from CLI')
        with patch.object(self.app, 'log_hosts') as control:
            devloop.dispatch(self.app, args, cli)
        control.assert_called_once_with('status')
        for name in ('start_services', 'rebuild', 'github'):
            import inspect
            self.assertIn('prepare_log_start()', inspect.getsource(getattr(devloop.DevLoop, name)))


    def test_repeated_application_is_idempotent_when_both_layers_match(self):
        self.app.write_log_acl()
        with patch.object(self.app, 'services', return_value={'squid': {'State': 'running'}, 'github-broker': {'State': 'running'}}), \
             patch.object(self.app, 'live_log_hosts', return_value={'squid': [], 'github-broker': []}), \
             patch.object(self.app, 'dc') as dc:
            self.app.apply_log_hosts()
        dc.assert_not_called()


    def test_reviewed_migration_rebuilds_default_deny_before_enable(self):
        from test_devloop import DockerDouble
        self.app.runner = DockerDouble()
        self.app.runner.running = False
        with patch.object(self.app, 'prerequisites'), patch.object(self.app, 'network_check'), \
             patch.object(self.app, 'runtime_check'), patch.object(self.app, 'refresh_reference'), \
             patch.object(self.app, 'status'), patch('scripts.devloop.fetch_log_hosts', return_value=[HOST1]) as fetch:
            self.app.rebuild()
            fetch.assert_not_called()
            calls = self.app.runner.calls
            build = next(i for i, (args, _) in enumerate(calls) if 'build' in args[8:])
            parse = next(i for i, (args, _) in enumerate(calls) if 'parse' in args[8:])
            self.assertLess(build, parse)
            self.assertTrue(all(options['env']['BROKER_LOG_HOSTS'] == '' for _, options in calls))
            with patch.object(self.app, 'live_log_hosts', return_value={'squid': [HOST1], 'github-broker': None}):
                self.app.log_hosts('enable')
            fetch.assert_called_once()
        self.assertEqual(self.app.effective_log_hosts(), [HOST1])
        docs = (devloop.ROOT / 'docs/actions-diagnostics.md').read_text()
        activation = docs.split('## Owner-only activation: default deny', 1)[1]
        self.assertLess(activation.index('./devloop rebuild'), activation.index('./devloop github --log-hosts enable'))

    def test_rebuild_prepares_policy_before_first_service_start(self):
        import inspect
        source = inspect.getsource(devloop.DevLoop.rebuild)
        self.assertLess(source.index('prepare_log_start()'), source.index('self.dc("up"'))

    def test_additions_removals_disable_and_lock(self):
        with self.app.administration(), self.assertRaisesRegex(devloop.Error, 'Another DevLoop'):
            self.app.log_hosts('enable')
        with patch('scripts.devloop.fetch_log_hosts', side_effect=[[HOST1], [HOST2]]), \
             patch.object(self.app, 'apply_log_hosts'):
            self.app.log_hosts('enable')
            self.app.log_hosts('refresh')
            self.assertEqual(self.app.effective_log_hosts(), [HOST2])
            self.app.log_hosts('disable')
            self.assertEqual(self.app.effective_log_hosts(), [])


    def test_status_failure_during_disable_still_attempts_emergency_stop(self):
        self.app.save_settings({'log_hosts_enabled': False})
        with patch.object(self.app, 'services', side_effect=devloop.Error('Docker unavailable')), \
             patch.object(self.app, 'dc') as dc:
            with self.assertRaisesRegex(devloop.Error, 'not verified'):
                self.app.apply_log_hosts()
        self.assertIn(unittest.mock.call('--profile', 'github', 'stop', 'github-broker', 'squid', timeout=90), dc.call_args_list)


    def test_emergency_stop_does_not_claim_stopped_when_readback_is_running(self):
        with patch.object(self.app, 'services', return_value={'squid': {'State': 'running'}}), \
             patch.object(self.app, 'dc', side_effect=lambda *a, **k: (_ for _ in ()).throw(devloop.Error('bad parse')) if 'parse' in a else ''), \
             patch.object(self.app, 'live_log_hosts', return_value={'squid': None, 'github-broker': None}):
            with self.assertRaisesRegex(devloop.Error, 'live access is unknown'):
                self.app.apply_log_hosts()


    def test_startup_end_to_end_uses_cached_hosts_offline_and_disable_denies_both(self):
        from test_devloop import DockerDouble
        runner = DockerDouble()
        self.app.runner = runner
        live = {'squid': [], 'github-broker': []}
        run = runner.run
        def docker(args, **options):
            command = args[8:]
            if 'up' in command and command[-1] in live:
                live[command[-1]] = options['env']['BROKER_LOG_HOSTS'].split(',') if options['env']['BROKER_LOG_HOSTS'] else []
            if '-c' in command and '/run/github-log-hosts.txt' in command[-1]:
                runner.calls.append((args, options))
                return json.dumps(live['squid'])
            return run(args, **options)
        self.app.save_settings({'log_hosts_enabled': True,
                                'log_snapshot': {'hosts': [HOST1], 'fetched_at': 1}})
        runner.broker_running = True
        with patch.object(runner, 'run', side_effect=docker), \
             patch('scripts.devloop.fetch_log_hosts', side_effect=devloop.Error('offline')):
            self.app.prepare_log_start()
            self.assertEqual(live, {'squid': [HOST1], 'github-broker': [HOST1]})
            self.app.log_hosts('disable')
            self.assertEqual(live, {'squid': [], 'github-broker': []})
        self.assertIn('failed', self.app.host_settings()['log_fetch'])
        acl = self.app.control / 'log-policy/github-log-hosts.txt'
        self.assertTrue(acl.read_text().startswith('#'))
        self.assertEqual(acl.stat().st_mode & 0o777, 0o600)

    def test_initial_offline_start_completes_with_empty_policy(self):
        from test_devloop import DockerDouble
        self.app.runner = DockerDouble()
        self.app.save_settings({'log_hosts_enabled': True})
        with patch('scripts.devloop.fetch_log_hosts', side_effect=devloop.Error('offline')):
            self.app.prepare_log_start()
        self.assertEqual(self.app.effective_log_hosts(), [])
        self.assertIn('age (seconds): unavailable', self.output.getvalue())
        self.assertTrue((self.app.control / 'log-policy/github-log-hosts.txt').is_file())


if __name__ == '__main__':
    unittest.main()
