"""Cancellations and returns: offered in chat, committed once on confirmation (routing contract step 6).

Offers are made with the quick actions and accepted by the fake Jev from conftest.py.
"""
import json
import re

import pytest

KURTA, SHIRT, JACKET = "ITM-1001", "ITM-1002", "ITM-1003"

MESSAGES = {
    "cancel": "I want to cancel this item",
    "return": "I want to return this item",
    "refund": "Where is my refund?",
}

# ponytail: a regex reading of "claims the refund was credited", the same reading as the backend
# status check: a positive past or perfect claim, ignored when the sentence negates or conditions
# it first. It misses unusual phrasings; widen CLAIM if the rule wording ever needs it.
CLAIM = re.compile(r"\b(?:has|have|had|was|were)\s+(?:already\s+)?(?:been\s+)?(?:credited|refunded)\b", re.I)
HEDGE = re.compile(r"\b(?:not|never|no longer|once|after|when|if|until|before|unless|as soon as)\b|n't", re.I)


def claims_credit(reply: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+", reply):
        claim = CLAIM.search(sentence)
        if claim and not HEDGE.search(sentence[: claim.start()]):
            return True
    return False


def ask(session, action: str, item_id: str) -> dict:
    """Use a quick action on one item and return the chat payload."""
    response = session.chat(MESSAGES[action], item_id, action)
    assert response.status_code == 200, response.text
    return response.json()


def offer(session, action: str, item_id: str) -> str:
    """Ask for an action and return the token of the pending action that is offered."""
    pending = ask(session, action, item_id)["pending_action"]
    assert (pending["action"], pending["item_id"]) == (action, item_id)
    assert pending["token"] and pending["label"]
    return pending["token"]


def test_cancellation_is_committed_on_confirmation(session, providers):
    token = offer(session, "cancel", SHIRT)

    response = session.confirm(token)

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["action"], body["item_id"]) == ("done", "cancel", SHIRT)
    # Today is Saturday 2026-11-21, so 7 business days later is Tuesday 2026-12-01.
    assert "₹1,299" in body["reply"]
    assert "December 1" in body["reply"]
    assert session.items()[SHIRT]["status"] == "cancelled"
    refunds = session.sql("SELECT * FROM refunds WHERE item_id = ?", SHIRT)
    assert len(refunds) == 1
    refund = refunds[0]
    assert (refund["amount_inr"], refund["status"], refund["credited_at"]) == (1299, "initiated", None)
    assert (refund["initiated_at"], refund["expected_by"]) == ("2026-11-21", "2026-12-01")
    paid_with = session.sql(
        "SELECT payment_method FROM orders WHERE id = (SELECT order_id FROM items WHERE id = ?)", SHIRT
    )
    assert refund["method"] == paid_with[0]["payment_method"]


def test_repeated_confirmation_returns_the_original_result(session, providers):
    token = offer(session, "cancel", SHIRT)
    first = session.confirm(token)

    again = session.confirm(token)

    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(session.sql("SELECT * FROM refunds WHERE item_id = ?", SHIRT)) == 1
    stored = session.sql("SELECT outcome FROM actions WHERE token = ?", token)
    assert json.loads(stored[0]["outcome"]) == first.json()


def test_item_shipped_after_the_offer_is_not_cancelled(session, providers):
    """Eligibility is rechecked against current state when the customer confirms."""
    token = offer(session, "cancel", SHIRT)
    session.sql("UPDATE items SET status = 'shipped', shipped_at = '2026-11-21' WHERE id = ?", SHIRT)

    response = session.confirm(token)

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["action"], body["item_id"]) == ("rejected", "cancel", SHIRT)
    assert "shipped" in body["reply"].lower()
    assert session.items()[SHIRT]["status"] == "shipped"
    assert session.sql("SELECT * FROM refunds WHERE item_id = ?", SHIRT) == []
    assert session.confirm(token).json() == body


def test_return_is_committed_on_confirmation(session, providers):
    token = offer(session, "return", JACKET)

    response = session.confirm(token)

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["action"], body["item_id"]) == ("done", "return", JACKET)
    assert "November 23" in body["reply"]
    assert session.items()[JACKET]["status"] == "return_requested"
    returns = session.sql("SELECT status, requested_at, pickup_on FROM returns WHERE item_id = ?", JACKET)
    assert [tuple(row) for row in returns] == [("requested", "2026-11-21", "2026-11-23")]


def test_return_window_boundaries(session, providers):
    """The window is inclusive: delivery date plus 7 days is the last eligible day."""
    last_day = ask(session, "return", "ITM-1004")
    assert last_day["pending_action"]["action"] == "return"
    assert "November 21" in last_day["reply"]

    one_day_late = ask(session, "return", "ITM-1005")
    assert "pending_action" not in one_day_late
    assert "November 20" in one_day_late["reply"]


