"""HTTP API of the support demo: one in-memory Store per session token, plus the static web page."""
import logging
import secrets
import threading
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StringConstraints, model_validator

from app import jev, router, rules
from app.store import Store

logging.basicConfig(level=logging.INFO)

MAX_SESSIONS = 200
SESSIONS: dict[str, Store] = {}  # session token -> Store, oldest first
_sessions_lock = threading.Lock()  # creating and evicting sessions is not one atomic dict operation
_WEB = Path(__file__).resolve().parent / "web"

# A larger body is refused with 413 before it is read or parsed; the page's largest chat body is about 6 kB.
# No documentation routes: nothing here uses them, and /docs would load scripts from another host.
app = FastAPI(max_body_size=16 * 1024, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(RequestValidationError)
def _invalid_body(request: Request, error: RequestValidationError) -> JSONResponse:
    # FastAPI's own handler copies the rejected input into the 422, and fails with a 500 when that input cannot
    # be encoded as JSON (NaN, a lone surrogate, bytes that are not UTF-8). This one says what is wrong only.
    detail = [{"type": each["type"], "loc": each["loc"], "msg": each["msg"]} for each in error.errors()]
    return JSONResponse({"detail": detail}, status_code=422)


class ChatRequest(BaseModel):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    item_id: Annotated[str, StringConstraints(max_length=64)] | None = None  # real ids are 8 characters
    quick_action: Literal[rules.INTENTS] | None = None  # subscripting with the tuple lists its members

    @model_validator(mode="before")
    @classmethod
    def _encodable(cls, body: object) -> object:
        # JSON can escape half of a surrogate pair. That is not text: SQLite and the providers cannot encode it,
        # so the lookup of an unknown item_id would fail on it. It becomes "?" here.
        if not isinstance(body, dict):
            return body
        return {
            key: value.encode("utf-8", "replace").decode() if isinstance(value, str) else value
            for key, value in body.items()
        }


def _session(x_session: Annotated[str | None, Header()] = None) -> Store:
    """The Store of the session named by the X-Session header."""
    store = SESSIONS.get(x_session)
    if store is None:
        raise HTTPException(status_code=401, detail="session_expired")
    return store


# Each endpoint takes store.lock in its own body, not in the dependency: FastAPI can run a dependency and its
# endpoint on different worker threads, and an RLock belongs to the thread that took it.
# ponytail: the lock is held across the provider calls of a chat, so the session's other requests wait behind
# it. That suits one browser tab per session. Upgrade path: release it around the provider calls and re-read
# the records before replying.
SessionStore = Annotated[Store, Depends(_session)]


@app.post("/api/session")
def create_session() -> dict:
    token, store = secrets.token_urlsafe(24), Store()
    with _sessions_lock:
        SESSIONS[token] = store
        # ponytail: evicts by creation order, so a burst of new sessions ends the live ones. Evict by last use,
        # or limit how fast sessions can be created, if the demo is abused.
        while len(SESSIONS) > MAX_SESSIONS:
            del SESSIONS[next(iter(SESSIONS))]
    return {"session": token}


@app.get("/api/orders")
def orders(store: SessionStore) -> dict:
    with store.lock:
        return {"customer": {"name": store.customer["name"]}, "today": store.today, "orders": store.orders()}


@app.post("/api/chat")
def chat(body: ChatRequest, response: Response, store: SessionStore) -> dict:
    with store.lock:
        payload = router.handle(store, body.message, body.item_id, body.quick_action)
    if payload.get("limit_reached"):
        response.status_code = 429
    return payload


@app.post("/api/actions/{token}/confirm")
def confirm(token: str, store: SessionStore) -> dict:
    with store.lock:
        outcome = router.confirm(store, token)
    if outcome is None:  # unknown here, which includes a token that belongs to another session
        raise HTTPException(status_code=404, detail="unknown_action")
    return outcome


@app.get("/api/trace")
def trace(store: SessionStore) -> dict:
    gate = {key: jev.GATE[key] for key in ("version", "status", "model", "policy_version", "thresholds")}
    with store.lock:
        return {"gate": gate, "traces": store.traces()}


class _Revalidated(StaticFiles):
    """The page's files with Cache-Control: no-cache, so the browser asks before reusing its copy (a 304 when
    the ETag still matches) and a redeploy is not masked by a stale app.js or style.css."""

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# After the API routes, so that /api paths are matched first.
app.mount("/", _Revalidated(directory=_WEB, html=True), name="web")
