"""Read the complete branch workflow set through the pinned upstream MCP API."""
import json
import os
from policy import PolicyError
from workflow_policy import WORKFLOW_PATH, validate_workflow


def directory(result):
    payload = getattr(result, 'structured_content', None)
    if payload is None:
        try:
            texts = [b.text for b in result.content if b.type == 'text']
            payload = json.loads(texts[0]) if len(texts) == 1 else None
        except (ValueError, IndexError):
            payload = None
    if not isinstance(payload, list) or len(payload) >= 1000:
        raise PolicyError('Complete workflow directory evidence unavailable; no write attempted.')
    return payload


async def validate_proposal(call, branch, files):
    async def listing(path):
        return directory(await call('get_file_contents', {'path': path, 'ref': 'refs/heads/' + branch}))
    github = await listing('.github')
    entries = [e for e in github if isinstance(e, dict) and e.get('path') == '.github/workflows']
    if len(entries) > 1 or any(not isinstance(e, dict) or not isinstance(e.get('path'), str) for e in github):
        raise PolicyError('Invalid .github directory evidence.')
    workflows = {}
    if entries:
        if entries[0].get('type') != 'dir':
            raise PolicyError('Workflow directory must be a real directory.')
        for entry in await listing('.github/workflows'):
            if (not isinstance(entry, dict) or entry.get('type') != 'file'
                    or entry.get('path') != WORKFLOW_PATH or entry['path'] in workflows):
                raise PolicyError('Unsupported existing workflow; owner remediation required.')
            workflows[entry['path']] = None
    proposed = {f['path']: f['content'] for f in files if f['path'].startswith('.github/workflows/')}
    workflows.update(proposed)
    if proposed and os.environ.get('BROKER_WORKFLOW_PUBLICATION') != 'verified':
        raise PolicyError('Workflow publication disabled pending owner security verification and activation.')
    for path, text in workflows.items():
        if text is None:
            result = await call('get_file_contents', {'path': path, 'ref': 'refs/heads/' + branch})
            resources = [b.resource for b in result.content if b.type == 'resource']
            if len(resources) != 1 or not isinstance(getattr(resources[0], 'text', None), str):
                raise PolicyError('Complete workflow text unavailable; no write attempted.')
            text = resources[0].text
        validate_workflow(path, text)
