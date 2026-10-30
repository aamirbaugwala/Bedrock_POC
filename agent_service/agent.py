"""Multi-persona agent service.

One process serves every persona defined in agents.yaml. Each request picks a
persona, checks the caller's Cognito groups against it, connects to its MCP
tools, and runs a Strands agent on Bedrock. Served with the AgentCore Runtime
SDK, so the same code runs locally on :8080 and on AgentCore Runtime.

POST /invocations {"agent": "...", "prompt": "...", "session_id": "optional", "stream": false}
  -> {"agent", "answer", "tools_used", "identity", "events", "summary"}
  With "stream": true the same events arrive live as Server-Sent Events, ending in a "done" event.

`events` describe each hop this request makes from inside the agent (Gateway tool listing,
every Bedrock model call, every tool call with the MCP server's own timing stamp), so a UI can
draw the request's journey.

Role routing (agents.marketpro.yaml): with no "agent" in the payload, the agent asks the data
source for the caller's role (`role_lookup`), using the caller's own token, and runs the persona
whose `roles` list it, or the `default` persona. The browser can't pick or fake the persona.
"""

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

import yaml
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from botocore.exceptions import ClientError
from starlette.middleware.cors import CORSMiddleware
from strands import Agent
from strands.hooks import AfterModelCallEvent, AfterToolCallEvent, BeforeModelCallEvent, BeforeToolCallEvent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import load_env  # noqa: E402
from common.identity import caller  # noqa: E402

load_env()

MAX_TOOL_CALLS = 8
RUN_TIMEOUT_S = int(os.environ.get("RUN_TIMEOUT_S", 60))
# Cloud: one connection to the AgentCore Gateway, carrying the caller's token. Local: straight to each MCP server.
GATEWAY_URL = os.environ.get("GATEWAY_URL")
# Cloud runtimes sit behind a Cognito JWT authorizer, so a token is always present there.
REQUIRE_AUTH = os.environ.get("REQUIRE_AUTH") == "1"
# Set per runtime in the cloud so a payload only needs {"prompt": ...}.
DEFAULT_AGENT = os.environ.get("DEFAULT_AGENT")
CONFIG_PATH = Path(os.environ.get("AGENTS_CONFIG", Path(__file__).with_name("agents.yaml")))
PREVIEW_CHARS = 1200
# Payload "agent" value (or its absence, with role routing configured) that picks the persona by role.
AUTO = "auto"
# Local development only: browser origins allowed to call this process directly (AgentCore handles CORS itself).
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
# Demo only: with role routing, let the browser pick the persona ("agent" in the payload) and list the
# choices in the intro. Off, the caller's role always decides. Data access is the caller's either way.
ALLOW_PERSONA_OVERRIDE = os.environ.get("ALLOW_PERSONA_OVERRIDE") == "1"

log = logging.getLogger("agent_service")
logging.basicConfig(level=logging.INFO, format="%(message)s")


def _expand(value: str) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), ""), value)


def load_config(path: Path = CONFIG_PATH) -> tuple[dict, dict, dict | None]:
    """Load and validate agents.yaml; fail fast on anything malformed."""
    raw = yaml.safe_load(path.read_text())
    servers = raw.get("mcp_servers") or {}
    personas = raw.get("agents") or {}
    role_lookup = raw.get("role_lookup")
    if not personas:
        raise ValueError(f"{path}: no agents defined")
    for name, spec in personas.items():
        missing = {"model", "prompt", "tools"} - set(spec or {})
        if missing:
            raise ValueError(f"{path}: agent '{name}' missing {sorted(missing)}")
        spec["model"] = _expand(spec["model"])
        if not spec["model"]:
            raise ValueError(f"agent '{name}': model is empty (is MODEL_ID set?)")
        for server in spec["tools"]:
            if server not in servers:
                raise ValueError(f"agent '{name}': unknown MCP server '{server}'")
    if role_lookup:
        missing = {"server", "tool", "arguments", "path"} - set(role_lookup)
        if missing:
            raise ValueError(f"{path}: role_lookup missing {sorted(missing)}")
        if role_lookup["server"] not in servers:
            raise ValueError(f"{path}: role_lookup uses unknown MCP server '{role_lookup['server']}'")
        defaults = [name for name, spec in personas.items() if spec.get("default")]
        if len(defaults) != 1:
            raise ValueError(f"{path}: role routing needs exactly one persona with default: true")
    return servers, personas, role_lookup