@pytest.mark.parametrize(
    "action, item_id",
    [("cancel", KURTA), ("return", "ITM-1006"), ("return", "ITM-1012"), ("return", "ITM-1011"), ("cancel", "ITM-1015")],
    ids=["cancel_after_shipment", "return_already_requested", "return_of_a_final_sale_item",
         "return_of_a_cash_on_delivery_item", "cancel_of_a_cancelled_item"],
)
def test_action_that_is_not_permitted_is_not_offered(session, providers, action, item_id):
    payload = ask(session, action, item_id)

    assert "pending_action" not in payload
    assert session.sql("SELECT * FROM actions") == []


def test_return_of_a_cash_on_delivery_item_goes_to_the_support_team(session, providers):
    """Its refund is paid by bank transfer, so the policy has a person arrange it: a ticket, never an offer."""
    payload = ask(session, "return", "ITM-1011")

    assert payload["ticket_ref"] == "TKT-0001"
    assert [row["kind"] for row in session.sql("SELECT kind FROM tickets")] == ["cod_return"]
    assert session.items()["ITM-1011"]["status"] == "delivered"
    assert providers.score_calls == []


def test_action_is_offered_inside_a_reply_that_answers_several_parts(session, providers):
    """One reply, one offer: the question about the delivery is answered and the cancellation still needs a confirmation."""
    response = session.chat("When will it arrive, and can I cancel it?", SHIRT)

    payload = response.json()
    assert "expected on November 26" in payload["reply"] and "please confirm to cancel it" in payload["reply"]
    pending = payload["pending_action"]
    assert (pending["action"], pending["item_id"], pending["label"]) == ("cancel", SHIRT, "Cancel Mustard linen shirt")
    assert session.items()[SHIRT]["status"] == "packed"
    assert session.confirm(pending["token"]).json()["status"] == "done"


@pytest.mark.parametrize("item_id", [SHIRT, JACKET], ids=["cancel_is_permitted", "return_is_permitted"])
def test_two_actions_asked_for_at_once_offer_neither(session, providers, item_id):
    """At most one action per reply: asked to cancel and to return, the reply offers nothing and asks which one."""
    response = session.chat("I want to cancel it or return it", item_id)

    payload = response.json()
    assert "pending_action" not in payload
    assert payload["reply"].endswith("Which one would you like to go ahead with: a cancellation or a return?")
    assert session.sql("SELECT * FROM actions") == []
    assert session.last_trace()["candidate"]["action"] is None


def test_refund_in_progress_is_not_reported_as_credited(session, providers):
    reply = ask(session, "refund", "ITM-1007")["reply"]

    assert "₹2,199" in reply
    assert "November 18" in reply
    assert "November 27" in reply
    assert not claims_credit(reply)


def test_confirmation_is_bound_to_the_session(session, new_session, providers):
    unknown = session.confirm("no-such-token")
    assert unknown.status_code == 404
    assert unknown.json() == {"detail": "unknown_action"}

    token = offer(session, "cancel", SHIRT)
    other = new_session()
    elsewhere = other.confirm(token)

    assert elsewhere.status_code == 404
    assert elsewhere.json() == {"detail": "unknown_action"}
    assert session.items()[SHIRT]["status"] == "packed"
    assert other.items()[SHIRT]["status"] == "packed"
    assert session.confirm(token).json()["status"] == "done"


def test_offer_changes_nothing_before_confirmation(session, providers):
    token = offer(session, "cancel", SHIRT)

    assert session.items()[SHIRT]["status"] == "packed"
    assert session.sql("SELECT * FROM refunds WHERE item_id = ?", SHIRT) == []
    pending = session.sql("SELECT item_id, action, outcome FROM actions WHERE token = ?", token)
    assert [tuple(row) for row in pending] == [(SHIRT, "cancel", None)]


def test_permitted_action_proposed_by_the_fallback_can_be_confirmed(session, providers):
    """Step 5: a fallback action that is permitted and verified becomes a pending action like any other."""
    providers.draft_result.update(text="I can cancel your shirt for you. Please confirm.", action="cancel")

    response = session.chat("I have changed my plans for this one", SHIRT)  # no help topic: the fallback answers

    assert response.status_code == 200
    pending = response.json()["pending_action"]
    assert (pending["action"], pending["item_id"]) == ("cancel", SHIRT)
    assert session.items()[SHIRT]["status"] == "packed"
    assert session.confirm(pending["token"]).json()["status"] == "done"
    assert session.items()[SHIRT]["status"] == "cancelled"
