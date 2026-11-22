"""POST /api/chat routing, asserted against the routing contract.

Each docstring names the contract step the test enforces. Jev and the fallback
model are the recording fakes from conftest.py; app.fallback.problems is the
real backend validator, so the fallback texts here are checked against the fixtures.
"""
import json
import re
import sqlite3
from datetime import datetime

import pytest

KURTA, SHIRT, JACKET = "ITM-1001", "ITM-1002", "ITM-1003"
HOODIE, SWEATER = "ITM-1013", "ITM-1011"  # late in transit; cash on delivery, inside its return window

TRACK = "When will my kurta arrive?"
TRACK_AND_CANCEL = "When will it arrive, and can I still cancel it?"
NOT_RECEIVED = "I never received it"
OUT_OF_SCOPE = "Do you sell gift cards?"  # no help topic: the rules have no draft
# The one reply the spec fixes word for word.
KURTA_DRAFT = "Your kurta has shipped. It is expected on November 24."
# The rule draft for TRACK_AND_CANCEL: the tracking sentences, then why it cannot be cancelled and what can be done.
BOTH_DRAFT = (
    f"{KURTA_DRAFT} It shipped on November 20, so it can no longer be cancelled. "
    "Once it is delivered, it can be returned within 7 days of the delivery date."
)
# A fallback text for the same message, for the tests in which Jev rejects the rule draft.
BOTH_PARTS = (
    "Your kurta has shipped and is expected on November 24. "
    "Because it has shipped, it can no longer be cancelled."
)

GOOD = {"supported": 0.95, "complete": 0.95, "requires_human": 0.02}
INCOMPLETE = {**GOOD, "complete": 0.2}

OPTIONAL_PAYLOAD_KEYS = {"item_choices", "pending_action", "ticket_ref", "retry", "limit_reached"}
TRACE_KEYS = {
    "at",
    "message",
    "item_id",
    "intent",
    "route",
    "reason",
    "candidate",
    "scores",
    "decision",
    "fallback",
    "fallback_scores",
    "fallback_decision",
    "error",
    "ticket_ref",
    "calls",
    "elapsed_ms",
    "cost_usd",
    "reply",
}


def send(
    session, providers, message: str, item_id: str | None = None, quick_action: str | None = None
) -> tuple[dict, dict]:
    """Send one chat message and return (payload, trace).

    Every scenario that goes through here also proves the provider budget:
    at most two Jev calls and one fallback call per message.
    """
    jev_before, fallback_before = len(providers.score_calls), len(providers.draft_calls)
    response = session.chat(message, item_id, quick_action)
    assert response.status_code == 200, response.text
    payload, trace = response.json(), session.last_trace()
    assert len(providers.score_calls) - jev_before <= 2
    assert len(providers.draft_calls) - fallback_before <= 1
    assert trace["calls"]["jev"] <= 2
    assert trace["calls"]["fallback"] <= 1
    return payload, trace


def stored_ticket(session, payload: dict, trace: dict, route: str) -> sqlite3.Row:
    """Check a ticket outcome and return its row: a reference is shown only for a ticket that was stored."""
    ref = payload["ticket_ref"]
    assert re.fullmatch(r"TKT-\d{4}", ref)
    assert ref in payload["reply"]
    assert "pending_action" not in payload
    assert trace["route"] == route
    assert trace["ticket_ref"] == ref
    rows = session.sql("SELECT * FROM tickets WHERE ref = ?", ref)
    assert len(rows) == 1
    return rows[0]


def test_supported_and_complete_draft_is_sent(session, providers):
    """Step 4, the first demo: the tracking draft is accepted and sent as written."""
    payload, trace = send(session, providers, TRACK, KURTA)

    assert payload["reply"] == KURTA_DRAFT
    assert payload["item_id"] == KURTA
    assert not OPTIONAL_PAYLOAD_KEYS & set(payload)
    assert providers.draft_calls == []
    assert len(providers.score_calls) == 1
    call = providers.score_calls[0]
    assert call["message"] == TRACK
    assert call["reply"] == KURTA_DRAFT
    assert call["history"] == []
    assert call["item"]["id"] == KURTA
    assert call["policy"]["version"] == "2026-11-22"
    assert call["today"] == "2026-11-21"
    assert trace["route"] == "rule"
    assert trace["intent"] == "track_eta"
    assert trace["item_id"] == KURTA
    assert trace["candidate"]["draft"] == KURTA_DRAFT
    assert trace["scores"] == pytest.approx(GOOD)
    assert trace["decision"] == "accept"
    assert trace["fallback"] is None
    assert trace["ticket_ref"] is None
    assert trace["error"] is None
    assert trace["calls"] == {"jev": 1, "fallback": 0}


