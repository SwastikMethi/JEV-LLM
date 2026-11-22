"""Offline tests for app/fallback.py.

draft() sends its request through an in-process httpx.MockTransport, so the request that would go over the wire is
what gets inspected and nothing touches the network.
"""
import json
import os
import types

import httpx
import pytest

from app import fallback, jev

# Bound at import, so a stub that another test installs on the fallback module cannot stand in for the real functions.
from app.fallback import FallbackError, draft, problems

TODAY = "2026-11-21"
MESSAGE = "When will it arrive, and can I still cancel it?"
HISTORY = [{"role": "customer", "text": "When will my kurta arrive?"}, {"role": "agent", "text": "It has shipped."}]
KURTA_DRAFT = "Your kurta has shipped. It is expected on November 24."
BOTH_PARTS = (
    "Your kurta has shipped and is expected on November 24. "
    "Because it has already shipped, it can no longer be cancelled; you can return it within 7 days of delivery."
)
POLICY = {  # the shapes of data/policy.json: each section has a customer-facing "text" beside what the code reads
    "version": "2026-11-22",
    "store": "Saanjh Clothing",
    "cancellation": {"topics": ["cancel"], "cancellable_statuses": ["packed"],
                     "text": "An item can be cancelled until it ships."},
    "returns": {"topics": ["return"], "window_days": 7,
                "text": "A delivered item can be returned within 7 days of its delivery date."},
    "refunds": {"topics": ["refund_status"], "business_days": 7,
                "text": "Refunds are credited within 7 business days of being initiated."},
    "shipping_charges": {"topics": ["shipping_charges"], "fee_inr": 79, "free_from_inr": 999,
                         "text": "Shipping costs ₹79 per order and is free on orders of ₹999 or more."},
    "cash_on_delivery": {"topics": ["cash_on_delivery"], "payment_method": "Cash on delivery", "fee_inr": 49,
                         "max_order_inr": 5000,
                         "text": "Cash on delivery is available on orders up to ₹5,000, for a fee of ₹49 per order."},
    "support": {"topics": ["support_contact"],
                "text": "Our support team works from 9 am to 9 pm, Monday to Saturday. We do not offer phone support."},
    "escalation": [
        {"id": "asks_for_person", "enforced_by": "backend", "when": {"intent": ["human"]},
         "text": "The customer explicitly asks to speak to a person."},
    ],
}
KURTA = {  # shipped, not delivered: nothing is permitted
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
SHIRT = {  # packed: can be cancelled
    **KURTA, "id": "ITM-1002", "name": "Mustard linen shirt", "noun": "shirt", "price_inr": 1299,
    "order_total_inr": 1299, "status": "packed", "shipped_at": None, "eta": "2026-11-26", "courier": None,
    "tracking_number": None, "can_cancel": True, "permitted_actions": ["cancel"],
    "events": [{"at": "2026-11-18", "kind": "placed", "note": "Order placed"}],
}
BELT = {  # packed, cash on delivery: the order total is the price and both fees
    **SHIRT, "id": "ITM-1010", "name": "Tan leather belt", "noun": "belt", "price_inr": 599,
    "payment_method": "Cash on delivery", "shipping_fee_inr": 79, "cod_fee_inr": 49, "order_total_inr": 727,
    "prepaid": False,
}
JACKET = {  # delivered, inside the return window
    **KURTA, "id": "ITM-1003", "name": "Black denim jacket", "noun": "jacket", "price_inr": 3499,
    "order_total_inr": 4398, "status": "delivered", "placed_at": "2026-11-12", "shipped_at": "2026-11-14",
    "eta": "2026-11-17", "delivered_at": "2026-11-17", "delivery_attempts": 1, "return_deadline": "2026-11-24",
    "can_return": True, "permitted_actions": ["return"],
    "events": [{"at": "2026-11-12", "kind": "placed", "note": "Order placed"},
               {"at": "2026-11-15", "kind": "hub", "note": "Reached the city hub"}],
}
DRESS = {  # a refund in progress; its amount differs from the price on purpose, since either may be quoted
    **JACKET, "id": "ITM-1007", "name": "Floral summer dress", "noun": "dress", "price_inr": 2499,
    "order_total_inr": 2499, "status": "refund_processing", "can_return": False, "permitted_actions": [],
    "refund": {"amount_inr": 2199, "status": "initiated", "initiated_at": "2026-11-18", "expected_by": "2026-11-27",
               "credited_at": None, "method": "UPI", "reason": "return", "overdue": False},
    "return": {"status": "requested", "requested_at": "2026-11-19", "pickup_on": "2026-11-22",
               "pickup_status": "scheduled"},
}
CREDITED = {**DRESS, "refund": {**DRESS["refund"], "status": "credited", "credited_at": "2026-11-20"}}
CANCELLED = {**SHIRT, "status": "cancelled", "can_cancel": False, "permitted_actions": []}


def check(text: str, item: dict | None = KURTA, action: str | None = None) -> list[str]:
    return problems({"kind": "reply", "text": text, "action": action}, item, POLICY, TODAY)


# --- problems(): action ----------------------------------------------------------------------------------------------


def test_a_grounded_reply_has_no_problems():
    assert check(BOTH_PARTS) == []
    assert check("Could you tell me which item you would like to return?", None) == []


@pytest.mark.parametrize(("item", "action"), [(SHIRT, "cancel"), (JACKET, "return"), (KURTA, None), (None, None)])
def test_a_permitted_action_passes(item, action):
    assert check("Please confirm and I will set that up.", item, action) == []


@pytest.mark.parametrize(
    ("item", "action"), [(KURTA, "cancel"), (KURTA, "return"), (JACKET, "cancel"), (SHIRT, "return"), (None, "return")]
)
def test_an_action_that_is_not_permitted_is_a_problem(item, action):
    (problem,) = check("Please confirm and I will set that up.", item, action)
    assert action in problem


# --- problems(): dates ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quoted",
    [
        "November 24", "Nov 24", "Nov. 24", "November 24th", "november 24", "24 November", "24th Nov",
        "24th of November", "2026-11-24", "November 24, 2026", "24 Nov 2026",
        "November 18", "20 November", "November 21",  # placed, shipped, today
    ],
)
def test_a_date_from_the_record_passes_in_any_form(quoted):
    assert check(f"The date you asked about is {quoted}.") == []


