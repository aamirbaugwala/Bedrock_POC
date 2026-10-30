"""List tools through the AgentCore Gateway as a demo Cognito user, and check token-less calls are rejected."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.env import load_env, state_path  # noqa: E402
from common.identity import demo_user_token  # noqa: E402

load_env()
URL = json.loads(state_path().read_text())["gateway"]["url"]
USER = os.environ.get("DEMO_USER", "meera")  # any demo pool user; the Gateway checks the token


async def main() -> None:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {demo_user_token(USER)}"}, timeout=60) as http:
        async with streamable_http_client(URL, http_client=http) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
    print(f"{URL}\n{len(tools)} tools:")
    for t in sorted(tools, key=lambda t: t.name):
        print(f"  {t.name}")

    r = httpx.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Accept": "application/json, text/event-stream"})
    print(f"\nrequest without a token -> HTTP {r.status_code}")


asyncio.run(main())
