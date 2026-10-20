#!/usr/bin/env python3
"""Test MCP from Hermes; --proposal explicitly creates a draft canary PR if writes are permitted."""
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
                  'commit_proposal_files', 'open_proposal_pull_request'}


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


async def inside(proposal):
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
            results.append('PASS: exact proposal-only MCP tool surface; no merge or generic execution tool')
            read = await session.call_tool('read_project_file', {'path': 'LICENSE'})
            assert not read.is_error and 'MIT License' in read.model_dump_json(), 'Authenticated project read failed'
            results.append('PASS: Hermes -> internal MCP broker -> official GitHub MCP -> real repository read')
            review = await session.call_tool('read_pull_request', {'number': 1})
            assert not review.is_error, 'Project PR read failed'
            results.append('PASS: real project PR metadata available through the broker')
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
                output['results'].append('SKIP: draft PR creation blocked until human-only merge protection is active')
            return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--proposal', action='store_true', help='Create a disposable branch/commit/draft PR only if protection passes')
    args = parser.parse_args()
    if args.inside:
        try:
            output = asyncio.run(inside(args.proposal))
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
            print(json.dumps({'completed': False, 'causes': causes, 'reason': 'MCP integration check failed; inspect broker readiness and App configuration.'}))
            return 1
    root = Path(__file__).resolve().parents[1]
    command = ['docker', 'compose', '--project-directory', str(root), '-f', str(root / 'compose.yaml'),
               'exec', '-T', 'hermes', '/opt/hermes/.venv/bin/python', '-', '--inside']
    if args.proposal:
        command.append('--proposal')
    checked = subprocess.run(command, input=Path(__file__).read_text(), text=True, capture_output=True, timeout=120)
    if checked.stdout:
        print(checked.stdout.strip())
    else:
        print('GitHub broker check could not run inside Hermes. Start the broker and retry.')
    return checked.returncode


if __name__ == '__main__':
    raise SystemExit(main())
