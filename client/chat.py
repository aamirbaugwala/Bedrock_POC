"""CLI chat client for the agent service.

  python client/chat.py --list
  python client/chat.py --agent aws_architect            # interactive, multi-turn
  python client/chat.py --agent oss_mentor -q "How does strands-agents/sdk-python load MCP tools?"
  python client/chat.py --all -q "Should we run MCP servers on AgentCore Runtime?"   # same question, every persona

Add --cloud to talk to the deployed AgentCore Runtimes (one per persona) instead of localhost,
signed in as a demo Cognito user (--user, default meera).
"""

import argparse
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.env import load_env, state_path  # noqa: E402
from common.identity import demo_user_token  # noqa: E402

URL = os.environ.get("AGENT_URL", "http://localhost:8080/invocations")
CLOUD = False
USER = "meera"
COLORS = ["\033[36m", "\033[35m", "\033[33m", "\033[32m"]
RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"


def _runtimes() -> dict:
    return json.loads(state_path().read_text())["agent_runtimes"]


def ask(agent: str, prompt: str, session_id: str | None = None) -> tuple[dict, float]:
    start = time.monotonic()
    if CLOUD:
        # JWT-authorized runtimes are called over plain HTTPS with the user's bearer token (not SigV4).
        r = requests.post(_runtimes()[agent]["invocation_url"], json={"prompt": prompt}, timeout=180, headers={
            "Authorization": f"Bearer {demo_user_token(USER)}",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id or f"oneshot-{uuid.uuid4()}",
        })
        return r.json(), time.monotonic() - start
    r = requests.post(URL, json={"agent": agent, "prompt": prompt, "session_id": session_id}, timeout=120)
    r.raise_for_status()
    return r.json(), time.monotonic() - start


def show(resp: dict, secs: float, color: str = COLORS[0]) -> None:
    name = resp.get("agent", "?")
    print(f"\n{color}{BOLD}── {name} ─────────────────────────{RESET}")
    if "error" in resp:
        print(f"{color}error: {resp['error']}{RESET}")
        return
    print(resp["answer"])
    print(f"{DIM}tools_used: {resp['tools_used'] or 'none'}   ({secs:.1f}s){RESET}")


def list_agents() -> dict:
    if CLOUD:
        return {name: [r["name"]] for name, r in _runtimes().items()}
    return requests.post(URL, json={"agent": "__list__"}, timeout=10).json()["agents"]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--agent")
    p.add_argument("--all", action="store_true", help="ask every persona the same question in parallel")
    p.add_argument("--list", action="store_true")
    p.add_argument("-q", "--question")
    p.add_argument("--cloud", action="store_true", help="use the deployed AgentCore Runtimes")
    p.add_argument("--user", default="meera", help="demo Cognito user for --cloud")
    args = p.parse_args()
    global CLOUD, USER
    CLOUD, USER = args.cloud, args.user
    if CLOUD:
        load_env()

    if args.list:
        for name, tools in list_agents().items():
            print(f"{BOLD}{name}{RESET}: {', '.join(tools)}")
        return

    if args.all:
        if not args.question:
            sys.exit("--all needs -q")
        agents = list(list_agents())
        with ThreadPoolExecutor(len(agents)) as pool:
            results = list(pool.map(lambda a: ask(a, args.question), agents))
        for i, (resp, secs) in enumerate(results):
            show(resp, secs, COLORS[i % len(COLORS)])
        return

    if not args.agent:
        sys.exit("pass --agent <name>, --all, or --list")
    if args.question:
        show(*ask(args.agent, args.question))
        return

    session_id = f"chat-{uuid.uuid4()}"  # >= 33 chars, matches the AgentCore session-id rule
    print(f"{DIM}chatting with {args.agent} (session {session_id}); Ctrl-D to quit{RESET}")
    while True:
        try:
            prompt = input(f"\n{BOLD}you>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if prompt:
            show(*ask(args.agent, prompt, session_id))


if __name__ == "__main__":
    main()