def test_message_with_two_parts_gets_one_rule_draft_that_answers_both(session, providers):
    """Steps 3 and 4, the second demo as the owner now wants it: the rules combine the tracking answer with why the
    kurta can no longer be cancelled, Jev accepts the one draft, and the fallback is not needed."""
    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    assert payload["reply"] == BOTH_DRAFT
    assert not OPTIONAL_PAYLOAD_KEYS & set(payload)
    assert providers.draft_calls == []
    assert [call["reply"] for call in providers.score_calls] == [BOTH_DRAFT]
    assert trace["route"] == "rule"
    assert trace["intent"] == "track_eta, cancel"
    assert trace["candidate"] == {
        "draft": BOTH_DRAFT,
        "evidence": ["item.status", "item.eta", "item.shipped_at", "policy.cancellation", "policy.returns"],
        "action": None,
    }
    assert trace["decision"] == "accept"
    assert trace["calls"] == {"jev": 1, "fallback": 0}


def test_incomplete_draft_goes_to_the_fallback(session, providers):
    """Steps 4 and 5: a draft Jev scores as supported but incomplete is not sent; the fallback gets its one attempt."""
    providers.scores = [INCOMPLETE, GOOD]
    providers.draft_result["text"] = BOTH_PARTS

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    assert len(providers.draft_calls) == 1
    call = providers.draft_calls[0]
    assert call["message"] == TRACK_AND_CANCEL
    assert call["item"]["id"] == KURTA
    assert call["today"] == "2026-11-21"
    assert call["rejected"]["draft"] == BOTH_DRAFT
    assert {name: call["rejected"]["scores"][name] for name in GOOD} == pytest.approx(INCOMPLETE)
    assert [c["reply"] for c in providers.score_calls] == [BOTH_DRAFT, BOTH_PARTS]
    assert payload["reply"] == BOTH_PARTS
    assert not OPTIONAL_PAYLOAD_KEYS & set(payload)
    assert trace["route"] == "fallback"
    assert trace["decision"] == "reject"
    assert trace["fallback"]["problems"] == []
    assert trace["fallback_decision"] == "accept"
    assert trace["calls"] == {"jev": 2, "fallback": 1}


def test_tracking_reply_follows_the_current_record(session, providers):
    """Drafts are built from current rows, never from a cached fixture."""
    session.sql("UPDATE items SET eta = '2026-11-27' WHERE id = ?", KURTA)

    payload, trace = send(session, providers, TRACK, KURTA)

    assert payload["reply"] == "Your kurta has shipped. It is expected on November 27."
    assert providers.score_calls[0]["reply"] == payload["reply"]
    assert trace["route"] == "rule"


def test_tracking_question_about_a_late_item_is_a_mandated_ticket(session, providers):
    """Step 2: once the estimated date has passed, a question about the delivery goes to a person, not to a draft
    that would promise a past date. On the estimated date itself the item is not late yet."""
    session.sql("UPDATE items SET eta = ? WHERE id = ?", session.store.today, KURTA)
    on_the_day, _ = send(session, providers, TRACK, KURTA)
    session.sql("UPDATE items SET eta = '2026-11-19' WHERE id = ?", KURTA)

    payload, trace = send(session, providers, TRACK, KURTA)

    assert on_the_day["reply"] == "Your kurta has shipped. It is expected on November 21."
    row = stored_ticket(session, payload, trace, "mandated_ticket")
    assert (row["kind"], row["item_id"]) == ("late_delivery", KURTA)
    assert "November 19" not in payload["reply"]
    assert len(providers.score_calls) == 1 and providers.draft_calls == []  # the one call scored the first reply


def test_unknown_item_id_asks_which_item(session, providers):
    """Step 1: an item id outside this session is not an error; the customer is asked to choose."""
    from app import rules

    payload, trace = send(session, providers, "Where is my order?", "ITM-9999")

    assert payload["reply"] == rules.UNKNOWN_ITEM
    assert sorted(choice["id"] for choice in payload["item_choices"]) == sorted(session.items())
    assert all({"id", "order_id", "name", "noun", "status"} <= set(choice) for choice in payload["item_choices"])
    assert "ticket_ref" not in payload
    assert providers.score_calls == [] and providers.draft_calls == []
    assert trace["route"] == "clarify_item"


def test_two_kurtas_need_a_choice(session, providers):
    """Step 1: several items fit and the choice affects the answer, so the customer selects one."""
    from app import rules

    payload, trace = send(session, providers, TRACK)

    assert sorted(choice["id"] for choice in payload["item_choices"]) == ["ITM-1001", "ITM-1009"]
    assert payload["reply"] == rules.CHOOSE_ITEM
    assert payload["item_id"] is None
    assert providers.score_calls == [] and providers.draft_calls == []
    assert trace["route"] == "clarify_item"
    assert trace["intent"] == "track_eta"