@pytest.mark.parametrize(
    "quoted",
    ["November 25", "Nov 25", "Nov. 25", "25 November", "25th Nov", "25th of November", "2026-11-25", "December 24",
     "24 December", "2026-12-24", "November 2"],
)
def test_a_date_that_is_not_in_the_record_is_a_problem(quoted):
    (problem,) = check(f"The date you asked about is {quoted}.")
    assert quoted in problem


def test_refund_return_deadline_and_event_dates_count_as_record_dates():
    assert check("Your refund was initiated on November 18 and is expected by November 27.", DRESS) == []
    assert check("The return was requested on November 19 and the pickup is on November 22.", DRESS) == []
    assert check("You can return it until November 24.", JACKET) == []
    assert check("It reached the city hub on November 15.", JACKET) == []
    assert check("The refund reached your account on November 20.", CREDITED) == []


def test_without_an_item_only_today_is_a_known_date():
    assert check("Today, November 21, the returns window is 7 days from delivery.", None) == []
    assert len(check("It will arrive on November 24.", None)) == 1


def test_a_weekday_is_a_problem_because_the_record_holds_only_dates():
    (problem,) = check("It is expected on November 24, which is a Wednesday.")  # the date is right; it is a Tuesday
    assert "Wednesday" in problem


@pytest.mark.parametrize(
    "text",
    [
        "Our support team works from 9 am to 9 pm, Monday to Saturday.",
        "You can reach the team monday to saturday, 9 am to 9 pm.",
        "The team works Monday  to\nSaturday.",
    ],
)
def test_the_weekdays_of_the_policy_pass_as_the_policy_states_them(text):
    assert check(text) == [] and check(text, None) == []


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("It will arrive on Monday.", ["Monday"]),
        ("Our support team works Monday to Friday.", ["Monday to Friday"]),
        ("Our support team works Monday through Saturday.", ["Monday", "Saturday"]),
        ("Write to us on Saturday.", ["Saturday"]),
        ("It arrives on Saturday; the team works Monday to Saturday.", ["Saturday"]),
        ("The team works Tuesday to Saturday.", ["Tuesday to Saturday"]),
        ("The team works Monday to Saturday and Sunday.", ["Monday to Saturday and Sunday"]),
    ],
)
def test_a_weekday_outside_the_policy_wording_is_still_a_problem(text, flagged):
    found = check(text)
    assert len(found) == len(flagged)
    assert all(found[index].endswith(phrase) for index, phrase in enumerate(flagged))


