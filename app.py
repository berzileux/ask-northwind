import inspect
import logging
import math
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from about import ABOUT_TEXT
import history
import pipeline
import sandbox
from model import MODEL_NAME, generate_sql
from schema import schema_data
from ratelimit import RateLimiter, client_key

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "northwind.db"
# Module level so tests can replace the model.
GENERATE = generate_sql
DEMO_TIMEOUT_S = 2.0
MAX_BODY_BYTES = 16384
# One visitor should not be able to use up the shared model quota, and ten
# questions a minute is plenty for a person reading each answer.
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW_S = 60
# State is per process; two instances each keep their own count.
LIMITER = RateLimiter(RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_S)
# Only the routes that can call the model or run a query are limited.
LIMITED_PATHS = ("/api/ask", "/api/sandbox-demo")

# Read from the sandbox signature so the number is defined in one place only.
MAX_ROWS = inspect.signature(sandbox.run_query).parameters["max_rows"].default

PAGE = (Path(__file__).parent / "index.html").read_text()

# Fixed on the server; a request names a demo by id and never carries SQL.
DEMOS = {
    "delete": {
        "label": "Delete all customers",
        "sql": "DELETE FROM Customers;",
    },
    "stacked": {
        "label": "Show customers, then drop the orders table",
        "sql": "SELECT * FROM Customers; DROP TABLE Orders;",
    },
    "slow": {
        "label": "Combine every order line with every other, four times over",
        "sql": (
            'SELECT COUNT(*) FROM "Order Details" a, "Order Details" b, '
            '"Order Details" c, "Order Details" d'
        ),
    },
}


@asynccontextmanager
async def lifespan(app):
    if not DB_PATH.exists():
        raise RuntimeError(f"{DB_PATH.name} is missing; see the README (Data section)")
    yield


# The interactive documentation pages are switched off.
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class AskBody(BaseModel):
    question: str = Field(max_length=2000)
    # Left untyped here and checked by clean_history in the handler, because the
    # framework's own error body for a wrong type would repeat the input.
    history: Any = None


class DemoBody(BaseModel):
    id: str


def security_header_items(path):
    items = [
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Frame-Options", "DENY"),
    ]
    if path.startswith("/api"):
        items.append(("Cache-Control", "no-store"))
    return items


def rate_limited_response(wait_s):
    message = f"You are asking too fast. Please wait {wait_s} seconds."
    body = {
        "question": "",
        "sql": "",
        "columns": [],
        "rows": [],
        "truncated": False,
        "elapsed_s": 0.0,
        "query_s": None,
        "error": message,
        "error_kind": "rate_limited",
        "source": "model",
        "limits": {"timeout_s": pipeline.QUERY_TIMEOUT_S, "max_rows": MAX_ROWS},
        "retry_after_s": wait_s,
    }
    return JSONResponse(body, status_code=429, headers={"Retry-After": str(wait_s)})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_BODY_BYTES:
        response = JSONResponse({"error": "The request is too large."}, status_code=413)
    elif request.method == "POST" and request.url.path in LIMITED_PATHS:
        host = request.client.host if request.client else None
        allowed, wait_s = LIMITER.allow(client_key(request.headers.get("x-forwarded-for"), host))
        response = await call_next(request) if allowed else rate_limited_response(wait_s)
    else:
        response = await call_next(request)
    for name, value in security_header_items(request.url.path):
        response.headers[name] = value
    return response


class BodyLimit:
    """Counts the request body as it arrives, whatever the headers claim.

    The body is read up to the cap and replayed to the app, so a chunked
    request with no Content-Length cannot get past the limit.
    """

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.inner(scope, receive, send)
            return
        seen, total = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.inner(scope, receive, send)
                return
            total += len(message.get("body", b""))
            if total > MAX_BODY_BYTES:
                await self.refuse(scope, send)
                return
            seen.append(message)
            if not message.get("more_body", False):
                break
        replay = iter(seen)

        async def replayed():
            try:
                return next(replay)
            except StopIteration:
                return await receive()

        await self.inner(scope, replayed, send)

    async def refuse(self, scope, send):
        body = b'{"error":"The request is too large."}'
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        for name, value in security_header_items(scope.get("path", "")):
            headers.append((name.lower().encode(), value.encode()))
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})


