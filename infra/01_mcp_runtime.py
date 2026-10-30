"""Deploy the finance MCP server (mcp_server/) on AgentCore Runtime, protocol MCP. Idempotent.

Inbound auth is IAM: only the Gateway's role may invoke it. The end user's identity and token
arrive inside each tools/call (`params._meta`, added by the Gateway's REQUEST interceptor).
For an MCP that must reach a private API (e.g. GraphQL in a VPC), use networkMode VPC.

The Alpha Vantage key goes to Secrets Manager (never into env vars or code); the S3 bucket
doubles as the shared daily cache that every persona reads through.
"""

import os

from helpers import (
    BUCKET, PROJECT, REGION, ROOT, build_and_upload, client, deploy_runtime,
    ensure_bucket, ensure_role, iam_tags, load_state, runtime_base_policy, save_state,
)

SECRET_NAME = f"{PROJECT}/alphavantage-api-key"
state = load_state()

# 1. API key -> Secrets Manager
sm = client("secretsmanager")
key = os.environ.get("ALPHAVANTAGE_API_KEY") or exit("ALPHAVANTAGE_API_KEY is not set in .env")
try:
    secret_arn = sm.describe_secret(SecretId=SECRET_NAME)["ARN"]
    sm.put_secret_value(SecretId=secret_arn, SecretString=key)
    print(f"secret {SECRET_NAME} updated")
except sm.exceptions.ResourceNotFoundException:
    secret_arn = sm.create_secret(Name=SECRET_NAME, SecretString=key, Tags=iam_tags())["ARN"]
    print(f"created secret {SECRET_NAME}")
state["secret_arn"] = secret_arn

# 2. Bucket (code + cache) and execution role
ensure_bucket()
state["s3"] = {"bucket": BUCKET}
role_arn = ensure_role(f"{PROJECT}-{REGION}-mcp-role", {"Version": "2012-10-17", "Statement": runtime_base_policy() + [
    {"Sid": "Cache", "Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": f"arn:aws:s3:::{BUCKET}/cache/*"},
    {"Sid": "CacheList", "Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{BUCKET}"},  # 404 instead of 403 on misses
    {"Sid": "ApiKey", "Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arn},
]}, "AgentCore demo finance MCP server execution role")
state.setdefault("iam", {})["mcp_role_arn"] = role_arn
save_state(state)

# 3. Package and deploy
code_key, digest = build_and_upload("mcp-server", [ROOT / "mcp_server/server.py"], ROOT / "mcp_server/requirements.txt")
state["mcp_runtime"] = deploy_runtime(
    name=f"{PROJECT.replace('-', '_')}_finance_mcp",
    key=code_key, digest=digest, entry="server.py", protocol="MCP", role_arn=role_arn,
    env={"CACHE_BUCKET": BUCKET, "ALPHAVANTAGE_SECRET_ARN": secret_arn},
    description="Finance MCP server (Alpha Vantage, cached daily)",
    previous=state.get("mcp_runtime"),
)
save_state(state)
print(f"\nMCP runtime: {state['mcp_runtime']['arn']}\ninvocation URL: {state['mcp_runtime']['invocation_url']}")
