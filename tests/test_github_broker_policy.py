"""Boundary policy tests without GitHub credentials or remote mutations."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('broker_policy', Path(__file__).resolve().parents[1] / 'infrastructure/github-broker/policy.py')
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class BrokerPolicyTests(unittest.TestCase):
    def fetch(self, *, actors=None, active=True, effective=True, parameters=None):
        def read(path):
            if '/rules/branches/' in path:
                return [{'type': 'pull_request', 'ruleset_id': 42, 'parameters': parameters if parameters is not None else {
                    'required_approving_review_count': 1, 'require_code_owner_review': True,
                    'dismiss_stale_reviews_on_push': True}}] if effective else []
            return {'id': 42, 'enforcement': 'active' if active else 'disabled', 'bypass_actors': actors or [],
                    'current_user_can_bypass': 'never'}
        return read

    def test_issue_argument_bounds_reject_wrong_types_and_unsupported_content(self):
        self.assertEqual(policy.issue_number(3), 3)
        for value in (True, 0, -1, 2**31, '3', 3.0):
            with self.subTest(value=value), self.assertRaises(policy.PolicyError):
                policy.issue_number(value)
        for value in (None, 42, ' ', 'a' * 201, '<b>title</b>', '```title', 'a\u200bb', 'a\nB'):
            with self.subTest(value=value), self.assertRaises(policy.PolicyError):
                policy.issue_text(value, title=True)
        self.assertEqual(policy.issue_text('Valid title', title=True), 'Valid title')
        self.assertEqual(policy.issue_text('Markdown\n<b>body</b>'), 'Markdown\n<b>body</b>')
        for value in (None, 4, 'a' * 20001, 'x\x00', 'x\u200b', 'x\r\n'):
            with self.subTest(value=value), self.assertRaises(policy.PolicyError):
                policy.issue_text(value)
        for value in ('bad-id', '12345678-1234-1234-1234-123456789ABC', None):
            with self.assertRaises(policy.PolicyError):
                policy.issue_request_id(value)
        for value in ('bug', ['bug', 'bug'], [' '], ['x' * 51], [None], ['x\n'], list(map(str, range(21)))):
            with self.assertRaises(policy.PolicyError):
                policy.issue_labels(value)
        self.assertEqual(policy.issue_labels(['bug']), ['bug'])

    def test_active_review_rule_with_no_bypass_enables_proposals(self):
        protection = policy.review_protection(self.fetch())
        self.assertEqual(protection['required_approvals'], 1)
        self.assertFalse(protection['app_can_bypass'])
        for fetch in (self.fetch(active=False), self.fetch(effective=False)):
            with self.assertRaises(policy.PolicyError):
                policy.review_protection(fetch)

    def test_admin_app_or_writer_bypass_rejects(self):
        for actor in ({'actor_type': 'RepositoryRole', 'actor_id': 5, 'bypass_mode': 'pull_request'},
                      {'actor_type': 'Integration', 'actor_id': policy.APP_ID, 'bypass_mode': 'always'},
                      {'actor_type': 'RepositoryRole', 'actor_id': 3, 'bypass_mode': 'always'}):
            with self.assertRaises(policy.PolicyError):
                policy.review_protection(self.fetch(actors=[actor]))

    def test_approval_owner_and_stale_review_requirements_cannot_be_weakened(self):
        good = {'required_approving_review_count': 1, 'require_code_owner_review': True,
                'dismiss_stale_reviews_on_push': True}
        for key, value in [('required_approving_review_count', 0), ('require_code_owner_review', False),
                           ('dismiss_stale_reviews_on_push', False)]:
            with self.subTest(key=key), self.assertRaises(policy.PolicyError):
                policy.review_protection(self.fetch(parameters={**good, key: value}))
        for missing in good:
            with self.subTest(missing=missing), self.assertRaises(policy.PolicyError):
                policy.review_protection(self.fetch(parameters={k: v for k, v in good.items() if k != missing}))

    def test_hidden_bypass_list_requires_explicit_authenticated_no_bypass_result(self):
        fetch = self.fetch()
        def hidden(path, permission):
            data = fetch(path)
            if isinstance(data, dict):
                data.pop('bypass_actors')
                data['current_user_can_bypass'] = permission
            return data
        result = policy.review_protection(lambda path: hidden(path, 'never'))
        self.assertFalse(result['bypass_list_visible'])
        for permission in (None, 'always', 'pull_requests_only'):
            with self.subTest(permission=permission), self.assertRaises(policy.PolicyError):
                policy.review_protection(lambda path: hidden(path, permission))

    def test_official_mcp_grouped_branch_rule_shape_is_validated(self):
        fetch = self.fetch()
        def grouped(path):
            data = fetch(path)
            if isinstance(data, list):
                return {'PullRequest': [{k: v for k, v in data[0].items() if k != 'type'}], 'Deletion': []}
            return data
        self.assertEqual(policy.review_protection(grouped)['required_approvals'], 1)
        for data in ({'PullRequest': None}, {'PullRequest': 'invalid'}, {'PullRequest': [None]}, [None]):
            with self.subTest(data=data), self.assertRaises(policy.PolicyError):
                policy.review_protection(lambda path: data)

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