@pytest.mark.parametrize(
    "text",
    ["You may return it within 7 days of delivery.", "It usually ships within 24 hours.", "Order ORD-1001 has 1 item."],
)
def test_numbers_that_are_not_dates_are_left_alone(text):
    assert check(text) == []


# --- problems(): amounts -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quoted", ["₹1,499", "₹1499", "₹ 1,499", "Rs 1,499", "Rs. 1,499", "rs.1499", "INR 1,499", "₹1,499.00"]
)
def test_the_item_price_passes_in_any_form(quoted):
    assert check(f"You paid {quoted}.") == []


def test_the_refund_amount_passes_beside_the_price():
    assert check("We are refunding ₹2,199 of the ₹2,499 you paid.", DRESS) == []


@pytest.mark.parametrize("quoted", ["₹1,599", "₹149", "Rs. 998", "INR 15,000", "₹1,499.50", "₹727", "₹0.50"])
def test_an_amount_that_is_not_in_the_record_is_a_problem(quoted):
    (problem,) = check(f"You paid {quoted}.")
    assert quoted in problem


def test_the_fees_and_the_order_total_pass_beside_the_price():
    text = "Your order comes to ₹727: ₹599 for the belt, ₹79 for shipping and a ₹49 cash-on-delivery fee."
    assert check(text, BELT) == []
    assert check("The order came to ₹4,398 in all; the jacket was ₹3,499.", JACKET) == []
    assert check("Shipping was free: the fee was ₹0.", KURTA) == []
    # One order's total is not another's.
    assert len(check("Your order comes to ₹727.", SHIRT)) == 1


@pytest.mark.parametrize(
    "text",
    [
        "Shipping costs ₹79 per order and is free on orders of ₹999 or more.",
        "Cash on delivery is available on orders up to ₹5,000, for a fee of ₹49 per order.",
        "Orders of Rs. 999 or more ship free; below that the fee is INR 79.",
    ],
)
def test_an_amount_the_policy_states_passes_with_or_without_an_item(text):
    assert check(text, None) == [] and check(text, KURTA) == []


def test_without_an_item_only_the_amounts_of_the_policy_are_known():
    assert len(check("Returns are refunded up to ₹1,499.", None)) == 1
    assert len(check("Shipping costs ₹99 per order.", None)) == 1
    # Read from the policy texts, not from a list in the code: a policy that states no amount allows none.
    silent = {**POLICY, "shipping_charges": {"text": "Shipping is charged per order."}}
    found = problems({"kind": "reply", "text": "Shipping costs ₹79 per order.", "action": None}, None, silent, TODAY)
    assert len(found) == 1


# --- problems(): statuses ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "item"),
    [
        ("Your kurta has shipped.", KURTA),
        ("It was shipped on November 20.", KURTA),
        ("It has already been shipped.", KURTA),
        ("Your jacket has been delivered.", JACKET),
        ("It was delivered on November 17.", JACKET),
        ("Your shirt has been cancelled.", CANCELLED),
        ("It was canceled.", CANCELLED),
        ("Your refund has been credited.", CREDITED),
        ("₹2,199 has been refunded.", CREDITED),
    ],
)
def test_a_status_that_agrees_with_the_record_passes(text, item):
    assert check(text, item) == []


