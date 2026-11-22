"""Sessions over HTTP: authentication, isolation, restart reset and the session cap."""
from pathlib import Path

import pytest

SHIRT = "ITM-1002"
STATUSES = {
    "ITM-1001": "shipped",
    "ITM-1002": "packed",
    "ITM-1003": "delivered",
    "ITM-1004": "delivered",
    "ITM-1005": "delivered",
    "ITM-1006": "return_requested",
    "ITM-1007": "refund_processing",
    "ITM-1008": "refund_processing",
    "ITM-1009": "delivered",
}
ORDER_ITEM_KEYS = {"id", "order_id", "name", "noun", "status", "variant", "price_inr", "eta", "delivered_at"}
INDEX = Path(__file__).resolve().parents[1] / "app" / "web" / "index.html"


def cancel_shirt(session) -> None:
    """Change one session's state the way a customer does: offer, then confirm."""
    payload = session.chat("I want to cancel this item", SHIRT, "cancel").json()
    response = session.confirm(payload["pending_action"]["token"])
    assert response.json()["status"] == "done"


@pytest.mark.parametrize("headers", [{}, {"X-Session": "not-a-session"}], ids=["missing", "unknown"])
@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/api/orders", None),
        ("POST", "/api/chat", {"message": "hello", "item_id": None, "quick_action": None}),
        ("POST", "/api/actions/some-token/confirm", None),
        ("GET", "/api/trace", None),
    ],
    ids=["orders", "chat", "confirm", "trace"],
)
def test_api_requires_a_live_session(session, method, path, body, headers):
    """A live session exists, so an unknown token is refused for what it is, not for an empty table."""
    response = session.client.request(method, path, json=body, headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": "session_expired"}


def test_sessions_do_not_share_state(new_session, providers):
    first, second = new_session(), new_session()

    cancel_shirt(first)

    assert first.token != second.token
    assert first.items()[SHIRT]["status"] == "cancelled"
    assert second.items()[SHIRT]["status"] == "packed"
    assert second.get("/api/trace").json()["traces"] == []
    assert second.sql("SELECT * FROM messages") == []
    assert second.sql("SELECT * FROM refunds WHERE item_id = ?", SHIRT) == []


def test_restart_starts_again_from_the_fixtures(new_session, providers):
    import app.main

    before = new_session()
    pristine = before.get("/api/orders").json()
    cancel_shirt(before)
    assert before.get("/api/orders").json() != pristine

    app.main.SESSIONS.clear()  # sessions live in memory only, so this is all a restart does

    expired = before.get("/api/orders")
    assert expired.status_code == 401
    assert expired.json() == {"detail": "session_expired"}
    after = new_session()
    assert after.get("/api/orders").json() == pristine
    assert after.get("/api/trace").json()["traces"] == []


def test_oldest_session_is_evicted_at_the_cap(new_session, monkeypatch):
    import app.main

    monkeypatch.setattr(app.main, "MAX_SESSIONS", 3)

    sessions = [new_session() for _ in range(4)]

    assert sessions[0].get("/api/orders").status_code == 401
    assert [s.get("/api/orders").status_code for s in sessions[1:]] == [200, 200, 200]
    assert list(app.main.SESSIONS) == [s.token for s in sessions[1:]]


def test_requests_on_one_session_are_served_one_at_a_time(session, providers, monkeypatch):
    """The session lock is held for the whole request, so concurrent chats cannot get past the message limit."""
    import threading
    import time

    import app.router
    from app import jev

    def slow_score(*args):
        time.sleep(0.05)
        return providers.score(*args)

    monkeypatch.setattr(app.router, "MAX_MESSAGES", 3)
    monkeypatch.setattr(jev, "score", slow_score)
    statuses = []

    def chat():
        statuses.append(session.chat("When will my kurta arrive?", "ITM-1001").status_code)

    threads = [threading.Thread(target=chat) for _ in range(10)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(statuses) == [200] * 3 + [429] * 7
    assert session.store.customer_message_count() == 3


def test_orders_show_the_customer_and_every_item(session):
    response = session.get("/api/orders")

    assert response.status_code == 200
    body = response.json()
    assert body["customer"]["name"] == "Meera Iyer"
    assert body["today"] == "2026-11-21"
    items = {item["id"]: item for order in body["orders"] for item in order["items"]}
    assert {item_id: items[item_id]["status"] for item_id in STATUSES} == STATUSES
    assert all(ORDER_ITEM_KEYS <= set(item) for item in items.values())
    assert all({"id", "placed_at", "items"} <= set(order) for order in body["orders"])
    kurta = items["ITM-1001"]
    assert (kurta["order_id"], kurta["price_inr"], kurta["eta"]) == ("ORD-1001", 1499, "2026-11-24")
    assert [item["id"] for order in body["orders"] if order["id"] == "ORD-1001" for item in order["items"]] == [
        "ITM-1001"
    ]


def test_index_page_is_served(request):
    if not INDEX.exists():
        pytest.skip("app/web/index.html is not written yet")
    client = request.getfixturevalue("client")

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", ["/", "/app.js", "/style.css"])
def test_static_files_are_revalidated_on_every_load(client, path):
    """no-cache: the browser asks before it reuses its copy, so a redeploy is not masked by a stale file."""
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    unchanged = client.get(path, headers={"If-None-Match": response.headers["etag"]})
    assert unchanged.status_code == 304
    assert unchanged.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_framework_documentation_routes_are_off(client, path):
    assert client.get(path).status_code == 404
