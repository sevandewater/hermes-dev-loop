"""Exercise broker dispatch and actual SDK tool registration when SDK is installed."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

BROKER = Path(__file__).resolve().parents[1] / 'infrastructure/github-broker'
sys.path.insert(0, str(BROKER))
try:
    import mcp.server
    SDK = hasattr(mcp.server, 'MCPServer')
except ImportError:
    SDK = False


@unittest.skipUnless(SDK, 'Run with /opt/hermes/.venv/bin/python (pinned MCP SDK 2.3.0)')
class ServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('project_broker', BROKER / 'server.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    async def test_exact_narrow_tool_surface_and_typed_issue_arguments(self):
        tools = {t.name: t for t in await self.module.server.list_tools()}
        expected = {'broker_status', 'read_project_file', 'read_pull_request', 'create_proposal_branch',
                    'commit_proposal_files', 'open_proposal_pull_request', 'list_project_issues',
                    'read_project_issue', 'list_project_labels', 'create_project_issue',
                    'update_project_issue', 'change_project_issue_labels', 'comment_on_project_issue',
                    'list_project_action_runs', 'read_project_action_run', 'list_project_action_jobs',
                    'read_project_job_logs', 'read_main_check_requirements'}
        self.assertEqual(set(tools), expected)
        self.assertEqual(tools['read_project_issue'].input_schema['properties']['number']['type'], 'integer')
        for name in expected - {'broker_status'}:
            self.assertNotIn('owner', tools[name].input_schema['properties'])
            self.assertNotIn('repo', tools[name].input_schema['properties'])
        self.assertEqual(set(self.module.UPSTREAM_TOOLS), {'get_file_contents', 'pull_request_read',
                         'create_branch', 'push_files', 'create_pull_request', 'repository_ruleset_read',
                         'list_issues', 'issue_read', 'issue_write', 'list_label', 'add_issue_comment',
                         'actions_list', 'actions_get', 'get_job_logs'})

    async def test_sdk_dispatch_rejects_non_integer_issue_numbers_before_backend(self):
        from mcp.types import CallToolRequestParams, CallToolResult
        calls = []

        async def call(name, args):
            calls.append((name, args))
            return CallToolResult(content=[], structured_content={})

        context = SimpleNamespace(lifespan_context=SimpleNamespace(call_tool=call))
        tools = {
            'read_project_issue': {},
            'update_project_issue': {'title': 'Updated'},
            'change_project_issue_labels': {'add': ['bug'], 'remove': []},
            'comment_on_project_issue': {
                'body': 'Comment', 'request_id': '12345678-1234-1234-1234-123456789abc'},
        }
        for name, arguments in tools.items():
            for number in (True, False, '007', '7', 'true', 7.0, 1.0, 0, -1,
                           2_147_483_648, None, [], {}):
                with self.subTest(tool=name, number=repr(number)):
                    calls.clear()
                    result = await self.module.server._handle_call_tool(
                        context, CallToolRequestParams(name=name, arguments={
                            **arguments, 'number': number}))
                    self.assertTrue(result.is_error)
                    self.assertEqual(calls, [], 'Rejected identifiers must never reach GitHub')

    async def test_sdk_dispatch_preserves_valid_integer_targets_and_ignores_unexposed_fields(self):
        from mcp.types import CallToolRequestParams, CallToolResult
        import tempfile
        from unittest.mock import patch

        tools = {
            'read_project_issue': ({}, None),
            'update_project_issue': ({'title': 'Updated'}, 'issue_write'),
            'change_project_issue_labels': ({'add': ['bug'], 'remove': []}, 'issue_write'),
            'comment_on_project_issue': ({
                'body': 'Comment', 'request_id': '12345678-1234-1234-1234-123456789abc'},
                'add_issue_comment'),
        }
        for name, (arguments, mutation) in tools.items():
            for number in (1, 7, 2_147_483_647):
                with self.subTest(tool=name, number=number), tempfile.TemporaryDirectory() as directory:
                    calls = []
                    issue = {'number': number, 'title': 'Original', 'body': 'Body', 'labels': [],
                             'comments': 0,
                             'html_url': f'https://github.com/sevandewater/hermes-dev-loop/issues/{number}'}
                    comments = []

                    async def call(tool, args):
                        calls.append((tool, args.copy()))
                        if tool == 'issue_read':
                            payload = issue.copy() if args['method'] == 'get' else comments.copy()
                        elif tool == 'list_label':
                            payload = {'labels': [{'name': 'bug'}], 'totalCount': 1}
                        elif tool == 'issue_write':
                            issue.update({k: args[k] for k in ('title', 'body', 'labels') if k in args})
                            payload = issue.copy()
                        elif tool == 'add_issue_comment':
                            comments.append({'id': 42, 'body': args['body'],
                                             'html_url': issue['html_url'] + '#issuecomment-42'})
                            payload = comments[-1]
                        else:
                            self.fail(f'Unexpected backend tool: {tool}')
                        return CallToolResult(content=[], structured_content=payload)

                    context = SimpleNamespace(lifespan_context=SimpleNamespace(call_tool=call))
                    adapter = self.module.Issues(
                        lambda tool, args: self.module.backend(
                            SimpleNamespace(request_context=context), tool, args),
                        journal=Path(directory) / 'issues.sqlite3')
                    with patch.object(self.module, 'issues', return_value=adapter):
                        result = await self.module.server._handle_call_tool(
                            context, CallToolRequestParams(name=name, arguments={
                                **arguments, 'number': number, 'issue_number': 99,
                                'owner': 'other', 'repo': 'other', 'state': 'closed'}))
                    self.assertFalse(result.is_error, result)
                    self.assertTrue(calls)
                    for tool, args in calls:
                        self.assertEqual(args['owner'], 'sevandewater')
                        self.assertEqual(args['repo'], 'hermes-dev-loop')
                        self.assertNotIn('state', args)
                        if tool != 'list_label':
                            self.assertIs(type(args['issue_number']), int)
                            self.assertEqual(args['issue_number'], number)
                    writes = [tool for tool, _ in calls if tool in ('issue_write', 'add_issue_comment')]
                    self.assertEqual(writes, [mutation] if mutation else [])
                    import json
                    returned = json.loads(result.content[0].text)
                    if name == 'read_project_issue':
                        self.assertEqual(returned['issue']['number'], number)
                    elif name == 'update_project_issue':
                        self.assertEqual(returned['title'], 'Updated')
                    elif name == 'change_project_issue_labels':
                        self.assertEqual(returned['labels'], ['bug'])
                    else:
                        self.assertEqual(returned['id'], 42)

    async def test_issue_mutations_are_serialized_without_touching_proposal_gate(self):
        import asyncio
        from unittest.mock import patch
        active = 0
        maximum = 0
        class Adapter:
            async def labels(self, *args):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0)
                active -= 1
                return {}
        with patch.object(self.module, 'issues', return_value=Adapter()):
            await asyncio.gather(*(self.module.change_project_issue_labels(3, ['bug'], [], None) for _ in range(3)))
        self.assertEqual(maximum, 1)

    async def test_existing_proposal_mutations_still_require_review_protection(self):
        from unittest.mock import AsyncMock, patch
        denied = AsyncMock(side_effect=self.module.PolicyError('review blocked'))
        backend = AsyncMock()
        with patch.object(self.module, 'current_review_protection', denied), patch.object(self.module, 'backend', backend):
            for action in (
                lambda: self.module.create_proposal_branch('hermes/test', None),
                lambda: self.module.commit_proposal_files('hermes/test', [{'path': 'test.txt', 'content': 'test'}], 'test', None),
                lambda: self.module.open_proposal_pull_request('hermes/test', 'Title', 'Body', None),
            ):
                with self.assertRaisesRegex((self.module.PolicyError, self.module.ToolError), 'review blocked'):
                    await action()
        self.assertEqual(denied.await_count, 3)
        backend.assert_not_awaited()

    async def test_actions_sdk_rejects_coercions_before_upstream(self):
        from mcp.types import CallToolRequestParams
        from unittest.mock import AsyncMock, patch
        for name, args, field in (
            ('read_project_action_run', {}, 'run_id'),
            ('list_project_action_jobs', {'run_id': 10}, 'attempt'),
            ('read_project_job_logs', {'run_id': 10, 'job_id': 20, 'attempt': 1}, 'job_id'),
            ('list_project_action_runs', {'sha': 'a' * 40}, 'page'),
        ):
            for value in (True, '7', 7.0, 0, -1):
                backend = AsyncMock()
                with patch.object(self.module, 'backend', backend):
                    result = await self.module.server._handle_call_tool(SimpleNamespace(),
                        CallToolRequestParams(name=name, arguments={**args, field: value}))
                    self.assertTrue(result.is_error)
                    backend.assert_not_awaited()

    async def test_actions_dispatch_pins_repo_and_keeps_error_cause_ambiguous(self):
        from mcp.types import CallToolRequestParams, CallToolResult
        import json
        from unittest.mock import patch
        calls = []
        async def call(name, args):
            calls.append((name, args))
            return CallToolResult(content=[], structured_content={
                'id': 10, 'head_sha': 'a' * 40, 'run_attempt': 2, 'workflow_id': 5,
                'status': 'queued', 'conclusion': None})
        context = SimpleNamespace(lifespan_context=SimpleNamespace(call_tool=call))
        out = await self.module.server._handle_call_tool(context, CallToolRequestParams(
            name='read_project_action_run', arguments={'run_id': 10, 'owner': 'evil',
                'repo': 'evil', 'method': 'download_workflow_run_artifact', 'url': 'https://evil'}))
        self.assertFalse(out.is_error)
        self.assertEqual(json.loads(out.content[0].text)['status'], 'queued')
        self.assertEqual(calls, [('actions_get', {'owner': 'sevandewater', 'repo': 'hermes-dev-loop',
                                                'method': 'get_workflow_run', 'resource_id': '10'})])
        async def failed(name, args):
            raise RuntimeError('https://logs.example/?sig=secret 404 token=secret')
        with patch.object(context.lifespan_context, 'call_tool', failed):
            out = await self.module.server._handle_call_tool(context, CallToolRequestParams(
                name='read_project_action_run', arguments={'run_id': 10}))
        self.assertTrue(out.is_error)
        self.assertIn('unverified', out.content[0].text)
        self.assertNotIn('sig=', out.content[0].text)
        self.assertNotIn('secret', out.content[0].text)

    async def test_workflow_commit_dispatch_validates_complete_set_and_activation(self):
        import json
        import os
        from unittest.mock import patch
        from mcp.types import CallToolRequestParams, CallToolResult
        from workflow_policy import WORKFLOW
        for enabled, existing, proposed, permitted in (
            (False, [], True, False),
            (True, [], True, True),
            (True, ['verify.yml'], False, True),
            (True, ['unsafe.yml'], False, False),
        ):
            calls = []
            async def call(name, args):
                calls.append((name, args))
                if name == 'repository_ruleset_read':
                    if args['method'] == 'get_rules_for_branch':
                        value = [{'type': 'pull_request', 'ruleset_id': 42, 'parameters': {
                            'required_approving_review_count': 1, 'require_code_owner_review': True,
                            'dismiss_stale_reviews_on_push': True}}]
                    else:
                        value = {'id': 42, 'enforcement': 'active', 'current_user_can_bypass': 'never'}
                elif name == 'get_file_contents':
                    path = args['path']
                    if path == '.github':
                        value = [{'path': '.github/workflows', 'type': 'dir', 'name': 'workflows'}] if existing else []
                    elif path == '.github/workflows':
                        value = [{'path': '.github/workflows/' + n, 'name': n, 'type': 'file'} for n in existing]
                    else:
                        return CallToolResult(content=[{'type': 'resource', 'resource': {
                            'uri': 'repo://test/' + path, 'text': json.dumps(WORKFLOW), 'mimeType': 'text/plain'}}])
                elif name == 'push_files':
                    value = {'ref': 'refs/heads/hermes/test'}
                else:
                    self.fail(name)
                return CallToolResult(content=[{'type': 'text', 'text': json.dumps(value)}])
            context = SimpleNamespace(lifespan_context=SimpleNamespace(call_tool=call))
            files = [{'path': 'README.md', 'content': 'test'}]
            if proposed:
                files.append({'path': '.github/workflows/verify.yml', 'content': json.dumps(WORKFLOW)})
            with self.subTest(enabled=enabled, existing=existing, proposed=proposed), patch.dict(
                    os.environ, {'BROKER_WORKFLOW_PUBLICATION': 'verified' if enabled else ''}):
                result = await self.module.server._handle_call_tool(context, CallToolRequestParams(
                    name='commit_proposal_files', arguments={'branch': 'hermes/test', 'files': files, 'message': 'test'}))
                self.assertEqual(not result.is_error, permitted, result)
                self.assertEqual(any(n == 'push_files' for n, _ in calls), permitted)
                for _, args in calls:
                    self.assertEqual(args['owner'], 'sevandewater')
                    self.assertEqual(args['repo'], 'hermes-dev-loop')

    async def test_backend_pins_repository_and_sanitizes_errors(self):
        calls = []
        async def call(name, args):
            calls.append((name, args))
            raise RuntimeError('secret-token-must-not-escape')
        context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(call_tool=call)))
        with self.assertRaisesRegex(self.module.PolicyError, '^GitHub operation failed'):
            await self.module.backend(context, 'issue_read', {'method': 'get', 'issue_number': 3})
        self.assertEqual(calls[0][1], {'owner': 'sevandewater', 'repo': 'hermes-dev-loop',
                                     'method': 'get', 'issue_number': 3})
        with self.assertRaises(self.module.PolicyError):
            await self.module.backend(context, 'delete_issue', {})
        self.assertEqual(len(calls), 1)
