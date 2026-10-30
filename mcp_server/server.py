"""Finance MCP server: Alpha Vantage free endpoints, cached once per day and trimmed for LLMs.

Every persona reads the same cached copy, so N agents asking about one company cost one
Alpha Vantage fetch per endpoint per day. Runs on the AgentCore Runtime MCP contract:
streamable HTTP, stateless, 0.0.0.0:8000/mcp.

Cache: S3 when CACHE_BUCKET is set (cloud), else ./.cache (local).
API key: ALPHAVANTAGE_API_KEY, or ALPHAVANTAGE_SECRET_ARN to read it from Secrets Manager.
"""

import asyncio
import contextvars
import datetime as dt
import json
import logging
import math
import os
import re
import statistics
import sys
import time
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import ClientError
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.env import load_env  # noqa: E402
from common.identity import caller  # noqa: E402

load_env()

AV_URL = "https://www.alphavantage.co/query"
MIN_INTERVAL_S = 1.2  # free tier rejects bursts
CACHE_BUCKET = os.environ.get("CACHE_BUCKET")
LOCAL_CACHE = Path(os.environ.get("CACHE_DIR", Path(__file__).resolve().parent.parent / ".cache"))
PERIODS = 4

log = logging.getLogger("finance_mcp")
logging.basicConfig(level=logging.INFO, format="%(message)s")

mcp = MCPServer(
    name="finance",
    instructions="Company financial data (US-listed tickers work best). Call get_company_snapshot first; "
                 "it is the shared fact base. Use the other tools only for detail it lacks.",
)

_s3 = boto3.client("s3") if CACHE_BUCKET else None
_key_lock = asyncio.Lock()
_throttle_lock = asyncio.Lock()
_last_call = 0.0
_api_key: str | None = None
# Per tool call: how many endpoint reads came from the cache vs Alpha Vantage (shown in the UI).
_data_stats: contextvars.ContextVar[dict | None] = contextvars.ContextVar("data_stats", default=None)
_cold = True  # first tool call in this microVM


async def _get_api_key() -> str:
    global _api_key
    if _api_key is None:
        if arn := os.environ.get("ALPHAVANTAGE_SECRET_ARN"):
            resp = await asyncio.to_thread(boto3.client("secretsmanager").get_secret_value, SecretId=arn)
            _api_key = resp["SecretString"]
        else:
            _api_key = os.environ["ALPHAVANTAGE_API_KEY"]
    return _api_key


# --- cache -------------------------------------------------------------------

def _cache_get(key: str) -> dict | None:
    if _s3:
        try:
            return json.loads(_s3.get_object(Bucket=CACHE_BUCKET, Key=key)["Body"].read())
        except _s3.exceptions.NoSuchKey:
            return None
    path = LOCAL_CACHE / key
    return json.loads(path.read_text()) if path.exists() else None


def _cache_put(key: str, value: dict) -> None:
    if _s3:
        _s3.put_object(Bucket=CACHE_BUCKET, Key=key, Body=json.dumps(value).encode(), ContentType="application/json")
        return
    path = LOCAL_CACHE / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _claim(key: str) -> bool:
    """S3 conditional write: exactly one caller (across microVMs) wins the right to fetch."""
    if not _s3:
        return True
    try:
        _s3.put_object(Bucket=CACHE_BUCKET, Key=f"{key}.lock", Body=b"", IfNoneMatch="*")
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("PreconditionFailed", "ConditionalRequestConflict"):
            return False
        raise


# --- Alpha Vantage -----------------------------------------------------------

async def _av_fetch(params: dict) -> dict:
    global _last_call
    key = await _get_api_key()
    for attempt in range(3):
        async with _throttle_lock:
            wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            async with httpx.AsyncClient(timeout=30) as http:
                data = (await http.get(AV_URL, params={**params, "apikey": key})).json()
            _last_call = time.monotonic()
        note = data.get("Information") or data.get("Note") or data.get("Error Message")
        if not note:
            return data
        if "premium" in note.lower() or "Error Message" in data:
            return {"error": note}
        if "per day" in note.lower() or attempt == 2:
            return {"error": f"Alpha Vantage rate limit: {note}"}
        await asyncio.sleep(3 * (attempt + 1))  # burst limit: back off and retry
    return {"error": "unreachable"}


def _release(key: str) -> None:
    if _s3:
        _s3.delete_object(Bucket=CACHE_BUCKET, Key=f"{key}.lock")


