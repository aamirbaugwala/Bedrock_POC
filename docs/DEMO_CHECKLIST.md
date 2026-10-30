# Data Insights demo: start-up checklist

Local demo with real data: misight on your laptop → agent on your laptop → `hodor-mcp` Lambda (dev) → MarketPro data.

```
misight (https://localhost:19006) ──Okta token──▶ agent (localhost:8080) ──▶ MCP Lambda (dev) ──▶ data
                                                        └── Bedrock Nova Pro (your AWS creds)
```

Start about 15 minutes before the demo.

## 1. Agent (terminal 1)

Paste fresh AWS credentials into this terminal first (AWS access portal → Market Metrics Infrastructure → Access keys → option 1). The agent only uses credentials from the terminal it runs in.

```bash
cd ~/Desktop/lms/Bedrock_POC
aws sts get-caller-identity --query Account --output text   # must print 117963202140
lsof -i :8080                                               # if anything is listed: kill <PID>

AWS_REGION=us-east-1 MODEL_ID=us.amazon.nova-pro-v1:0 REQUIRE_AUTH=1 ALLOW_PERSONA_OVERRIDE=1 \
AGENTS_CONFIG=agent_service/agents.demo.yaml CORS_ORIGINS=https://localhost:19006 \
.venv/bin/python agent_service/agent.py
```

Leave it running. It logs every model and tool call, which helps if an answer looks wrong.

`ALLOW_PERSONA_OVERRIDE=1` turns on the "Viewing as" persona switcher in the UI (demo only). Without it, your MarketPro role picks the persona and the switcher is hidden.

## 2. misight (terminal 2)

```bash
cd ~/Desktop/lms/misight
nvm use 22
yarn start:web
```

## 3. Browser

1. Open `https://localhost:19006`.
2. **Sign out, then sign in.** Tokens last about an hour, and misight doesn't renew them, so an old one fails mid-demo.
3. Open **Data Insights** in the left menu. The pill shows the persona: "Viewing as Market research ▾" for the `developer` role.
4. To show another persona, click the pill and pick one (Wholesaler, Sales manager, Recruiter, Key accounts). Each switch starts a new chat with that persona's suggested questions. Only the instructions change; the data is still limited to your own access.

## 4. Warm up (2 minutes before)

Ask one question. That wakes the MCP Lambda and the model, so the first demo answer isn't slow.

## 5. Demo questions

Rehearse each one before the demo; use the ones that answer well.

- Which firms in Texas have the most advisors? *(tested with real data)*
- Show me teams with 10 or more members in California
- How many advisors in Florida have 20+ years of experience?
- Which offices in Texas gained the most advisors recently?

Avoid "which office lost the most…": it's the least reliable.

Answers take about 10–15 seconds.

## If something goes wrong

| What you see | Fix |
| --- | --- |
| Answer says "authentication error" | Sign out and in again (expired token). Still failing: check the token's client in the Console (below) |
| No Data Insights in the menu | `cd ~/Desktop/lms/misight && REACT_NATIVE_OKTA_WEB_CLIENT_ID=0oaw4oae00WC4hNFm1d7 yarn set:env local`, restart `yarn start:web`, sign in again |
| Agent errors mentioning Bedrock or credentials | AWS credentials expired: paste fresh ones into terminal 1, restart the agent |
| CORS error in the browser console | Restart the agent with `CORS_ORIGINS=https://localhost:19006` exactly |
| `Address already in use` when starting the agent | `lsof -i :8080`, then `kill <PID>` |

Check which Okta client issued your token (Console on the `localhost:19006` tab). It must be `0oaw4oae00WC4hNFm1d7`:

```js
const t = JSON.parse(localStorage['okta-token-storage']).accessToken.accessToken;
JSON.parse(atob(t.split('.')[1].replace(/-/g,'+').replace(/_/g,'/'))).cid
```

## Things that can silently undo the setup

- **Running `yarn set:env local` without the client override** resets local misight to the old Okta client, and the MCP rejects its tokens. Always include `REACT_NATIVE_OKTA_WEB_CLIENT_ID=0oaw4oae00WC4hNFm1d7`.
- **If you fixed it on the Lambda instead** (`OKTA_CLIENT_ID` in the Lambda console), the next `hodor-mcp` deploy resets that value.
- **`agents.demo.yaml`** must keep the line `url: https://hodor.dev.marketmetrics.strategic-i.com/mcp`.

## Backup: mock data, no AWS or Okta

Stop the real agent first (both use port 8080), then in three terminals:

```bash
cd ~/Desktop/lms/Bedrock_POC && make marketpro-sample
cd ~/Desktop/lms/Bedrock_POC && make marketpro-agent
cd ~/Desktop/lms/Bedrock_POC && make marketpro-preview     # open http://localhost:19007
```

## After the demo

- Stop both terminals (Ctrl+C).
- If you pasted AWS credentials as `export` lines, clear them from shell history: `history -c`, and remove them from `~/.bash_history`.



https://claude.ai/artifact/GavjLmxkYmWb3pJvsSvGWC#47bba1d0-2aae.mmaz2nccj8c.1444