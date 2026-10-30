"""MarketPro MCP with SAMPLE data, for trying the Data Insights page without MarketPro AWS access.

This runs the real hodor `mcp_graphql` server in AgentCore mode (same three tools, same public
schema, same read-only check, same token handling) and swaps only the data source: queries are
validated against the real schema and then answered from marketpro_sample_data.json, a small
synthetic dataset. Every firm, office, rep and team in it is made up.

Run it with the mcp_graphql virtualenv, which has the MarketPro schema installed:
  ../hodor/services/mcp_graphql/.venv/bin/python mcp_server/marketpro_sample.py
It serves http://127.0.0.1:8000/mcp, the same address as the real server in local mode.
"""

import base64
import contextvars
import json
import logging
import os
from pathlib import Path
from typing import Any

# The real server reads these at import time; sample mode never touches AWS or Okta.
for key, value in {
    "MCP_AUTH_MODE": "agentcore",
    "MCP_HOST": "127.0.0.1",
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "DB_CATALOG_MISIGHT": "sample",
    "DB_WORKGROUP": "sample",
    "AURORA_CLUSTER_ARN": "arn:aws:rds:us-east-1:000000000000:cluster:sample",
    "AURORA_SECRETS_ARN": "arn:aws:secretsmanager:us-east-1:000000000000:secret:sample",
    "DB_CATALOG_MI_INTEGRATION": "sample",
    "MEILISEARCH_API_SECRETS_ARN": "sample",
    "OKTA_CLIENT_ID": "sample",
    "OKTA_DOMAIN": "sample.okta.invalid",
}.items():
    os.environ.setdefault(key, value)

import graphql  # noqa: E402
from mcp_graphql import server  # noqa: E402
from mcp_graphql.schema_tools import public_schema, schema_data  # noqa: E402

# The caller's role, as `{ user { role } }` returns it. A mock token's `mock_role` claim wins, so
# the preview page can switch personas per request; otherwise SAMPLE_ROLE.
SAMPLE_ROLE = os.environ.get("SAMPLE_ROLE", "Wholesaler")
_caller_role: contextvars.ContextVar[str] = contextvars.ContextVar("caller_role", default=SAMPLE_ROLE)
DEFAULT_PAGE_SIZE = 10

log = logging.getLogger("marketpro_sample")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# --- sample dataset ------------------------------------------------------------
# Edit marketpro_sample_data.json to change the data (restart the server afterwards). The file holds
# each record's real GraphQL fields; the flat keys that filterBy / sortBy use are derived below, so
# a value lives in one place only.

DATA_FILE = Path(os.environ.get("SAMPLE_DATA_FILE", Path(__file__).with_name("marketpro_sample_data.json")))


