"""Issue-only adapter for the pinned official MCP tools; no GitHub HTTP client."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from policy import OWNER, REPO, PolicyError, issue_number, issue_text, issue_request_id, issue_labels

MAX_PAGES = 100


def data(result):
    # v2.0.2 modern protocol wraps issue_read results by method; legacy
    # protocol returns the inner object/array. Both contracts are explicit.
    payload = getattr(result, 'structured_content', None)
    if payload is None:
        for block in result.content:
            if getattr(block, 'type', None) == 'text':
                try:
                    payload = json.loads(block.text)
                    break
                except (ValueError, TypeError):
                    pass
    if isinstance(payload, dict) and 'method' in payload:
        field = {'get': 'issue', 'get_comments': 'comments'}.get(payload['method'])
        if field is None or field not in payload:
            raise PolicyError('GitHub returned invalid issue method data.')
        payload = payload[field]
    if payload is None:
        raise PolicyError('GitHub returned invalid issue data.')
    return payload


class Issues:
    def __init__(self, call, journal='/broker-state/issues.sqlite3'):
        self.call = call
        self.journal = Path(journal)

    def reserve(self, key, payload):
        # Persist BEFORE dispatch, including timeouts/cancellation/crashes. A used
        # key can only reconcile, never send a second non-idempotent request.
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        try:
            with closing(sqlite3.connect(self.journal)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, digest TEXT NOT NULL)')
                inserted = db.execute('INSERT OR IGNORE INTO requests VALUES (?, ?)', (key, digest)).rowcount
                stored = db.execute('SELECT digest FROM requests WHERE key = ?', (key,)).fetchone()[0]
                if stored != digest:
                    raise PolicyError('Request ID already belongs to a different operation.')
            self.journal.chmod(0o600)
        except (sqlite3.Error, OSError):
            raise PolicyError('Durable issue request journal unavailable; no write attempted. Preserve request ID.') from None
        return bool(inserted)

    async def find_created(self, title, body):
        after = ''
        found = []
        for _ in range(MAX_PAGES):
            page = await self.list(after=after)
            found.extend(i for i in page['issues'] if i.get('body') == body)
            after = page['next_cursor']
            if after is None:
                break
        else:
            raise PolicyError('Issue reconciliation limit reached; do not use a new request ID.')
        if len(found) > 1:
            raise PolicyError('Multiple issues match request ID; human reconciliation required.')
        if not found:
            return None
        issue = await self.metadata(found[0]['number'])
        if issue.get('title') != title or issue.get('body') != body:
            raise PolicyError('Created issue does not match requested content.')
        return issue

    async def create(self, title, body, request_id):
        issue_text(title, title=True)
        issue_text(body)
        issue_request_id(request_id)
        marked = body + '\n\n<!-- hermes-issue-request:' + request_id + ' -->'
        fresh = self.reserve(request_id, {'operation': 'create', 'title': title, 'body': body})
        existing = await self.find_created(title, marked)
        if existing:
            return existing
        if fresh:
            try:
                await self.call('issue_write', {'method': 'create', 'title': title, 'body': marked,
                                               'labels': [], 'assignees': []})
            except PolicyError:
                pass  # Exact readback reconciles both returned and lost responses.
            existing = await self.find_created(title, marked)
            if existing:
                return existing
        raise PolicyError('Issue creation unresolved; retry SAME request ID to read/reconcile only. Never blindly use a new ID.')

    async def comment(self, number, body, request_id):
        issue_number(number)
        issue_text(body)
        issue_request_id(request_id)
        if not body.strip():
            raise PolicyError('Comment body must not be blank.')
        await self.metadata(number)  # Reject PR targets before journal or mutation.
        marked = body + '\n\n<!-- hermes-issue-request:' + request_id + ' -->'
        fresh = self.reserve(request_id, {'operation': 'comment', 'number': number, 'body': body})
        async def find():
            matches = [c for c in await self.comments(number) if c.get('body') == marked]
            if len(matches) > 1:
                raise PolicyError('Multiple comments match request ID; human reconciliation required.')
            if matches:
                comment = matches[0]
                if comment.get('html_url') != f'https://github.com/{OWNER}/{REPO}/issues/{number}#issuecomment-{comment["id"]}':
                    raise PolicyError('Comment target could not be verified.')
                return comment
            return None
        existing = await find()
        if existing:
            return existing
        if fresh:
            try:
                await self.call('add_issue_comment', {'issue_number': number, 'body': marked})
            except PolicyError:
                pass
            existing = await find()
            if existing:
                return existing
        raise PolicyError('Comment unresolved; retry SAME request ID to reconcile only. Never blindly use a new ID.')

    async def available_labels(self):
        response = data(await self.call('list_label', {}))
        if (not isinstance(response, dict) or not isinstance(response.get('labels'), list)
                or type(response.get('totalCount')) is not int
                or response['totalCount'] != len(response['labels'])
                or any(not isinstance(i, dict) or not isinstance(i.get('name'), str) for i in response['labels'])):
            raise PolicyError('Complete label catalog unavailable (upstream limit: 100); labels remain unchanged.')
        return response

    async def labels(self, number, add, remove):
        issue_number(number)
        issue_labels(add)
        issue_labels(remove)
        if set(add) & set(remove) or not add + remove:
            raise PolicyError('Provide disjoint nonempty label deltas.')
        before = await self.metadata(number)
        catalog = {i['name'] for i in (await self.available_labels())['labels']}
        if not set(add + remove) <= catalog:
            raise PolicyError('Use exact existing repository label names; label creation is forbidden.')
        current = before.get('labels', [])
        if not isinstance(current, list) or any(not isinstance(i, str) for i in current):
            raise PolicyError('Invalid current issue labels.')
        expected = sorted((set(current) | set(add)) - set(remove))
        if set(current) == set(expected):
            return before
        # Upstream only supports replacement. Recheck immediately before write
        # rather than quietly overwrite a label change observed during catalog IO.
        if (await self.metadata(number)).get('labels', []) != current:
            raise PolicyError('Issue labels changed; reread before retrying.')
        try:
            await self.call('issue_write', {'method': 'update', 'issue_number': number, 'labels': expected})
        except PolicyError:
            pass
        after = await self.metadata(number)
        if set(after.get('labels', [])) != set(expected):
            raise PolicyError('Label delta not verified; reread target before retrying.')
        return after

    async def update(self, number, title=None, body=None):
        issue_number(number)
        if title is None and body is None:
            raise PolicyError('Provide title and/or body.')
        if title is not None:
            issue_text(title, title=True)
        if body is not None:
            issue_text(body)
            if body == '':
                raise PolicyError('Pinned upstream cannot clear an issue body; provide nonempty text.')
        expected = {k: v for k, v in {'title': title, 'body': body}.items() if v is not None}
        before = await self.metadata(number)
        if all(before.get(k, '') == v for k, v in expected.items()):
            return before
        try:
            await self.call('issue_write', {'method': 'update', 'issue_number': number, **expected})
        except PolicyError:
            pass
        after = await self.metadata(number)
        if any(after.get(k, '') != v for k, v in expected.items()):
            raise PolicyError('Issue update not verified; read target before retrying.')
        return after

    async def list(self, query='', state='all', after=''):
        if (not isinstance(query, str) or len(query) > 200 or state not in ('open', 'closed', 'all')
                or not isinstance(after, str) or len(after) > 2000):
            raise PolicyError('Invalid issue query/state/cursor.')
        arguments = {'after': after, 'perPage': 100, 'orderBy': 'CREATED_AT', 'direction': 'ASC'}
        if state != 'all':
            arguments['state'] = state.upper()
        response = data(await self.call('list_issues', arguments))
        if (not isinstance(response, dict) or not isinstance(response.get('issues'), list)
                or not isinstance(response.get('pageInfo'), dict)):
            raise PolicyError('GitHub returned invalid issue pagination.')
        page = response['pageInfo']
        if type(page.get('hasNextPage')) is not bool or (page['hasNextPage'] and
                (not isinstance(page.get('endCursor'), str) or not page['endCursor'] or page['endCursor'] == after)):
            raise PolicyError('GitHub returned invalid issue cursor.')
        issues = response['issues']
        if any(not isinstance(i, dict) or type(i.get('number')) is not int or i.get('pull_request') is not None for i in issues):
            raise PolicyError('GitHub returned invalid issues.')
        # GraphQL repository.issues excludes PRs. Local literal filtering avoids
        # forwarding arbitrary GitHub search qualifiers that could escape scope.
        matches = [i for i in issues if query.casefold() in (i.get('title', '') + '\n' + (i.get('body') or '')).casefold()]
        return {'issues': matches, 'next_cursor': page['endCursor'] if page['hasNextPage'] else None,
                'search_scope': 'title/body literal substring on this page; follow next_cursor even if empty'}

    async def metadata(self, number):
        issue_number(number)
        issue = data(await self.call('issue_read', {'method': 'get', 'issue_number': number}))
        if (not isinstance(issue, dict) or issue.get('number') != number
                or issue.get('pull_request') is not None
                or issue.get('html_url') != f'https://github.com/{OWNER}/{REPO}/issues/{number}'):
            raise PolicyError('Target is not a verified project issue; pull requests are not allowed.')
        return issue

    async def comments(self, number):
        comments = []
        seen = set()
        for page in range(1, MAX_PAGES + 1):
            batch = data(await self.call('issue_read', {'method': 'get_comments',
                               'issue_number': number, 'page': page, 'perPage': 100}))
            if not isinstance(batch, list) or len(batch) > 100:
                raise PolicyError('GitHub returned invalid comments.')
            for comment in batch:
                if not isinstance(comment, dict) or not comment.get('id') or comment['id'] in seen:
                    raise PolicyError('GitHub returned invalid or repeated comments.')
                seen.add(comment['id'])
                comments.append(comment)
            if len(batch) < 100:
                return comments
        raise PolicyError('Comment pagination limit reached; full discussion is unavailable.')

    async def read(self, number):
        issue = await self.metadata(number)
        comments = await self.comments(number)
        if type(issue.get('comments', 0)) is not int or len(comments) != issue.get('comments', 0):
            raise PolicyError('Issue discussion changed or was filtered; reread before acting.')
        return {'issue': issue, 'comments': comments, 'comments_complete': True}