def _tool_matches(tool_name: str, allowed: list[str]) -> bool:
    return any(tool_name == a or tool_name.endswith(f"___{a}") for a in allowed)


SERVERS, PERSONAS, ROLE_LOOKUP = load_config()
# session_id -> conversation history, per persona. In-memory; fine for a demo.
SESSIONS: dict[tuple[str, str], list] = {}
# AgentCore gives each session its own microVM; the first request in it pays the cold start.
_cold = True

app = BedrockAgentCoreApp()
if CORS_ORIGINS:
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["POST", "GET", "OPTIONS"],
                       allow_headers=["*"])


class Forbidden(Exception):
    """The caller's groups don't include any of the persona's allowed_groups."""


def _authorized(who: dict | None, persona: dict) -> bool:
    allowed_groups = persona.get("allowed_groups") or []
    return not (who and allowed_groups) or bool(set(who["groups"]) & set(allowed_groups))


def _persona_for_role(role: str | None) -> str:
    """The persona whose `roles` include `role` (case-insensitive), else the default persona."""
    wanted = (role or "").strip().lower()
    for name, spec in PERSONAS.items():
        if wanted and wanted in {r.lower() for r in spec.get("roles") or []}:
            return name
    return next(name for name, spec in PERSONAS.items() if spec.get("default"))


def _dig(data, path: str):
    for key in path.split("."):
        data = data.get(key) if isinstance(data, dict) else None
    return data


def _lookup_role(authorization: str | None) -> str | None:
    """Ask the data source for the caller's role with the caller's own token (blocking)."""
    server = SERVERS[ROLE_LOOKUP["server"]]
    headers = {"Authorization": authorization} if authorization else None
    url, tool = server["url"], ROLE_LOOKUP["tool"]
    if GATEWAY_URL:
        url, tool = GATEWAY_URL, f"{server['gateway_target']}___{tool}"
    with MCPClient(url=url, headers=headers, startup_timeout=20) as client:
        result = client.call_tool_sync(f"role-{uuid.uuid4().hex[:8]}", tool, ROLE_LOOKUP["arguments"])
    if result.get("status") != "success":
        return None
    for block in result.get("content", []):
        try:
            return _dig(json.loads(block.get("text", "")), ROLE_LOOKUP["path"])
        except (ValueError, AttributeError):
            continue
    return None


async def resolve_persona(authorization: str | None, journey: "Journey") -> str:
    journey.emit("role_lookup", phase="start", id="role")
    t0 = journey.ms()
    try:
        role = await asyncio.to_thread(_lookup_role, authorization)
    except Exception as e:  # noqa: BLE001 - an unreachable role lookup falls back to the default persona
        log.warning(json.dumps({"event": "role_lookup_failed", "error": str(e)[:300]}))
        role = None
    name = _persona_for_role(role)
    journey.emit("role_lookup", phase="end", id="role", ms=journey.ms() - t0, role=role, persona=name)
    return name


def _data_sources(text: str) -> list[dict]:
    """Which root datasets a GraphQL tool result came from, and how many records matched.

    Lets a UI say "Based on Firms · 4 records" from what was actually queried.
    """
    try:
        data = json.loads(text).get("data")
    except (ValueError, AttributeError):
        return []
    sources = []
    for field, value in (data or {}).items():
        if field == "user" or not isinstance(value, dict):
            continue
        rows = (value.get("queryInfo") or {}).get("rowCount")
        listed = value.get("list")
        sources.append({"dataset": field, "matched": rows if rows is not None else (
            len(listed) if isinstance(listed, list) else None)})
    return sources


def _persona_event(name: str) -> dict:
    spec = PERSONAS[name]
    return {"persona": name, "label": spec.get("label", name), "blurb": spec.get("blurb", ""),
            "suggestions": spec.get("suggestions", [])}


