"""Real upstream Subscription Proxy with a fake adapter; no OAuth or provider calls."""
import asyncio
import os
from pathlib import Path

from aiohttp import web
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential
from hermes_cli.proxy.server import run_server


class Adapter(UpstreamAdapter):
    name = "devloop-fixture"
    display_name = "Credential-free test fixture"
    allowed_paths = frozenset({"/models"})

    def is_authenticated(self):
        return True

    def get_credential(self):
        return UpstreamCredential(bearer="dummy-provider-fixture", base_url="http://127.0.0.1:18645/v1")


async def main():
    async def models(request):
        return web.json_response({
            "object": "list", "data": [],
            "fixture": True,
            "credential_attached": request.headers.get("Authorization") == "Bearer dummy-provider-fixture",
        })

    origin = web.Application()
    origin.router.add_get("/v1/models", models)
    runner = web.AppRunner(origin, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 18645).start()
    Path("/tmp/devloop-gateway-fixture.pid").write_text(str(os.getpid()))
    try:
        await asyncio.wait_for(run_server(Adapter(), host="10.237.81.2", port=8645), 180)
    finally:
        await runner.cleanup()


asyncio.run(main())
