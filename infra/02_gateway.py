"""AgentCore Gateway (MCP) with the finance MCP runtime as its target. Idempotent.

Inbound:  Cognito JWT authorizer - the end user's access token, forwarded by the agent.
Interceptor: a REQUEST Lambda copies the validated caller + token into each tools/call's
          `params._meta` (MCP-server targets don't support JWT passthrough, and interceptors
          for MCP targets can rewrite the body but not headers).
Outbound: SigV4 with the Gateway's own IAM role (GATEWAY_IAM_ROLE); the MCP runtime trusts only it.
"""

import io
import json
import time
import zipfile

import yaml

from helpers import (
    PROJECT, REGION, ROOT, TAGS, client, ensure_role, iam_tags, jwt_authorizer, load_state, save_state, wait_for,
)

GATEWAY_NAME = f"{PROJECT}-gw"
FUNCTION_NAME = f"{PROJECT}-gateway-interceptor"
TARGET_MODE = "iam+interceptor"  # recorded in state; a target created with another mode is replaced

ctl = client("bedrock-agentcore-control")
state = load_state()
servers = yaml.safe_load((ROOT / "agent_service/agents.yaml").read_text())["mcp_servers"]
mcp_runtime = state["mcp_runtime"]
authorizer = jwt_authorizer(state["cognito"])


# --- 1. interceptor Lambda ---------------------------------------------------

def ensure_interceptor() -> str:
    lam, iam = client("lambda"), client("iam")
    role_name = f"{PROJECT}-{REGION}-interceptor-role"
    try:
        role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        role_arn = iam.create_role(RoleName=role_name, Tags=iam_tags(), AssumeRolePolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}],
        }))["Role"]["Arn"]
        iam.attach_role_policy(RoleName=role_name, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        print(f"created role {role_name}")
        time.sleep(15)
    state.setdefault("iam", {})["interceptor_role_arn"] = role_arn

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write(ROOT / "infra/interceptor/handler.py", "handler.py")
    try:
        lam.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=buf.getvalue())
        print(f"updated Lambda {FUNCTION_NAME}")
    except lam.exceptions.ResourceNotFoundException:
        lam.create_function(FunctionName=FUNCTION_NAME, Runtime="python3.12", Handler="handler.lambda_handler",
                            Role=role_arn, Code={"ZipFile": buf.getvalue()}, Timeout=10, MemorySize=256,
                            Architectures=["arm64"], Tags=TAGS,
                            Description="AgentCore Gateway REQUEST interceptor: user identity -> MCP _meta")
        print(f"created Lambda {FUNCTION_NAME}")
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
    return lam.get_function(FunctionName=FUNCTION_NAME)["Configuration"]["FunctionArn"]


interceptor_arn = ensure_interceptor()
state["interceptor_arn"] = interceptor_arn
save_state(state)

# --- 2. Gateway role: call the MCP runtime and the interceptor -------------------

role_arn = ensure_role(f"{PROJECT}-{REGION}-gateway-role", {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": "bedrock-agentcore:InvokeAgentRuntime",
     "Resource": [mcp_runtime["arn"], f"{mcp_runtime['arn']}/*"]},
    {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": interceptor_arn},
]}, "AgentCore demo gateway service role")
state["iam"]["gateway_role_arn"] = role_arn

# --- 3. Gateway ------------------------------------------------------------------

