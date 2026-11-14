"""Regression contracts for the reviewed v0.23.1 integration (no Docker/OAuth)."""
import ast
import asyncio
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TARGET = '3b421578588243f920886e6fe79a6faa87ea026e'


class UpgradeTests(unittest.TestCase):
    def test_fetch_assert_and_label_use_reviewed_release(self):
        recipe = (ROOT / 'infrastructure/hermes/Dockerfile').read_text()
        self.assertIn(f'git fetch --depth=1 origin {TARGET}', recipe)
        self.assertIn(f'test "$(git rev-parse HEAD)" = {TARGET}', recipe)
        self.assertIn(f'devloop.hermes.revision="{TARGET}"', recipe)
        self.assertEqual(recipe.count(TARGET), 3)
        self.assertEqual(recipe.count('FROM python:3.14-slim@sha256:'), 2)
        self.assertIn('uv==0.12.3', recipe)
        self.assertIn('uv sync --frozen --no-default-groups --no-install-project '
                      '--extra mcp --extra pty --extra sms', recipe)


    def test_dummy_logout_seed_satisfies_keyword_only_renewal_contract(self):
        # Extract only the seeding call: importing this script would run Docker.
        module = ast.parse((ROOT / 'tests/check_gateway_logout.py').read_text())
        program = next(ast.literal_eval(n.value) for n in module.body
                       if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'PROGRAM' for t in n.targets))
        calls = [n for n in ast.walk(ast.parse(program)) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == '_write_shared_nous_state']
        self.assertEqual(len(calls), 1)
        seen = []

        def seed(state, *, renewal):
            seen.append((state, renewal))

        dummy = {'access_token': 'dummy-access'}
        expression = ast.fix_missing_locations(ast.Expression(calls[0]))
        try:
            eval(compile(expression, '<dummy seed>', 'eval'),
                 {'_write_shared_nous_state': seed, 'dummy': dummy})
        except TypeError as error:
            self.fail(f'Dummy fixture violates target auth signature: {error}')
        self.assertEqual(seen, [(dummy, False)])


    def test_diagnostic_accepts_target_aiohttp_before_source_guard(self):
        spec = importlib.util.spec_from_file_location('upgrade_probe', ROOT / 'tests/probe_subscription_proxy.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            source = checkout / 'hermes_cli/proxy/server.py'
            source.parent.mkdir(parents=True)
            source.write_text('# deliberately unreviewed source\n')
            with patch.object(probe.aiohttp, '__version__', '3.14.5'):
                with self.assertRaisesRegex(RuntimeError, 'Upstream transport changed'):
                    asyncio.run(probe.probe(checkout))
            with patch.object(probe.aiohttp, '__version__', '3.14.3'):
                with self.assertRaisesRegex(RuntimeError, 'Use aiohttp==3.14.5'):
                    asyncio.run(probe.probe(checkout))
            self.assertEqual(probe.EXPECTED_BLOB, '49575154482db3d336b00501dc74dfb5b8711a89')


if __name__ == '__main__':
    unittest.main()
