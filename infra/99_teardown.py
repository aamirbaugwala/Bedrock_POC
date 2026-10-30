"""Delete every resource recorded in .demo-state.<region>.json, in reverse dependency order.

Region comes from AWS_REGION (e.g. `AWS_REGION=us-east-1 make teardown`).
"""

import time

from helpers import REGION, client, load_state, save_state, session

state = load_state()
if not state:
    raise SystemExit(f"nothing recorded for {REGION}")
print(f"tearing down {REGION}")
ctl = client("bedrock-agentcore-control")
iam = client("iam")


def gone(fn, what):
    try:
        fn()
        print(f"deleted {what}")
    except Exception as e:  # already deleted / never created
        print(f"skip {what}: {type(e).__name__}: {str(e)[:120]}")


for persona, r in state.get("agent_runtimes", {}).items():
    gone(lambda: ctl.delete_agent_runtime(agentRuntimeId=r["id"]), f"runtime {r['name']}")

gw = state.get("gateway", {})
for name, target_id in gw.get("targets", {}).items():
    gone(lambda: ctl.delete_gateway_target(gatewayIdentifier=gw["id"], targetId=target_id), f"gateway target {name}")
if gw.get("id"):
    for _ in range(24):  # gateway can't be deleted until its targets are gone
        if not ctl.list_gateway_targets(gatewayIdentifier=gw["id"])["items"]:
            break
        time.sleep(5)
    gone(lambda: ctl.delete_gateway(gatewayIdentifier=gw["id"]), f"gateway {gw['id']}")

if arn := state.get("interceptor_arn"):
    gone(lambda: client("lambda").delete_function(FunctionName=arn), f"Lambda {arn.split(':')[-1]}")

if r := state.get("mcp_runtime"):
    gone(lambda: ctl.delete_agent_runtime(agentRuntimeId=r["id"]), f"runtime {r['name']}")

if arn := state.get("secret_arn"):
    gone(lambda: client("secretsmanager").delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True), f"secret {arn}")

if s3 := state.get("s3"):
    bucket = session.resource("s3").Bucket(s3["bucket"])
    gone(lambda: (bucket.object_versions.delete(), bucket.delete()), f"bucket {s3['bucket']}")

if cognito := state.get("cognito"):
    gone(lambda: client("cognito-idp").delete_user_pool(UserPoolId=cognito["pool_id"]), f"user pool {cognito['pool_id']}")

for key, arn in state.get("iam", {}).items():
    role = arn.split("/")[-1]

    def delete_role(role=role):
        for policy in iam.list_role_policies(RoleName=role)["PolicyNames"]:
            iam.delete_role_policy(RoleName=role, PolicyName=policy)
        for attached in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=role, PolicyArn=attached["PolicyArn"])
        iam.delete_role(RoleName=role)
    gone(delete_role, f"role {role}")

save_state({})
print("state cleared")