common = dict(
    name=GATEWAY_NAME,
    description="Shared finance MCP tools for the persona agents",
    roleArn=role_arn,
    protocolType="MCP",
    protocolConfiguration={"mcp": {"supportedVersions": ["2025-03-26"]}},
    authorizerType="CUSTOM_JWT",
    authorizerConfiguration=authorizer,
    interceptorConfigurations=[{
        "interceptor": {"lambda": {"arn": interceptor_arn}},
        "interceptionPoints": ["REQUEST"],
        "inputConfiguration": {"passRequestHeaders": True},
    }],
    exceptionLevel="DEBUG",
)
gw = state.get("gateway", {})
existing = {g["name"]: g["gatewayId"] for g in ctl.list_gateways()["items"]}
if GATEWAY_NAME in existing and ctl.get_gateway(gatewayIdentifier=existing[GATEWAY_NAME])["authorizerType"] != "CUSTOM_JWT":
    # The authorizer type is immutable, so an IAM-auth gateway from an earlier deploy is replaced (new URL).
    old = existing.pop(GATEWAY_NAME)
    for t in ctl.list_gateway_targets(gatewayIdentifier=old)["items"]:
        ctl.delete_gateway_target(gatewayIdentifier=old, targetId=t["targetId"])
    while ctl.list_gateway_targets(gatewayIdentifier=old)["items"]:
        time.sleep(5)
    ctl.delete_gateway(gatewayIdentifier=old)
    print(f"deleted IAM-auth gateway {old}; recreating with Cognito JWT auth")
    gw = {}
    time.sleep(10)
if GATEWAY_NAME in existing:
    gw["id"] = existing[GATEWAY_NAME]
    current = ctl.get_gateway(gatewayIdentifier=gw["id"])
    if (current.get("authorizerConfiguration") != authorizer
            or current.get("interceptorConfigurations") != common["interceptorConfigurations"]
            or current.get("roleArn") != role_arn):
        ctl.update_gateway(gatewayIdentifier=gw["id"], **common)
        print(f"updated gateway {gw['id']}")
    else:
        print(f"gateway {GATEWAY_NAME} exists ({gw['id']})")
else:
    gw["id"] = ctl.create_gateway(**common, tags=TAGS)["gatewayId"]
    print(f"created gateway {gw['id']}")
desc = wait_for(lambda: ctl.get_gateway(gatewayIdentifier=gw["id"]), "READY", what="gateway")
gw.update(url=desc["gatewayUrl"], arn=desc["gatewayArn"])
state["gateway"] = gw
save_state(state)

# --- 4. Targets (every server in agents.yaml is hosted on the finance MCP runtime here) ---

targets = {t["name"]: t["targetId"] for t in ctl.list_gateway_targets(gatewayIdentifier=gw["id"])["items"]}
modes = gw.get("target_modes", {})
gw["targets"] = {}
for server, spec in servers.items():
    name = spec["gateway_target"]
    if name in targets:
        failed = ctl.get_gateway_target(gatewayIdentifier=gw["id"], targetId=targets[name])["status"] == "FAILED"
        if failed or modes.get(name) != TARGET_MODE:
            # Usually a fresh IAM role that hadn't propagated yet, or an older auth mode.
            ctl.delete_gateway_target(gatewayIdentifier=gw["id"], targetId=targets.pop(name))
            print(f"deleted target {name} ({'failed' if failed else 'auth mode changed'}); recreating")
            time.sleep(10)
    if name in targets:
        target_id = targets[name]
        print(f"target {name} exists ({target_id})")
    else:
        target_id = ctl.create_gateway_target(
            gatewayIdentifier=gw["id"],
            name=name,
            description=f"MCP server on AgentCore Runtime {mcp_runtime['name']}",
            targetConfiguration={"mcp": {"mcpServer": {"endpoint": mcp_runtime["invocation_url"]}}},
            credentialProviderConfigurations=[{
                "credentialProviderType": "GATEWAY_IAM_ROLE",
                "credentialProvider": {"iamCredentialProvider": {"service": "bedrock-agentcore", "region": REGION}},
            }],
        )["targetId"]
        print(f"created target {name} ({target_id})")
    wait_for(lambda: ctl.get_gateway_target(gatewayIdentifier=gw["id"], targetId=target_id), "READY", what=f"target {name}")
    gw["targets"][name] = target_id
    modes[name] = TARGET_MODE
    gw["target_modes"] = modes
    save_state(state)

print(f"\nGateway URL: {gw['url']}")
