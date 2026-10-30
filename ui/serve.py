"""Serve the demo UI on http://localhost:3000.

This is only a static file server. The browser talks to AWS directly:
  - Cognito InitiateAuth (sign-in)              https://cognito-idp.<region>.amazonaws.com/
  - AgentCore Runtime invocations (chat)        https://bedrock-agentcore.<region>.amazonaws.com/runtimes/...
/config.json hands the page the public identifiers it needs (app client ID, runtime URLs).
"""

import json
import os
import sys
from pathlib import Path

import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.env import load_env, state_path  # noqa: E402

load_env()


def config(request):
    state = json.loads(state_path().read_text())
    personas = yaml.safe_load((ROOT / "agent_service/agents.yaml").read_text())["agents"]
    return JSONResponse({
        "region": os.environ["AWS_REGION"],
        "model": os.environ.get("MODEL_ID"),
        "cognito": {"client_id": state["cognito"]["client_id"], "pool_id": state["cognito"]["pool_id"],
                    "users": state["cognito"]["users"]},
        # Demo pool only, served on localhost only, so the user picker can sign in with one click.
        "demo_password": os.environ.get("DEMO_USER_PASSWORD") if os.environ.get("UI_PREFILL_PASSWORD", "1") == "1" else None,
        "personas": [{
            "id": pid, "label": p.get("label", pid), "blurb": p.get("blurb", ""),
            "allowed_groups": p.get("allowed_groups", []),
            "runtime_name": state["agent_runtimes"][pid]["name"],
            "url": state["agent_runtimes"][pid]["invocation_url"],
        } for pid, p in personas.items() if pid in state.get("agent_runtimes", {})],
        "gateway": {"url": state["gateway"]["url"], "interceptor": state.get("interceptor_arn", "").split(":")[-1]},
        "mcp_runtime": state["mcp_runtime"]["name"],
    })


app = Starlette(routes=[
    Route("/", lambda r: FileResponse(Path(__file__).with_name("index.html"))),
    Route("/config.json", config),
    Route("/favicon.ico", lambda r: Response(status_code=204)),
])

if __name__ == "__main__":
    print("demo UI on http://localhost:3000")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("UI_PORT", 3000)), log_level="warning")
