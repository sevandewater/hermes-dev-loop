#!/usr/bin/env python3
"""Check broker reads/denials; --proposal and --issue-canary explicitly opt into disposable remote writes."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
import re
import subprocess
import sys
import uuid

EXPECTED_TOOLS = {'broker_status', 'read_project_file', 'read_pull_request', 'create_proposal_branch',
                  'commit_proposal_files', 'open_proposal_pull_request', 'list_project_issues',
                  'read_project_issue', 'list_project_labels', 'create_project_issue', 'update_project_issue',
                  'change_project_issue_labels', 'comment_on_project_issue'}


def result_data(result):
    if result.structured_content:
        return result.structured_content
    for block in result.content:
        if getattr(block, 'type', None) == 'text':
            try:
                return json.loads(block.text)
            except ValueError:
                pass
    return {}


async def inside(proposal, issue_canary=None):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.exceptions import MCPError
    logging.disable(logging.CRITICAL)
    results = []
    async with streamable_http_client('http://github-broker:8000/mcp') as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert tools == EXPECTED_TOOLS, 'MCP tool allowlist mismatch'
            results.append('PASS: exact proposal/issue MCP tool surface; no merge or generic execution tool')
            read = await session.call_tool('read_project_file', {'path': 'LICENSE'})
            assert not read.is_error and 'MIT License' in read.model_dump_json(), 'Authenticated project read failed'
            results.append('PASS: Hermes -> internal MCP broker -> official GitHub MCP -> real repository read')
            review = await session.call_tool('read_pull_request', {'number': 1})
            assert not review.is_error, 'Project PR read failed'
            results.append('PASS: real project PR metadata available through the broker')
            issue_read = await session.call_tool('read_project_issue', {'number': 3})
            issue_info = result_data(issue_read)
            assert not issue_read.is_error and issue_info.get('issue', {}).get('number') == 3
            assert issue_info.get('comments_complete') is True, 'Full issue discussion unavailable'
            listed = await session.call_tool('list_project_issues', {'query': '', 'state': 'all'})
            assert not listed.is_error and isinstance(result_data(listed).get('issues'), list)
            labels = await session.call_tool('list_project_labels', {})
            assert not labels.is_error and isinstance(result_data(labels).get('labels'), list)
            results.append('PASS: live issue metadata/full comments, issue list and existing label catalog')
            for tool, arguments in (
                ('update_project_issue', {'number': 1, 'title': 'must not change PR'}),
                ('change_project_issue_labels', {'number': 1, 'add': ['bug'], 'remove': []}),
                ('comment_on_project_issue', {'number': 1, 'body': 'must not post', 'request_id': str(uuid.uuid4())}),
            ):
                assert (await session.call_tool(tool, arguments)).is_error, 'PR mutation unexpectedly accepted'
            results.append('PASS: issue mutation tools reject PR #1 without a remote mutation')
            try:
                unavailable = await session.call_tool('merge_pull_request', {})
                assert unavailable.is_error, 'Unregistered merge operation unexpectedly accepted'
            except MCPError as error:
                code = getattr(error, 'code', None)
                if code is None:
                    code = getattr(getattr(error, 'error', None), 'code', None)
                assert code in (-32601, -32602), 'Merge probe failed for an unexpected reason'
            results.append('PASS: unregistered merge tool is not callable; no GitHub merge request sent')
            for tool, arguments in (
                ('create_proposal_branch', {'branch': 'main'}),
                ('commit_proposal_files', {'branch': 'main', 'files': [{'path': 'probe.txt', 'content': 'probe'}], 'message': 'must fail'}),
                ('commit_proposal_files', {'branch': 'hermes/probe', 'files': [{'path': '.github/workflows/probe.yml', 'content': 'probe'}], 'message': 'must fail'}),
                ('read_project_file', {'path': '/broker-secrets/github-app.pem'}),
            ):
                denied = await session.call_tool(tool, arguments)
                assert denied.is_error, 'Protected operation unexpectedly accepted'
            results.append('PASS: main writes, workflow changes and key-path reads rejected without remote mutation')
            status = await session.call_tool('broker_status', {})
            assert not status.is_error, 'Broker policy status unavailable'
            info = result_data(status)
            ready = info.get('writes_enabled') is True
            output = {'completed': True, 'results': results, 'writes_enabled': ready,
                      'protection': info, 'proposal_created': False}
            if proposal and ready:
                branch = 'hermes/integration-' + uuid.uuid4().hex[:12]
                created = await session.call_tool('create_proposal_branch', {'branch': branch})
                assert not created.is_error, 'Proposal branch creation failed'
                pushed = await session.call_tool('commit_proposal_files', {'branch': branch,
                    'files': [{'path': 'integration/github-broker-check.txt', 'content': 'Disposable GitHub broker integration check. No production behavior changed.\n'}],
                    'message': 'test: verify Hermes GitHub broker proposal path'})
                assert not pushed.is_error, 'Proposal commit failed'
                opened = await session.call_tool('open_proposal_pull_request', {'branch': branch,
                    'title': 'test: Hermes GitHub broker integration canary',
                    'body': 'Disposable integration canary created through the isolated Hermes MCP broker. Do not merge; close after verification. No production behavior changes.'})
                assert not opened.is_error, 'Draft proposal creation failed'
                urls = re.findall(r'https://github\.com/sevandewater/hermes-dev-loop/pull/\d+', opened.model_dump_json())
                output.update({'proposal_created': True, 'branch': branch, 'pull_request': urls[0] if urls else None})
                output['results'].append('PASS: App-authored branch, commit and draft PR created through Hermes MCP')
            elif proposal:
                output['results'].append('SKIP: draft PR creation blocked until required PR/code-owner review rules pass')
            if issue_canary:
                request_id = str(uuid.UUID(issue_canary))
                assert request_id == issue_canary, 'Use exact lowercase UUID canary request ID'
                assert len(result_data(labels)['labels']) >= 2, 'Canary needs two existing labels; provision separately as human'
                title = 'test: disposable issue broker canary ' + request_id
                body = 'Explicitly authorized issue broker canary. Human: close after verification. Do not implement.'
                arguments = {'title': title, 'body': body, 'request_id': request_id}
                created = await session.call_tool('create_project_issue', arguments)
                assert not created.is_error, 'Issue canary creation unresolved; preserve request ID ' + request_id
                issue = result_data(created)
                number = issue['number']
                replay = await session.call_tool('create_project_issue', arguments)
                assert not replay.is_error and result_data(replay)['number'] == number, 'Create replay failed'
                updated = await session.call_tool('update_project_issue', {'number': number, 'title': title + ' verified', 'body': issue['body'] + '\nVerified body update.'})
                assert not updated.is_error and result_data(updated)['title'] == title + ' verified'
                catalog = result_data(labels)['labels']
                assert catalog, 'No existing label for canary; human must choose/provision separately'
                label, unrelated = catalog[0]['name'], catalog[1]['name']
                seeded = await session.call_tool('change_project_issue_labels', {'number': number, 'add': [unrelated], 'remove': []})
                assert not seeded.is_error, 'Unrelated canary label setup failed'
                for add, remove in (([label], []), ([], [label])):
                    changed = await session.call_tool('change_project_issue_labels', {'number': number, 'add': add, 'remove': remove})
                    assert not changed.is_error and (label in result_data(changed).get('labels', [])) == bool(add)
                    assert unrelated in result_data(changed).get('labels', []), 'Unrelated label was lost'
                comment_args = {'number': number, 'body': 'Issue broker canary verified.', 'request_id': str(uuid.uuid5(uuid.UUID(request_id), 'comment'))}
                posted = await session.call_tool('comment_on_project_issue', comment_args)
                repeated = await session.call_tool('comment_on_project_issue', comment_args)
                assert not posted.is_error and not repeated.is_error and result_data(posted)['id'] == result_data(repeated)['id']
                final = await session.call_tool('read_project_issue', {'number': number})
                final_data = result_data(final)
                assert not final.is_error and final_data['comments_complete'] and len(final_data['comments']) == 1
                output['issue_canary'] = {'number': number, 'url': issue['html_url'], 'request_id': request_id,
                                          'human_cleanup': 'Close this disposable issue manually; broker cannot close issues.'}
                output['results'].append('PASS: issue create/replay, title update, existing-label add/remove, comment/replay and exact readback')
            return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--proposal', action='store_true', help='Create a disposable branch/commit/draft PR only if protection passes')
    parser.add_argument('--issue-canary', metavar='UUID', help='Explicitly create/update/comment/label a disposable issue; retain this lowercase UUID for reconciliation')
    args = parser.parse_args()
    if args.inside:
        try:
            output = asyncio.run(inside(args.proposal, args.issue_canary))
            print(json.dumps(output))
            return 0 if not args.proposal or output['proposal_created'] else 2
        except Exception as error:
            # Do not emit backend results, OAuth data, request bodies or tracebacks.
            causes = []
            def describe(problem):
                if isinstance(problem, BaseExceptionGroup):
                    for child in problem.exceptions:
                        describe(child)
                else:
                    causes.append({'type': type(problem).__name__, 'http_status': getattr(getattr(problem, 'response', None), 'status_code', None)})
            describe(error)
            print(json.dumps({'completed': False, 'causes': causes, 'issue_canary_request_id': args.issue_canary, 'reason': 'MCP integration check failed; inspect broker readiness. Reconcile any canary using the SAME UUID; do not blindly rerun with a new UUID.'}))
            return 1
    root = Path(__file__).resolve().parents[1]
    command = ['docker', 'compose', '--project-directory', str(root), '-f', str(root / 'compose.yaml'),
               'exec', '-T', 'hermes', '/opt/hermes/.venv/bin/python', '-', '--inside']
    if args.proposal:
        command.append('--proposal')
    if args.issue_canary:
        command.extend(['--issue-canary', args.issue_canary])
    checked = subprocess.run(command, input=Path(__file__).read_text(), text=True, capture_output=True, timeout=120)
    if checked.stdout:
        print(checked.stdout.strip())
    else:
        print('GitHub broker check could not run inside Hermes. Start the broker and retry.')
    return checked.returncode


if __name__ == '__main__':
    raise SystemExit(main())
