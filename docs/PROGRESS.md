# Progress

**Current state (2026-10-28, ap-south-1):** four finance personas behind Cognito login with
group-based access, a demo UI that shows every hop of each request, and the end user's identity
carried all the way to the MCP server. See [ARCHITECTURE.md](ARCHITECTURE.md) for the request path
and how to plug in your own MCP/GraphQL and Cognito pool.

| Phase | Status | Notes |
| --- | --- | --- |
| 0 Environment | done | `make setup` |
| 1 MCP | done | Finance MCP over Alpha Vantage, S3-cached daily (`mcp_server/server.py`) |
| 2 Agent service local | done | `make run-mcp-local` + `make test-local` |
| 3 Cognito | done | Demo pool `agentcore-demo-users`: 4 groups, 5 users (`guest` has no groups) |
| 4 MCP on Runtime | done | IAM auth; only the Gateway role can invoke it |
| 5 Gateway | done | Cognito JWT inbound, REQUEST interceptor → identity in `_meta`, IAM outbound |
| 6 Agent runtimes | done | Cognito JWT inbound, `Authorization` forwarded, group check in `agent.py` |
| 7 E2E/teardown | partial | Verified in headless Chrome (allowed + forbidden paths); teardown covers all resources but not re-run since Cognito was added |
| UI | done | `make ui` → http://localhost:3000 |

## Verified behaviour
- No token → **401** at the runtime's JWT authorizer (agent code never runs).
- `rahul` (recruiters) → `cfo_briefing` → **forbidden** by the agent's group check.
- `meera` (finance-leadership) → any persona; the MCP server reports `served_for: meera`.
- Gateway without a token → **401**; MCP runtime without IAM signature → **403**.

## How to run
```
make install && cp .env.example .env    # AWS keys, AWS_REGION, MODEL_ID, ALPHAVANTAGE_API_KEY
make deploy                              # Cognito, MCP runtime, Gateway + interceptor, 4 personas (idempotent)
make ui                                  # http://localhost:3000
make warm SYMBOLS="NVDA AAPL"            # pre-fetch tickers before a demo (6 Alpha Vantage calls each)
make chat-cloud-all Q="..."              # CLI, as meera; --user <name> on chat.py to switch
make run-mcp-local / make test-local     # local mode, no AWS hosting
make teardown                            # deletes everything in .demo-state.<region>.json
```
Demo users: `priya` (advisors), `rahul` (recruiters), `meera` (finance-leadership),
`arjun` (risk-compliance), `guest` (none). Password: `DEMO_USER_PASSWORD` in `.env`.

## Deviations from BRIEF.md
- **Identity to MCP:** Gateway MCP targets reject `JWT_PASSTHROUGH`, and MCP-target interceptors
  can't set headers, so the interceptor puts the caller + token into `params._meta`.
- **Console Test** no longer works for the persona runtimes (they require a Cognito JWT, and the
  console signs with IAM). Use the UI or `chat.py --cloud`.
- **Data:** Alpha Vantage free tier (`OVERVIEW`/`NEWS_SENTIMENT` are premium; 25 calls/day).
- **Model:** `apac.amazon.nova-pro-v1:0`; Claude blocked on this account (billing / access).
- **One runtime per persona**, direct code deployment (no Docker), state per region,
  `infra/helpers.py` instead of `infra/common.py`.
- The Gateway's authorizer type is immutable, so switching IAM → JWT replaced the Gateway (new URL).

## Open issues / production gaps
See ARCHITECTURE.md → Production gaps. Most urgent: root-account keys in `.env`.
