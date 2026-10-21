"""Deterministic official-MCP adapter tests: no credentials or remote writes."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

BROKER = Path(__file__).resolve().parents[1] / 'infrastructure/github-broker'
sys.path.insert(0, str(BROKER))
from policy import PolicyError


def result(data, structured=None, error=False):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(data))],
                           structured_content=structured, is_error=error)


class IssueAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_reconciles_lost_response_and_restart_without_duplicate(self):
        import tempfile
        from issue_management import Issues
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            stored = None
            async def call(name, args):
                nonlocal stored
                calls.append((name, args))
                if name == 'list_issues':
                    return result({'issues': [] if stored is None else [stored], 'pageInfo': {'hasNextPage': False}})
                if name == 'issue_write':
                    stored = {'number': 9, 'title': args['title'], 'body': args['body'], 'labels': [],
                              'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/9'}
                    raise PolicyError('transport lost')
                return result(stored)
            key = '12345678-1234-1234-1234-123456789abc'
            adapter = Issues(call, journal=Path(directory) / 'journal.db')
            created = await adapter.create('Task', 'Details', key)
            self.assertEqual(created['number'], 9)
            replay = await Issues(call, journal=Path(directory) / 'journal.db').create('Task', 'Details', key)
            self.assertEqual(replay, created)
            self.assertEqual(sum(n == 'issue_write' for n, a in calls), 1)
            with self.assertRaises(PolicyError):
                await adapter.create('Changed', 'Details', key)

    async def test_update_reconciles_lost_response_and_sends_only_title_body(self):
        from issue_management import Issues
        issue = {'number': 3, 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3',
                 'title': 'Old', 'body': 'Old body', 'labels': ['keep']}
        calls = []
        async def call(name, args):
            calls.append((name, args))
            if name == 'issue_write':
                issue.update({k: v for k, v in args.items() if k in ('title', 'body')})
                raise PolicyError('lost response')
            return result(dict(issue))
        updated = await Issues(call).update(3, title='New', body='New body')
        self.assertEqual(updated['title'], 'New')
        self.assertEqual(updated['labels'], ['keep'])
        self.assertEqual([a for n, a in calls if n == 'issue_write'],
                         [{'method': 'update', 'issue_number': 3, 'title': 'New', 'body': 'New body'}])
        await Issues(call).update(3, title='New', body='New body')
        self.assertEqual(sum(n == 'issue_write' for n, a in calls), 1)

    async def test_label_delta_preserves_unrelated_and_reconciles_failure(self):
        from issue_management import Issues
        issue = {'number': 3, 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3',
                 'title': 'Task', 'labels': ['keep', 'old']}
        writes = []
        async def call(name, args):
            if name == 'list_label':
                return result({'labels': [{'name': n} for n in ['keep', 'old', 'new']], 'totalCount': 3})
            if name == 'issue_write':
                writes.append(args)
                issue['labels'] = args['labels']
                raise PolicyError('lost response')
            return result(dict(issue))
        adapter = Issues(call)
        after = await adapter.labels(3, add=['new'], remove=['old'])
        self.assertEqual(after['labels'], ['keep', 'new'])
        self.assertEqual(writes, [{'method': 'update', 'issue_number': 3, 'labels': ['keep', 'new']}])
        await adapter.labels(3, add=['new'], remove=['old'])
        self.assertEqual(len(writes), 1)
        with self.assertRaises(PolicyError):
            await adapter.labels(3, add=['not-existing'], remove=[])
        self.assertEqual(len(writes), 1)

    async def test_comment_reconciles_lost_response_and_restart(self):
        import tempfile
        from issue_management import Issues
        with tempfile.TemporaryDirectory() as directory:
            comments = []
            writes = []
            async def call(name, args):
                if name == 'add_issue_comment':
                    writes.append(args)
                    comments.append({'id': 17, 'body': args['body'], 'html_url':
                                     'https://github.com/sevandewater/hermes-dev-loop/issues/3#issuecomment-17'})
                    raise PolicyError('lost response')
                if args['method'] == 'get_comments':
                    return result(comments)
                return result({'number': 3, 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3'})
            key = '12345678-1234-1234-1234-123456789abc'
            journal = Path(directory) / 'journal.db'
            posted = await Issues(call, journal).comment(3, 'Progress', key)
            self.assertEqual(posted['id'], 17)
            self.assertEqual(await Issues(call, journal).comment(3, 'Progress', key), posted)
            self.assertEqual(len(writes), 1)

    async def test_invalid_arguments_fail_before_upstream_calls(self):
        from issue_management import Issues
        calls = []
        async def call(name, args):
            calls.append((name, args))
            return result({})
        adapter = Issues(call)
        for action in (lambda: adapter.read(True), lambda: adapter.list(state='closed OR is:pr'),
                       lambda: adapter.list(after='x' * 2001), lambda: adapter.list(query='x' * 201),
                       lambda: adapter.update(3), lambda: adapter.update(3, body=''),
                       lambda: adapter.labels(3, ['bug'], ['bug']),
                       lambda: adapter.comment(3, ' ', 'bad'), lambda: adapter.create('Task', '', 'bad')):
            with self.subTest(action=action), self.assertRaises(PolicyError):
                await action()
        self.assertEqual(calls, [])

    async def test_read_fails_on_missing_comments_instead_of_claiming_complete(self):
        from issue_management import Issues
        async def call(name, args):
            if args['method'] == 'get':
                return result({'number': 3, 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3', 'comments': 2})
            return result([{'id': 1, 'body': 'only one'}])
        with self.assertRaises(PolicyError):
            await Issues(call).read(3)

    async def test_mutations_reject_pr_targets_without_dispatch(self):
        import tempfile
        from issue_management import Issues
        calls = []
        async def call(name, args):
            calls.append((name, args))
            return result({'number': 3, 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/pull/3'})
        with tempfile.TemporaryDirectory() as directory:
            adapter = Issues(call, Path(directory) / 'journal.db')
            for action in (lambda: adapter.update(3, title='No'),
                           lambda: adapter.labels(3, ['bug'], []),
                           lambda: adapter.comment(3, 'No', '12345678-1234-1234-1234-123456789abc')):
                with self.assertRaises(PolicyError):
                    await action()
            self.assertTrue(all(n == 'issue_read' and a['method'] == 'get' for n, a in calls))
            self.assertFalse((Path(directory) / 'journal.db').exists())

    async def test_unresolved_create_never_dispatches_twice_even_after_restart(self):
        import tempfile
        from issue_management import Issues
        writes = []
        async def call(name, args):
            if name == 'issue_write':
                writes.append(args)
                raise PolicyError('rejected or unknown outcome')
            return result({'issues': [], 'pageInfo': {'hasNextPage': False}})
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'journal.db'
            for _ in range(2):
                with self.assertRaisesRegex(PolicyError, 'SAME request ID'):
                    await Issues(call, journal).create('Task', 'Details', '12345678-1234-1234-1234-123456789abc')
            self.assertEqual(len(writes), 1)

    async def test_read_rejects_repeated_pages_and_backend_errors(self):
        from issue_management import Issues
        calls = []
        async def call(name, args):
            calls.append(args)
            if args['method'] == 'get':
                return result({'number': 3, 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3'})
            return result([{'id': i + 1} for i in range(100)])
        with self.assertRaises(PolicyError):
            await Issues(call).read(3)
        self.assertEqual(len(calls), 3)
        async def fails(name, args):
            raise PolicyError('sanitized upstream failure')
        with self.assertRaisesRegex(PolicyError, 'sanitized upstream failure'):
            await Issues(fails).read(3)

    async def test_bad_cursor_and_incomplete_catalog_fail_closed(self):
        from issue_management import Issues
        for response in ({'issues': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'same'}},
                         {'issues': [], 'pageInfo': {}}, {'issues': 'invalid'}):
            async def call(name, args):
                return result(response)
            with self.assertRaises(PolicyError):
                await Issues(call).list(after='same')
        async def catalog(name, args):
            return result({'labels': [{'name': 'bug'}], 'totalCount': 101})
        with self.assertRaisesRegex(PolicyError, 'Complete label catalog unavailable'):
            await Issues(catalog).available_labels()

    async def test_pagination_limits_and_page_failures_are_not_partial_success(self):
        from unittest.mock import patch
        from issue_management import Issues
        async def call(name, args):
            if name == 'list_issues':
                return result({'issues': [], 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}})
            return result([{'id': i} for i in range(1, 101)])
        with patch('issue_management.MAX_PAGES', 1):
            with self.assertRaisesRegex(PolicyError, 'pagination limit'):
                await Issues(call).comments(3)
            with self.assertRaisesRegex(PolicyError, 'reconciliation limit'):
                await Issues(call).find_created('Task', 'Body')
        async def fails(name, args):
            if args['page'] == 2:
                raise PolicyError('page unavailable')
            return result([{'id': i} for i in range(1, 101)])
        with self.assertRaisesRegex(PolicyError, 'page unavailable'):
            await Issues(fails).comments(3)

    async def test_label_concurrent_change_aborts_before_write(self):
        from issue_management import Issues
        reads = 0
        async def call(name, args):
            nonlocal reads
            if name == 'list_label':
                return result({'labels': [{'name': 'new'}], 'totalCount': 1})
            self.assertEqual(name, 'issue_read')
            reads += 1
            return result({'number': 3, 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3',
                           'labels': ['keep'] if reads == 1 else ['keep', 'concurrent']})
        with self.assertRaisesRegex(PolicyError, 'labels changed'):
            await Issues(call).labels(3, ['new'], [])

    async def test_returned_success_is_not_success_without_exact_readback(self):
        import tempfile
        from issue_management import Issues
        writes = []
        async def call(name, args):
            if name in ('issue_write', 'add_issue_comment'):
                writes.append((name, args))
                return result({'id': '17', 'url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3'})
            if name == 'list_issues':
                return result({'issues': [], 'pageInfo': {'hasNextPage': False}})
            if name == 'list_label':
                return result({'labels': [{'name': 'new'}], 'totalCount': 1})
            if args['method'] == 'get_comments':
                return result([])
            return result({'number': 3, 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3',
                           'title': 'Old', 'body': 'Old', 'labels': ['keep']})
        with tempfile.TemporaryDirectory() as directory:
            adapter = Issues(call, Path(directory) / 'journal.db')
            for action in (lambda: adapter.update(3, title='New'), lambda: adapter.labels(3, ['new'], []),
                           lambda: adapter.create('Task', 'Body', '12345678-1234-1234-1234-123456789abc'),
                           lambda: adapter.comment(3, 'Body', '22345678-1234-1234-1234-123456789abc')):
                with self.assertRaises(PolicyError):
                    await action()
        self.assertEqual(len(writes), 4)

    async def test_storage_failure_is_sanitized_and_sends_no_write(self):
        import tempfile
        from issue_management import Issues
        calls = []
        async def call(name, args):
            calls.append((name, args))
            return result({})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PolicyError, 'Durable issue request journal unavailable'):
                await Issues(call, Path(directory) / 'missing' / 'journal.db').create(
                    'Task', 'Body', '12345678-1234-1234-1234-123456789abc')
        self.assertEqual(calls, [])

    async def test_modern_official_method_envelopes_and_structured_content(self):
        from issue_management import Issues
        issue = {'number': 3, 'title': 'Task', 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3', 'comments': 1}
        async def call(name, args):
            envelope = {'method': args['method']}
            if args['method'] == 'get':
                envelope['issue'] = issue
            else:
                envelope['comments'] = [{'id': 7, 'body': 'Full comment'}]
            return result(envelope, structured=envelope)
        read = await Issues(call).read(3)
        self.assertEqual(read['issue'], issue)
        self.assertEqual(read['comments'], [{'id': 7, 'body': 'Full comment'}])

    def test_invalid_json_and_wrong_method_envelopes_fail_closed(self):
        from issue_management import data
        for response in (SimpleNamespace(content=[SimpleNamespace(type='text', text='not JSON')]),
                         result({'method': 'get', 'issue': None}),
                         result({'method': 'get_sub_issues', 'sub_issues': []})):
            with self.assertRaises(PolicyError):
                data(response)

    async def test_duplicate_request_markers_require_human_reconciliation(self):
        import tempfile
        from issue_management import Issues
        with tempfile.TemporaryDirectory() as directory:
            key = '12345678-1234-1234-1234-123456789abc'
            marked = 'Body\n\n<!-- hermes-issue-request:' + key + ' -->'
            async def call(name, args):
                self.assertNotIn(name, ('issue_write', 'add_issue_comment'))
                if name == 'list_issues':
                    return result({'issues': [{'number': i, 'body': marked} for i in (3, 4)],
                                   'pageInfo': {'hasNextPage': False}})
                if args['method'] == 'get':
                    return result({'number': 3, 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3'})
                comment_body = marked.replace(key, '22345678-1234-1234-1234-123456789abc')
                return result([{'id': i, 'body': comment_body} for i in (1, 2)])
            adapter = Issues(call, Path(directory) / 'journal.db')
            with self.assertRaisesRegex(PolicyError, 'Multiple issues'):
                await adapter.create('Task', 'Body', key)
            with self.assertRaisesRegex(PolicyError, 'Multiple comments'):
                await adapter.comment(3, 'Body', '22345678-1234-1234-1234-123456789abc')

    async def test_list_uses_cursor_and_plain_local_search(self):
        from issue_management import Issues
        calls = []
        async def call(name, args):
            calls.append((name, args))
            return result({'issues': [{'number': 3, 'title': 'Bug fix', 'body': 'details'}],
                           'totalCount': 2, 'pageInfo': {'hasNextPage': True, 'endCursor': 'next'}})
        adapter = Issues(call)
        output = await adapter.list(query='bug', after='cursor', state='open')
        self.assertEqual(output['issues'][0]['number'], 3)
        self.assertEqual(output['next_cursor'], 'next')
        self.assertEqual(calls, [('list_issues', {'after': 'cursor', 'perPage': 100,
                          'orderBy': 'CREATED_AT', 'direction': 'ASC', 'state': 'OPEN'})])
        output = await adapter.list(query='repo:other/repo OR is:pr')
        self.assertEqual(output['issues'], [])

    async def test_read_issue_returns_full_paginated_comments(self):
        spec = importlib.util.spec_from_file_location('issue_management', BROKER / 'issue_management.py')
        self.assertIsNotNone(spec)
        self.assertTrue((BROKER / 'issue_management.py').exists(), 'issue adapter is missing')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        calls = []
        async def call(name, args):
            calls.append((name, args))
            if args['method'] == 'get':
                return result({'number': 3, 'html_url': 'https://github.com/sevandewater/hermes-dev-loop/issues/3',
                               'title': 'Task', 'body': 'Details', 'comments': 101, 'labels': []})
            return result([{'id': i, 'body': str(i)} for i in (range(1, 101) if args['page'] == 1 else [101])])
        adapter = module.Issues(call)
        data = await adapter.read(3)
        self.assertEqual(len(data['comments']), 101)
        self.assertEqual([a['page'] for n, a in calls if a['method'] == 'get_comments'], [1, 2])
        self.assertTrue(all(a['perPage'] == 100 for n, a in calls if a['method'] == 'get_comments'))
        self.assertTrue(data['comments_complete'])