@pytest.mark.parametrize(
    ("text", "item"),
    [
        ("Your shirt has shipped.", SHIRT),
        ("It cannot be cancelled because it has shipped.", SHIRT),
        ("It was shipped on November 18.", SHIRT),
        ("It’s been shipped.", SHIRT),
        ("Your kurta has been delivered.", KURTA),
        ("It was delivered on November 20.", KURTA),
        ("Your order has been cancelled.", KURTA),
        ("I have cancelled your shirt.", SHIRT),
        ("I've cancelled it for you.", SHIRT),
        ("We cancelled it for you.", SHIRT),
        ("Your refund has been credited.", DRESS),
        ("It has now been refunded.", DRESS),
        ("It was credited on November 18.", DRESS),
        ("Your order has shipped.", None),
    ],
)
def test_a_status_that_disagrees_with_the_record_is_a_problem(text, item):
    (problem,) = check(text, item)
    assert problem.startswith("status")


@pytest.mark.parametrize(
    "text",
    [
        "It is not true that it has shipped.",
        "I can't confirm that it has shipped.",
        "I can’t confirm that it has shipped.",
        "We never said it has shipped.",
        "The tracking page no longer says it has shipped.",
        "Once it has shipped it cannot be cancelled.",
        "After it has been delivered you can return it within 7 days.",
        "You can return it when it has been delivered.",
        "If it has shipped, it cannot be cancelled.",
        "It can be cancelled until it has shipped.",
        "Before it has shipped you can still cancel it.",
        "It can be cancelled unless it has shipped.",
        "We will write to you as soon as it has shipped.",
    ],
)
def test_a_negation_or_condition_earlier_in_the_sentence_exempts_the_status(text):
    assert check(text, SHIRT) == []


def test_the_contract_example_passes_on_a_shipped_item():
    assert check("Once it has been delivered you can return it.", KURTA) == []


@pytest.mark.parametrize(
    "text",
    [
        "It is not delayed. It has shipped.",
        "Is it not there yet? It has shipped!",
        "This is not a delay\nIt has shipped",
        "It has shipped, if I am not mistaken.",
    ],
)
def test_the_exemption_covers_only_the_same_sentence_before_the_claim(text):
    assert len(check(text, SHIRT)) == 1


@pytest.mark.parametrize(
    "text",
    [
        "It is packed and has not shipped yet.",
        "It hasn't been delivered.",
        "It will be delivered by November 26.",
        "It can still be cancelled. Would you like it cancelled?",
        "Refunds are credited within 7 business days of being initiated.",
        "A cancelled prepaid item is refunded in full.",
    ],
)
def test_future_negative_and_policy_wording_is_not_a_status_claim(text):
    assert check(text, SHIRT) == []


# --- problems(): text ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   \n"])
def test_empty_text_is_a_problem(text):
    assert len(check(text)) == 1


def test_text_may_be_600_characters_but_not_more():
    assert check("a" * 600) == []
    assert len(check("a" * 601)) == 1


def test_a_look_alike_letter_is_a_problem_not_a_crash():
    # re.IGNORECASE reads "ſ" (long s) as "s", but lower-casing does not turn it into one.
    assert check("Your kurta has ſhipped.")[0].startswith("status")
    assert check("It arrives on ſeptember 5.")[0].startswith("date")


def test_every_problem_is_reported():
    found = check("It has been delivered and will arrive by November 30 for ₹9,999.", KURTA, "cancel")
    assert len(found) == 4 and all(isinstance(problem, str) for problem in found)


# --- draft(): a stubbed transport, as in test_jev.py ------------------------------------------------------------------

GOOD = {"kind": "reply", "text": BOTH_PARTS, "action": None}
SETTINGS = (
    "FALLBACK_BASE_URL", "FALLBACK_MODEL", "FALLBACK_API_KEY", "FALLBACK_REASONING_EFFORT", "FALLBACK_TIMEOUT_S",
    "FALLBACK_USD_PER_MTOK_IN", "FALLBACK_USD_PER_MTOK_OUT",
)