def test_item_named_in_the_message_is_resolved(session, providers):
    """Step 1: exactly one item matches the message, so no clarification is needed."""
    payload, trace = send(session, providers, "When will my jacket arrive?")

    assert payload["item_id"] == JACKET
    assert "item_choices" not in payload
    assert trace["route"] != "clarify_item"
    assert trace["item_id"] == JACKET
    assert providers.score_calls[0]["item"]["id"] == JACKET


def test_full_item_name_in_the_message_resolves_the_item(session, providers):
    """Step 1: "my blue kurta" is one item, although "my kurta" alone fits two."""
    payload, trace = send(session, providers, "Where is my blue kurta?")

    assert payload["item_id"] == KURTA
    assert "item_choices" not in payload
    assert payload["reply"] == KURTA_DRAFT
    assert trace["route"] == "rule"
    assert providers.score_calls[0]["item"]["id"] == KURTA


def test_general_policy_question_needs_no_order(session, providers):
    """Steps 1 and 3: a general policy question proceeds without an order, and the rules answer it from the policy."""
    payload, trace = send(session, providers, "What is your return policy?")

    answer = session.store.policy["returns"]["text"]
    assert "item_choices" not in payload
    assert payload["item_id"] is None
    assert payload["reply"] == answer
    assert providers.draft_calls == []
    assert [(call["item"], call["reply"]) for call in providers.score_calls] == [(None, answer)]
    assert trace["route"] == "rule"
    assert trace["intent"] == "return_policy"
    assert trace["item_id"] is None
    assert trace["candidate"] == {"draft": answer, "evidence": ["policy.returns"], "action": None}


def test_general_question_is_answered_from_the_policy_even_when_an_item_is_selected(session, providers):
    payload, trace = send(session, providers, "Is delivery free and do you have cash on delivery?", KURTA)

    policy = session.store.policy
    assert payload["reply"] == f"{policy['shipping_charges']['text']} {policy['cash_on_delivery']['text']}"
    assert payload["item_id"] == KURTA
    assert trace["route"] == "rule"
    assert trace["intent"] == "shipping_charges, cash_on_delivery"
    assert trace["candidate"]["evidence"] == ["policy.shipping_charges", "policy.cash_on_delivery"]


def test_question_the_rules_do_not_know_goes_to_the_fallback_without_an_order(session, providers):
    """Steps 1, 4 and 5: nothing recognised and no item named, so it proceeds without an order to the fallback."""
    answer = "I can help with your orders and our store policy."
    providers.draft_result["text"] = answer

    payload, trace = send(session, providers, OUT_OF_SCOPE)

    assert "item_choices" not in payload
    assert payload["item_id"] is None
    assert payload["reply"] == answer
    assert len(providers.draft_calls) == 1
    assert providers.draft_calls[0]["item"] is None
    assert providers.draft_calls[0]["rejected"] is None
    assert [(call["item"], call["reply"]) for call in providers.score_calls] == [(None, answer)]
    assert trace["route"] == "fallback"
    assert trace["intent"] is None
    assert trace["candidate"] is None
    assert trace["scores"] is None


@pytest.mark.parametrize(
    "message, section",
    [("Can I cancel after it has shipped?", "cancellation"), ("How do I return something?", "returns"),
     ("How long do refunds take?", "refunds")],
)
def test_order_question_about_no_order_in_particular_gets_the_general_answer(session, providers, message, section):
    """Step 1: with nothing selected and no order pointed at, the choice of item does not affect the answer."""
    payload, trace = send(session, providers, message)

    assert "item_choices" not in payload
    assert payload["reply"] == session.store.policy[section]["text"]
    assert trace["route"] == "rule"
    assert trace["candidate"]["evidence"] == [f"policy.{section}"]


@pytest.mark.parametrize(
    "message", ["I want to cancel my order", "Where is my order?", "I never received it", "It arrived damaged"]
)
def test_order_question_about_one_order_asks_which_item(session, providers, message):
    """Step 1: the answer depends on the item, none is selected or named, so the customer chooses from all of them."""
    from app import rules

    payload, trace = send(session, providers, message)

    assert payload["reply"] == rules.CHOOSE_ITEM
    assert sorted(choice["id"] for choice in payload["item_choices"]) == sorted(session.items())
    assert payload["item_id"] is None and "ticket_ref" not in payload
    assert providers.score_calls == [] and providers.draft_calls == []
    assert trace["route"] == "clarify_item"


