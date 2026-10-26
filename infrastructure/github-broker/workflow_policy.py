"""Deliberately finite Actions envelope, not an arbitrary YAML analyzer.

JSON is YAML 1.2; accepting JSON only avoids YAML tags, aliases, merges,
duplicate keys and parser-version ambiguity. Execution lives in scripts/verify.py.
"""
import json
from policy import PolicyError

WORKFLOW_PATH = '.github/workflows/verify.yml'
WORKFLOW = {
    'name': 'Verification',
    'on': {'pull_request': {'branches': ['main'], 'types': ['opened', 'synchronize', 'reopened']}},
    'permissions': {'contents': 'read'},
    'concurrency': {'group': 'verify-${{ github.event.pull_request.number }}', 'cancel-in-progress': True},
    'jobs': {'verify': {
        'name': 'unit-and-images', 'runs-on': 'ubuntu-24.04', 'timeout-minutes': 45,
        'steps': [
            {'uses': 'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683',
             'with': {'persist-credentials': False}},
            {'name': 'Verify tested revision', 'run': 'python3 scripts/verify.py'},
        ],
    }},
}


def validate_workflow(path, text):
    if path != WORKFLOW_PATH or not isinstance(text, str) or len(text.encode()) > 16_384:
        raise PolicyError('Only the bounded .github/workflows/verify.yml envelope is supported.')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=unique)
        # Comparing canonical JSON preserves bool/int distinctions and rejects
        # NaN and infinity, unlike ordinary Python container equality.
        canonical = json.dumps(value, sort_keys=True, allow_nan=False)
        expected = json.dumps(WORKFLOW, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise PolicyError('Workflow must be unambiguous JSON (a YAML subset).') from None
    if canonical != expected:
        raise PolicyError('Workflow must match the approved finite verification envelope.')
    return value