def completion(content=json.dumps(GOOD, indent=2), finish_reason="stop", **changes) -> dict:
    """A chat-completions body as Ollama sends it: the draft is pretty-printed JSON text in message.content."""
    return {
        "id": "chatcmpl-0", "object": "chat.completion", "created": 1795256943, "model": "qwen3.5:9b",
        "system_fingerprint": "fp_ollama",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 216, "completion_tokens": 85, "total_tokens": 301},
        **changes,
    }


def respond_with(body, status: int = 200):
    return lambda request: httpx.Response(status, json=body)


@pytest.fixture
def api(monkeypatch):
    """Route draft()'s HTTP to a handler in this process: `api.respond` answers, `api.requests` records every call."""
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)
    api = types.SimpleNamespace(requests=[], respond=respond_with(completion()))

    def handler(request: httpx.Request) -> httpx.Response:
        api.requests.append(request)
        return api.respond(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(fallback, "_client", lambda: client)
    return api


def ask(rejected: dict | None = None) -> dict:
    return draft(MESSAGE, HISTORY, KURTA, POLICY, TODAY, rejected)


def sent(api) -> dict:
    """The JSON body of the one request that was made."""
    (request,) = api.requests
    return json.loads(request.content)


# --- the request ------------------------------------------------------------------------------------------------------


def test_request_goes_to_the_local_chat_completions_endpoint_without_a_key(api):
    ask()
    (request,) = api.requests
    assert request.method == "POST"
    assert str(request.url) == "http://localhost:11434/v1/chat/completions"
    assert request.headers["content-type"] == "application/json"
    assert "authorization" not in request.headers


def test_base_url_can_be_overridden(api, monkeypatch):
    monkeypatch.setenv("FALLBACK_BASE_URL", "http://qwen.test/v1/")
    ask()
    assert str(api.requests[0].url) == "http://qwen.test/v1/chat/completions"


@pytest.mark.parametrize(("key", "header"), [("", None), ("   ", None), ("test-key", "Bearer test-key")])
def test_authorization_is_sent_only_when_a_key_is_set(api, monkeypatch, key, header):
    monkeypatch.setenv("FALLBACK_API_KEY", key)
    ask()
    assert api.requests[0].headers.get("authorization") == header


def test_body_is_one_schema_constrained_call_without_thinking(api):
    ask()
    body = sent(api)
    assert set(body) == {
        "model", "messages", "response_format", "reasoning_effort", "temperature", "max_tokens", "stream",
    }
    assert body["model"] == "qwen3.5:9b"
    assert body["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "draft", "schema": fallback._SCHEMA, "strict": True},
    }
    assert body["reasoning_effort"] == "none"
    assert body["temperature"] == 0 and body["temperature"] is not False
    assert body["max_tokens"] == 500
    assert body["stream"] is False
    assert [turn["role"] for turn in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == fallback.SYSTEM_PROMPT


def test_schema_requires_all_three_fields_and_nothing_else():
    schema = fallback._SCHEMA
    assert schema["type"] == "object"
    assert schema["required"] == ["kind", "text", "action"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["kind"]["enum"] == ["reply", "clarify", "handoff"]
    assert schema["properties"]["text"]["type"] == "string"
    action = schema["properties"]["action"]
    assert action["anyOf"] == [{"enum": ["cancel", "return"], "type": "string"}, {"type": "null"}]


@pytest.mark.parametrize(("setting", "sent_effort"), [(None, "none"), ("none", "none"), ("low", "low"), ("", None)])
def test_reasoning_effort_follows_the_environment_and_an_empty_value_omits_it(api, monkeypatch, setting, sent_effort):
    if setting is not None:
        monkeypatch.setenv("FALLBACK_REASONING_EFFORT", setting)
    ask()
    assert sent(api).get("reasoning_effort") == sent_effort
    assert ("reasoning_effort" in sent(api)) is (sent_effort is not None)


def test_the_model_is_given_the_evidence_as_json_with_the_customer_message_last(api):
    ask()
    turn = sent(api)["messages"][1]
    payload = json.loads(turn["content"])
    assert payload == {"evidence": jev.evidence(MESSAGE, HISTORY, KURTA, POLICY, TODAY)}
    # What changes least comes first and the message last; the values are the shared evidence, untouched.
    assert list(payload["evidence"]) == [
        "store_policy", "today", "order_record", "permitted_actions", "conversation_history", "customer_message",
    ]
    # The text form the system prompt was tuned against on the live model: indented, the rupee sign unescaped.
    assert turn["content"] == json.dumps(payload, ensure_ascii=False, indent=2)
    assert "₹79" in turn["content"]


def test_the_user_turn_keeps_rupee_signs_readable(api):
    draft("Is ₹1,499 the right price?", HISTORY, KURTA, POLICY, TODAY)
    assert "Is ₹1,499 the right price?" in sent(api)["messages"][1]["content"]


def test_a_rejected_draft_is_passed_as_a_hint_without_request_noise(api):
    scores = {"supported": 0.95, "complete": 0.2, "requires_human": 0.02}
    ask({"draft": KURTA_DRAFT, "scores": {**scores, "model": "jev-1.14.0", "request_id": "req_1", "elapsed_ms": 12}})
    payload = json.loads(sent(api)["messages"][1]["content"])
    assert payload["rejected_draft"] == {"draft": KURTA_DRAFT, "scores": scores}
    assert list(payload) == ["evidence", "rejected_draft"]


def test_the_same_inputs_give_a_byte_identical_request(api):
    ask()
    ask()
    assert api.requests[0].content == api.requests[1].content


def test_model_and_timeout_follow_the_environment(api, monkeypatch):
    ask()
    monkeypatch.setenv("FALLBACK_MODEL", "qwen3.5:27b")
    monkeypatch.setenv("FALLBACK_TIMEOUT_S", "7")
    ask()
    first, second = api.requests
    assert json.loads(second.content)["model"] == "qwen3.5:27b"
    assert [request.extensions["timeout"]["read"] for request in (first, second)] == [30.0, 7.0]


# --- a good answer ----------------------------------------------------------------------------------------------------


def test_valid_output_returns_the_contract_dict_and_a_local_model_costs_nothing(api):
    result = ask()
    assert set(result) == {"kind", "text", "action", "input_tokens", "output_tokens", "cost_usd", "elapsed_ms"}
    assert (result["kind"], result["text"], result["action"]) == ("reply", BOTH_PARTS, None)
    assert (result["input_tokens"], result["output_tokens"]) == (216, 85)
    assert result["cost_usd"] == 0.0 and isinstance(result["cost_usd"], float)
    assert isinstance(result["elapsed_ms"], int) and result["elapsed_ms"] >= 0
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    ("price_in", "price_out", "cost"),
    [("0.5", "1.5", 0.0002355), ("0.5", None, 0.000108), (None, "1.5", 0.0001275), ("", "", 0.0)],
)
def test_cost_follows_the_prices_in_the_environment(api, monkeypatch, price_in, price_out, cost):
    for name, price in (("FALLBACK_USD_PER_MTOK_IN", price_in), ("FALLBACK_USD_PER_MTOK_OUT", price_out)):
        if price is not None:
            monkeypatch.setenv(name, price)
    assert ask()["cost_usd"] == pytest.approx(cost)  # 216 input and 85 output tokens, priced per million


@pytest.mark.parametrize(
    "output",
    [
        {"kind": "reply", "text": "It can still be cancelled. Please confirm.", "action": "cancel"},
        {"kind": "reply", "text": "You can return it until November 24. Please confirm.", "action": "return"},
        {"kind": "clarify", "text": "Which item would you like to return?", "action": None},
        {"kind": "handoff", "text": "The customer reports a damaged item.", "action": None},
    ],
)
def test_every_kind_and_action_comes_back_unchanged(api, output):
    api.respond = respond_with(completion(json.dumps(output)))
    result = ask()
    assert {key: result[key] for key in output} == output


def test_fields_the_endpoint_adds_are_ignored(api):
    message = {"role": "assistant", "content": json.dumps(GOOD), "reasoning": "The customer asks two things."}
    choice = {"index": 0, "message": message, "finish_reason": "stop", "logprobs": None}
    usage = {"prompt_tokens": 216, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens": 85}
    api.respond = respond_with(completion(choices=[choice], usage=usage, service_tier="default"))
    assert ask()["text"] == BOTH_PARTS


# --- answers that must never become a draft ---------------------------------------------------------------------------


def choice_with(**changes) -> dict:
    choice = completion()["choices"][0]
    return completion(choices=[{**choice, **changes}])


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        # A reply that did not finish fails on finish_reason alone, even when its text happens to be valid.
        (completion(finish_reason="length"), "finish_reason length"),
        (completion('{"kind": "reply", "text": "Your kur', "length"), "finish_reason length"),
        (completion("", "length"), "finish_reason length"),  # thinking used up max_tokens
        (completion(finish_reason="content_filter"), "finish_reason content_filter"),
        (completion(None, "tool_calls"), "finish_reason tool_calls"),
        (completion(finish_reason=None), "finish_reason None"),
        (completion("Sure, here is the reply: your kurta has shipped."), "content is not JSON"),
        (completion("```json\n" + json.dumps(GOOD) + "\n```"), "content is not JSON"),
        (completion(""), "content is not JSON"),
        (completion(None), "content is not JSON"),
        (completion(GOOD), "content is not JSON"),  # an object where the JSON text should be
        (completion("[" * 20_000), "content is not JSON"),
        (completion(json.dumps({"kind": "reply", "text": "No action key."})), "invalid output"),
        (completion(json.dumps({**GOOD, "kind": "escalate"})), "invalid output"),
        (completion(json.dumps({**GOOD, "action": "refund"})), "invalid output"),
        (completion(json.dumps({**GOOD, "text": 42})), "invalid output"),
        (completion(json.dumps({**GOOD, "item_id": "ITM-1001"})), "invalid output"),
        (completion(json.dumps([GOOD])), "invalid output"),
        (completion(json.dumps("reply")), "invalid output"),
        ({"ok": True}, "malformed response"),
        ({"error": {"message": "model not found"}}, "malformed response"),
        (completion(choices=[]), "malformed response"),
        (completion(choices=None), "malformed response"),
        (completion(choices={"0": completion()["choices"][0]}), "malformed response"),
        (completion(choices=["stop"]), "malformed response"),
        (choice_with(message=None), "malformed response"),
        (choice_with(message={"role": "assistant"}), "malformed response"),
        (completion(choices=[{"index": 0, "message": {"role": "assistant", "content": json.dumps(GOOD)}}]),
         "malformed response"),
        (completion(usage=None), "malformed response"),
        (completion(usage={"prompt_tokens": 216}), "malformed response"),
        (completion(usage={"prompt_tokens": "216", "completion_tokens": 85}), "malformed response"),
        (completion(usage={"prompt_tokens": 216, "completion_tokens": 85.5}), "malformed response"),
        (completion(usage={"prompt_tokens": True, "completion_tokens": 85}), "malformed response"),
        (completion(usage={"prompt_tokens": 216, "completion_tokens": None}), "malformed response"),
        ([completion()], "malformed response"),
        ("ok", "malformed response"),
        (None, "malformed response"),
    ],
    ids=[
        "cut-off-but-valid", "cut-off", "cut-off-empty", "content-filter", "tool-call", "no-finish-reason",
        "prose", "fenced-json", "empty-content", "null-content", "object-content", "deeply-nested",
        "missing-key", "unknown-kind", "unknown-action", "text-not-a-string", "extra-key", "json-array", "json-string",
        "another-object", "error-object", "no-choices", "null-choices", "choices-not-a-list", "choice-not-an-object",
        "null-message", "no-content-key", "no-finish-reason-key", "null-usage", "usage-without-output-tokens",
        "token-count-a-string", "token-count-a-float", "token-count-a-bool", "token-count-null",
        "body-is-a-list", "body-is-a-string", "body-is-null",
    ],
)
def test_an_unusable_answer_raises(api, body, reason):
    api.respond = respond_with(body)
    with pytest.raises(FallbackError, match=f"^{reason}$"):
        ask()
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    "content",
    [b"<html>gateway</html>", b"not json", b"", b"\xff\xfe", pytest.param(b"[" * 20_000, id="deeply-nested")],
)
def test_a_200_whose_body_is_not_json_raises(api, content):
    api.respond = lambda request: httpx.Response(200, content=content)
    with pytest.raises(FallbackError, match="^malformed response$"):
        ask()
    assert len(api.requests) == 1


