# Bedrock AgentCore POC

Can we run **role-aware AI assistants on Amazon Bedrock AgentCore** that answer questions from
our own data, **as the signed-in user**, without building our own agent platform?

This repo answers that question end to end: a browser signs in, an agent hosted on AgentCore
picks a persona for the user's role, the model calls tools through an AgentCore Gateway, and
the MCP server behind it sees exactly who is asking, so data access stays the user's own.

## What we're trying to prove

| Question | How the POC answers it |
| --- | --- |
| **Can AgentCore host our agents and MCP servers?** | The agent service and the MCP server each run on AgentCore Runtime (isolated microVMs per session), deployed by Python scripts in [infra/](infra/). |
| **Can one codebase serve many personas?** | One agent process serves every persona in a YAML file. Adding a persona means adding a YAML entry, with no code change. |
| **Can access follow our existing identity provider?** | Cognito groups (finance demo) or the MarketPro role behind an Okta token (Data Insights) decide which persona a user gets. The browser can't choose or fake it. |
| **Does the end user's identity reach the data?** | Yes. The Gateway interceptor passes the validated caller and token to the MCP server in `params._meta`, so GraphQL can enforce per-user access. |
| **Is it locked down at every hop?** | No token → 401 at the runtime. Wrong group → refused by the agent. Calling the Gateway without a token → 401. Calling the MCP runtime directly → 403. |
| **Can it plug into misight?** | The Data Insights page in misight talks to this agent, and the agent queries MarketPro through the real `hodor` MCP-over-GraphQL server. |
| **What does it cost in latency?** | Every request returns a hop-by-hop trace with timings. Roughly 10–15 s per answer. About 5 s of that is AgentCore cold-start overhead, which drops when warm. |

## Two demos, one platform

**1. Finance personas (fully on AgentCore, `ap-south-1`)**
Four personas (financial advisor, recruiter, CFO briefing, risk & compliance) read the same
company data from Alpha Vantage, each through a different lens. Users sign in with Cognito, and
groups decide which personas they may use. `make ui` shows each request's path live.

**2. MarketPro Data Insights (misight integration)**
Five personas (market research, wholesaler, sales manager, recruiter, key accounts) answer
questions about firms, offices, advisors and teams. The persona is picked from the caller's
MarketPro role, looked up with their own Okta token. It runs against real MarketPro data (dev)
or a synthetic sample dataset with no AWS or Okta needed.

## How a request flows

```
Browser ──JWT──▶ AgentCore Runtime (agent) ──▶ Amazon Bedrock (model)
                   │  JWT authorizer, persona ↔ role check
                   └──▶ AgentCore Gateway ──▶ interceptor λ ──▶ AgentCore Runtime (MCP) ──▶ data
                         JWT authorizer        identity → _meta     IAM: Gateway only
```

The model only *asks* for a tool. The agent makes the call, through the Gateway, carrying the
user's token. The full hop-by-hop walkthrough, including how to plug in your own MCP server,
GraphQL API and Cognito pool, is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Stack

- **Amazon Bedrock**: Nova Pro (`*.amazon.nova-pro-v1:0`) for inference
- **Bedrock AgentCore**: Runtime (agent + MCP hosting), Gateway (single MCP endpoint), Identity (JWT authorizers)
- **Strands Agents**: agent loop and MCP client
- **MCP**: streamable HTTP, stateless servers
- **Cognito / Okta**: sign-in and roles
- **Python 3.12**, managed with `uv`

## Repo layout

| Path | What's in it |
| --- | --- |
| [agent_service/](agent_service/) | The multi-persona agent ([agent.py](agent_service/agent.py)) and persona configs: [agents.yaml](agent_service/agents.yaml) (finance), [agents.demo.yaml](agent_service/agents.demo.yaml) / [agents.marketpro.yaml](agent_service/agents.marketpro.yaml) (MarketPro) |
| [mcp_server/](mcp_server/) | Finance MCP server over Alpha Vantage, cached daily in S3. Also a MarketPro sample server with synthetic data |
| [infra/](infra/) | Idempotent deploy scripts: Cognito → MCP runtime → Gateway + interceptor → agent runtimes, plus teardown |
| [ui/](ui/) | Demo UI showing each request's journey, and a standalone preview of misight's Data Insights page |
| [client/](client/) | CLI chat, Gateway tool listing, cache warm-up |
| [common/](common/) | Env loading and caller identity, shared by agent and MCP |
| [tests/](tests/) | Local end-to-end tests |
| [docs/](docs/) | [Architecture](docs/ARCHITECTURE.md), [progress & verified behaviour](docs/PROGRESS.md), [Data Insights demo checklist](docs/DEMO_CHECKLIST.md) |

## Quick start

```bash
make install && cp .env.example .env   # fill in AWS creds, AWS_REGION, MODEL_ID, ALPHAVANTAGE_API_KEY
make setup                              # check the environment
```

**Local only (no AgentCore hosting):**
```bash
make run-mcp-local      # terminal 1: finance MCP on :8000
make test-local         # terminal 2: end-to-end tests against it
```

**Deploy to AgentCore and try the UI:**
```bash
make deploy             # Cognito, MCP runtime, Gateway, persona runtimes (safe to re-run)
make ui                 # http://localhost:3000
make teardown           # delete everything recorded in .demo-state.<region>.json
```
Demo users: `priya` (advisors), `rahul` (recruiters), `meera` (finance-leadership),
`arjun` (risk-compliance) and `guest` (no groups). The password is `DEMO_USER_PASSWORD` in `.env`.

**MarketPro Data Insights with mock data** (needs the sibling `hodor` and `misight` repos):
```bash
make marketpro-sample   # terminal 1: sample MCP on :8000
make marketpro-agent    # terminal 2: agent on :8080
make marketpro-preview  # terminal 3: http://localhost:19007
```
For the real-data demo through misight, follow [docs/DEMO_CHECKLIST.md](docs/DEMO_CHECKLIST.md).

## Status

All phases are working and verified in a browser, covering both allowed and forbidden paths.
Teardown hasn't been re-run since Cognito was added. See [docs/PROGRESS.md](docs/PROGRESS.md)
for details and deviations from the original brief.

**This is a POC, not production.** Known gaps before going further:
- Use short-lived, least-privilege credentials, not long-lived keys in `.env`
- Use Cognito Hosted UI with PKCE instead of username/password sign-in
- Run the MCP runtime in `VPC` network mode to reach internal GraphQL
- Warm-up or provisioned capacity to hide cold-start latency
- Claude models were unavailable on the test account, so Nova Pro was used. Answer quality should be re-checked once Claude is available.
