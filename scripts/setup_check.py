"""Phase 0 check: AWS identity, Claude Sonnet inference profiles, one Bedrock call."""

import os
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import load_env  # noqa: E402

load_env()
region = os.environ["AWS_REGION"]

ident = boto3.client("sts").get_caller_identity()
print(f"account: {ident['Account']}  region: {region}  arn: {ident['Arn']}")

bedrock = boto3.client("bedrock", region_name=region)
profiles = []
for page in bedrock.get_paginator("list_inference_profiles").paginate(typeEquals="SYSTEM_DEFINED"):
    profiles += [p for p in page["inferenceProfileSummaries"] if "claude" in p["inferenceProfileId"] and "sonnet" in p["inferenceProfileId"]]
print("Claude Sonnet inference profiles:")
for p in sorted(profiles, key=lambda p: p["inferenceProfileId"]):
    print(f"  {p['inferenceProfileId']:55s} {p['status']}")

model_id = os.environ.get("MODEL_ID")
if not model_id:
    sys.exit("\nMODEL_ID is not set in .env; pick one from the list above.")

resp = boto3.client("bedrock-runtime", region_name=region).converse(
    modelId=model_id,
    messages=[{"role": "user", "content": [{"text": "reply ok"}]}],
)
text = "".join(b.get("text", "") for b in resp["output"]["message"]["content"])
print(f"\nmodel {model_id} replied: {text.strip()!r}")
