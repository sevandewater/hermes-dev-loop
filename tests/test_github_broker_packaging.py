"""Deployment wiring for the issue adapter and durable replay safety."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_copied_python_sources_are_allowed_in_build_context(self):
        broker = ROOT / 'infrastructure/github-broker'
        rules = [line.strip() for line in (broker / '.dockerignore').read_text().splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        # This context uses a deny-all followed by explicit file exceptions.
        self.assertEqual(rules[0], '*')
        self.assertTrue(all(rule.startswith('!') for rule in rules[1:]))
        allowed = {rule[1:] for rule in rules[1:]}
        sources = []
        for line in (broker / 'Dockerfile').read_text().splitlines():
            parts = line.split()
            if parts and parts[0].upper() == 'COPY' and not any(
                    part.startswith('--from=') for part in parts[1:]):
                sources.extend(part for part in parts[1:-1] if part.endswith('.py'))
        self.assertTrue(sources, 'Expected explicit local Python COPY sources')
        for source in sources:
            with self.subTest(source=source):
                self.assertIn(source, allowed, f'{source} excluded from build context')
                self.assertTrue((broker / source).is_file())

    def test_adapter_is_packaged_with_private_persistent_broker_only_state(self):
        dockerfile = (ROOT / 'infrastructure/github-broker/Dockerfile').read_text()
        compose = (ROOT / 'compose.yaml').read_text()
        self.assertIn('COPY server.py policy.py issue_management.py actions.py job_logs.py /opt/github-broker/', dockerfile)
        self.assertIn('BROKER_LOG_HOSTS:', compose)
        self.assertIn('${BROKER_LOG_HOSTS:-}', compose)
        squid = (ROOT / 'infrastructure/squid/squid.conf').read_text()
        self.assertIn('github-log-hosts.txt', squid)
        self.assertIn('http_access deny github_broker !github_api !github_logs', squid)
        self.assertLess(squid.index('http_access deny prohibited'), squid.index('http_access allow github_broker github_logs'))
        hosts = (ROOT / 'infrastructure/squid/github-log-hosts.txt').read_text()
        self.assertEqual([s for s in hosts.splitlines() if s.strip() and not s.startswith('#')], [])
        self.assertIn('/broker-state', dockerfile)
        self.assertIn('github-broker-state:/broker-state', compose)
        self.assertEqual(compose.count('github-broker-state:/broker-state'), 1)
        self.assertIn('volumes:\n  github-broker-state:', compose)
        self.assertNotIn('/broker-state', compose.split('  github-broker:\n')[0])