async def av(function: str, **params) -> dict:
    """Fetch an Alpha Vantage endpoint through the shared daily cache. Errors are not cached."""
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    ident = re.sub(r"[^A-Za-z0-9._-]", "_", "_".join(str(v).upper() for v in params.values()))
    key = f"cache/{day}/{function}/{ident}.json"
    stats = _data_stats.get()
    if stats is None:  # called outside a traced tool
        stats = {}
    if (hit := await asyncio.to_thread(_cache_get, key)) is not None:
        log.info(json.dumps({"event": "cache_hit", "key": key}))
        stats["cache_hits"] = stats.get("cache_hits", 0) + 1
        return hit
    async with _key_lock:  # serialise fetches inside this process
        if (hit := await asyncio.to_thread(_cache_get, key)) is not None:
            return hit
        if not await asyncio.to_thread(_claim, key):
            for _ in range(30):  # another microVM is fetching it; wait for its result
                await asyncio.sleep(1)
                if (hit := await asyncio.to_thread(_cache_get, key)) is not None:
                    return hit
        data = await _av_fetch({"function": function, **params})
        stats["api_calls"] = stats.get("api_calls", 0) + 1
        log.info(json.dumps({"event": "alpha_vantage_fetch", "function": function, **params, "ok": "error" not in data}))
        if "error" in data:
            await asyncio.to_thread(_release, key)  # let the next caller retry
        else:
            await asyncio.to_thread(_cache_put, key, data)
        return data


# --- shaping -----------------------------------------------------------------

def num(value) -> float | None:
    try:
        f = float(value)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def ratio(a, b) -> float | None:
    return round(a / b, 4) if a is not None and b not in (None, 0) else None


def pick(report: dict, fields: list[str]) -> dict:
    return {"period_end": report.get("fiscalDateEnding"), **{f: num(report.get(f)) for f in fields}}


STATEMENT_FIELDS = {
    "INCOME_STATEMENT": ["totalRevenue", "grossProfit", "operatingIncome", "netIncome", "ebitda",
                         "researchAndDevelopment", "sellingGeneralAndAdministrative", "interestExpense"],
    "BALANCE_SHEET": ["totalAssets", "totalLiabilities", "totalShareholderEquity", "cashAndCashEquivalentsAtCarryingValue",
                      "totalCurrentAssets", "totalCurrentLiabilities", "shortTermDebt", "longTermDebt", "commonStockSharesOutstanding"],
    "CASH_FLOW": ["operatingCashflow", "capitalExpenditures", "dividendPayout", "paymentsForRepurchaseOfCommonStock"],
}
STATEMENTS = {"income": "INCOME_STATEMENT", "balance": "BALANCE_SHEET", "cash_flow": "CASH_FLOW"}


def trimmed(data: dict, function: str, period: str) -> list[dict]:
    reports = data.get("annualReports" if period == "annual" else "quarterlyReports", [])
    return [pick(r, STATEMENT_FIELDS[function]) for r in reports[:PERIODS]]


# --- identity ----------------------------------------------------------------

def end_user(ctx: Context) -> tuple[dict, str | None]:
    """Who this call is for, and their token.

    Cloud: the Gateway's interceptor puts the validated caller and token into `params._meta`
    (the Gateway itself reaches us with its IAM role). Local: the token arrives as a header.
    A GraphQL-backed MCP would send `token` on to the API so its resolvers enforce per-user access.
    """
    meta = (ctx.request_context.params or {}).get("_meta") or {}
    if who := meta.get("agentcore-demo/caller"):
        return who, meta.get("agentcore-demo/user_token")
    header = (ctx.headers or {}).get("authorization")
    return caller(header), header


async def traced(ctx: Context, tool: str, work) -> dict:
    """Run a tool's work and stamp the result with `_trace`: who it was for, when the request arrived,
    how long we took, and where the data came from. The agent strips it before the model sees it."""
    global _cold
    received_ms = int(time.time() * 1000)
    stats: dict = {}
    _data_stats.set(stats)
    start = time.monotonic()
    result = await work
    who, _token = end_user(ctx)
    meta = (ctx.request_context.params or {}).get("_meta") or {}
    stamp = {
        "user": who.get("user"), "groups": who.get("groups"),
        "identity_via": "gateway interceptor (_meta)" if meta.get("agentcore-demo/caller") else ("header" if who else None),
        "intercepted_at_ms": meta.get("agentcore-demo/intercepted_at_ms"),
        "received_at_ms": received_ms, "server_ms": int((time.monotonic() - start) * 1000),
        "cache_hits": stats.get("cache_hits", 0), "api_calls": stats.get("api_calls", 0),
        "microvm": "cold" if _cold else "warm",
    }
    _cold = False
    log.info(json.dumps({"event": "tool_call", "tool": tool, **{k: v for k, v in stamp.items() if k != "groups"}}))
    return {**result, "_trace": stamp}


