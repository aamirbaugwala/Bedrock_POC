# How a request travels: UI → AgentCore → your data

This is the request path as deployed in ap-south-1, and how your own pieces (an MCP server
over GraphQL in a VPC, existing Cognito groups) slot into it. `make ui` shows the same path live,
with timings, for every message.

## The two things called "Bedrock"

| | What it is | What it does in this design |
| --- | --- | --- |
| **Amazon Bedrock** | Managed LLM inference | Answers `ConverseStream` calls. It only sees text and tool *schemas*; it never calls anything itself. |
| **Bedrock AgentCore** | Platform for running agents | **Runtime** hosts your agent and MCP code in microVMs. **Identity** (JWT authorizers) checks callers. **Gateway** is the single MCP endpoint in front of your tools. |

The model *asks* for a tool. Your agent code makes the call, through the Gateway.

## One request, hop by hop

```
Browser ─①─▶ Cognito                          sign in → JWT access token {username, cognito:groups, client_id}
   │
   └─②─▶ AgentCore Runtime endpoint           POST /runtimes/<agent-arn>/invocations
          ③ JWT authorizer                     signature (Cognito JWKS), expiry, client_id ∈ allowedClients → else 401
          ④ session → isolated microVM         X-Amzn-Bedrock-AgentCore-Runtime-Session-Id
          ⑤ agent.py (Strands)                 reads forwarded Authorization header; cognito:groups ∩ persona.allowed_groups
             │
             ├─⑥─▶ Amazon Bedrock             ConverseStream, signed with the runtime's IAM execution role
             │       ◀─ "call get_company_snapshot(MSFT)"
             │
             └─⑦─▶ AgentCore Gateway /mcp     tools/call + the user's token; Gateway JWT authorizer checks it
                     ⑧ REQUEST interceptor λ   copies the validated caller + token into params._meta
                     ⑨ ─▶ MCP runtime          Gateway signs with its own IAM role (only it may invoke the MCP)
                            └─⑩─▶ data         S3 cache / Alpha Vantage   (yours: GraphQL, with the user's token)
             ◀── tool result ── back to ⑥ until the model writes the final answer
   ◀────── answer + tools_used + trace (same HTTPS response)
```

| # | Component | Auth on this hop | Where it lives in the repo |
| --- | --- | --- | --- |
| 1 | Cognito user pool | username/password → JWT (demo); Hosted UI + PKCE in prod | `infra/00_cognito.py` |
| 2-4 | AgentCore Runtime (agent) | **Cognito JWT authorizer**, `allowedClients=[demo-ui]` | `infra/03_agent_runtimes.py` |
| 5 | Agent code | persona ↔ group check (authorisation) | `agent_service/agent.py`, `agents.yaml` |
| 6 | Bedrock model | IAM role `agentcore-demo-ap-south-1-agent-role` | same |
| 7 | AgentCore Gateway | **Cognito JWT authorizer** (same token) | `infra/02_gateway.py` |
| 8 | Interceptor Lambda | runs inside the Gateway call; sees request headers | `infra/interceptor/handler.py` |
| 9 | AgentCore Runtime (MCP) | **IAM**: only the Gateway role may invoke it | `infra/01_mcp_runtime.py`, `mcp_server/server.py` |
| 10 | Data source | MCP's choice: service creds, or the user's token from `_meta` | `mcp_server/server.py` (`end_user`) |

Measured (cold session, MSFT cached): ~12.5 s end to end, of which ~5 s is AgentCore overhead
(cold microVMs + Lambda); the agent's own ~7.5 s splits into model calls (~1.2 s + ~1.4 s), the
tool call through Gateway → interceptor → MCP (~4.3 s cold, ~0.5 s warm) and tool listing (~0.5 s).

## Why identity crosses the Gateway via `_meta`, not a header

* MCP-server Gateway targets **don't support `JWT_PASSTHROUGH`** (the API rejects it).
* Gateway REQUEST interceptors for MCP targets can rewrite the request **body**, not headers.
* So the interceptor puts `{user, sub, groups}` and the raw token into `params._meta` of each
  `tools/call`, the MCP-standard extension field. The MCP runtime only accepts calls signed by
  the Gateway's role, so `_meta` can't be forged by anyone who goes around the Gateway.

## Mapping your existing pieces onto this

| Your piece | What to do |
| --- | --- |
| **MCP server over GraphQL (runs locally)** | Make it speak streamable HTTP, stateless, on `0.0.0.0:8000/mcp` (same as `mcp_server/server.py`). Deploy it like `01_mcp_runtime.py`, with **`networkMode: VPC`** plus subnets/security groups that can reach GraphQL. Keep IAM auth so only the Gateway can call it. |
| **Per-user GraphQL access** | In each tool, read `_meta["agentcore-demo/user_token"]` (see `end_user()` in `mcp_server/server.py`) and send it as `Authorization` to GraphQL. If GraphQL is AppSync with Cognito user-pool auth, its resolvers already see the user and groups. |
| **Your Cognito pool + groups** | Create one app client for the UI in your pool; put its discovery URL and client ID in state `cognito`; re-run `make deploy`. Map groups to personas with `allowed_groups` in `agents.yaml`. |
| **Your UI** | Exactly what `ui/index.html` does: sign in with Cognito, then `POST` to the runtime URL with `Authorization: Bearer <access token>` and a session-ID header. AgentCore allows CORS, so no backend is required (a BFF is fine too). |