class Journey:
    """Collects the hops of one request; with a queue, also pushes each one out live."""

    def __init__(self):
        self.events: list[dict] = []
        self.queue: asyncio.Queue | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.start = time.monotonic()

    def go_live(self) -> None:
        """Start pushing events to a queue on the loop that is consuming the stream."""
        self.loop, self.queue = asyncio.get_running_loop(), asyncio.Queue()

    def ms(self) -> int:
        return int((time.monotonic() - self.start) * 1000)

    def emit(self, type_: str, **data) -> dict:
        event = {"type": type_, "t_ms": self.ms(), **data}
        self.events.append(event)
        if self.queue is not None:  # hooks may fire on MCP client threads
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
        return event


def _mcp_clients(persona: dict, authorization: str | None) -> list[MCPClient]:
    headers = {"Authorization": authorization} if authorization else None
    if GATEWAY_URL:
        # Gateway names tools <target>___<tool>; only allow the persona's tools on the right target.
        allowed = {SERVERS[server]["gateway_target"]: tools for server, tools in persona["tools"].items()}

        def gateway_filter(t) -> bool:
            target, _, tool = t.mcp_tool.name.partition("___")
            return target in allowed and _tool_matches(tool, allowed[target])

        # The caller's own token goes to the Gateway (its JWT authorizer checks it); the Gateway's
        # interceptor then hands the caller to the MCP server inside the request's _meta.
        return [MCPClient(url=GATEWAY_URL, headers=headers, tool_filters={"allowed": [gateway_filter]}, startup_timeout=20)]

    return [
        MCPClient(
            url=SERVERS[server]["url"],
            headers=headers,
            tool_filters={"allowed": [lambda t, allowed=allowed: _tool_matches(t.mcp_tool.name, allowed)]},
            startup_timeout=20,
        )
        for server, allowed in persona["tools"].items()
    ]


def _split_mcp_trace(result: dict | None) -> tuple[dict | None, dict | None, str, int]:
    """Remove the MCP server's `_trace` stamp from a tool result (the model never sees it).

    Returns (stamp, result without the stamp, a short preview for the UI, result size in bytes).
    """
    stamp, blocks, text = None, [], ""
    for block in (result or {}).get("content", []):
        try:
            data = json.loads(block.get("text", ""))
            if isinstance(data, dict) and "_trace" in data:
                stamp = data.pop("_trace")
                block = {**block, "text": json.dumps(data)}
        except (ValueError, AttributeError, TypeError):
            pass
        blocks.append(block)
        text += block.get("text", "")
    cleaned = {**result, "content": blocks} if result else result
    return stamp, cleaned, text[:PREVIEW_CHARS], len(text.encode())


