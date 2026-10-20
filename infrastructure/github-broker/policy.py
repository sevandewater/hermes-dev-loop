"""Fixed project/branch policy. No credentials, token issuer or agent-controlled URLs."""
import json
from pathlib import PurePosixPath
import re
import urllib.error
import urllib.request

OWNER = 'sevandewater'
REPO = 'hermes-dev-loop'
BASE = 'main'
APP_ID = 5402740
PREFIX = f'/repos/{OWNER}/{REPO}'


class PolicyError(ValueError):
    pass


def proposal_branch(value):
    if not isinstance(value, str) or not re.fullmatch(r'hermes/[a-zA-Z0-9][a-zA-Z0-9._/-]{0,100}', value):
        raise PolicyError('Proposal branches must use the hermes/ namespace.')
    if any(part in ('', '.', '..') or part.endswith(('.', '.lock')) or '..' in part for part in value.split('/')):
        raise PolicyError('Invalid proposal branch.')
    return value


def source_path(value, *, writing=False):
    if not isinstance(value, str) or not value or len(value) > 512 or '\\' in value or '%' in value or any(ord(c) < 32 for c in value):
        raise PolicyError('Invalid project file path.')
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or '..' in path.parts or str(path) != value:
        raise PolicyError('Use a normalized relative project file path.')
    blocked = {'.git', 'runtime', 'secrets', '.ssh', '.aws', '.gnupg', '.netrc'}
    if any(part.lower() in blocked or (part.startswith('.env') and part != '.env.example') for part in path.parts):
        raise PolicyError('Protected project path.')
    if path.suffix.lower() in ('.key', '.pem'):
        raise PolicyError('Key files are unavailable through the broker.')
    if writing and path.parts[:2] == ('.github', 'workflows'):
        raise PolicyError('Workflow changes require separate human permissions.')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, *args, **kwargs):
        return None


def public_json(path):
    if not path.startswith(PREFIX + '/'):
        raise PolicyError('Repository is outside broker policy.')
    request = urllib.request.Request('https://api.github.com' + path, headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': 'hermes-dev-loop-broker',
    })
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            payload = response.read(2_000_001)
            if len(payload) > 2_000_000:
                raise ValueError('Response too large')
            return json.loads(payload)
    except Exception:
        raise PolicyError('Cannot verify current GitHub branch rules; writes remain blocked.') from None


def merge_protection(fetch=public_json):
    """GitHub's effective-rules endpoint excludes disabled rulesets. Recheck on every write."""
    rules = fetch(PREFIX + '/rules/branches/' + BASE)
    if not isinstance(rules, list):
        raise PolicyError('GitHub returned invalid branch rules; writes remain blocked.')
    for rule in rules:
        if rule.get('type') != 'update' or not isinstance(rule.get('ruleset_id'), int):
            continue
        details = fetch(PREFIX + '/rulesets/' + str(rule['ruleset_id']))
        if details.get('enforcement') != 'active':
            continue
        actors = details.get('bypass_actors', [])
        # On this personal repository, administrator role 5 is the human owner.
        # No GitHub App, writer role, deploy key or unknown actor may bypass.
        if actors and all(a.get('actor_type') == 'RepositoryRole' and a.get('actor_id') == 5
                          and a.get('bypass_mode') in ('always', 'pull_request') for a in actors):
            return {'human_only_updates': True, 'ruleset_id': details['id']}
    raise PolicyError('main needs an active Restrict updates rule with human-admin bypass only; writes remain blocked.')


def validate_files(files):
    if not isinstance(files, list) or not 1 <= len(files) <= 50:
        raise PolicyError('Submit between 1 and 50 text files per commit.')
    total = 0
    paths = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'content'}:
            raise PolicyError('Each file needs only path and content.')
        path = source_path(item['path'], writing=True)
        if path in paths or not isinstance(item['content'], str):
            raise PolicyError('File paths must be unique and content must be text.')
        if re.search(r'(?m)^-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----', item['content']):
            raise PolicyError('Private-key PEM content cannot be published.')
        paths.add(path)
        total += len(item['content'].encode())
    if total > 1_000_000:
        raise PolicyError('Commit content exceeds the 1 MB broker limit.')
    return files