# --- tools -------------------------------------------------------------------
# Each public tool is traced (who, timing, data source); the private _impl does the data work.

@mcp.tool()
async def search_company(query: str, ctx: Context) -> dict:
    """Find ticker symbols by company name or keyword, e.g. "microsoft" -> MSFT. Use when the ticker is unknown."""
    return await traced(ctx, "search_company", _search_company(query))


async def _search_company(query: str) -> dict:
    data = await av("SYMBOL_SEARCH", keywords=query)
    if "error" in data:
        return data
    return {"matches": [
        {"symbol": m.get("1. symbol"), "name": m.get("2. name"), "region": m.get("4. region"), "currency": m.get("8. currency")}
        for m in data.get("bestMatches", [])[:5]
    ]}


@mcp.tool()
async def get_company_snapshot(symbol: str, ctx: Context) -> dict:
    """One-call fact base for a listed company (use this first): latest price, revenue and growth,
    margins, net income, leverage and liquidity, free cash flow, dividends and buybacks, last 4
    quarters of EPS vs estimates, and 12-month price return, volatility and max drawdown.
    All figures in the company's reporting currency; `data_gaps` lists anything unavailable."""
    return await traced(ctx, "get_company_snapshot", _get_company_snapshot(symbol))


async def _get_company_snapshot(symbol: str) -> dict:
    symbol = symbol.upper().strip()
    quote, income, balance, cash, earnings, monthly = [
        await av(f, symbol=symbol) for f in ("GLOBAL_QUOTE", "INCOME_STATEMENT", "BALANCE_SHEET", "CASH_FLOW", "EARNINGS", "TIME_SERIES_MONTHLY")
    ]
    gaps = [name for name, d in [("quote", quote), ("income", income), ("balance", balance), ("cash_flow", cash),
                                 ("earnings", earnings), ("price_history", monthly)] if "error" in d]
    snap: dict = {"symbol": symbol, "as_of": dt.date.today().isoformat(), "data_gaps": gaps}

    if q := quote.get("Global Quote"):
        snap["price"] = {"last": num(q.get("05. price")), "change_pct": q.get("10. change percent"),
                         "latest_trading_day": q.get("07. latest trading day")}

    inc = trimmed(income, "INCOME_STATEMENT", "annual")
    if inc:
        cur, prev = inc[0], (inc[1] if len(inc) > 1 else {})
        rev = cur["totalRevenue"]
        snap["currency"] = income["annualReports"][0].get("reportedCurrency")
        snap["income_latest_fy"] = {
            "period_end": cur["period_end"], "revenue": rev, "net_income": cur["netIncome"],
            "revenue_growth_yoy": ratio(rev - prev["totalRevenue"], prev["totalRevenue"]) if rev and prev.get("totalRevenue") else None,
            "gross_margin": ratio(cur["grossProfit"], rev), "operating_margin": ratio(cur["operatingIncome"], rev),
            "net_margin": ratio(cur["netIncome"], rev), "rnd_to_revenue": ratio(cur["researchAndDevelopment"], rev),
        }
        snap["revenue_history"] = [{"period_end": r["period_end"], "revenue": r["totalRevenue"], "net_income": r["netIncome"]} for r in inc]

    if bal := trimmed(balance, "BALANCE_SHEET", "annual"):
        b = bal[0]
        debt = (b["shortTermDebt"] or 0) + (b["longTermDebt"] or 0)
        snap["balance_latest_fy"] = {
            "period_end": b["period_end"], "total_assets": b["totalAssets"], "equity": b["totalShareholderEquity"],
            "cash": b["cashAndCashEquivalentsAtCarryingValue"], "total_debt": debt,
            "debt_to_equity": ratio(debt, b["totalShareholderEquity"]),
            "current_ratio": ratio(b["totalCurrentAssets"], b["totalCurrentLiabilities"]),
            "shares_outstanding": b["commonStockSharesOutstanding"],
        }

    if cf := trimmed(cash, "CASH_FLOW", "annual"):
        c = cf[0]
        fcf = c["operatingCashflow"] - (c["capitalExpenditures"] or 0) if c["operatingCashflow"] is not None else None
        snap["cash_flow_latest_fy"] = {"period_end": c["period_end"], "operating_cash_flow": c["operatingCashflow"],
                                       "capex": c["capitalExpenditures"], "free_cash_flow": fcf,
                                       "dividends_paid": c["dividendPayout"], "buybacks": c["paymentsForRepurchaseOfCommonStock"]}

    if q_eps := earnings.get("quarterlyEarnings"):
        snap["eps_last_4_quarters"] = [{"period_end": e.get("fiscalDateEnding"), "reported": num(e.get("reportedEPS")),
                                        "estimated": num(e.get("estimatedEPS")), "surprise_pct": num(e.get("surprisePercentage"))}
                                       for e in q_eps[:4]]

    closes = [num(v["4. close"]) for _, v in sorted(monthly.get("Monthly Time Series", {}).items(), reverse=True)[:13]][::-1]
    if len(closes) >= 3:
        rets = [b / a - 1 for a, b in zip(closes, closes[1:])]
        peak, drawdown = closes[0], 0.0
        for c in closes:
            peak = max(peak, c)
            drawdown = min(drawdown, c / peak - 1)
        snap["price_12m"] = {"return": round(closes[-1] / closes[0] - 1, 4),
                             "annualised_volatility": round(statistics.stdev(rets) * math.sqrt(12), 4),
                             "max_drawdown": round(drawdown, 4)}

    if price := snap.get("price", {}).get("last"):
        shares = snap.get("balance_latest_fy", {}).get("shares_outstanding")
        ni = snap.get("income_latest_fy", {}).get("net_income")
        if shares:
            snap["valuation"] = {"market_cap_approx": round(price * shares), "pe_trailing_fy": ratio(price * shares, ni)}
    return snap


