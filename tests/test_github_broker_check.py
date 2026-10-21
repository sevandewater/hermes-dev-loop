"""The host acceptance check defaults to reads; mutations require an explicit flag."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('broker_check', ROOT / 'tests/check_github_broker.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class CheckTests(unittest.TestCase):
    def test_issue_tools_are_part_of_live_discovery_contract(self):
        self.assertTrue({'list_project_issues', 'read_project_issue', 'list_project_labels',
                         'create_project_issue', 'update_project_issue', 'change_project_issue_labels',
                         'comment_on_project_issue'} <= check.EXPECTED_TOOLS)

    def test_explicit_issue_canary_has_separate_opt_in(self):
        self.assertIn('issue_canary', __import__('inspect').signature(check.inside).parameters)
        source = (ROOT / 'tests/check_github_broker.py').read_text()
        self.assertIn("'--issue-canary'", source)
        self.assertIn("if issue_canary:", source)
