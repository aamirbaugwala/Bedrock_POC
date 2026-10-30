"""Demo Cognito user pool: groups that mirror the personas, a public app client for the UI, test users.

Idempotent. Your real pool is never touched; to switch, put its discovery URL and app client ID
into .demo-state.<region>.json["cognito"] and re-run 01-03.
"""

import os
import secrets
import string

from helpers import PROJECT, REGION, ROOT, TAGS, client, load_state, save_state

POOL_NAME = f"{PROJECT}-users"
CLIENT_NAME = "demo-ui"
GROUPS = {
    "advisors": "Wealth / financial advisors",
    "recruiters": "Talent acquisition",
    "finance-leadership": "CFO office",
    "risk-compliance": "Risk & compliance analysts",
}
# username -> groups. `guest` has none, to show the agent rejecting a valid but unauthorised user.
USERS = {
    "priya": ["advisors"],
    "rahul": ["recruiters"],
    "meera": ["finance-leadership"],
    "arjun": ["risk-compliance"],
    "guest": [],
}

idp = client("cognito-idp")
state = load_state()


def demo_password() -> str:
    """One shared password for the demo users, kept in .env (generated on first run)."""
    if pw := os.environ.get("DEMO_USER_PASSWORD"):
        return pw
    alphabet = string.ascii_letters + string.digits
    pw = "Demo-" + "".join(secrets.choice(alphabet) for _ in range(12)) + "9a!"
    with open(ROOT / ".env", "a") as f:
        f.write(f"\nDEMO_USER_PASSWORD={pw}\n")
    print("generated DEMO_USER_PASSWORD and saved it to .env")
    return pw


pools = {p["Name"]: p["Id"] for p in idp.list_user_pools(MaxResults=60)["UserPools"]}
if POOL_NAME in pools:
    pool_id = pools[POOL_NAME]
    print(f"user pool {POOL_NAME} exists ({pool_id})")
else:
    pool_id = idp.create_user_pool(
        PoolName=POOL_NAME,
        Policies={"PasswordPolicy": {"MinimumLength": 12, "RequireUppercase": True, "RequireLowercase": True,
                                     "RequireNumbers": True, "RequireSymbols": True}},
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
        UserPoolTags=TAGS,
    )["UserPool"]["Id"]
    print(f"created user pool {pool_id}")

clients = {c["ClientName"]: c["ClientId"] for c in idp.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)["UserPoolClients"]}
if CLIENT_NAME in clients:
    client_id = clients[CLIENT_NAME]
else:
    client_id = idp.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName=CLIENT_NAME,
        GenerateSecret=False,  # public client: the browser holds the token, never a secret
        ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],  # demo only; use Hosted UI + PKCE in prod
        AccessTokenValidity=1, IdTokenValidity=1, TokenValidityUnits={"AccessToken": "hours", "IdToken": "hours"},
    )["UserPoolClient"]["ClientId"]
    print(f"created app client {CLIENT_NAME} ({client_id})")

existing_groups = {g["GroupName"] for g in idp.list_groups(UserPoolId=pool_id)["Groups"]}
for name, desc in GROUPS.items():
    if name not in existing_groups:
        idp.create_group(UserPoolId=pool_id, GroupName=name, Description=desc)
        print(f"created group {name}")

password = demo_password()
for username, groups in USERS.items():
    try:
        idp.admin_get_user(UserPoolId=pool_id, Username=username)
    except idp.exceptions.UserNotFoundException:
        idp.admin_create_user(UserPoolId=pool_id, Username=username, MessageAction="SUPPRESS")
        print(f"created user {username}")
    idp.admin_set_user_password(UserPoolId=pool_id, Username=username, Password=password, Permanent=True)
    for g in groups:
        idp.admin_add_user_to_group(UserPoolId=pool_id, Username=username, GroupName=g)

state["cognito"] = {
    "pool_id": pool_id,
    "client_id": client_id,
    "discovery_url": f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/.well-known/openid-configuration",
    "users": USERS,
}
save_state(state)
print(f"\npool {pool_id}, app client {client_id}\nusers: {', '.join(USERS)} (password in .env as DEMO_USER_PASSWORD)")