@pytest.mark.parametrize(
    "message, reply",
    [("hello", "Hello! How can I help you today?"),
     ("Thanks a lot!", "You're welcome! Is there anything else I can help you with?")],
)
def test_small_talk_gets_the_fixed_reply_once_jev_accepts_it(session, providers, message, reply):
    """Small talk is a rule draft like any other: it is sent only after Jev scored it."""
    payload, trace = send(session, providers, message, KURTA)

    assert payload["reply"] == reply
    assert [call["reply"] for call in providers.score_calls] == [reply]
    assert providers.draft_calls == []
    assert trace["route"] == "rule"
    assert trace["candidate"] == {"draft": reply, "evidence": [], "action": None}


def test_jev_failure_on_the_draft_releases_nothing(session, providers):
    """Step 5: a failed Jev call is never a "no" and never an accept; the fixed handoff reply is sent."""
    from app import rules

    providers.scores = [providers.JevError("timeout")]

    payload, trace = send(session, providers, TRACK, KURTA)

    stored_ticket(session, payload, trace, "gate_error")
    assert payload["reply"] == rules.ticket_reply(payload["ticket_ref"], verified=False)
    assert KURTA_DRAFT not in payload["reply"]
    assert len(providers.score_calls) == 1
    assert providers.draft_calls == []
    assert trace["error"]


@pytest.mark.parametrize("scored_before", [[], [INCOMPLETE]], ids=["first_score", "rescoring"])
def test_unexpected_error_from_jev_fails_closed_like_a_jev_error(session, providers, caplog, scored_before):
    """Fail closed: an exception that is not JevError ends in the same ticket, is logged, and is never a 500."""
    from app import rules

    providers.scores = [*scored_before, RuntimeError("boom")]

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    stored_ticket(session, payload, trace, "gate_error")
    assert payload["reply"] == rules.ticket_reply(payload["ticket_ref"], verified=False)
    assert trace["error"] == "jev: unexpected RuntimeError"
    assert len(providers.score_calls) == len(scored_before) + 1
    assert len(providers.draft_calls) == len(scored_before)
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("broken", ["draft", "problems"])
def test_unexpected_error_from_the_fallback_fails_closed_like_a_fallback_error(
    session, providers, monkeypatch, caplog, broken
):
    """Fail closed: a fallback step that raises anything ends in the handoff ticket, and nothing is scored or sent."""
    from app import fallback

    def boom(*args):
        raise RuntimeError("boom")

    providers.scores = [INCOMPLETE]
    if broken == "draft":
        providers.draft_result = RuntimeError("boom")
    else:
        monkeypatch.setattr(fallback, "problems", boom)

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    stored_ticket(session, payload, trace, "handoff")
    assert len(providers.draft_calls) == 1
    assert len(providers.score_calls) == 1
    assert "RuntimeError" in (trace["error"] if broken == "draft" else trace["fallback"]["problems"][0])
    assert any(record.exc_info for record in caplog.records)
    assert session.store.customer_message_count() == 1


def test_required_handoff_wins_over_a_good_draft(session, providers):
    """Step 4: required handoff is prioritised, even when the draft is supported and complete."""
    providers.scores = [{**GOOD, "requires_human": 0.9}]

    payload, trace = send(session, providers, TRACK, KURTA)

    stored_ticket(session, payload, trace, "handoff")
    assert KURTA_DRAFT not in payload["reply"]
    assert len(providers.score_calls) == 1
    assert providers.draft_calls == []
    assert trace["decision"] == "handoff"


@pytest.mark.parametrize(
    "first_scores",
    [{**GOOD, "requires_human": 0.5}, {**GOOD, "supported": 0.3}],
    ids=["escalation_risk_between_cutoffs", "unsupported"],
)
def test_rejected_draft_goes_to_the_fallback(session, providers, first_scores):
    """Step 4: a draft is accepted only when it is supported and escalation risk is low."""
    providers.scores = [first_scores, GOOD]

    payload, trace = send(session, providers, TRACK, KURTA)

    assert trace["decision"] == "reject"
    assert len(providers.draft_calls) == 1
    assert providers.draft_calls[0]["rejected"]["draft"] == KURTA_DRAFT
    assert payload["reply"] == providers.draft_result["text"]
    assert trace["route"] == "fallback"


def test_fallback_error_becomes_a_ticket(session, providers):
    """Step 5: the fallback gets one attempt; when it fails, a ticket is opened."""
    providers.scores = [INCOMPLETE]
    providers.draft_result = providers.FallbackError("timeout")

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    stored_ticket(session, payload, trace, "handoff")
    assert KURTA_DRAFT not in payload["reply"]
    assert len(providers.draft_calls) == 1
    assert len(providers.score_calls) == 1
    assert trace["error"]


