"""Deployment wiring for the issue adapter and durable replay safety."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_adapter_is_packaged_with_private_persistent_broker_only_state(self):
        dockerfile = (ROOT / 'infrastructure/github-broker/Dockerfile').read_text()
        compose = (ROOT / 'compose.yaml').read_text()
        self.assertIn('COPY server.py policy.py issue_management.py /opt/github-broker/', dockerfile)
        self.assertIn('/broker-state', dockerfile)
        self.assertIn('github-broker-state:/broker-state', compose)
        self.assertEqual(compose.count('github-broker-state:/broker-state'), 1)
        self.assertIn('volumes:\n  github-broker-state:', compose)
        self.assertNotIn('/broker-state', compose.split('  github-broker:\n')[0])
