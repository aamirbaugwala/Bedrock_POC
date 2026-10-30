"""Shared helpers for infra scripts: env, state file, boto3 clients, tags, waiters."""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

import boto3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from common.env import load_env, state_path  # noqa: E402

load_env()

PROJECT = "agentcore-demo"
REGION = os.environ["AWS_REGION"]
STATE_PATH = state_path()
TAGS = {"project": PROJECT}

session = boto3.Session(region_name=REGION)
ACCOUNT = session.client("sts").get_caller_identity()["Account"]


def client(name: str):
    return session.client(name)


def load_state() -> dict:
    return json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def iam_tags() -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in TAGS.items()]


def wait_for(describe, ok: str, bad: tuple[str, ...] = ("FAILED", "CREATE_FAILED", "UPDATE_FAILED"), timeout=600, what="resource"):
    """Poll describe() until its 'status' is ok; raise on a failure status."""
    start = time.monotonic()
    while True:
        resp = describe()
        status = resp.get("status")
        if status == ok:
            return resp
        if status in bad:
            raise RuntimeError(f"{what} is {status}: {resp.get('statusReasons') or resp.get('failureReason')}")
        if time.monotonic() - start > timeout:
            raise TimeoutError(f"{what} still {status} after {timeout}s")
        print(f"  {what}: {status}...")
        time.sleep(5)


def ensure_role(name: str, policy: dict, description: str) -> str:
    """Create (or update the inline policy of) a role assumable by bedrock-agentcore."""
    iam = client("iam")
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}},
        }],
    }
    created = False
    try:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"role {name} exists")
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(
            RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Description=description, Tags=iam_tags()
        )["Role"]["Arn"]
        print(f"created role {name}")
        created = True
    policy_name = f"{name}-policy"
    try:
        changed = iam.get_role_policy(RoleName=name, PolicyName=policy_name)["PolicyDocument"] != policy
    except iam.exceptions.NoSuchEntityException:
        changed = True
    if changed:
        iam.put_role_policy(RoleName=name, PolicyName=policy_name, PolicyDocument=json.dumps(policy))
    if created or changed:
        print(f"  waiting for IAM to propagate {name}...")
        time.sleep(20)  # AgentCore assumes the role within seconds; a fresh policy may not be live yet
    return arn


# --- code packaging & runtime deployment (shared by the MCP server and the agents) ---

BUCKET = f"{PROJECT}-{ACCOUNT}-{REGION}"  # code bundles under <name>/, finance cache under cache/


def runtime_base_policy() -> list[dict]:
    """Permissions every AgentCore runtime here needs: read its code, logs, traces, workload identity."""
    return [
        {"Sid": "Code", "Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": f"arn:aws:s3:::{BUCKET}/*"},
        {"Sid": "Logs", "Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams", "logs:DescribeLogGroups"],
         "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:*"},
        {"Sid": "Observability", "Effect": "Allow",
         "Action": ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets", "cloudwatch:PutMetricData"],
         "Resource": "*"},
        {"Sid": "WorkloadIdentity", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT", "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
         "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:workload-identity-directory/default*"},
    ]