def test_fallback_handoff_becomes_a_ticket_without_rescoring(session, providers):
    """Step 5: when the fallback itself asks for a person there is nothing to score."""
    providers.scores = [INCOMPLETE]
    providers.draft_result.update(kind="handoff", text="This needs a person to review.")

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    stored_ticket(session, payload, trace, "handoff")
    assert providers.draft_result["text"] not in payload["reply"]
    assert len(providers.score_calls) == 1


@pytest.mark.parametrize(
    "result",
    [
        {"text": "I can cancel your kurta for you. Please confirm.", "action": "cancel"},
        {"text": "Your kurta has shipped and will arrive by November 30."},
        {"text": "Your kurta has shipped. Its price was ₹9,999."},
        {"text": "Your kurta has been delivered."},
        {"text": "Your kurta has ſhipped. It is expected on November 24."},
        {"text": "Your kurta has shipped. It is expected on Wednesday, November 24."},
    ],
    ids=[
        "action_not_permitted", "date_not_in_record", "amount_not_in_record", "status_not_in_record",
        "status_in_a_look_alike_letter", "weekday",
    ],
)
def test_fallback_reply_that_fails_backend_validation_is_not_sent(session, providers, result):
    """Step 5: actions, dates, amounts and statuses are checked against the backend before Jev sees the reply."""
    providers.scores = [INCOMPLETE]
    providers.draft_result.update(result)

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    stored_ticket(session, payload, trace, "handoff")
    assert result["text"] not in payload["reply"]
    assert trace["fallback"]["problems"]
    assert len(providers.score_calls) == 1


@pytest.mark.parametrize(
    "second_scores, decision",
    [({**GOOD, "supported": 0.2}, "reject"), (INCOMPLETE, "reject"), ({**GOOD, "requires_human": 0.9}, "handoff")],
    ids=["unsupported", "incomplete", "needs_a_person"],
)
def test_fallback_reply_that_fails_rescoring_is_not_sent(session, providers, second_scores, decision):
    """Step 5: the new reply is scored once; if it still fails, a ticket is opened."""
    providers.scores = [INCOMPLETE, second_scores]

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    stored_ticket(session, payload, trace, "handoff")
    assert providers.draft_result["text"] not in payload["reply"]
    assert len(providers.draft_calls) == 1
    assert len(providers.score_calls) == 2
    assert trace["fallback_decision"] == decision


@pytest.mark.parametrize(
    "supported, complete, requires_human, expected",
    [(0.7, 0.7, 0.29, "accept"), (0.7, 0.7, 0.3, "reject"), (0.7, 0.7, 0.7, "handoff")],
)
def test_decide_cutoffs_are_inclusive_except_low_risk(supported, complete, requires_human, expected):
    """Step 4: support and completeness count as met at their cutoffs; escalation risk must be below the low one."""
    from app import router

    cutoffs = {"supported_min": 0.7, "complete_min": 0.7, "requires_human_handoff": 0.7, "requires_human_low": 0.3}
    scores = {"supported": supported, "complete": complete, "requires_human": requires_human}

    assert router.decide(scores, cutoffs) == expected


def test_clarifying_question_need_not_be_complete(session, providers):
    """Step 5: one specific question may be sent when it is supported, even though it answers nothing yet."""
    question = "Could you tell me what you would like to change about this order?"
    providers.scores = [INCOMPLETE, {**GOOD, "complete": 0.1}]
    providers.draft_result.update(kind="clarify", text=question)

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    assert payload["reply"] == question
    assert "ticket_ref" not in payload
    assert trace["route"] == "fallback_clarify"
    assert trace["fallback_decision"] == "accept"


def test_statement_labelled_clarify_must_be_complete(session, providers):
    """Step 5: only a question is excused from completeness; the fallback's own label does not excuse a statement."""
    providers.scores = [INCOMPLETE, {**GOOD, "complete": 0.1}]
    providers.draft_result.update(kind="clarify", text="Your kurta has shipped and is expected on November 24.")

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    stored_ticket(session, payload, trace, "handoff")
    assert providers.draft_result["text"] not in payload["reply"]
    assert trace["fallback_decision"] == "reject"


def test_complete_statement_labelled_clarify_is_sent_as_a_reply(session, providers):
    """Step 5: a statement labelled clarify is treated as a reply, so it is sent once it meets every cutoff."""
    providers.scores = [INCOMPLETE, GOOD]
    providers.draft_result.update(kind="clarify", text=BOTH_PARTS)

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    assert payload["reply"] == BOTH_PARTS
    assert trace["route"] == "fallback"
    assert trace["fallback_decision"] == "accept"


