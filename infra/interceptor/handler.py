"""AgentCore Gateway REQUEST interceptor: carry the end user's identity to the MCP target.

The Gateway's JWT authorizer has already validated the caller's Cognito token before this runs.
MCP-target interceptors can rewrite the request body but not its headers, so the caller is put
into the MCP-standard `params._meta` of each tools/call:

  _meta["agentcore-demo/caller"]      {"user", "sub", "groups"}  - for authorisation decisions
  _meta["agentcore-demo/user_token"]  the raw access token        - for the MCP to forward downstream
                                                                    (e.g. to a Cognito-authorised GraphQL API)
  _meta["agentcore-demo/intercepted_at_ms"]                       - when this ran, for the UI's timing split

META_PREFIX replaces "agentcore-demo" (MarketPro: "marketpro", read by hodor's mcp_graphql).
Works with Cognito and Okta tokens. Never log the token.
"""

import base64
import json
import logging
import os
import time

META_PREFIX = os.environ.get("META_PREFIX", "agentcore-demo")

log = logging.getLogger()
log.setLevel(logging.INFO)


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def lambda_handler(event, context):
    request = event.get("mcp", {}).get("gatewayRequest", {})
    body = request.get("body") or {}
    headers = {k.lower(): v for k, v in (request.get("headers") or {}).items()}
    token = headers.get("authorization", "").removeprefix("Bearer ").strip()

    if body.get("method") == "tools/call" and token:
        claims = _claims(token)
        caller = {"user": claims.get("username") or claims.get("sub"), "sub": claims.get("sub"),
                  "groups": claims.get("cognito:groups") or claims.get("group") or []}
        params = body.setdefault("params", {})
        params.setdefault("_meta", {}).update({f"{META_PREFIX}/caller": caller, f"{META_PREFIX}/user_token": token,
                                               f"{META_PREFIX}/intercepted_at_ms": int(time.time() * 1000)})
        log.info(json.dumps({"event": "identity_injected", "tool": params.get("name"), **caller}))

    return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayRequest": {"body": body}}}
