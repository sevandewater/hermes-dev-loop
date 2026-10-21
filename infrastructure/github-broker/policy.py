"""Fixed project/branch policy. No credentials, token issuer or agent-controlled URLs."""
from pathlib import PurePosixPath
import re
import unicodedata


def issue_number(value):
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise PolicyError('Issue number must be a positive 32-bit integer.')
    return value


def issue_text(value, *, title=False):
    limit = 200 if title else 20_000
    if (not isinstance(value, str) or len(value) > limit or (title and not value.strip())
            or any((unicodedata.category(c).startswith('C') and c not in ('\n', '\t'))
                   or c in '\u00ad' or 0xFE00 <= ord(c) <= 0xFE0F or 0xE0100 <= ord(c) <= 0xE01EF for c in value)
            or (title and any(c in value for c in ('<', '>', '&', '`', '\n', '\t')))):
        raise PolicyError('Invalid issue text: title <=200, body/comment <=20000; use visible text and LF newlines.')
    return value


def issue_request_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value):
        raise PolicyError('Provide a lowercase UUID request_id; reuse it on retries.')
    return value


def issue_labels(value):
    if (not isinstance(value, list) or len(value) > 20
            or any(not isinstance(i, str) or not i.strip() or len(i) > 50
                   or any(unicodedata.category(c).startswith('C') for c in i) for i in value)
            or len(set(value)) != len(value)):
        raise PolicyError('Provide up to 20 unique existing label names, <=50 characters each.')
    return value

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


def pull_request_rules(data):
    # The official MCP server serializes go-github's grouped branch rules;
    # the REST representation is a flat list. Accept both explicit shapes.
    if isinstance(data, dict):
        entries = data.get('PullRequest')
        entries = [] if entries is None else entries
        if isinstance(entries, list) and all(isinstance(entry, dict) for entry in entries):
            return [{**entry, 'type': 'pull_request'} for entry in entries]
    elif isinstance(data, list) and all(isinstance(entry, dict) for entry in data):
        return [entry for entry in data if entry.get('type') == 'pull_request']
    raise PolicyError('GitHub returned invalid branch rules; writes remain blocked.')


def review_protection(fetch):
    """Effective rules exclude disabled rulesets; the caller authenticates as the App."""
    rules = pull_request_rules(fetch(PREFIX + '/rules/branches/' + BASE))
    if not rules:
        raise PolicyError('GitHub returned invalid branch rules; writes remain blocked.')
    for rule in rules:
        if rule.get('type') != 'pull_request' or not isinstance(rule.get('ruleset_id'), int):
            continue
        parameters = rule.get('parameters', {})
        approvals = parameters.get('required_approving_review_count', 0)
        if (type(approvals) is not int or approvals < 1
                or parameters.get('require_code_owner_review') is not True
                or parameters.get('dismiss_stale_reviews_on_push') is not True):
            continue
        details = fetch(PREFIX + '/rulesets/' + str(rule['ruleset_id']))
        if details.get('enforcement') != 'active' or details.get('current_user_can_bypass') != 'never':
            continue
        if 'bypass_actors' in details and details['bypass_actors'] != []:
            continue
        # GitHub may hide the full bypass list. Its authenticated actor result
        # must explicitly say the App cannot bypass; missing data is not proof.
        # CODEOWNERS on the base branch determines the actual reviewers.
        return {'required_approvals': approvals, 'require_code_owner_review': True,
                'dismiss_stale_approvals': True, 'app_can_bypass': False,
                'bypass_list_visible': 'bypass_actors' in details, 'ruleset_id': details['id']}
    raise PolicyError('main needs active PR/code-owner review with stale-approval dismissal and no App bypass; writes remain blocked.')


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
