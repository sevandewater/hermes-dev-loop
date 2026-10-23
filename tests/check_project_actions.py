#!/usr/bin/env python3
"""Read-only host qualification after activation; never prints raw diagnostics."""
import argparse
import asyncio
import json
import logging
from pathlib import Path
import re
import subprocess


def read_arguments(run_id, job_id, attempt):
    for value in (run_id, job_id, attempt):
        if type(value) is not int or value < 1:
            raise ValueError('Positive integer IDs/attempt required.')
    return {'run_id': run_id, 'job_id': job_id, 'attempt': attempt, 'tail': True, 'lines': 20}


async def qualify(session, sha, run_id, job_id, attempt):
    async def read(name, args):
        response = await session.call_tool(name, args)
        if response.is_error:
            raise RuntimeError('Read unavailable')
        value = response.structured_content
        if value is None:
            value = json.loads(response.content[0].text)
        return value
    tools = {t.name for t in (await session.list_tools()).tools}
    required = {'list_project_action_runs', 'read_project_action_run', 'list_project_action_jobs',
                'read_project_job_logs', 'read_main_check_requirements'}
    assert required <= tools
    discovery = await read('list_project_action_runs', {'sha': sha, 'per_page': 100})
    run = await read('read_project_action_run', {'run_id': run_id})
    assert run['head_sha'] == sha and run['id'] == run_id
    jobs = await read('list_project_action_jobs', {'run_id': run_id, 'attempt': attempt, 'per_page': 100})
    rules = await read('read_main_check_requirements', {})
    logs = await read('read_project_job_logs', read_arguments(run_id, job_id, attempt))
    job = logs['job']
    assert job['run_id'] == run_id and job['id'] == job_id and job['run_attempt'] == attempt and job['head_sha'] == sha
    diagnostics = logs['diagnostics']
    assert diagnostics['input_bytes'] > 0 and diagnostics['untrusted']
    # No raw job text, arbitrary upstream strings, signed URLs or requirements are printed.
    statuses = {'queued', 'in_progress', 'completed', 'requested', 'waiting', 'pending'}
    conclusions = {'success', 'failure', 'cancelled', 'skipped', 'neutral', 'timed_out', 'action_required', 'stale', None}
    return {'run_status': run.get('status') if run.get('status') in statuses else 'unknown',
            'run_conclusion': run.get('conclusion') if run.get('conclusion') in conclusions else 'unknown',
            'completed': True, 'head_sha': sha, 'run_id': run_id, 'job_id': job_id, 'attempt': attempt,
            'latest_attempt': run['latest_attempt'], 'input_bytes': diagnostics['input_bytes'],
            'input_complete': diagnostics['input_complete'], 'true_tail': diagnostics['true_tail'],
            'discovery_complete': discovery['complete'], 'jobs_complete': jobs['complete'],
            'rules_page_read': rules['page'], 'classic_branch_protection': 'unavailable',
            'all_required_checks_passed': 'not assessed'}


async def inside(args):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    logging.disable(logging.CRITICAL)
    async with streamable_http_client('http://github-broker:8000/mcp') as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            return await qualify(session, args.sha, args.run_id, args.job_id, args.attempt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inside', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--run-id', required=True, type=int)
    parser.add_argument('--job-id', required=True, type=int)
    parser.add_argument('--attempt', required=True, type=int)
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-fA-F]{40}', args.sha):
        parser.error('Exact full SHA required')
    read_arguments(args.run_id, args.job_id, args.attempt)
    if args.inside:
        try:
            print(json.dumps(asyncio.run(inside(args))))
            return 0
        except Exception:
            print(json.dumps({'completed': False, 'reason': 'Read-only qualification failed; no cause inferred.'}))
            return 1
    root = Path(__file__).resolve().parents[1]
    command = ['docker', 'compose', '--project-directory', str(root), '-f', str(root / 'compose.yaml'),
               'exec', '-T', 'hermes', '/opt/hermes/.venv/bin/python', '-', '--inside',
               '--sha', args.sha, '--run-id', str(args.run_id), '--job-id', str(args.job_id),
               '--attempt', str(args.attempt)]
    try:
        run = subprocess.run(command, input=Path(__file__).read_text(), text=True, capture_output=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        print('Read-only qualification could not run inside Hermes.')
        return 1
    print(run.stdout.strip() if run.stdout else 'Read-only qualification unavailable.')
    return run.returncode


if __name__ == '__main__':
    raise SystemExit(main())
