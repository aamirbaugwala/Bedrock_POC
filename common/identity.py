"""Cognito helpers: sign a user in, and read claims from a bearer token.

`claims()` does NOT verify the signature. It is only used behind an AgentCore JWT authorizer,
which has already verified signature, expiry and client ID before our code runs.
"""

import base64
import json
import os

import boto3


def claims(authorization: str | None) -> dict:
    if not authorization:
        return {}
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def caller(authorization: str | None) -> dict:
    """The fields of interest for authorisation and display, from a Cognito or Okta access token.

    Okta (MarketPro): `sub` is the login, groups are in `group`, the client ID is in `cid`.
    """
    c = claims(authorization)
    if not c:
        return {}
    return {"user": c.get("username") or c.get("cognito:username") or c.get("sub"),
            "groups": c.get("cognito:groups") or c.get("group") or [],
            "client_id": c.get("client_id") or c.get("cid"), "exp": c.get("exp")}


def sign_in(client_id: str, username: str, password: str) -> dict:
    """USER_PASSWORD_AUTH against the demo app client; returns Cognito's AuthenticationResult."""
    idp = boto3.client("cognito-idp", region_name=os.environ["AWS_REGION"])
    return idp.initiate_auth(
        ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )["AuthenticationResult"]


def demo_user_token(username: str) -> str:
    """Access token for one of the demo pool's users (password from .env), for CLI clients and tests."""
    from common.env import state_path

    cognito = json.loads(state_path().read_text())["cognito"]
    return sign_in(cognito["client_id"], username, os.environ["DEMO_USER_PASSWORD"])["AccessToken"]