async def run_persona(name: str, prompt: str, session_id: str | None, authorization: str | None, journey: Journey) -> dict:
    if name == AUTO:
        name = await resolve_persona(authorization, journey)
        if not _authorized(caller(authorization), PERSONAS[name]):
            raise Forbidden(name)
    journey.emit("persona", **_persona_event(name))
    persona = PERSONAS[name]
    max_calls = persona.get("max_tool_calls", MAX_TOOL_CALLS)
    tools_used: list[str] = []
    model_calls = 0
    started: dict[str, tuple[int, int]] = {}  # step id -> (journey ms, epoch ms)

    def before_model(event: BeforeModelCallEvent) -> None:
        nonlocal model_calls
        model_calls += 1
        started["model"] = (journey.ms(), 0)
        journey.emit("model", phase="start", id=f"model-{model_calls}", n=model_calls, model=persona["model"],
                     messages=len(event.agent.messages), tools=len(tools))

    def after_model(event: AfterModelCallEvent) -> None:
        began, _ = started.pop("model", (journey.ms(), 0))
        detail: dict = {"stop_reason": "error", "wants": [], "text_chars": 0}
        if event.stop_response:
            message = event.stop_response.message
            usage = (message.get("metadata") or {}).get("usage") or {}
            detail = {
                "stop_reason": event.stop_response.stop_reason,
                "wants": [{"name": b["toolUse"]["name"], "input": b["toolUse"].get("input")}
                          for b in message.get("content", []) if "toolUse" in b],
                "text_chars": sum(len(b.get("text", "")) for b in message.get("content", [])),
                "tokens_in": usage.get("inputTokens"), "tokens_out": usage.get("outputTokens"),
            }
        elif event.exception:
            detail["error"] = str(event.exception)[:300]
        journey.emit("model", phase="end", id=f"model-{model_calls}", n=model_calls, ms=journey.ms() - began, **detail)

    def before_tool(event: BeforeToolCallEvent) -> None:
        use = event.tool_use
        if len(tools_used) >= max_calls:
            event.cancel_tool = f"Tool call limit ({max_calls}) reached; answer with what you have."
            journey.emit("tool", phase="cancelled", id=use["toolUseId"], name=use["name"], reason=event.cancel_tool)
            return
        tools_used.append(use["name"])
        started[use["toolUseId"]] = (journey.ms(), int(time.time() * 1000))
        journey.emit("tool", phase="start", id=use["toolUseId"], name=use["name"], input=use.get("input"))

    def after_tool(event: AfterToolCallEvent) -> None:
        use = event.tool_use
        if use["toolUseId"] not in started:
            return  # cancelled above
        began, began_epoch = started.pop(use["toolUseId"])
        stamp, cleaned, preview, size = _split_mcp_trace(event.result)
        if cleaned is not None:
            event.result = cleaned
        full_text = "".join(b.get("text", "") for b in (event.result or {}).get("content", []))
        journey.emit("tool", phase="end", id=use["toolUseId"], name=use["name"], ms=journey.ms() - began,
                     status=(event.result or {}).get("status"), mcp=stamp, preview=preview, bytes=size,
                     sent_at_ms=began_epoch, done_at_ms=int(time.time() * 1000), sources=_data_sources(full_text))

    def connect() -> tuple[ExitStack, list]:
        stack, found = ExitStack(), []
        for client in _mcp_clients(persona, authorization):
            stack.enter_context(client)
            found.extend(client.list_tools_sync())
        return stack, found

    journey.emit("tools_list", phase="start", id="tools", via="AgentCore Gateway" if GATEWAY_URL else "MCP server (direct)")
    t0 = journey.ms()
    stack, tools = await asyncio.to_thread(connect)  # off the event loop, so streamed events keep flowing
    journey.emit("tools_list", phase="end", id="tools", ms=journey.ms() - t0,
                 via="AgentCore Gateway" if GATEWAY_URL else "MCP server (direct)",
                 tools=[t.tool_name for t in tools], allowed=sorted({t for ts in persona["tools"].values() for t in ts}))
    try:
        history_key = (session_id, name) if session_id else None
        agent = Agent(
            model=BedrockModel(model_id=persona["model"], max_tokens=4096),
            system_prompt="\n\n".join(filter(None, [persona["prompt"], persona.get("rules")])),
            tools=tools,
            messages=SESSIONS.get(history_key, []) if history_key else None,
            callback_handler=None,
        )
        for event_type, callback in [(BeforeModelCallEvent, before_model), (AfterModelCallEvent, after_model),
                                     (BeforeToolCallEvent, before_tool), (AfterToolCallEvent, after_tool)]:
            agent.hooks.add_callback(event_type, callback)
        result = await asyncio.wait_for(agent.invoke_async(prompt), timeout=RUN_TIMEOUT_S)
        if history_key:
            SESSIONS[history_key] = agent.messages
    finally:
        await asyncio.to_thread(stack.close)

    usage = result.metrics.accumulated_usage
    summary = {"agent_ms": journey.ms(), "input_tokens": usage.get("inputTokens"),
               "output_tokens": usage.get("outputTokens"), "model_calls": model_calls}
    log.info(json.dumps({"event": "invocation", "agent": name, "tools_used": tools_used, **summary,
                         "stop_reason": result.stop_reason}))
    # Some models (e.g. Nova) wrap their reasoning in <thinking> tags; users only see the answer.
    answer = re.sub(r"<thinking>.*?</thinking>", "", str(result), flags=re.DOTALL).strip()
    return {"agent": name, "answer": answer, "tools_used": tools_used, "summary": summary}


async def _stream(name, prompt, session_id, authorization, journey: Journey):
    """Yield journey events as they happen, then a final `done` (or `error`) event."""
    for event in list(journey.events):  # agent_start and authz, emitted before streaming began
        yield event
    journey.go_live()
    task = asyncio.create_task(run_persona(name, prompt, session_id, authorization, journey))
    while True:
        getter = asyncio.ensure_future(journey.queue.get())
        done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            yield getter.result()
            continue
        getter.cancel()
        break
    while not journey.queue.empty():
        yield journey.queue.get_nowait()
    try:
        yield {"type": "done", "t_ms": journey.ms(), **task.result()}
    except Forbidden as e:
        yield {"type": "error", "t_ms": journey.ms(), "error": f"forbidden: your groups don't allow '{e}'"}
    except asyncio.TimeoutError:
        yield {"type": "error", "t_ms": journey.ms(), "error": f"timed out after {RUN_TIMEOUT_S}s"}
    except ClientError as e:
        yield {"type": "error", "t_ms": journey.ms(), "error": f"Bedrock: {e.response['Error']['Message']}"}