def test_jev_failure_on_rescoring_releases_nothing(session, providers):
    """Step 5: an unscored fallback reply is unverified, so it is not sent."""
    from app import rules

    providers.scores = [INCOMPLETE, providers.JevError("malformed scores")]

    payload, trace = send(session, providers, TRACK_AND_CANCEL, KURTA)

    stored_ticket(session, payload, trace, "gate_error")
    assert payload["reply"] == rules.ticket_reply(payload["ticket_ref"], verified=False)
    assert providers.draft_result["text"] not in payload["reply"]
    assert KURTA_DRAFT not in payload["reply"]
    assert len(providers.score_calls) == 2


def test_message_without_intent_goes_straight_to_the_fallback(session, providers):
    """Step 4: with no candidate there is nothing to score first; Jev only scores the fallback reply."""
    payload, trace = send(session, providers, OUT_OF_SCOPE, KURTA)

    assert len(providers.draft_calls) == 1
    assert providers.draft_calls[0]["rejected"] is None
    assert [call["reply"] for call in providers.score_calls] == [providers.draft_result["text"]]
    assert payload["reply"] == providers.draft_result["text"]
    assert trace["route"] == "fallback"
    assert trace["intent"] is None
    assert trace["candidate"] is None


def test_disputed_delivery_scan_is_a_mandated_ticket(session, providers):
    """Step 2: the backend rule mandates a ticket; no model is consulted."""
    from app import rules

    payload, trace = send(session, providers, NOT_RECEIVED, JACKET)

    row = stored_ticket(session, payload, trace, "mandated_ticket")
    assert payload["ticket_ref"] == "TKT-0001"
    assert payload["reply"] == rules.ticket_reply("TKT-0001")
    assert (row["kind"], row["item_id"]) == ("delivery_scan_disputed", JACKET)
    assert providers.score_calls == [] and providers.draft_calls == []


def test_overdue_refund_is_a_mandated_ticket(session, providers):
    """Step 2: a refund past its expected credit date is escalated by the backend."""
    payload, trace = send(session, providers, "Where is my refund?", "ITM-1008", "refund")

    row = stored_ticket(session, payload, trace, "mandated_ticket")
    assert (row["kind"], row["item_id"]) == ("refund_overdue", "ITM-1008")
    assert providers.score_calls == [] and providers.draft_calls == []


@pytest.mark.parametrize(
    "message, item_id, rule",
    [
        ("Where is my jacket? It says delivered but I never received it", JACKET, "delivery_scan_disputed"),
        ("The tracking says delivered but I didn't get it", JACKET, "delivery_scan_disputed"),
        ("Status shows delivered but I have not received it", JACKET, "delivery_scan_disputed"),
        ("I returned the sneakers weeks ago, where is my refund?", "ITM-1008", "refund_overdue"),
    ],
)
def test_backend_rule_applies_when_its_request_is_not_the_first_in_the_message(
    session, providers, message, item_id, rule
):
    """Step 2: the rule mandates its ticket even when the message opens with a tracking or return phrase."""
    payload, trace = send(session, providers, message, item_id)

    row = stored_ticket(session, payload, trace, "mandated_ticket")
    assert (row["kind"], row["item_id"]) == (rule, item_id)
    assert providers.score_calls == [] and providers.draft_calls == []


@pytest.mark.parametrize(
    "message, item_id, rule",
    [
        ("Where is my hoodie?", None, "late_delivery"),  # the item is named, not selected
        ("It still hasn't arrived", HOODIE, "late_delivery"),
        ("Can you deliver it faster?", HOODIE, "late_delivery"),
        ("Can I cancel it? And what is the tracking number?", HOODIE, "late_delivery"),
        ("It arrived torn", JACKET, "damaged_or_wrong_item"),
        ("You sent the wrong item, I want a refund", KURTA, "damaged_or_wrong_item"),
        ("Please change my delivery address", SHIRT, "address_change"),
        ("I want to return it", SWEATER, "cod_return"),
        ("Can I talk to a person?", KURTA, "asks_for_person"),
        ("When will it arrive? Actually, just get me a human", KURTA, "asks_for_person"),
    ],
)
def test_backend_rules_of_the_new_policy_are_mandated_tickets(session, providers, message, item_id, rule):
    """Step 2: every backend rule the policy lists is enforced, on any topic of the message; no model is consulted."""
    payload, trace = send(session, providers, message, item_id)

    row = stored_ticket(session, payload, trace, "mandated_ticket")
    assert row["kind"] == rule
    assert row["item_id"] == trace["item_id"] == payload["item_id"]
    assert row["summary"].startswith(f"Backend rule {rule} on ") and row["item_id"] in row["summary"]  # which rule fired
    assert rule in trace["reason"]
    assert providers.score_calls == [] and providers.draft_calls == []


