"""The published workflow must remain the broker-approved envelope."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'infrastructure/github-broker'))
from workflow_policy import WORKFLOW, WORKFLOW_PATH, validate_workflow


class CIWorkflowTests(unittest.TestCase):
    def test_issue8_controlled_ci_failure(self):
        self.fail('ISSUE8_CONTROLLED_CI_FAILURE: temporary qualification canary; do not merge')

    def test_published_workflow_matches_approved_envelope(self):
        path = ROOT / WORKFLOW_PATH
        self.assertTrue(path.is_file(), 'Approved CI workflow is not published')
        self.assertEqual(validate_workflow(WORKFLOW_PATH, path.read_text()), WORKFLOW)
        self.assertEqual(json.loads(path.read_text()), WORKFLOW)
