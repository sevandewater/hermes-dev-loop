"""Boundary policy tests without GitHub credentials or remote mutations."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('broker_policy', Path(__file__).resolve().parents[1] / 'infrastructure/github-broker/policy.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class BrokerPolicyTests(unittest.TestCase):
    def fetch(self, *, actors=None, active=True, effective=True):
        def read(path):
            if '/rules/branches/' in path:
                return [{'type': 'update', 'ruleset_id': 42}] if effective else []
            return {'id': 42, 'enforcement': 'active' if active else 'disabled', 'bypass_actors': actors or []}
        return read

    def test_human_admin_only_update_rule_is_required(self):
        admin = {'actor_type': 'RepositoryRole', 'actor_id': 5, 'bypass_mode': 'pull_request'}
        self.assertTrue(policy.merge_protection(self.fetch(actors=[admin]))['human_only_updates'])
        for fetch in (self.fetch(), self.fetch(actors=[admin], active=False), self.fetch(actors=[admin], effective=False)):
            with self.assertRaises(policy.PolicyError):
                policy.merge_protection(fetch)

    def test_app_or_writer_bypass_rejects_even_with_human_admin(self):
        admin = {'actor_type': 'RepositoryRole', 'actor_id': 5, 'bypass_mode': 'always'}
        for extra in ({'actor_type': 'Integration', 'actor_id': policy.APP_ID, 'bypass_mode': 'always'},
                      {'actor_type': 'RepositoryRole', 'actor_id': 3, 'bypass_mode': 'always'}):
            with self.assertRaises(policy.PolicyError):
                policy.merge_protection(self.fetch(actors=[admin, extra]))

    def test_only_proposal_branches_are_writable(self):
        self.assertEqual(policy.proposal_branch('hermes/improve-tools'), 'hermes/improve-tools')
        for name in ('main', 'feat/infrastructure-foundation', 'hermes/../main', 'hermes/name.lock', 'hermes//main', 'hermes/x..y', 'hermes/a%2fb'):
            with self.subTest(name=name), self.assertRaises(policy.PolicyError):
                policy.proposal_branch(name)

    def test_file_paths_cannot_escape_or_publish_protected_files(self):
        for name in ('/broker-secrets/github-app.pem', '../README.md', 'runtime/gateway/auth.json', '.env', 'app.pem',
                     '.git/config', '.github/workflows/ci.yml', 'a/../b', 'a//b'):
            with self.subTest(name=name), self.assertRaises(policy.PolicyError):
                policy.source_path(name, writing=True)
        self.assertEqual(policy.source_path('scripts/tool.py', writing=True), 'scripts/tool.py')
        self.assertEqual(policy.source_path('.env.example', writing=True), '.env.example')

    def test_batch_limits_and_private_key_content(self):
        good = [{'path': 'scripts/tool.py', 'content': 'print(42)\n'}]
        self.assertEqual(policy.validate_files(good), good)
        for files in ([], good * 2, [{'path': 'large.txt', 'content': 'a' * 1_000_001}],
                      [{'path': 'ordinary.txt', 'content': '-----BEGIN ' + 'PRIVATE KEY-----\nfixture'}],
                      [{'path': 'file.txt', 'content': 'x', 'owner': 'someone-else'}]):
            with self.assertRaises(policy.PolicyError):
                policy.validate_files(files)


if __name__ == '__main__':
    unittest.main()
