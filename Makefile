PY := .venv/bin/python
AGENT ?= aws_architect

.PHONY: setup install run-mcp-local run-agent-local test-mcp test-local chat chat-all

install:
	uv venv --python 3.12 .venv -q
	uv pip install --python $(PY) -q -r requirements.txt

setup:
	$(PY) scripts/setup_check.py

run-mcp-local:
	$(PY) mcp_server/server.py

run-agent-local:
	$(PY) agent_service/agent.py

test-mcp:
	$(PY) -m pytest tests/test_local.py -k test_mcp -v

test-local:
	$(PY) -m pytest tests/test_local.py -v

# make chat AGENT=oss_mentor
chat:
	$(PY) client/chat.py --agent $(AGENT)

# make chat-all Q="Should we host MCP servers on AgentCore Runtime?"
chat-all:
	$(PY) client/chat.py --all -q "$(Q)"

# --- Cloud (AgentCore) --------------------------------------------------------
.PHONY: deploy gateway-tools chat-cloud chat-cloud-all logs teardown warm ui

deploy:
	cd infra && ../$(PY) 00_cognito.py && ../$(PY) 01_mcp_runtime.py && ../$(PY) 02_gateway.py && ../$(PY) 03_agent_runtimes.py

gateway-tools:
	$(PY) client/list_gateway_tools.py

# make chat-cloud AGENT=oss_mentor
chat-cloud:
	$(PY) client/chat.py --cloud --agent $(AGENT)

chat-cloud-all:
	$(PY) client/chat.py --cloud --all -q "$(Q)"

# make logs AGENT=oss_mentor
logs:
	@ID=$$(PYTHONPATH=. $(PY) -c "import json;from common.env import load_env,state_path;load_env();print(json.load(open(state_path()))['agent_runtimes']['$(AGENT)']['id'])"); \
	set -a; . ./.env; set +a; aws logs tail /aws/bedrock-agentcore/runtimes/$$ID-DEFAULT --follow --since 30m

teardown:
	cd infra && ../$(PY) 99_teardown.py

# make warm SYMBOLS="MSFT NVDA"   (pre-fetch before a demo; 6 Alpha Vantage calls per new ticker)
warm:
	$(PY) client/warm_cache.py $(SYMBOLS)

# Demo UI on http://localhost:3000 (browser calls Cognito and AgentCore directly)
ui:
	$(PY) ui/serve.py

# --- MarketPro Data Insights, mock mode ---------------------------------------
# No company AWS, Okta or AgentCore: made-up data, mock login. Bedrock calls use this folder's .env.
# Run each in its own terminal, then open http://localhost:19007. See docs/MARKETPRO_LOCAL.md.
.PHONY: marketpro-sample marketpro-agent marketpro-preview
MCP_PY := $(abspath ../hodor/services/mcp_graphql/.venv/bin/python)

marketpro-sample:
	$(MCP_PY) mcp_server/marketpro_sample.py

marketpro-agent:
	AGENTS_CONFIG=agent_service/agents.marketpro.yaml \
	CORS_ORIGINS=http://localhost:19007,https://localhost:19006 \
	$(PY) agent_service/agent.py

marketpro-preview:
	cd ../misight && node_modules/.bin/webpack serve --config ../Bedrock_POC/ui/insights_preview/webpack.config.js
