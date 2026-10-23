"""The host acceptance check defaults to reads; mutations require an explicit flag."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('broker_check', ROOT / 'tests/check_github_broker.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CheckTests(unittest.TestCase):
    def test_actions_tools_are_in_live_discovery_contract(self):
        self.assertTrue({'list_project_action_runs', 'read_project_action_run', 'list_project_action_jobs',
                         'read_project_job_logs', 'read_main_check_requirements'} <= check.EXPECTED_TOOLS)

    def test_actions_check_is_read_only_and_preserves_exact_run_attempt_evidence(self):
        spec = importlib.util.spec_from_file_location('actions_check', ROOT / 'tests/check_project_actions.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.read_arguments(10, 20, 1),
                         {'run_id': 10, 'job_id': 20, 'attempt': 1, 'tail': True, 'lines': 20})
        source = (ROOT / 'tests/check_project_actions.py').read_text()
        self.assertNotIn('create_proposal', source)
        self.assertNotIn('dispatch', source)
        self.assertIn("'diagnostics'", source)
        self.assertIn("'read_main_check_requirements'", source)
        self.assertIn("'read_project_job_logs'", source)

    def test_read_only_actions_qualification_executes_reads_without_printing_logs(self):
        import asyncio
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location('actions_check', ROOT / 'tests/check_project_actions.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []
        class Session:
            async def list_tools(self):
                return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in check.EXPECTED_TOOLS])
            async def call_tool(self, name, args):
                calls.append((name, args))
                responses = {
                    'list_project_action_runs': {'complete': True},
                    'read_project_action_run': {'head_sha': 'a' * 40, 'id': 10, 'latest_attempt': 1,
                                                'status': 'completed', 'conclusion': 'failure'},
                    'list_project_action_jobs': {'complete': True},
                    'read_main_check_requirements': {'page': 1},
                    'read_project_job_logs': {'job': {'run_id': 10, 'id': 20, 'run_attempt': 1, 'head_sha': 'a' * 40},
                                             'diagnostics': {'text': 'DO-NOT-PRINT', 'input_bytes': 12,
                                                             'untrusted': True, 'input_complete': True, 'true_tail': True}},
                }
                return SimpleNamespace(is_error=False, structured_content=responses[name])
        out = asyncio.run(module.qualify(Session(), 'a' * 40, 10, 20, 1))
        self.assertTrue(out['completed'])
        self.assertEqual(out['run_conclusion'], 'failure')
        self.assertEqual(out['run_status'], 'completed')
        self.assertNotIn('DO-NOT-PRINT', str(out))
        self.assertEqual([n for n, _ in calls], ['list_project_action_runs', 'read_project_action_run',
                        'list_project_action_jobs', 'read_main_check_requirements', 'read_project_job_logs'])

    def test_issue_tools_are_part_of_live_discovery_contract(self):
        self.assertTrue({'list_project_issues', 'read_project_issue', 'list_project_labels',
                         'create_project_issue', 'update_project_issue', 'change_project_issue_labels',
                         'comment_on_project_issue'} <= check.EXPECTED_TOOLS)

    def test_explicit_issue_canary_has_separate_opt_in(self):
        self.assertIn('issue_canary', __import__('inspect').signature(check.inside).parameters)
        source = (ROOT / 'tests/check_github_broker.py').read_text()
        self.assertIn("'--issue-canary'", source)
        self.assertIn("if issue_canary:", source)