def _with_filter_keys(data: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    for firm in data["firms"]:
        firm["numTotalReps"] = firm["employees"]["totalReps"]
        firm["mainOfficeState"] = firm["hqState"]
        firm["totalAssetsInMillions"] = firm["assets"]["total"]
    for office in data["offices"]:
        office["state"], office["city"] = office["address"]["state"], office["address"]["city"]
        office["firmName"], office["firmCrd"] = office["firm"]["name"], office["firm"]["crd"]
        office["reps"]["netGainLoss"] = office["reps"]["joined"] - office["reps"]["left"]
    for rep in data["reps"]:
        rep["firstName"], rep["lastName"], rep["fullName"] = rep["name"]["first"], rep["name"]["last"], rep["name"]["full"]
        rep["branchState"] = rep["officeState"]
        rep["firmName"], rep["firmCrd"] = rep["firm"]["name"], rep["firm"]["crd"]
    return data


DATA = _with_filter_keys(json.loads(DATA_FILE.read_text()))

# --- a tiny GraphQL engine over the dataset ----------------------------------


def _matches(row: dict, filter_by: dict | None) -> bool:
    for field, condition in (filter_by or {}).items():
        if field == "and_":
            if not all(_matches(row, sub) for sub in condition):
                return False
            continue
        if field == "or_":
            if condition and not any(_matches(row, sub) for sub in condition):
                return False
            continue
        if field not in row or not isinstance(condition, dict):
            continue  # filters the sample data doesn't model are ignored
        value = row[field]
        for op, target in condition.items():
            text = str(value).lower()
            ok = {
                "eq": lambda: value == target or text == str(target).lower(),
                "neq": lambda: value != target,
                "like": lambda: str(target).strip("%").lower() in text,
                "notLike": lambda: str(target).strip("%").lower() not in text,
                "in_": lambda: value in (target or []),
                "notIn": lambda: value not in (target or []),
                "gt": lambda: value is not None and value > target,
                "gte": lambda: value is not None and value >= target,
                "lt": lambda: value is not None and value < target,
                "lte": lambda: value is not None and value <= target,
            }.get(op, lambda: True)()
            if not ok:
                return False
    return True


def _list(rows: list[dict], args: dict) -> dict:
    found = [r for r in rows if _matches(r, args.get("filterBy"))]
    for field, direction in reversed(list((args.get("sortBy") or {}).items())):
        found.sort(key=lambda r: (r.get(field) is None, r.get(field)), reverse=direction == "DESC")
    size = min(args.get("pageSize") or DEFAULT_PAGE_SIZE, 100)
    return {"list": found[:size], "queryInfo": {"id": "sample", "rowCount": len(found), "nextToken": None}}


def _default(type_: graphql.GraphQLOutputType) -> Any:
    """A valid empty value for fields the sample data doesn't fill."""
    if not isinstance(type_, graphql.GraphQLNonNull):
        return None
    inner = type_.of_type
    if isinstance(inner, graphql.GraphQLList):
        return []
    if isinstance(inner, graphql.GraphQLEnumType):
        return next(iter(inner.values))
    if isinstance(inner, graphql.GraphQLScalarType):
        return {"String": "", "ID": "0", "Int": 0, "Float": 0.0, "Boolean": False}.get(inner.name, "")
    return {}


def _resolve(source: Any, info: graphql.GraphQLResolveInfo, **args: Any) -> Any:
    if info.parent_type.name == "Query":
        if info.field_name in DATA:
            return _list(DATA[info.field_name], args)
        if info.field_name == "user":
            return {"id": "sample-user", "username": "sample.user@example.com", "role": _caller_role.get(),
                    "firstName": "Sample", "lastName": "User", "client": "Sample Client"}
    if isinstance(source, dict) and source.get(info.field_name) is not None:
        return source[info.field_name]
    return _default(info.return_type)


class SampleSchema:
    """Validates with the real MarketPro schema; executes against the sample dataset."""

    def __init__(self):
        self._schema = public_schema()._schema
        self._sample = graphql.build_client_schema({"__schema": schema_data()})

    def execute_sync(self, query: str, context_value: Any = None) -> graphql.ExecutionResult:
        return graphql.graphql_sync(self._sample, query, field_resolver=_resolve)


_SAMPLE_SCHEMA = SampleSchema()


def _token_role(token: str) -> str:
    """The `mock_role` claim of an (unsigned) mock token, else SAMPLE_ROLE."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims.get("mock_role") or SAMPLE_ROLE
    except (IndexError, ValueError, AttributeError):
        return SAMPLE_ROLE


def _sample_context(token: str) -> dict:
    # The real server introspects the token with Okta here; sample mode only reads the mock role.
    role = _token_role(token)
    _caller_role.set(role)
    log.info("execute_query as role %r", role)
    return {"scope": None, "requestor": None}


server.public_schema = lambda: _SAMPLE_SCHEMA
server._get_context_for_query = _sample_context


if __name__ == "__main__":
    log.info("MarketPro MCP with SAMPLE data on http://127.0.0.1:8000/mcp (user role: %s)", SAMPLE_ROLE)
    server.mcp.run(transport="http", host="127.0.0.1", port=8000, path="/mcp",
                   stateless_http=True, json_response=True)