@mcp.tool()
async def get_financial_statements(ctx: Context, symbol: str, statement: str = "income", period: str = "annual") -> dict:
    """Last 4 periods of one statement. statement: "income" | "balance" | "cash_flow";
    period: "annual" | "quarterly". Use only when get_company_snapshot lacks the detail you need."""
    return await traced(ctx, "get_financial_statements", _get_financial_statements(symbol, statement, period))


async def _get_financial_statements(symbol: str, statement: str = "income", period: str = "annual") -> dict:
    if statement not in STATEMENTS or period not in ("annual", "quarterly"):
        return {"error": "statement must be income|balance|cash_flow and period annual|quarterly"}
    function = STATEMENTS[statement]
    data = await av(function, symbol=symbol.upper().strip())
    return data if "error" in data else {"symbol": symbol.upper(), "statement": statement, "period": period,
                                         "reports": trimmed(data, function, period)}


@mcp.tool()
async def get_earnings(symbol: str, ctx: Context) -> dict:
    """Last 4 annual EPS figures and last 4 quarters of reported vs estimated EPS with surprise %."""
    return await traced(ctx, "get_earnings", _get_earnings(symbol))


async def _get_earnings(symbol: str) -> dict:
    data = await av("EARNINGS", symbol=symbol.upper().strip())
    if "error" in data:
        return data
    return {"symbol": symbol.upper(),
            "annual": [{"year_end": e.get("fiscalDateEnding"), "eps": num(e.get("reportedEPS"))} for e in data.get("annualEarnings", [])[:4]],
            "quarterly": [{"period_end": e.get("fiscalDateEnding"), "reported": num(e.get("reportedEPS")),
                           "estimated": num(e.get("estimatedEPS")), "surprise_pct": num(e.get("surprisePercentage"))}
                          for e in data.get("quarterlyEarnings", [])[:4]]}


@mcp.tool()
async def get_price_history(symbol: str, ctx: Context) -> dict:
    """Month-end closing prices for the last 12 months."""
    return await traced(ctx, "get_price_history", _get_price_history(symbol))


async def _get_price_history(symbol: str) -> dict:
    data = await av("TIME_SERIES_MONTHLY", symbol=symbol.upper().strip())
    if "error" in data:
        return data
    series = sorted(data.get("Monthly Time Series", {}).items(), reverse=True)[:12]
    return {"symbol": symbol.upper(), "monthly_close": [{"month_end": d, "close": num(v["4. close"])} for d, v in series]}


@mcp.custom_route("/ping", methods=["GET"])
async def ping(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "Healthy"})


if __name__ == "__main__":
    asyncio.run(mcp.run_streamable_http_async(
        host="0.0.0.0", port=8000, streamable_http_path="/mcp", stateless_http=True, json_response=True,
        # AgentCore and the Gateway call with their own Host header; auth is enforced by the runtime.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    ))
