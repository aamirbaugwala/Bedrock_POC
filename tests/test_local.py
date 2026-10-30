"""Local acceptance tests: finance MCP reachable, personas use only their tools and keep their format.

Needs the finance MCP running locally (make run-mcp-local). It caches each company once per
day, so re-runs cost no Alpha Vantage quota. Set ALL_PERSONAS=1 to check every persona.

The agent tests start agent_service/agent.py on a free port and talk to it over HTTP.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.env import load_env  # noqa: E402

load_env()
CONFIG = yaml.safe_load((ROOT / "agent_service/agents.yaml").read_text())
PERSONAS = CONFIG["agents"]
MCP_URL = CONFIG["mcp_servers"]["finance"]["url"]
# One marker per persona proves its output format (same data, different lens).
FORMAT_MARKERS = {
    "financial_advisor": "Bottom line",
    "recruiter": "Pitch",
    "cfo_briefing": "|",
    "risk_compliance": "Risk rating",
}
QUESTION = "Give me your take on Microsoft (MSFT)."


def allowed(persona: str) -> set[str]:
    return {t for tools in PERSONAS[persona]["tools"].values() for t in tools}


def base(tool: str) -> str:
    return tool.split("___")[-1]


# --- MCP layer ---------------------------------------------------------------

async def _list_and_call() -> tuple[list[str], dict]:
    async with streamable_http_client(MCP_URL) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = [t.name for t in (await session.list_tools()).tools]
            result = await session.call_tool("get_company_snapshot", {"symbol": "MSFT"})
            return names, json.loads("".join(getattr(c, "text", "") for c in result.content))


def test_mcp():
    names, snap = asyncio.run(_list_and_call())
    expected = {t for p in PERSONAS.values() for t in p["tools"]["finance"]}
    assert expected <= set(names), f"missing tools: {expected - set(names)}"
    assert not snap["data_gaps"], snap["data_gaps"]
    assert snap["income_latest_fy"]["revenue"] > 1e11, snap


# --- Agent service -----------------------------------------------------------

@pytest.fixture(scope="session")
def agent_url():
    if url := os.environ.get("AGENT_URL"):
        yield url
        return
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if k != "GATEWAY_URL"}
    proc = subprocess.Popen([sys.executable, str(ROOT / "agent_service/agent.py")], env={**env, "PORT": str(port)})
    try:
        for _ in range(60):
            try:
                if requests.get(f"http://localhost:{port}/ping", timeout=1).ok:
                    break
            except requests.ConnectionError:
                time.sleep(0.5)
        else:
            pytest.fail("agent service did not start")
        yield f"http://localhost:{port}/invocations"
    finally:
        proc.terminate()
        proc.wait(10)


def invoke(url, agent, prompt):
    r = requests.post(url, json={"agent": agent, "prompt": prompt}, timeout=120)
    r.raise_for_status()
    return r.json()


def test_unknown_agent(agent_url):
    start = time.monotonic()
    resp = invoke(agent_url, "nope", "hello")
    assert resp["error"] == "unknown agent"
    assert time.monotonic() - start < 2, "should return without a model call"


PERSONAS_UNDER_TEST = list(FORMAT_MARKERS) if os.environ.get("ALL_PERSONAS") else ["financial_advisor"]


@pytest.mark.parametrize("persona", PERSONAS_UNDER_TEST)
def test_persona_answers_in_its_format(agent_url, persona):
    resp = invoke(agent_url, persona, QUESTION)
    assert "error" not in resp, resp
    assert resp["tools_used"], resp
    assert {base(t) for t in resp["tools_used"]} <= allowed(persona)
    assert len(resp["tools_used"]) <= PERSONAS[persona]["max_tool_calls"]
    assert FORMAT_MARKERS[persona] in resp["answer"], resp["answer"]
