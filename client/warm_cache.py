"""Pre-fetch company snapshots through the Gateway so demo questions hit the shared cache.

  python client/warm_cache.py MSFT NVDA AAPL

Each new ticker costs 6 Alpha Vantage calls (free tier: 25/day); cached tickers cost nothing.
"""

import asyncio
import json
import os
import sys
import time
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


async def main(symbols: list[str]) -> None:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {demo_user_token(USER)}"}, timeout=120) as http:
        async with streamable_http_client(URL, http_client=http) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for symbol in symbols:
                    start = time.monotonic()
                    result = await session.call_tool("finance___get_company_snapshot", {"symbol": symbol})
                    snap = json.loads("".join(getattr(c, "text", "") for c in result.content))
                    gaps = snap.get("data_gaps") or "none"
                    print(f"{symbol:6s} {time.monotonic() - start:5.1f}s  data gaps: {gaps}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    asyncio.run(main([s.upper() for s in sys.argv[1:]]))
