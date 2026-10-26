"""Shared verification gate; fixtures prove orchestration, not Docker builds."""
import importlib.util
import io
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_verify():
    spec = importlib.util.spec_from_file_location('verify', ROOT / 'scripts/verify.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VerificationTests(unittest.TestCase):
    def run_fixture(self, source):
        with tempfile.TemporaryDirectory() as directory:
            tests = Path(directory)
            (tests / f'test_fixture_{uuid.uuid4().hex}.py').write_text(source)
            output = io.StringIO()
            status = load_verify().run_units(tests, stream=output)
            return status, output.getvalue()

    def test_skipped_test_fails_gate_even_when_unittest_reports_ok(self):
        status, output = self.run_fixture(
            'import unittest\nclass Tests(unittest.TestCase):\n'
            '    @unittest.skip("missing SDK")\n'
            '    def test_skipped(self): pass\n')
        self.assertEqual(status, 1)
        self.assertIn('missing SDK', output)

    def test_default_command_builds_all_images_then_runs_units_in_pinned_image(self):
        verify = load_verify()
        with patch.object(verify, 'subprocess', create=True) as process:
            process.run.return_value.returncode = 0
            self.assertEqual(verify.main([]), 0)
        calls = process.run.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[0], [
            'docker', 'compose', '--project-directory', str(ROOT),
            '--file', str(ROOT / 'compose.yaml'), '--profile', 'github',
            'build', 'hermes', 'llm-gateway', 'squid', 'github-broker'])
        command = calls[1].args[0]
        self.assertEqual(command[:3], ['docker', 'run', '--rm'])
        self.assertIn('--network=none', command)
        self.assertIn('--read-only', command)
        self.assertIn(f'type=bind,src={ROOT},dst=/review,readonly', command)
        self.assertEqual(command[-5:], [
            'hermes-dev-loop-hermes:local', '-W', 'error::ResourceWarning',
            '/review/scripts/verify.py', '--units-only',])

    def test_build_failure_stops_before_unit_container(self):
        verify = load_verify()
        with patch.object(verify.subprocess, 'run') as run:
            run.return_value.returncode = 17
            self.assertEqual(verify.main([]), 17)
            self.assertEqual(run.call_count, 1)

    def test_units_only_is_explicit_partial_gate_without_docker(self):
        verify = load_verify()
        with patch.object(verify, 'run_units', return_value=3) as units, \
                patch.object(verify.subprocess, 'run') as run:
            self.assertEqual(verify.main(['--units-only']), 3)
            units.assert_called_once_with(ROOT / 'tests')
            run.assert_not_called()

    def test_cli_rejects_unknown_options(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/verify.py'),
                                 '--not-a-real-option'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('unrecognized arguments', result.stderr)

    def test_missing_docker_is_actionable_failure(self):
        with patch.dict('os.environ', {'PATH': ''}):
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/verify.py')],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Docker Engine and Compose', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_units_cli_imports_repository_modules_from_any_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'tests').mkdir()
            script = root / 'scripts/verify.py'
            script.write_text((ROOT / 'scripts/verify.py').read_text())
            (root / 'scripts/example.py').write_text('VALUE = 42\n')
            (root / 'tests/test_import.py').write_text(
                'import unittest\nfrom scripts.example import VALUE\n'
                'class Tests(unittest.TestCase):\n'
                '    def test_value(self): self.assertEqual(VALUE, 42)\n')
            result = subprocess.run([sys.executable, str(script), '--units-only'],
                                    cwd='/', capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Ran 1 test', result.stderr)

    def test_empty_suite_fails_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_verify().run_units(Path(directory), stream=io.StringIO()), 1)

    def test_discovers_and_runs_unit_tests(self):
        status, output = self.run_fixture(
            'import unittest\nclass Tests(unittest.TestCase):\n'
            '    def test_ok(self): self.assertTrue(True)\n')
        self.assertEqual(status, 0)
        self.assertIn('Ran 1 test', output)
