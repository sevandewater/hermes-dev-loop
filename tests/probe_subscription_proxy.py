"""Reproduce upstream's proxy-environment limitation; not a security-boundary test.

Runs the unmodified upstream transport function against two loopback HTTP listeners.
Requires Python 3.11+ and aiohttp==3.14.3. No real credentials or provider calls.
"""

import argparse
import ast
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web


EXPECTED_BLOB = "49575154482db3d336b00501dc74dfb5b8711a89"


def load_transport(checkout):
    source = checkout / "hermes_cli/proxy/server.py"
    raw = source.read_bytes()
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    if blob != EXPECTED_BLOB:
        raise RuntimeError("Upstream transport changed; review the new source before probing")
    tree = ast.parse(raw, filename=str(source))
    names = {"_open_upstream", "_filter_headers", "_json_error"}
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_HOP_BY_HOP_HEADERS"
            for target in node.targets
        ):
            nodes.append(node)
    namespace = {"aiohttp": aiohttp, "web": web, "logger": logging.getLogger("probe")}
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["_open_upstream"]


async def probe(checkout):
    if aiohttp.__version__ != "3.14.3":
        raise RuntimeError("Use aiohttp==3.14.3 to match the reviewed upstream lockfile")
    transport = load_transport(checkout)
    hits = {"direct": 0, "proxy": 0}

    async def listener(reader, writer, route):
        try:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            hits[route] += 1
            body = route.encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    direct = await asyncio.start_server(
        lambda reader, writer: listener(reader, writer, "direct"), "127.0.0.1", 0
    )
    proxy = await asyncio.start_server(
        lambda reader, writer: listener(reader, writer, "proxy"), "127.0.0.1", 0
    )
    async with direct, proxy:
        direct_port = direct.sockets[0].getsockname()[1]
        proxy_port = proxy.sockets[0].getsockname()[1]
        proxy_url = f"http://127.0.0.1:{proxy_port}"
        keys = ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY")
        previous = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ[key] = "" if key.lower() == "no_proxy" else proxy_url
            request = SimpleNamespace(method="GET", headers={}, query_string="")
            credential = SimpleNamespace(
                base_url=f"http://127.0.0.1:{direct_port}/v1",
                token_type="Bearer", bearer="dummy-not-a-provider-credential",
            )
            session, response = await transport(request, "/models", b"", credential)
            if response is None:
                raise RuntimeError("Upstream transport failed before routing could be observed")
            try:
                body = await response.text()
            finally:
                response.release()
                await session.close()
            if body != "direct" or hits != {"direct": 1, "proxy": 0}:
                raise RuntimeError(f"Expected limitation was not reproduced: {hits}")
            observed = dict(hits)
            # Positive control: prove that the same proxy and environment actually work.
            async with aiohttp.ClientSession(trust_env=True) as control:
                async with control.get(credential.base_url + "/models") as response:
                    if await response.text() != "proxy":
                        raise RuntimeError("Positive control did not reach the proxy")
            if hits != {"direct": 1, "proxy": 1}:
                raise RuntimeError(f"Unexpected positive-control routing: {hits}")
            print(json.dumps({
                "result": "LIMITATION_REPRODUCED",
                "upstream_transport_hits": observed,
                "positive_control_proxy_hits": hits["proxy"],
                "aiohttp": aiohttp.__version__,
                "security_boundaries_verified": False,
            }, sort_keys=True))
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    asyncio.run(asyncio.wait_for(probe(args.checkout), timeout=20))