def test_request_for_a_person_is_a_ticket_even_when_no_item_is_meant(session, providers):
    """Step 2: asking for a person needs no order, so the customer is not asked to choose one first."""
    payload, trace = send(session, providers, "I want to speak to a human about my order")

    row = stored_ticket(session, payload, trace, "mandated_ticket")
    assert (row["kind"], row["item_id"], payload["item_id"]) == ("asks_for_person", None, None)
    assert row["summary"].startswith("Backend rule asks_for_person:")
    assert "item_choices" not in payload
    assert providers.score_calls == [] and providers.draft_calls == []


@pytest.mark.parametrize(
    "message, item_id",
    [
        ("Can I cancel it?", HOODIE),  # late, but not a question about its delivery
        ("Please change my delivery address", KURTA),  # shipped: it cannot be changed, which the rules can say
        ("I want to return it", JACKET),  # prepaid: offered for confirmation
        ("How do I pay for this?", SWEATER),
    ],
)
def test_backend_rules_do_not_fire_outside_their_conditions(session, providers, message, item_id):
    payload, trace = send(session, providers, message, item_id)

    assert trace["route"] == "rule"
    assert "ticket_ref" not in payload


def test_refund_question_about_a_delivered_item_is_not_a_delivery_dispute(session, providers):
    """Step 2: "haven't received my refund" asks about a refund; the missing phrase inside it mandates nothing."""
    payload, trace = send(session, providers, "I haven't received my refund", JACKET)

    assert trace["route"] == "rule"
    assert "ticket_ref" not in payload
    assert "no refund on record" in payload["reply"]


def test_refund_that_is_not_overdue_gets_a_rule_reply(session, providers):
    """The same question on a refund still inside its window is a routine reply."""
    payload, trace = send(session, providers, "Where is my refund?", "ITM-1007", "refund")

    assert trace["route"] == "rule"
    assert "ticket_ref" not in payload
    assert "₹2,199" in payload["reply"]
    assert "November 27" in payload["reply"]


def test_ticket_write_failure_offers_a_retry(session, providers, monkeypatch):
    """Step 5: no reference is shown for a ticket that was not stored; the customer can retry."""
    from app import rules, store

    def failing(self, *args, **kwargs):
        raise store.TicketError("write failed")

    with monkeypatch.context() as patch:
        patch.setattr(store.Store, "create_ticket", failing)
        payload, trace = send(session, providers, NOT_RECEIVED, JACKET)

    assert payload["retry"] is True
    assert payload["reply"] == rules.TICKET_FAILED
    assert "ticket_ref" not in payload
    assert trace["route"] == "ticket_failed"
    assert trace["ticket_ref"] is None
    assert session.sql("SELECT * FROM tickets") == []

    payload, trace = send(session, providers, NOT_RECEIVED, JACKET)

    stored_ticket(session, payload, trace, "mandated_ticket")
    assert "retry" not in payload


def test_ticket_write_failure_after_a_gate_error_releases_nothing(session, providers, monkeypatch):
    """Fail closed twice over: Jev is down and the ticket cannot be stored, so the draft still stays unsent."""
    from app import rules, store

    def failing(self, *args, **kwargs):
        raise store.TicketError("write failed")

    monkeypatch.setattr(store.Store, "create_ticket", failing)
    providers.scores = [providers.JevError("timeout")]

    payload, trace = send(session, providers, TRACK, KURTA)

    assert payload["retry"] is True
    assert payload["reply"] == rules.TICKET_FAILED
    assert "ticket_ref" not in payload
    assert trace["route"] == "ticket_failed"
    assert providers.draft_calls == []


@pytest.mark.parametrize(
    "message, quick_action",
    [("", None), (" \n\t ", None), ("a" * 501, None), ("hello", "dance")],
    ids=["empty", "whitespace_only", "501_characters", "unknown_quick_action"],
)
def test_invalid_chat_input_is_rejected(session, message, quick_action):
    assert session.chat(message, KURTA, quick_action).status_code == 422