def ensure_bucket() -> str:
    s3 = client("s3")
    try:
        s3.head_bucket(Bucket=BUCKET)
        return BUCKET
    except s3.exceptions.ClientError:
        pass
    kwargs = {} if REGION == "us-east-1" else {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
    s3.create_bucket(Bucket=BUCKET, **kwargs)
    s3.put_bucket_tagging(Bucket=BUCKET, Tagging={"TagSet": iam_tags()})
    s3.put_bucket_lifecycle_configuration(Bucket=BUCKET, LifecycleConfiguration={"Rules": [
        {"ID": "expire-finance-cache", "Status": "Enabled", "Filter": {"Prefix": "cache/"}, "Expiration": {"Days": 7}},
    ]})
    print(f"created bucket {BUCKET}")
    return BUCKET


def build_and_upload(name: str, files: list[Path], requirements: Path) -> tuple[str, str]:
    """Bundle files + linux/arm64 wheels into a zip, upload to s3://BUCKET/<name>/. Returns (key, sha)."""
    import hashlib
    import shutil
    import subprocess
    import zipfile

    build = ROOT / "build" / name
    shutil.rmtree(build, ignore_errors=True)
    pkg = build / "package"
    pkg.mkdir(parents=True)
    print(f"[{name}] installing arm64 dependencies...")
    subprocess.run([
        "uv", "pip", "install", "-q", "--target", str(pkg),
        "--python-platform", "aarch64-manylinux2014", "--python-version", "3.12", "--only-binary", ":all:",
        "-r", str(requirements),
    ], check=True)
    for f in files + [ROOT / "common"]:
        if f.is_dir():
            shutil.copytree(f, pkg / f.name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy(f, pkg)
    zip_path = build / "deployment_package.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(pkg.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                info = zipfile.ZipInfo(str(path.relative_to(pkg)), date_time=(2020, 1, 1, 0, 0, 0))  # deterministic
                info.external_attr = 0o644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, path.read_bytes())
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()[:16]
    key = f"{name}/deployment_package.zip"
    client("s3").upload_file(str(zip_path), ensure_bucket(), key)
    print(f"[{name}] package {zip_path.stat().st_size / 1e6:.1f} MB, sha {digest}")
    return key, digest


def jwt_authorizer(cognito: dict) -> dict:
    """Inbound auth for runtimes and the Gateway: Cognito access tokens issued to the demo UI client."""
    return {"customJWTAuthorizer": {"discoveryUrl": cognito["discovery_url"], "allowedClients": [cognito["client_id"]]}}


def deploy_runtime(name: str, key: str, digest: str, entry: str, protocol: str, role_arn: str,
                   env: dict[str, str], description: str, previous: dict | None,
                   authorizer: dict | None = None, forward_headers: list[str] | None = None) -> dict:
    """Create or update an AgentCore Runtime from a code bundle; skip if code, env and auth are unchanged.

    authorizer=None means IAM (SigV4) callers; forward_headers lists request headers passed to our code.
    """
    ctl = client("bedrock-agentcore-control")
    spec = dict(
        agentRuntimeArtifact={"codeConfiguration": {
            "code": {"s3": {"bucket": BUCKET, "prefix": key}}, "runtime": "PYTHON_3_12", "entryPoint": [entry],
        }},
        roleArn=role_arn,
        networkConfiguration={"networkMode": "PUBLIC"},
        protocolConfiguration={"serverProtocol": protocol},
        environmentVariables=env,
        description=f"{description} (code sha {digest})",
        **({"authorizerConfiguration": authorizer} if authorizer else {}),
        **({"requestHeaderConfiguration": {"requestHeaderAllowlist": forward_headers}} if forward_headers else {}),
    )
    config = {"env": env, "authorizer": authorizer, "forward_headers": forward_headers}
    existing = {r["agentRuntimeName"]: r["agentRuntimeId"] for r in ctl.list_agent_runtimes(maxResults=100)["agentRuntimes"]}
    if name in existing:
        runtime_id = existing[name]
        if previous and previous.get("code_sha") == digest and previous.get("config") == config:
            print(f"runtime {name} up to date")
        else:
            ctl.update_agent_runtime(agentRuntimeId=runtime_id, **spec)
            print(f"updated runtime {name}")
    else:
        runtime_id = ctl.create_agent_runtime(agentRuntimeName=name, tags=TAGS, **spec)["agentRuntimeId"]
        print(f"created runtime {name} ({runtime_id})")
    desc = wait_for(lambda: ctl.get_agent_runtime(agentRuntimeId=runtime_id), "READY", what=f"runtime {name}")
    arn = desc["agentRuntimeArn"]
    return {"id": runtime_id, "arn": arn, "name": name, "code_sha": digest, "config": config,
            "invocation_url": f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{quote(arn, safe='')}/invocations?qualifier=DEFAULT"}