# Added after the middleware above so it is the outermost layer.
app.add_middleware(BodyLimit)


def json_safe(value):
    """Make one database cell safe to send as JSON and to read in a browser."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary, {len(value)} bytes>"
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        if math.isnan(value):
            return "NaN"
        return "Infinity" if value > 0 else "-Infinity"
    # Browsers read JSON numbers as doubles, which lose integers above 2**53.
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53 - 1:
        return str(value)
    return value


def payload(answer, source, timeout_s):
    return {
        "question": answer.question,
        "sql": answer.sql,
        "columns": answer.columns,
        "rows": [[json_safe(cell) for cell in row] for row in answer.rows],
        "truncated": answer.truncated,
        "elapsed_s": answer.elapsed_s,
        "query_s": answer.query_s,
        "error": answer.error,
        "error_kind": answer.error_kind,
        "source": source,
        "limits": {"timeout_s": timeout_s, "max_rows": MAX_ROWS},
    }


def respond(build):
    try:
        return JSONResponse(build())
    except Exception as exc:
        # The type and message only: never the question text or the environment.
        log.error("request failed: %s: %s", type(exc).__name__, exc)
        return JSONResponse({"error": "Something went wrong."}, status_code=500)


@app.get("/", response_class=HTMLResponse)
def index():
    nonce = secrets.token_urlsafe(16)
    response = HTMLResponse(PAGE.replace("__NONCE__", nonce))
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; "
        f"style-src 'nonce-{nonce}'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    return response


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/limits")
def api_limits():
    return {
        "timeout_s": pipeline.QUERY_TIMEOUT_S,
        "max_rows": MAX_ROWS,
        "slots": pipeline.GATE_SLOTS,
        "memory_turns": history.MAX_TURNS,
        "rate_limit_per_minute": RATE_LIMIT_REQUESTS,
        "model": MODEL_NAME,
    }


@app.get("/api/schema")
def api_schema():
    # Reads the file directly; no model call and no query slot is involved.
    return respond(lambda: {"tables": schema_data(DB_PATH)})


@app.get("/api/about")
def api_about():
    # The text is a file written by the site author; no model call, no database.
    return {"text": ABOUT_TEXT}


# The ask handlers are plain functions so the server runs them in its thread
# pool: ask() blocks while it waits for a slot and while the query runs.
@app.post("/api/ask")
def api_ask(body: AskBody):
    try:
        turns = history.clean_history(body.history)
    except ValueError:
        return JSONResponse({"error": "The history could not be accepted."}, status_code=422)

    def build():
        answer = pipeline.ask(body.question, DB_PATH, generate=GENERATE, history=turns)
        return payload(answer, "model", pipeline.QUERY_TIMEOUT_S)

    return respond(build)


@app.get("/api/sandbox-demos")
def api_sandbox_demos():
    return [{"id": key, "label": demo["label"]} for key, demo in DEMOS.items()]


@app.post("/api/sandbox-demo")
def api_sandbox_demo(body: DemoBody):
    demo = DEMOS.get(body.id)
    if demo is None:
        return JSONResponse({"error": "Unknown demo."}, status_code=404)

    def build():
        fixed_sql = demo["sql"]
        answer = pipeline.ask(
            demo["label"],
            DB_PATH,
            generate=lambda prompt: fixed_sql,
            timeout_s=DEMO_TIMEOUT_S,
            # No model is called for a demo, so a used-up quota must not block it.
            use_cooldown=False,
        )
        return payload(answer, "demo", DEMO_TIMEOUT_S)

    return respond(build)
