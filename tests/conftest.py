"""Fixtures for the black-box API tests.

Only the provider block is autouse: it removes credentials and points the
fallback at a closed local port. The provider fakes are opt-in because
test_jev.py and test_fallback.py run the real providers on stub transports.
App modules are imported inside fixtures so a broken app module fails the tests
that need it instead of every test in this directory.
"""
import sqlite3
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test can reach a real provider: there are no credentials, and the fallback points at a closed local port."""
    # The two ANTHROPIC names date from the earlier fallback provider. Nothing reads them now that the fallback is
    # an OpenAI-compatible endpoint (local Ollama by default); they are still removed so no test can depend on them.
    for name in (
        "TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
        "FALLBACK_API_KEY", "FALLBACK_MODEL", "FALLBACK_REASONING_EFFORT", "FALLBACK_TIMEOUT_S",
        "FALLBACK_USD_PER_MTOK_IN", "FALLBACK_USD_PER_MTOK_OUT",
    ):
        monkeypatch.delenv(name, raising=False)
    # The fallback needs no key, so a developer's running Ollama would otherwise answer an unstubbed call.
    monkeypatch.setenv("FALLBACK_BASE_URL", "http://127.0.0.1:9/v1")


class Providers:
    """Recording stand-ins for app.jev.score and app.fallback.draft."""

    def __init__(self, jev_error: type[Exception], fallback_error: type[Exception]) -> None:
        self.JevError = jev_error
        self.FallbackError = fallback_error
        # Each jev.score call consumes one entry; the last entry repeats. An exception instance is raised.
        self.scores: list[dict | Exception] = [{"supported": 0.95, "complete": 0.95, "requires_human": 0.02}]
        # What fallback.draft returns. Assign an exception instance to make it raise.
        self.draft_result: dict | Exception = {
            "kind": "reply",
            "text": "Thanks for getting in touch. Is there anything else I can help you with?",
            "action": None,
            "input_tokens": 1200,
            "output_tokens": 80,
            "cost_usd": 0.012,
            "elapsed_ms": 900,
        }
        self.score_calls: list[dict] = []
        self.draft_calls: list[dict] = []

    def score(
        self, message: str, history: list[dict], item: dict | None, policy: dict, today: str, reply: str
    ) -> dict:
        self.score_calls.append(
            {
                "message": message,
                "history": list(history),
                "item": item,
                "policy": policy,
                "today": today,
                "reply": reply,
            }
        )
        result = self.scores.pop(0) if len(self.scores) > 1 else self.scores[0]
        if isinstance(result, Exception):
            raise result
        return {
            **result,
            "model": "jev-1.14.0",
            "input_tokens": 900,
            "cost_usd": 0.0000378,
            "request_id": "req_test",
            "elapsed_ms": 12,
        }

    def draft(
        self,
        message: str,
        history: list[dict],
        item: dict | None,
        policy: dict,
        today: str,
        rejected: dict | None = None,
    ) -> dict:
        self.draft_calls.append(
            {
                "message": message,
                "history": list(history),
                "item": item,
                "policy": policy,
                "today": today,
                "rejected": rejected,
            }
        )
        if isinstance(self.draft_result, Exception):
            raise self.draft_result
        return dict(self.draft_result)


class Session:
    """One demo session, driven over HTTP the way the web page drives it."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        response = client.post("/api/session")
        assert response.status_code == 200, response.text
        self.token: str = response.json()["session"]
        self.headers = {"X-Session": self.token}

    @property
    def store(self):
        """This session's Store, where app.main keeps it."""
        import app.main

        return app.main.SESSIONS[self.token]

    def sql(self, query: str, *params: object) -> list[sqlite3.Row]:
        """Run one statement on this session's database and commit it."""
        db = self.store.db
        rows = db.execute(query, params).fetchall()
        db.commit()
        return rows

    def get(self, path: str) -> httpx.Response:
        return self.client.get(path, headers=self.headers)

    def chat(self, message: str, item_id: str | None = None, quick_action: str | None = None) -> httpx.Response:
        body = {"message": message, "item_id": item_id, "quick_action": quick_action}
        return self.client.post("/api/chat", json=body, headers=self.headers)

    def confirm(self, token: str) -> httpx.Response:
        return self.client.post(f"/api/actions/{token}/confirm", headers=self.headers)

    def items(self) -> dict[str, dict]:
        """Every item GET /api/orders shows, by id."""
        response = self.get("/api/orders")
        assert response.status_code == 200, response.text
        return {item["id"]: item for order in response.json()["orders"] for item in order["items"]}

    def last_trace(self) -> dict:
        """The newest trace GET /api/trace shows."""
        response = self.get("/api/trace")
        assert response.status_code == 200, response.text
        return response.json()["traces"][-1]


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> Providers:
    """Opt-in: replace Jev and the fallback model with recording fakes."""
    from app import fallback, jev

    fakes = Providers(jev.JevError, fallback.FallbackError)
    monkeypatch.setattr(jev, "score", fakes.score)
    monkeypatch.setattr(fallback, "draft", fakes.draft)
    return fakes


@pytest.fixture
def client() -> Iterator[TestClient]:
    import app.main

    app.main.SESSIONS.clear()  # every test starts like a freshly started server
    with TestClient(app.main.app) as test_client:
        yield test_client
    app.main.SESSIONS.clear()


@pytest.fixture
def new_session(client: TestClient) -> Callable[[], Session]:
    """A factory, for tests that need more than one session."""
    return lambda: Session(client)


@pytest.fixture
def session(new_session: Callable[[], Session]) -> Session:
    return new_session()
