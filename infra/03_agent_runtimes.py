"""Deploy one AgentCore Runtime per persona in agents.yaml (same code bundle). Idempotent.

Each runtime gets DEFAULT_AGENT=<persona>, so in the console it shows up as its own agent.
Inbound auth is a Cognito JWT authorizer (the UI user's access token), and the Authorization header
is forwarded to the agent, which checks the user's groups and passes the token on to the Gateway.
"""

import os

import yaml

from helpers import (
    ACCOUNT, PROJECT, REGION, ROOT, build_and_upload, deploy_runtime, ensure_role, jwt_authorizer,
    load_state, runtime_base_policy, save_state,
)

state = load_state()
gateway = state["gateway"]
personas = yaml.safe_load((ROOT / "agent_service/agents.yaml").read_text())["agents"]
model_id = os.environ["MODEL_ID"]

role_arn = ensure_role(f"{PROJECT}-{REGION}-agent-role", {"Version": "2012-10-17", "Statement": runtime_base_policy() + [
    {"Sid": "Bedrock", "Effect": "Allow",
     "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
     # Cross-region inference profiles (apac./global./in.) route to foundation models in other regions.
     "Resource": ["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:*:{ACCOUNT}:inference-profile/*"]},
]}, "AgentCore demo agent runtime execution role")
state.setdefault("iam", {})["agent_role_arn"] = role_arn
save_state(state)

code_key, digest = build_and_upload(
    "agent-service",
    [ROOT / "agent_service/agent.py", ROOT / "agent_service/agents.yaml"],
    ROOT / "agent_service/requirements.txt",
)

runtimes = state.setdefault("agent_runtimes", {})
for persona in personas:
    runtimes[persona] = deploy_runtime(
        name=f"{PROJECT.replace('-', '_')}_{persona}",
        key=code_key, digest=digest, entry="agent.py", protocol="HTTP", role_arn=role_arn,
        env={"GATEWAY_URL": gateway["url"], "MODEL_ID": model_id, "DEFAULT_AGENT": persona,
             "RUN_TIMEOUT_S": "90", "REQUIRE_AUTH": "1"},
        description=f"Finance persona '{persona}'",
        previous=runtimes.get(persona),
        authorizer=jwt_authorizer(state["cognito"]),
        forward_headers=["Authorization"],
    )
    save_state(state)

print("\nDeployed runtimes:")
for persona, r in runtimes.items():
    print(f"  {persona:18s} {r['arn']}")
