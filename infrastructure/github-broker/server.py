"""Narrow MCP policy adapter around the maintained GitHub MCP stdio server."""
from contextlib import asynccontextmanager
import asyncio
import json
import os
import socket
from typing import Annotated

from pydantic import Field

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from issue_management import Issues

from policy import APP_ID, BASE, OWNER, REPO, PREFIX, PolicyError, review_protection, pull_request_rules, proposal_branch, source_path, validate_files

UPSTREAM_TOOLS = ('get_file_contents', 'pull_request_read', 'create_branch', 'push_files', 'create_pull_request',
                  'repository_ruleset_read', 'list_issues', 'issue_read', 'issue_write', 'list_label', 'add_issue_comment')


@asynccontextmanager
async def lifespan(server):
    with open('/broker-secrets/github-app.json') as handle:
        settings = json.load(handle)
    if str(settings.get('app_id')) != str(APP_ID):
        raise RuntimeError('Broker App ID does not match the approved App.')
    environment = {name: os.environ[name] for name in (
        'PATH', 'HOME', 'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy'
    ) if name in os.environ}
    environment.update({
        'GITHUB_APP_ID': str(settings['app_id']),
        'GITHUB_APP_INSTALLATION_ID': str(settings['installation_id']),
        'GITHUB_APP_PRIVATE_KEY_PATH': '/broker-secrets/github-app.pem',
    })
    params = StdioServerParameters(command='/usr/local/bin/github-mcp-server',
        args=['stdio', '--tools=' + ','.join(UPSTREAM_TOOLS)], env=environment)
    # Upstream stderr can include credential-related failures; do not forward it to agent logs.
    with open(os.devnull, 'w') as errors:
        async with stdio_client(params, errlog=errors) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listed = {tool.name for tool in (await session.list_tools()).tools}
                if listed != set(UPSTREAM_TOOLS):
                    raise RuntimeError('Upstream MCP tool policy mismatch.')
                yield session


server = MCPServer('Hermes project GitHub broker', lifespan=lifespan, log_level='ERROR',
    instructions='Read the project and propose changes on hermes/ branches. The human alone merges and deploys.')


async def backend(ctx, name, args):
    if name not in UPSTREAM_TOOLS:
        raise PolicyError('Operation is not exposed by this broker.')
    try:
        result = await ctx.request_context.lifespan_context.call_tool(name, {'owner': OWNER, 'repo': REPO, **args})
    except Exception:
        raise PolicyError('GitHub operation failed; verify App authentication and repository access.') from None
    if result.is_error:
        raise PolicyError('GitHub rejected the operation. Check repository permissions and branch requirements.')
    return result


async def rules_json(ctx, **arguments):
    result = await backend(ctx, 'repository_ruleset_read', {'level': 'repository', **arguments})
    for block in result.content:
        if getattr(block, 'type', None) == 'text':
            try:
                return json.loads(block.text)
            except ValueError:
                pass
    raise PolicyError('GitHub returned invalid review-rule data; writes remain blocked.')


async def current_review_protection(ctx):
    rules = pull_request_rules(await rules_json(ctx, method='get_rules_for_branch', branch=BASE))
    details = {}
    for rule_id in {r['ruleset_id'] for r in rules if r.get('type') == 'pull_request' and type(r.get('ruleset_id')) is int}:
        details[PREFIX + '/rulesets/' + str(rule_id)] = await rules_json(ctx, method='get', ruleset_id=rule_id)
    return review_protection(lambda path: rules if path == PREFIX + '/rules/branches/' + BASE else details[path])


@server.tool()
async def broker_status(ctx: Context) -> dict:
    """Check the required PR/code-owner review settings and App bypass permission. Does not write."""
    try:
        protection = await current_review_protection(ctx)
        return {'repository': OWNER + '/' + REPO, 'writes_enabled': True, **protection}
    except PolicyError as error:
        return {'repository': OWNER + '/' + REPO, 'writes_enabled': False, 'reason': str(error)}


@server.tool()
async def read_project_file(path: str, ctx: Context, ref: str = BASE):
    """Read a tracked file from the approved project repository."""
    source_path(path)
    if ref != BASE:
        proposal_branch(ref)
    return await backend(ctx, 'get_file_contents', {'path': path, 'ref': 'refs/heads/' + ref})


