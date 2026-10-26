"""Fail-closed branch inventory, using pinned MCP response shapes."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'infrastructure/github-broker'))
from policy import PolicyError
from workflow_proposals import directory, validate_proposal


def result(value, modern=True):
    return SimpleNamespace(structured_content=value if modern else None,
                           content=[SimpleNamespace(type='text', text=json.dumps(value))])


class InventoryTests(unittest.IsolatedAsyncioTestCase):
    def test_directory_rejects_missing_truncated_and_non_directory_shapes(self):
        for modern in (True, False):
            self.assertEqual(directory(result([], modern)), [])
            for value in ({'content': []}, None, [{'path': 'x'}] * 1000):
                with self.subTest(modern=modern, value=type(value)), self.assertRaises(PolicyError):
                    directory(result(value, modern))

    async def test_existing_unsupported_entries_block_even_ordinary_changes(self):
        from unittest.mock import patch
        import os
        for entry in ({'path': '.github/workflows/unsafe.yaml', 'type': 'file'},
                      {'path': '.github/workflows/verify.yml', 'type': 'symlink'},
                      {'path': '.github/workflows/sub', 'type': 'dir'}, None):
            async def call(name, args):
                return result([{'path': '.github/workflows', 'type': 'dir'}] if args['path'] == '.github' else [entry])
            with self.subTest(entry=entry), patch.dict(os.environ, {'BROKER_WORKFLOW_PUBLICATION': 'verified'}):
                with self.assertRaises(PolicyError):
                    await validate_proposal(call, 'hermes/test', [{'path': 'README.md', 'content': 'x'}])

    async def test_unavailable_inventory_never_means_no_workflows(self):
        async def call(name, args):
            raise PolicyError('unavailable')
        with self.assertRaisesRegex(PolicyError, 'unavailable'):
            await validate_proposal(call, 'hermes/test', [])

    async def test_untrusted_workflow_content_cannot_be_hidden_in_metadata(self):
        from unittest.mock import patch
        import os
        for response in (result({'content': []}),
                         SimpleNamespace(content=[SimpleNamespace(type='resource_link')]),
                         SimpleNamespace(content=[])):
            async def call(name, args):
                if args['path'] == '.github':
                    return result([{'path': '.github/workflows', 'type': 'dir'}])
                if args['path'] == '.github/workflows':
                    return result([{'path': '.github/workflows/verify.yml', 'type': 'file'}])
                return response
            with patch.dict(os.environ, {'BROKER_WORKFLOW_PUBLICATION': 'verified'}), self.assertRaises(PolicyError):
                await validate_proposal(call, 'hermes/test', [])
