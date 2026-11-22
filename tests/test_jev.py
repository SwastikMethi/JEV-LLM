"""Offline tests for app/jev.py. Every request goes to an in-process httpx.MockTransport, never to the network."""
import json
import re
import types
from pathlib import Path

import httpx
import pytest

from app import jev

# Bound at import, so a stub that another test installs on the jev module cannot stand in for the real functions.
from app.jev import GATE, QUESTIONS, JevError, evidence, score

DATA = Path(__file__).resolve().parents[1] / "data"
TODAY = "2026-11-21"
MESSAGE = "When will my kurta arrive?"
REPLY = "Your kurta has shipped. It is expected on November 24."
HISTORY = [{"role": "customer", "text": "Hi"}, {"role": "agent", "text": "Hello, how can I help?"}]
STATE_FIELDS = {
    "customer_message", "conversation_history", "order_record", "permitted_actions", "store_policy", "today",
    "proposed_reply",
}
# The shapes of data/policy.json: a section is an object with a customer-facing "text" beside what the code reads.
SECTIONS = ("cancellation", "returns", "refunds", "shipping_charges", "support")
POLICY = {
    "version": "2026-11-22",
    "store": "Saanjh Clothing",
    "cancellation": {"topics": ["cancel", "cancellation_policy"], "cancellable_statuses": ["packed"],
                     "text": "An item can be cancelled until it ships."},
    "returns": {"topics": ["return", "return_policy"], "window_days": 7,
                "non_returnable": {"final_sale": "it was bought in a final sale"},
                "text": "A delivered item can be returned within 7 days of its delivery date."},
    "refunds": {"topics": ["refund_status", "refund_policy"], "business_days": 7,
                "text": "Refunds are credited within 7 business days of being initiated."},
    "shipping_charges": {"topics": ["shipping_charges", "payment"], "fee_inr": 79, "free_from_inr": 999,
                         "text": "Shipping costs ₹79 per order and is free on orders of ₹999 or more."},
    "support": {"topics": ["support_contact", "human"],
                "text": "Our support team works from 9 am to 9 pm, Monday to Saturday."},
    "escalation": [
        {"id": "refund_overdue", "enforced_by": "backend", "when": {"intent": ["refund_status"], "refund_overdue": True},
         "text": "A refund was initiated, its expected credit date has passed, and it has not been credited."},
        {"id": "abuse", "enforced_by": "gate", "text": "The customer is abusive or threatening towards the store."},
    ],
}
KURTA = {
    "id": "ITM-1001", "order_id": "ORD-1001", "name": "Blue kurta", "noun": "kurta", "variant": "Size M",
    "category": "Kurtas", "colour": "Blue", "size": "M", "price_inr": 1499, "non_returnable_reason": None,
    "status": "shipped", "placed_at": "2026-11-18", "payment_method": "UPI", "shipping_fee_inr": 0, "cod_fee_inr": 0,
    "order_total_inr": 1499, "delivery_city": "Bengaluru", "delivery_pincode": "560034", "shipped_at": "2026-11-20",
    "eta": "2026-11-24", "delivered_at": None, "courier": "Pavan Express", "tracking_number": "PVX731984126",
    "delivery_attempts": 0, "returnable": True, "prepaid": True, "late": False, "return_deadline": None,
    "can_cancel": False, "can_return": False, "permitted_actions": [], "refund": None, "return": None,
    "events": [
        {"at": "2026-11-18", "kind": "placed", "note": "Order placed"},
        {"at": "2026-11-20", "kind": "shipped", "note": "Picked up by courier"},
    ],
}
RETURNED = {
    **KURTA, "status": "refund_processing", "delivered_at": "2026-11-05", "return_deadline": "2026-11-12",
    "refund": {"amount_inr": 1499, "status": "initiated", "initiated_at": "2026-11-18", "expected_by": "2026-11-27",
               "credited_at": None, "method": "UPI", "reason": "return", "overdue": False},
    "return": {"status": "requested", "requested_at": "2026-11-09", "pickup_on": "2026-11-11",
               "pickup_status": "scheduled"},
}


def answers(supported=0.93, complete=0.88, requires_human=0.04) -> dict:
    values = dict(zip(QUESTIONS, (supported, complete, requires_human)))
    return {name: {"type": "noul", "noul": value} for name, value in values.items()}