@server.tool()
async def read_pull_request(number: int, ctx: Context, method: str = 'get'):
    """Read a project PR, its diff, files or reviews."""
    if number < 1 or method not in ('get', 'get_diff', 'get_files', 'get_reviews', 'get_comments', 'get_status'):
        raise PolicyError('Invalid pull request read operation.')
    return await backend(ctx, 'pull_request_read', {'pullNumber': number, 'method': method})


@server.tool()
async def create_proposal_branch(branch: str, ctx: Context):
    """Create a hermes/ proposal branch from main. Required PR review must have no bypass."""
    proposal_branch(branch)
    await current_review_protection(ctx)
    return await backend(ctx, 'create_branch', {'branch': branch, 'from_branch': BASE})


@server.tool()
async def commit_proposal_files(branch: str, files: list[dict], message: str, ctx: Context):
    """Commit up to 50 text files on a hermes/ branch; cannot update main or workflows."""
    proposal_branch(branch)
    validate_files(files)
    if not message.strip() or len(message) > 2000:
        raise PolicyError('Provide a nonempty commit message under 2000 characters.')
    await current_review_protection(ctx)
    return await backend(ctx, 'push_files', {'branch': branch, 'files': files, 'message': message})


@server.tool()
async def open_proposal_pull_request(branch: str, title: str, body: str, ctx: Context):
    """Open a draft proposal against main. No merge, approval or auto-merge operation is available."""
    proposal_branch(branch)
    if not title.strip() or len(title) > 200 or len(body) > 20_000:
        raise PolicyError('Provide a title under 200 characters and body under 20000 characters.')
    await current_review_protection(ctx)
    return await backend(ctx, 'create_pull_request', {'head': branch, 'base': BASE, 'title': title, 'body': body,
        'draft': True, 'maintainer_can_modify': True})


issue_mutation_lock = asyncio.Lock()
# Reject coercible identifiers before the SDK can turn them into a different target.
IssueNumber = Annotated[int, Field(strict=True, ge=1, le=2_147_483_647)]


def issues(ctx):
    return Issues(lambda name, args: backend(ctx, name, args))


@server.tool()
async def list_project_issues(ctx: Context, query: str = '', state: str = 'all', after: str = '') -> dict:
    """List/search issue titles/bodies by literal substring, excluding PRs. Follow next_cursor even on empty pages."""
    return await issues(ctx).list(query, state, after)


@server.tool()
async def read_project_issue(number: IssueNumber, ctx: Context) -> dict:
    """Read exact issue metadata and all comment pages. Fails rather than silently truncates."""
    return await issues(ctx).read(number)


@server.tool()
async def list_project_labels(ctx: Context) -> dict:
    """List existing repository labels. Fails if upstream's 100-label catalog is incomplete."""
    return await issues(ctx).available_labels()


@server.tool()
async def create_project_issue(title: str, body: str, request_id: str, ctx: Context) -> dict:
    """Create and read back an issue. Supply a lowercase UUID; retries MUST reuse that ID and exact payload."""
    async with issue_mutation_lock:
        return await issues(ctx).create(title, body, request_id)


@server.tool()
async def update_project_issue(number: IssueNumber, ctx: Context, title: str | None = None, body: str | None = None) -> dict:
    """Update only title/body and verify exact readback. No state changes; empty-body clearing is unsupported upstream."""
    async with issue_mutation_lock:
        return await issues(ctx).update(number, title, body)


@server.tool()
async def change_project_issue_labels(number: IssueNumber, add: list[str], remove: list[str], ctx: Context) -> dict:
    """Add/remove exact existing labels, preserving unrelated labels; verify exact readback. No label creation."""
    async with issue_mutation_lock:
        return await issues(ctx).labels(number, add, remove)


@server.tool()
async def comment_on_project_issue(number: IssueNumber, body: str, request_id: str, ctx: Context) -> dict:
    """Add/read back an issue comment. Reuse the same lowercase UUID and payload when reconciling any failure."""
    async with issue_mutation_lock:
        return await issues(ctx).comment(number, body, request_id)


@server.custom_route('/health', methods=['GET'])
async def health(request):
    return JSONResponse({'status': 'ok', 'repository': OWNER + '/' + REPO})


if __name__ == '__main__':
    server.run(transport='streamable-http', host=socket.gethostbyname('github-broker'), port=8000,
        stateless_http=True, json_response=True, max_request_body_size=2_000_000, max_sessions=8,
        transport_security=TransportSecuritySettings(allowed_hosts=['github-broker:8000'],
                                                   allowed_origins=['http://github-broker:8000']))