@pytest.mark.parametrize(
    "body, content_type",
    [
        (b'{"message": NaN}', "application/json"),
        (b'{"message": "hi", "quick_action": Infinity}', "application/json"),
        (b'{"message": ["\\ud800"]}', "application/json"),
        (b'{"\\ud800": "x"}', "application/json"),
        (b'"\\ud800"', "application/json"),
        (b"\xff\xfe", "text/plain"),
    ],
    ids=["nan", "infinity", "nested_surrogate", "surrogate_key", "bare_surrogate", "text_that_is_not_utf8"],
)
def test_invalid_body_that_cannot_be_echoed_is_rejected(session, body, content_type):
    """The 422 does not copy the rejected input, which is not always something JSON can carry."""
    headers = {**session.headers, "Content-Type": content_type}

    response = session.client.post("/api/chat", content=body, headers=headers)

    assert response.status_code == 422
    assert all(set(error) == {"type", "loc", "msg"} for error in response.json()["detail"])


@pytest.mark.parametrize("with_session", [True, False], ids=["live_session", "no_session"])
def test_body_over_16_kb_is_refused_before_it_is_parsed(session, with_session):
    """Nothing bigger than 16 KB is read in: 413 comes before the 422 for bad JSON and the 401 for no session."""
    headers = {**(session.headers if with_session else {}), "Content-Type": "application/json"}

    response = session.client.post("/api/chat", content=b"x" * (16 * 1024 + 1), headers=headers)

    assert response.status_code == 413


def test_body_of_16_kb_is_still_read(session, providers):
    body = json.dumps({"message": "hello", "item_id": KURTA}).encode()
    headers = {**session.headers, "Content-Type": "application/json"}

    response = session.client.post("/api/chat", content=body.ljust(16 * 1024), headers=headers)

    assert response.status_code == 200


def test_item_id_is_capped_at_64_characters(session, providers):
    assert session.chat("hello", "x" * 64).status_code == 200  # unknown, so the customer is asked which item
    assert session.chat("hello", "x" * 65).status_code == 422


def test_message_is_stripped_before_its_length_is_checked(session, providers):
    assert session.chat("  " + "a" * 500 + "\n", KURTA, "track").status_code == 200


def test_message_limit_closes_the_session(session, providers, monkeypatch):
    """Step 0 of handle: past the limit nothing is stored and no model is called."""
    import app.router
    from app import rules

    monkeypatch.setattr(app.router, "MAX_MESSAGES", 2)
    for _ in range(2):
        assert session.chat(TRACK, KURTA).status_code == 200

    response = session.chat(TRACK, KURTA)

    assert response.status_code == 429
    assert response.json() == {"reply": rules.LIMIT_REACHED, "item_id": None, "limit_reached": True}
    assert session.store.customer_message_count() == 2
    assert len(providers.score_calls) == 2


def test_trace_endpoint_shows_the_gate_and_complete_traces(session, providers):
    from app import jev

    providers.scores = [GOOD, INCOMPLETE, GOOD]
    providers.draft_result["text"] = BOTH_PARTS
    sent = [(TRACK, KURTA), (TRACK_AND_CANCEL, KURTA), (TRACK, "ITM-9999"), (NOT_RECEIVED, JACKET)]
    replies = [send(session, providers, message, item_id)[0]["reply"] for message, item_id in sent]

    response = session.get("/api/trace")

    assert response.status_code == 200
    body = response.json()
    gate_keys = ("version", "status", "model", "policy_version", "thresholds")
    assert {key: body["gate"][key] for key in gate_keys} == {key: jev.GATE[key] for key in gate_keys}
    traces = body["traces"]
    assert [trace["route"] for trace in traces] == ["rule", "fallback", "clarify_item", "mandated_ticket"]
    for trace, (message, _), reply in zip(traces, sent, replies, strict=True):
        assert TRACE_KEYS <= set(trace)
        assert (trace["message"], trace["reply"]) == (message, reply)
        assert trace["reason"]
        datetime.fromisoformat(trace["at"])
        assert set(trace["calls"]) == {"jev", "fallback"}
        assert set(trace["elapsed_ms"]) == {"jev", "fallback", "total"}
    rule, fallback = traces[:2]
    assert set(rule["candidate"]) == {"draft", "evidence", "action"}
    assert set(rule["scores"]) == set(GOOD)
    assert set(fallback["fallback"]) == {"kind", "text", "action", "problems"}
    assert set(fallback["fallback_scores"]) == set(GOOD)


def test_history_is_kept_per_item(session, providers):
    """Jev judges completeness in context, and the context is this item's conversation only."""
    send(session, providers, TRACK, KURTA)
    send(session, providers, "Where is this one?", SHIRT, "track")
    send(session, providers, "Any update?", KURTA, "track")

    about_shirt, about_kurta_again = providers.score_calls[1:]
    assert about_shirt["item"]["id"] == SHIRT
    assert about_shirt["history"] == []
    assert about_kurta_again["history"] == [
        {"role": "customer", "text": TRACK},
        {"role": "agent", "text": KURTA_DRAFT},
    ]