def ok_body(**changes) -> dict:
    return {"model": "jev-1.14.0", "answers": answers(), "usage": {"input_tokens": 535, "output_tokens": 58}, **changes}


def respond_with(body, status: int = 200):
    # json.dumps writes NaN and Infinity literals, which httpx.Response(json=...) refuses to encode.
    headers = {"content-type": "application/json", "x-typesafe-request-id": "req_0123"}
    return lambda request: httpx.Response(status, content=json.dumps(body), headers=headers)


@pytest.fixture
def api(monkeypatch):
    """Route jev's HTTP to a handler in this process: `api.respond` answers, `api.requests` records every call."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)
    monkeypatch.delenv("JEV_TIMEOUT_S", raising=False)
    api = types.SimpleNamespace(requests=[], respond=respond_with(ok_body()))

    def handler(request: httpx.Request) -> httpx.Response:
        api.requests.append(request)
        return api.respond(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(jev, "_client", lambda: client)
    return api


def ask(policy: dict = POLICY, item: dict | None = KURTA) -> dict:
    return score(MESSAGE, HISTORY, item, policy, TODAY, REPLY)


# --- the request ---------------------------------------------------------------------------------------------------


def test_request_goes_to_the_official_endpoint_with_bearer_auth(api):
    ask()
    (request,) = api.requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer test-key"
    assert request.headers["content-type"] == "application/json"


def test_base_url_can_be_overridden(api, monkeypatch):
    monkeypatch.setenv("TYPESAFE_BASE_URL", "http://jev.test/")
    ask()
    assert str(api.requests[0].url) == "http://jev.test/v1/systemone"


def test_body_carries_the_pinned_model_and_three_noul_questions(api):
    ask()
    body = json.loads(api.requests[0].content)
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == GATE["model"] == "jev-1.14.0"
    assert set(body["questions"]) == set(QUESTIONS) and len(QUESTIONS) == 3
    for name, question in body["questions"].items():
        assert set(question) == {"type", "instructions", "criteria"}, name
        assert question["type"] == "noul"
        assert isinstance(question["instructions"], str) and question["instructions"]
        assert set(question["criteria"]) == {"true", "false"}
        assert all(isinstance(side, str) and side for side in question["criteria"].values())
        assert question["instructions"] == GATE["questions"][name]["instructions"]
        assert question["criteria"] == GATE["questions"][name]["criteria"]


def test_state_is_the_evidence_plus_the_proposed_reply_and_nothing_else(api):
    ask()
    ask()
    state = json.loads(api.requests[0].content)["state"]
    assert set(state) == STATE_FIELDS
    assert state == {**evidence(MESSAGE, HISTORY, KURTA, POLICY, TODAY), "proposed_reply": REPLY}
    # No ids, timestamps or other request-unique noise: the same inputs give a byte-identical request.
    assert api.requests[0].content == api.requests[1].content


def test_timeout_defaults_to_five_seconds_and_follows_the_environment(api, monkeypatch):
    ask()
    monkeypatch.setenv("JEV_TIMEOUT_S", "1.5")
    ask()
    assert [request.extensions["timeout"]["read"] for request in api.requests] == [5.0, 1.5]


# --- the gate file -------------------------------------------------------------------------------------------------


def test_gate_file_stores_thresholds_beside_model_questions_and_policy_version():
    keys = {"version", "status", "model", "policy_version", "assistant_scope", "questions", "thresholds",
            "usd_per_mtok_input"}
    assert keys <= set(GATE)
    assert GATE["model"] == "jev-1.14.0"
    assert (GATE["version"], GATE["policy_version"]) == ("pilot-3", "2026-11-22")
    # The cutoffs below were chosen under pilot-1 and the old policy: the status must say they are not yet proven here.
    assert "carried over from pilot-1" in GATE["status"] and "re-validated" in GATE["status"]
    assert set(GATE["questions"]) == set(QUESTIONS)
    assert GATE["thresholds"] == {
        "supported_min": 0.7, "complete_min": 0.7, "requires_human_handoff": 0.7, "requires_human_low": 0.3,
    }
    assert GATE["usd_per_mtok_input"] == 0.042


def test_gate_is_frozen_for_the_policy_the_store_loads():
    # Otherwise the real score() fails closed on every message: it refuses a policy the gate was not frozen for.
    assert GATE["policy_version"] == json.loads((DATA / "policy.json").read_text(encoding="utf-8"))["version"]


def test_gate_states_what_the_assistant_does_not_answer():
    # The judge can call a refusal supported only if the state says what the assistant leaves unanswered. The
    # statement names every kind of request that data/help_topics.json puts out of scope.
    scope = GATE["assistant_scope"]
    out_of_scope = json.loads((DATA / "help_topics.json").read_text(encoding="utf-8"))["out_of_scope"]
    for excluded in ("stock", "product", "discounts and offers", "careers", "unrelated chat", "instructions"):
        assert excluded in out_of_scope and excluded in scope, excluded
    assert "declines" in scope


def test_question_wording_points_at_real_state_fields():
    named = {name: set(re.findall(r"`(\w+)`", GATE["questions"][name]["instructions"])) for name in QUESTIONS}
    assert all(fields <= STATE_FIELDS for fields in named.values()), named
    assert {"proposed_reply", "order_record", "store_policy"} <= named["supported"]
    assert {"proposed_reply", "customer_message", "conversation_history"} <= named["complete"]
    assert "store_policy" in named["requires_human"]


# --- a good answer -------------------------------------------------------------------------------------------------


def test_happy_path_returns_scores_usage_cost_and_request_id(api):
    extra = {**answers(), "extra": {"type": "noul", "noul": 0.5}}  # unknown fields are ignored, not errors
    api.respond = respond_with(ok_body(answers=extra, unknown="ignored"))
    result = ask()
    assert set(result) == {
        "supported", "complete", "requires_human", "model", "input_tokens", "cost_usd", "request_id", "elapsed_ms",
    }
    assert (result["supported"], result["complete"], result["requires_human"]) == (0.93, 0.88, 0.04)
    assert result["model"] == "jev-1.14.0"
    assert result["input_tokens"] == 535
    assert result["cost_usd"] == pytest.approx(535 * 0.042 / 1_000_000)
    assert result["request_id"] == "req_0123"
    assert isinstance(result["elapsed_ms"], int) and result["elapsed_ms"] >= 0
    assert len(api.requests) == 1


def test_integer_zero_and_one_are_accepted_as_floats(api):
    api.respond = respond_with(ok_body(answers=answers(supported=1, complete=0, requires_human=0)))
    result = ask()
    assert (result["supported"], result["complete"], result["requires_human"]) == (1.0, 0.0, 0.0)
    assert all(isinstance(result[name], float) for name in QUESTIONS)


@pytest.mark.parametrize(
    "usage",
    [None, {}, {"input_tokens": None}, {"input_tokens": "535"}, {"input_tokens": 535.5}, {"input_tokens": True}, [535]],
)
def test_unknown_usage_is_not_an_error(api, usage):
    api.respond = respond_with(ok_body(usage=usage))
    result = ask()
    assert result["input_tokens"] is None and result["cost_usd"] is None
    assert result["supported"] == 0.93


def test_missing_model_and_request_id_do_not_fail_the_call(api):
    body = ok_body()
    del body["model"], body["usage"]
    api.respond = lambda request: httpx.Response(200, json=body)
    result = ask()
    assert result["model"] == GATE["model"]
    assert result["request_id"] is None and result["input_tokens"] is None


# --- answers that must never be read as scores -----------------------------------------------------------------------


@pytest.mark.parametrize("name", QUESTIONS)
@pytest.mark.parametrize(
    "noul", [True, False, "0.9", None, float("nan"), float("inf"), 1.2, -0.1, [0.9], {"value": 0.9}],
    ids=["true", "false", "string", "null", "nan", "infinity", "above-one", "below-zero", "list", "object"],
)
def test_an_invalid_noul_raises(api, name, noul):
    api.respond = respond_with(ok_body(answers={**answers(), name: {"type": "noul", "noul": noul}}))
    with pytest.raises(JevError):
        ask()
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        ok_body(answers={name: answer for name, answer in answers().items() if name != "complete"}),
        ok_body(answers={**answers(), "supported": {"type": "score", "noul": 0.9}}),
        ok_body(answers={**answers(), "supported": {"noul": 0.9}}),
        ok_body(answers={**answers(), "supported": {"type": "noul"}}),
        ok_body(answers={**answers(), "requires_human": 0.04}),
        ok_body(answers=list(answers().values())),
        ok_body(answers=None),
        {"model": "jev-1.14.0", "usage": {"input_tokens": 535, "output_tokens": 58}},
        [ok_body()],
        "ok",
        None,
    ],
    ids=[
        "missing-answer", "wrong-type-string", "missing-type", "missing-noul", "answer-not-an-object",
        "answers-is-a-list", "answers-is-null", "no-answers", "body-is-a-list", "body-is-a-string", "body-is-null",
    ],
)
def test_a_malformed_body_raises(api, body):
    api.respond = respond_with(body)
    with pytest.raises(JevError):
        ask()
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    "content", [b"<html>busy</html>", b"", b"\xff\xfe", pytest.param(b"[" * 20_000, id="deeply-nested")]
)
def test_a_body_that_is_not_json_raises(api, content):
    api.respond = lambda request: httpx.Response(200, content=content)
    with pytest.raises(JevError):
        ask()


# --- failures ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 422, 429, 500, 529, 201])
def test_any_other_status_raises_after_exactly_one_request(api, status):
    api.respond = respond_with(ok_body(), status=status)  # valid scores under a failed status still do not count
    with pytest.raises(JevError, match=str(status)):
        ask()
    assert len(api.requests) == 1


def test_a_timeout_raises_without_a_retry(api):
    def slow(request):
        raise httpx.ReadTimeout("no answer in time", request=request)

    api.respond = slow
    with pytest.raises(JevError, match="^timeout$"):
        ask()
    assert len(api.requests) == 1


def test_a_connection_error_raises_without_a_retry(api):
    def refused(request):
        raise httpx.ConnectError("connection refused", request=request)

    api.respond = refused
    with pytest.raises(JevError, match="ConnectError"):
        ask()
    assert len(api.requests) == 1


def test_a_timeout_the_socket_cannot_take_raises(api):
    def overflow(request):  # what the socket layer raises for JEV_TIMEOUT_S=inf
        raise OverflowError("timestamp out of range for platform time_t")

    api.respond = overflow
    with pytest.raises(JevError, match="^request failed: OverflowError$"):
        ask()


@pytest.mark.parametrize("key", [None, "", "   "])
def test_a_missing_key_raises_before_any_request(api, monkeypatch, key):
    if key is None:
        monkeypatch.delenv("TYPESAFE_API_KEY")
    else:
        monkeypatch.setenv("TYPESAFE_API_KEY", key)
    with pytest.raises(JevError, match="TYPESAFE_API_KEY"):
        ask()
    assert api.requests == []


def test_a_policy_the_gate_was_not_frozen_for_raises_before_any_request(api):
    with pytest.raises(JevError, match="2026-12-01"):
        ask(policy={**POLICY, "version": "2026-12-01"})
    assert api.requests == []


@pytest.mark.parametrize(("name", "value"), [("JEV_TIMEOUT_S", "soon"), ("TYPESAFE_BASE_URL", "http://jev.test:port")])
def test_unusable_settings_raise_before_any_request(api, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(JevError):
        ask()
    assert api.requests == []


def test_an_error_never_quotes_the_key(api, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "sécret-key")  # cannot be encoded into a header
    with pytest.raises(JevError) as raised:
        ask()
    assert "cret" not in str(raised.value)
    assert api.requests == []


# --- evidence ------------------------------------------------------------------------------------------------------


def test_evidence_renders_every_date_in_both_forms():
    before = json.dumps(RETURNED)
    record = evidence(MESSAGE, HISTORY, RETURNED, POLICY, TODAY)["order_record"]
    assert record["placed_at"] == "2026-11-18 (November 18)"
    assert record["shipped_at"] == "2026-11-20 (November 20)"
    assert record["eta"] == "2026-11-24 (November 24)"
    assert record["delivered_at"] == "2026-11-05 (November 5)"
    assert record["return_deadline"] == "2026-11-12 (November 12)"
    assert record["refund"]["initiated_at"] == "2026-11-18 (November 18)"
    assert record["refund"]["expected_by"] == "2026-11-27 (November 27)"
    assert record["refund"]["credited_at"] is None
    assert record["return"] == {
        "status": "requested", "requested_at": "2026-11-09 (November 9)", "pickup_on": "2026-11-11 (November 11)",
        "pickup_status": "scheduled",
    }
    assert [event["at"] for event in record["events"]] == ["2026-11-18 (November 18)", "2026-11-20 (November 20)"]
    assert (record["id"], record["price_inr"]) == ("ITM-1001", 1499)
    assert json.dumps(RETURNED) == before  # the caller's record is left alone


def test_evidence_carries_the_whole_order_record():
    record = evidence(MESSAGE, HISTORY, RETURNED, POLICY, TODAY)["order_record"]
    # Product, payment, delivery and eligibility facts all reach the judge as the store holds them.
    for field in ("category", "colour", "size", "payment_method", "shipping_fee_inr", "cod_fee_inr", "order_total_inr",
                  "delivery_city", "delivery_pincode", "courier", "tracking_number", "delivery_attempts",
                  "non_returnable_reason", "returnable", "prepaid", "late", "permitted_actions"):
        assert record[field] == RETURNED[field], field
    assert (record["refund"]["reason"], record["refund"]["overdue"]) == ("return", False)
    # Apart from the spoken form added to each date and the two renamed flags, nothing is changed, added or dropped.
    plain = json.loads(re.sub(r" \([A-Z][a-z]+ \d{1,2}\)", "", json.dumps(record)))
    assert list(plain) == [{"can_cancel": "can_cancel_today", "can_return": "can_return_today"}.get(key, key)
                           for key in RETURNED]
    assert list(plain.values()) == list(RETURNED.values())


def test_evidence_names_the_two_eligibility_flags_as_what_is_possible_today():
    # "can_return": false on an item that is still on its way reads as "cannot be returned at all".
    record = evidence(MESSAGE, HISTORY, {**KURTA, "can_cancel": True}, POLICY, TODAY)["order_record"]
    assert (record["can_cancel_today"], record["can_return_today"]) == (True, False)
    assert "can_cancel" not in record and "can_return" not in record


def test_evidence_has_the_contract_fields():
    result = evidence(MESSAGE, HISTORY, {**KURTA, "permitted_actions": ["return"]}, POLICY, TODAY)
    assert list(result) == [
        "customer_message", "conversation_history", "order_record", "permitted_actions", "store_policy", "today",
    ]
    assert result["customer_message"] == MESSAGE
    assert result["conversation_history"] == ["customer: Hi", "agent: Hello, how can I help?"]
    assert result["permitted_actions"] == ["return"]
    assert result["today"] == "2026-11-21 (November 21)"
    # Every section as its customer-facing text, in the policy's order, then what the assistant leaves
    # unanswered and the escalation conditions. The judge is asked about `store_policy`, so all of it sits there.
    assert list(result["store_policy"]) == ["version", "store", *SECTIONS, "assistant_scope", "escalation_conditions"]
    assert result["store_policy"] == {
        "version": "2026-11-22",
        "store": "Saanjh Clothing",
        **{name: POLICY[name]["text"] for name in SECTIONS},
        "assistant_scope": GATE["assistant_scope"],
        "escalation_conditions": [rule["text"] for rule in POLICY["escalation"]],
    }


def test_evidence_keeps_configuration_and_rule_ids_out_of_the_policy_text():
    sent = json.dumps(evidence(MESSAGE, HISTORY, KURTA, POLICY, TODAY)["store_policy"])
    for internal in ("topics", "cancellable_statuses", "window_days", "non_returnable", "fee_inr", "enforced_by",
                     "backend", "when", "refund_overdue", "abuse"):
        assert f'"{internal}"' not in sent, internal


def test_evidence_carries_every_section_of_the_real_policy():
    policy = json.loads((DATA / "policy.json").read_text(encoding="utf-8"))
    topics = json.loads((DATA / "help_topics.json").read_text(encoding="utf-8"))["topics"]
    sent = evidence(MESSAGE, [], None, policy, TODAY)["store_policy"]
    sections = [name for name, part in policy.items() if isinstance(part, dict)]
    assert list(sent) == ["version", "store", *sections, "assistant_scope", "escalation_conditions"]
    assert all(sent[name] == policy[name]["text"] for name in sections)
    assert sent["escalation_conditions"] == [rule["text"] for rule in policy["escalation"]]
    # Every help topic that a policy section answers has that section in the evidence. Small talk and the
    # details of an order need no policy: the order record answers the latter.
    answered = {topic for name in sections for topic in policy[name]["topics"]}
    unanswered = {topic["id"] for topic in topics if topic["group"] != "small_talk"} - answered
    assert unanswered == {"order_details"}


def test_evidence_without_an_item():
    result = evidence("What is your return policy?", [], None, POLICY, TODAY)
    assert result["order_record"] is None
    assert result["permitted_actions"] == []
    assert result["conversation_history"] == []
    assert result["store_policy"]["returns"] == POLICY["returns"]["text"]