@app.entrypoint
async def invoke(payload: dict, context=None):
    global _cold
    requested = payload.get("agent")
    if ROLE_LOOKUP and not ALLOW_PERSONA_OVERRIDE and requested != "__list__":
        requested = None  # the caller's role picks the persona, not the browser
    name = requested or DEFAULT_AGENT or (AUTO if ROLE_LOOKUP else None)
    authorization = (getattr(context, "request_headers", None) or {}).get("Authorization")
    who = caller(authorization) or None
    if name == "__list__":
        return {"agents": {n: {"tools": sorted({t for ts in p["tools"].values() for t in ts}),
                               "allowed_groups": p.get("allowed_groups", []), "roles": p.get("roles", []),
                               "label": p.get("label", n)} for n, p in PERSONAS.items()}}
    # Errors are returned as 200 + "error": AgentCore Runtime replaces any non-2xx response from
    # the container with its own HTTP 424 and drops the body (and with it the journey events).
    if name == AUTO and not ROLE_LOOKUP:
        return {"error": "role routing is not configured; name an agent", "available": sorted(PERSONAS)}
    if name != AUTO and name not in PERSONAS:
        return {"error": "unknown agent", "available": sorted(PERSONAS)}
    if payload.get("intro"):
        # Page load: tell the UI which persona this user gets (no model call). On AgentCore this
        # also starts the session's microVM before the first question.
        persona = await resolve_persona(authorization, Journey()) if name == AUTO else name
        intro = {"type": "persona", **_persona_event(persona)}
        if ROLE_LOOKUP and ALLOW_PERSONA_OVERRIDE:
            intro["choices"] = [{"persona": n, "label": p.get("label", n)} for n, p in PERSONAS.items()]
        return intro
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return {"error": "empty prompt"}

    journey = Journey()
    microvm = "cold" if _cold else "warm"
    _cold = False
    journey.emit("agent_start", persona=name, microvm=microvm, identity=who,
                 session_id=getattr(context, "session_id", None))

    # The runtime's JWT authorizer proved *who* the caller is; this decides *what* they may use.
    # With role routing the persona isn't known yet; run_persona checks it once resolved.
    allowed_groups = [] if name == AUTO else PERSONAS[name].get("allowed_groups") or []
    if REQUIRE_AUTH and not who:
        journey.emit("authz", allowed=False, reason="no bearer token reached the agent")
        return {"agent": name, "error": "unauthenticated", "events": journey.events}
    ok = name == AUTO or _authorized(who, PERSONAS[name])
    journey.emit("authz", allowed=ok, user_groups=(who or {}).get("groups", []), allowed_groups=allowed_groups)
    if not ok:
        log.info(json.dumps({"event": "forbidden", "agent": name, "user": who["user"], "groups": who["groups"]}))
        return {"agent": name, "identity": who, "events": journey.events,
                             "error": f"forbidden: '{name}' requires one of the groups {allowed_groups}"}

    session_id = payload.get("session_id") or getattr(context, "session_id", None)
    if payload.get("stream"):
        return _stream(name, prompt, session_id, authorization, journey)
    try:
        result = await run_persona(name, prompt, session_id, authorization, journey)
        return {**result, "identity": who, "events": journey.events}
    except Forbidden as e:
        return {"agent": str(e), "identity": who, "events": journey.events,
                "error": f"forbidden: your groups don't allow '{e}'"}
    except asyncio.TimeoutError:
        return {"agent": name, "error": f"timed out after {RUN_TIMEOUT_S}s"}
    except ClientError as e:
        log.error(json.dumps({"event": "bedrock_error", "agent": name, "error": str(e)}))
        return {"agent": name, "error": f"Bedrock: {e.response['Error']['Message']}"}


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 8080)))
