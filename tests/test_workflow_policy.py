"""The supported workflow language is JSON, a strict subset of Actions YAML."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest

BROKER = Path(__file__).resolve().parents[1] / 'infrastructure/github-broker'
sys.path.insert(0, str(BROKER))
from policy import PolicyError


class WorkflowPolicyTests(unittest.TestCase):
    def test_canonical_workflow_is_accepted_without_a_yaml_parser(self):
        spec = importlib.util.spec_from_file_location('workflow_policy', BROKER / 'workflow_policy.py')
        self.assertIsNotNone(spec)
        self.assertTrue(Path(spec.origin).exists(), 'Workflow envelope is not implemented')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = json.dumps(module.WORKFLOW)
        self.assertEqual(module.validate_workflow('.github/workflows/verify.yml', text), module.WORKFLOW)

    def test_envelope_rejects_ambiguity_escalation_and_indirection(self):
        import copy
        from workflow_policy import WORKFLOW, validate_workflow
        for field, value in (
            ('on', {'pull_request_target': {}}),
            ('permissions', {'contents': 'write'}),
            ('env', {'TOKEN': '${{ secrets.TOKEN }}'}),
        ):
            workflow = copy.deepcopy(WORKFLOW)
            workflow[field] = value
            with self.subTest(field=field), self.assertRaises(PolicyError):
                validate_workflow('.github/workflows/verify.yml', json.dumps(workflow))
        for text in ('name: CI\non: pull_request\n', '{"name":"bad","name":"Verification"}',
                     '---\n{}\n---\n{}', '{"x": NaN}', '[' * 2000):
            with self.subTest(text=text[:30]), self.assertRaises(PolicyError):
                validate_workflow('.github/workflows/verify.yml', text)
        for key, value in (('runs-on', ['self-hosted']), ('permissions', {'id-token': 'write'}),
                           ('environment', 'production'), ('uses', './local.yml'),
                           ('timeout-minutes', 1000), ('services', {'db': {'image': 'evil'}})):
            workflow = copy.deepcopy(WORKFLOW)
            workflow['jobs']['verify'][key] = value
            with self.subTest(key=key), self.assertRaises(PolicyError):
                validate_workflow('.github/workflows/verify.yml', json.dumps(workflow))
        workflow = copy.deepcopy(WORKFLOW)
        workflow['jobs']['verify']['steps'][0]['with']['persist-credentials'] = 0
        with self.assertRaises(PolicyError):
            validate_workflow('.github/workflows/verify.yml', json.dumps(workflow))
        for path in ('.github/workflows/other.yaml', '.github/workflows/sub/verify.yml'):
            with self.assertRaises(PolicyError):
                validate_workflow(path, json.dumps(WORKFLOW))

    def test_workflow_paths_require_explicit_proposal_commit_context(self):
        from policy import source_path, validate_files
        from workflow_policy import WORKFLOW
        files = [{'path': '.github/workflows/verify.yml', 'content': json.dumps(WORKFLOW)}]
        with self.assertRaises(PolicyError):
            source_path(files[0]['path'], writing=True)
        self.assertEqual(validate_files(files, workflow_proposal=True), files)
        with self.assertRaises(PolicyError):
            validate_files(files)
        for path in ('.github', '.github/workflows'):
            with self.subTest(path=path), self.assertRaises(PolicyError):
                validate_files([{'path': path, 'content': 'x'}], workflow_proposal=True)


if __name__ == '__main__':
    unittest.main()