# --- failures ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [201, 302, 400, 401, 403, 404, 429, 500, 503])
def test_any_other_status_raises_after_exactly_one_request(api, status):
    # A valid draft under a failed status still does not count, and a redirect is not followed.
    api.respond = lambda request: httpx.Response(status, json=completion(), headers={"location": "http://qwen.test/"})
    with pytest.raises(FallbackError, match=f"^http {status}$"):
        ask()
    assert len(api.requests) == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectTimeout])
def test_a_timeout_raises_without_a_retry(api, error):
    def slow(request):
        raise error("no answer in time", request=request)

    api.respond = slow
    with pytest.raises(FallbackError, match="^timeout$"):
        ask()
    assert len(api.requests) == 1


def test_a_connection_error_raises_without_a_retry(api):
    def refused(request):  # what httpx raises when Ollama is not running
        raise httpx.ConnectError("connection refused", request=request)

    api.respond = refused
    with pytest.raises(FallbackError, match="^request failed: ConnectError$"):
        ask()
    assert len(api.requests) == 1


def test_a_timeout_the_socket_cannot_take_raises(api):
    def overflow(request):  # what the socket layer raises for FALLBACK_TIMEOUT_S=inf
        raise OverflowError("timestamp out of range for platform time_t")

    api.respond = overflow
    with pytest.raises(FallbackError, match="^request failed: OverflowError$"):
        ask()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("FALLBACK_TIMEOUT_S", "soon"),
        ("FALLBACK_USD_PER_MTOK_IN", "free"),
        ("FALLBACK_USD_PER_MTOK_OUT", "1,5"),
        ("FALLBACK_BASE_URL", "http://qwen.test:port/v1"),
    ],
)
def test_unusable_settings_raise_before_any_request(api, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(FallbackError, match="^request failed: "):
        ask()
    assert api.requests == []


def test_an_error_never_quotes_the_key(api, monkeypatch):
    monkeypatch.setenv("FALLBACK_API_KEY", "sécret-key")  # cannot be encoded into a header
    with pytest.raises(FallbackError) as raised:
        ask()
    assert "cret" not in str(raised.value)
    assert api.requests == []

    def refused(request):  # a transport error whose own text quotes the header
        raise httpx.ConnectError(f"refused, sent {request.headers['authorization']}", request=request)

    monkeypatch.setenv("FALLBACK_API_KEY", "test-key")
    for respond in (refused, respond_with({"error": "bad key test-key"}, 401), respond_with({"key": "test-key"})):
        api.respond = respond
        with pytest.raises(FallbackError) as raised:
            ask()
        assert "test-key" not in str(raised.value)


def test_the_real_client_is_shared_and_follows_no_redirect():
    client = fallback._client()
    assert client is fallback._client()
    assert client.follow_redirects is False  # a redirect would be a second request


def test_without_a_stub_the_suite_cannot_reach_a_real_model(monkeypatch):
    # tests/conftest.py points every test at a closed local port, so a call that no test has stubbed fails closed.
    assert os.environ["FALLBACK_BASE_URL"] == "http://127.0.0.1:9/v1"
    monkeypatch.setenv("FALLBACK_TIMEOUT_S", "2")
    with pytest.raises(FallbackError):
        ask()
