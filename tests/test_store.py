"""Store: fixture loading, item facts, history, tickets and the confirm-once contract.

Also the knowledge the store serves: the versioned policy, the public help topics and the order records.
"""
import copy
import json
import re
import sqlite3
import threading
from datetime import date
from pathlib import Path

import pytest

from app.store import FIXTURES, POLICY, Store, TicketError, _add_business_days

HELP = json.loads((Path(__file__).resolve().parents[1] / "data" / "help_topics.json").read_text(encoding="utf-8"))

ALL_IDS = [f"ITM-10{n:02d}" for n in range(1, 16)]
ITEM_BRIEF_KEYS = {"id", "order_id", "name", "noun", "status"}
ORDER_ITEM_KEYS = ITEM_BRIEF_KEYS | {
    "variant", "price_inr", "eta", "delivered_at",
    "category", "colour", "size", "payment_method", "non_returnable_reason", "courier", "tracking_number", "late",
}
ITEM_FACT_KEYS = ITEM_BRIEF_KEYS | {
    "variant", "price_inr", "placed_at", "payment_method", "shipped_at", "eta", "delivered_at",
    "return_deadline", "can_cancel", "can_return", "permitted_actions", "refund", "return", "events",
    # the item
    "category", "colour", "size", "returnable", "non_returnable_reason",
    # its order
    "prepaid", "shipping_fee_inr", "cod_fee_inr", "order_total_inr", "delivery_city", "delivery_pincode",
    # its shipment
    "courier", "tracking_number", "delivery_attempts", "late",
}
REFUND_KEYS = {"amount_inr", "status", "initiated_at", "expected_by", "credited_at", "method", "reason", "overdue"}
RETURN_KEYS = {"status", "requested_at", "pickup_on", "pickup_status"}
TABLES = {
    "orders": ["id", "placed_at", "payment_method", "shipping_fee_inr", "cod_fee_inr", "delivery_city",
               "delivery_pincode"],
    "items": ["id", "order_id", "name", "noun", "variant", "price_inr", "status", "shipped_at", "eta", "delivered_at",
              "category", "colour", "size", "non_returnable_reason", "courier", "tracking_number",
              "delivery_attempts"],
    "events": ["id", "item_id", "at", "kind", "note"],
    "returns": ["item_id", "status", "requested_at", "pickup_on", "pickup_status"],
    "refunds": ["item_id", "amount_inr", "status", "initiated_at", "expected_by", "credited_at", "method", "reason"],
    "tickets": ["ref", "item_id", "kind", "summary", "created_at"],
    "actions": ["token", "item_id", "action", "created_at", "outcome"],
    "messages": ["id", "role", "text", "item_id"],
    "traces": ["id", "data"],
}
# id, name, noun, status, and the facts stated for that item. The first nine are the original demo items.
REQUIRED = [
    ("ITM-1001", "Blue kurta", "kurta", "shipped",
     {"order_id": "ORD-1001", "price_inr": 1499, "shipped_at": "2026-11-20", "eta": "2026-11-24",
      "delivered_at": None}),
    ("ITM-1002", "Mustard linen shirt", "shirt", "packed",
     {"price_inr": 1299, "shipped_at": None, "eta": "2026-11-26"}),
    ("ITM-1003", "Black denim jacket", "jacket", "delivered",
     {"delivered_at": "2026-11-17", "price_inr": 3499, "return_deadline": "2026-11-24"}),
    ("ITM-1004", "Maroon silk dupatta", "dupatta", "delivered",
     {"delivered_at": "2026-11-14", "return_deadline": "2026-11-21"}),
    ("ITM-1005", "Grey wool scarf", "scarf", "delivered",
     {"delivered_at": "2026-11-13", "return_deadline": "2026-11-20"}),
    ("ITM-1006", "Olive chinos", "chinos", "return_requested",
     {"delivered_at": "2026-11-15",
      "return": {"status": "requested", "requested_at": "2026-11-19", "pickup_on": "2026-11-22",
                 "pickup_status": "scheduled"}}),
    ("ITM-1007", "Floral summer dress", "dress", "refund_processing",
     {"refund": {"amount_inr": 2199, "status": "initiated", "initiated_at": "2026-11-18", "expected_by": "2026-11-27",
                 "credited_at": None, "method": "UPI", "reason": "return", "overdue": False}}),
    ("ITM-1008", "White sneakers", "sneakers", "refund_processing",
     {"refund": {"amount_inr": 2799, "status": "initiated", "initiated_at": "2026-11-05", "expected_by": "2026-11-16",
                 "credited_at": None, "method": "UPI", "reason": "return", "overdue": True}}),
    ("ITM-1009", "White chikankari kurta", "kurta", "delivered", {"delivered_at": "2026-11-19"}),
    # The items that exercise the 2026-11-22 policy.
    ("ITM-1010", "Tan leather belt", "belt", "packed",  # cash on delivery, below the free-shipping threshold
     {"order_id": "ORD-1008", "payment_method": "Cash on delivery", "prepaid": False, "price_inr": 599,
      "shipping_fee_inr": 79, "cod_fee_inr": 49, "order_total_inr": 727, "eta": "2026-11-27", "late": False,
      "courier": None, "tracking_number": None, "delivery_attempts": 0, "refund": None}),
    ("ITM-1011", "Navy wool sweater", "sweater", "delivered",  # cash on delivery, inside its return window
     {"payment_method": "Cash on delivery", "prepaid": False, "price_inr": 1799, "shipping_fee_inr": 0,
      "cod_fee_inr": 49, "order_total_inr": 1848, "eta": "2026-11-17", "delivered_at": "2026-11-18",
      "delivery_attempts": 2, "returnable": True, "return_deadline": "2026-11-25"}),
    ("ITM-1012", "Rust velvet blazer", "blazer", "delivered",  # final sale: not returnable
     {"delivered_at": "2026-11-16", "returnable": False, "non_returnable_reason": "final_sale",
      "return_deadline": None, "prepaid": True}),
    ("ITM-1013", "Charcoal fleece hoodie", "hoodie", "shipped",  # its estimated date has passed
     {"shipped_at": "2026-11-16", "eta": "2026-11-19", "delivered_at": None, "late": True, "delivery_attempts": 0,
      "courier": "Pavan Express", "tracking_number": "PVX731977015"}),
    ("ITM-1014", "Ivory cotton tunic", "tunic", "shipped",  # one failed delivery attempt, due tomorrow
     {"shipped_at": "2026-11-18", "eta": "2026-11-22", "late": False, "delivery_attempts": 1,
      "courier": "Sarathi Couriers"}),
    ("ITM-1015", "Emerald georgette skirt", "skirt", "cancelled",  # cancelled before shipping, refund credited
     {"shipped_at": None, "payment_method": "Credit card", "prepaid": True,
      "refund": {"amount_inr": 1399, "status": "credited", "initiated_at": "2026-11-10", "expected_by": "2026-11-19",
                 "credited_at": "2026-11-13", "method": "Credit card", "reason": "cancellation", "overdue": False}}),
]
# id, can_cancel, can_return, permitted_actions on the fixture day, 2026-11-21
ELIGIBILITY = [
    ("ITM-1001", False, False, []),  # shipped
    ("ITM-1002", True, False, ["cancel"]),  # packed: the only cancellable status
    ("ITM-1003", False, True, ["return"]),  # delivered, window open until 2026-11-24
    ("ITM-1004", False, True, ["return"]),  # delivered, today is the last eligible day
    ("ITM-1005", False, False, []),  # delivered, window closed yesterday
    ("ITM-1006", False, False, []),  # return_requested
    ("ITM-1007", False, False, []),  # refund_processing
    ("ITM-1008", False, False, []),  # refund_processing, overdue
    ("ITM-1009", False, True, ["return"]),  # delivered
    ("ITM-1010", True, False, ["cancel"]),  # packed, cash on delivery
    ("ITM-1011", False, True, []),  # delivered, cash on delivery: returnable, but support arranges the return
    ("ITM-1012", False, False, []),  # delivered, final sale
    ("ITM-1013", False, False, []),  # shipped and late
    ("ITM-1014", False, False, []),  # shipped
    ("ITM-1015", False, False, []),  # cancelled
]


def old_style_item(number: int, noun: str, status: str, **fields: object) -> dict:
    """An item as fixtures were written before the 2026-11-22 fields existed."""
    return {
        "id": f"ITM-900{number}", "name": f"Teal cotton {noun}", "noun": noun, "variant": "Size S",
        "price_inr": 1000 * number, "status": status, "shipped_at": None, "eta": "2026-11-16", "delivered_at": None,
        "events": [{"at": "2026-11-10", "kind": "placed", "note": "Order placed"}], "return": None, "refund": None,
        **fields,
    }


# One order in the shape of the archived evaluation cases.
OLD_STYLE = {
    "now": "2026-11-21T10:00:00+05:30",
    "customer": {"name": "Ananya Rao"},
    "orders": [{"id": "ORD-9001", "placed_at": "2026-11-10", "payment_method": "Cash on delivery", "items": [
        old_style_item(1, "kurti", "return_requested", shipped_at="2026-11-12", delivered_at="2026-11-15",
                       **{"return": {"status": "requested", "requested_at": "2026-11-19", "pickup_on": "2026-11-22"}}),
        old_style_item(2, "blouse", "cancelled", refund={
            "amount_inr": 2000, "status": "initiated", "initiated_at": "2026-11-11", "expected_by": "2026-11-20",
            "credited_at": None, "method": "UPI"}),
        old_style_item(3, "skirt", "shipped", shipped_at="2026-11-12"),
    ]}],
}

# The public catalog: what the case writers are given.
ORDER_TOPICS = [
    "track_status", "track_eta", "track_details", "late_delivery", "not_received", "cancel", "return", "exchange",
    "refund_status", "refund_terms", "damaged_wrong", "address_change", "modify_order", "payment", "invoice",
    "order_details", "speed_up", "delivery_options", "human",
]
GENERAL_TOPICS = [
    "return_policy", "refund_policy", "cancellation_policy", "exchange_policy", "shipping_times",
    "shipping_charges", "cash_on_delivery", "international_shipping", "payment_methods", "damaged_wrong_process",
    "how_to_track", "support_contact", "order_changes",
]
SMALL_TALK = ["greeting", "thanks", "goodbye"]
# Topics the policy has nothing to say about: the answer is the order record itself, or a courtesy.
NO_POLICY_NEEDED = {"order_details", *SMALL_TALK}

SECTIONS = {
    "cancellation", "returns", "exchanges", "refunds", "shipping_times", "shipping_charges", "cash_on_delivery",
    "international_shipping", "payment_methods", "tracking", "delivery_attempts", "delivery_changes",
    "late_delivery", "missing_delivery", "address_change", "order_changes", "damaged_or_wrong_item",
    "payment_problems", "invoices", "support",
}
ESCALATION = [  # id, enforced by
    ("delivery_scan_disputed", "backend"), ("refund_overdue", "backend"), ("late_delivery", "backend"),
    ("damaged_or_wrong_item", "backend"), ("address_change", "backend"), ("cod_return", "backend"),
    ("asks_for_person", "backend"), ("payment_problem", "gate"), ("legal_or_safety", "gate"), ("abuse", "gate"),
]
# The intents app/rules.py detects today, kept in the two rules that predate the help topics until it detects
# the topics themselves.
LEGACY_INTENTS = {"missing", "refund"}


@pytest.fixture
def store() -> Store:
    return Store()


def store_at(now: str) -> Store:
    """A store whose demo clock reads `now`."""
    return Store({**copy.deepcopy(FIXTURES), "now": now})


def refund_rows(store: Store, item_id: str) -> list[dict]:
    return [dict(row) for row in store.db.execute("SELECT * FROM refunds WHERE item_id = ?", (item_id,))]


def inr(amount: int) -> str:
    return f"₹{amount:,}"


def one_of(wanted: object) -> list:
    """A "when" value as the list of values it accepts: a list means one of them, anything else that value."""
    return wanted if isinstance(wanted, list) else [wanted]


def mandated_rule(intent: str | None, item: dict) -> str | None:
    """How a backend rule's "when" is to be read: the first rule whose every key matches. A key is "intent",
    "refund_overdue" or the name of an item fact."""
    context = {**item, "intent": intent, "refund_overdue": bool(item["refund"] and item["refund"]["overdue"])}
    for rule in POLICY["escalation"]:
        if rule["enforced_by"] == "backend" and all(
            context[key] in one_of(wanted) for key, wanted in rule["when"].items()
        ):
            return rule["id"]
    return None


def test_clock_customer_and_policy(store):
    assert store.now.isoformat() == "2026-11-21T10:00:00+05:30"
    assert store.now.utcoffset() is not None
    assert store.today == "2026-11-21"
    assert store.customer == {"name": "Meera Iyer"}
    assert store.policy == POLICY
    assert store.db.row_factory is sqlite3.Row


def test_tables_match_the_contract(store):
    for table, columns in TABLES.items():
        assert [row["name"] for row in store.db.execute(f"PRAGMA table_info({table})")] == columns


# --- the policy and the help topics ------------------------------------------------------------------------------

def test_policy_keeps_the_keys_the_code_reads():
    assert (POLICY["version"], POLICY["store"]) == ("2026-11-22", "Saanjh Clothing")
    assert POLICY["cancellation"]["cancellable_statuses"] == ["packed"]
    assert POLICY["returns"]["window_days"] == 7
    assert POLICY["refunds"]["business_days"] == 7
    assert set(POLICY) == SECTIONS | {"version", "store", "escalation"}


@pytest.mark.parametrize("name", sorted(SECTIONS))
def test_policy_section_has_a_customer_facing_text_and_its_topics(name):
    section = POLICY[name]
    text = section["text"]

    assert text == text.strip() and text.endswith(".") and "  " not in text and "\n" not in text
    # A section can be sent whole as the answer to a general question, so it keeps to the 600 characters that
    # the fallback check and the evaluation allow a reply.
    assert len(text) <= 600
    assert section["topics"] and set(section["topics"]) <= set(ORDER_TOPICS + GENERAL_TOPICS)


def test_help_topics_are_the_public_catalog():
    topics = HELP["topics"]

    assert HELP["version"] == POLICY["version"]
    assert [topic["id"] for topic in topics] == ORDER_TOPICS + GENERAL_TOPICS + SMALL_TALK
    assert all(set(topic) == {"id", "group", "title", "wants", "needs_item"} for topic in topics)
    groups = {"order": ORDER_TOPICS, "general": GENERAL_TOPICS, "small_talk": SMALL_TALK}
    assert {group: [topic["id"] for topic in topics if topic["group"] == group] for group in groups} == groups
    assert all(topic["needs_item"] is (topic["group"] == "order") for topic in topics)
    assert len({topic["title"] for topic in topics}) == len(topics)
    for topic in topics:
        # One or two plain sentences on what the customer wants: no sample wording, nothing about detection.
        wants = topic["wants"]
        assert wants.startswith("The customer ") and wants.endswith(".") and 1 <= wants.count(".") <= 2
        assert not re.search(r"[\"“”?]", wants)
    assert HELP["out_of_scope"].endswith(".")


def test_policy_answers_every_help_topic_that_needs_an_answer():
    answered = {topic for name in SECTIONS for topic in POLICY[name]["topics"]}

    assert set(ORDER_TOPICS + GENERAL_TOPICS + SMALL_TALK) - answered == NO_POLICY_NEEDED


def test_policy_texts_state_their_structured_values():
    stated = {
        "returns": [f"{POLICY['returns']['window_days']} days", "final-sale", "innerwear"],
        "refunds": [f"{POLICY['refunds']['business_days']} business days"],
        "shipping_times": [f"{POLICY['shipping_times']['dispatch_business_days']} business days",
                           "{} to {} days".format(*POLICY["shipping_times"]["delivery_days"])],
        "shipping_charges": [inr(POLICY["shipping_charges"]["fee_inr"]),
                             inr(POLICY["shipping_charges"]["free_from_inr"])],
        "cash_on_delivery": [inr(POLICY["cash_on_delivery"]["fee_inr"]),
                             inr(POLICY["cash_on_delivery"]["max_order_inr"])],
        "delivery_attempts": [f"{POLICY['delivery_attempts']['max_attempts']} attempts"],
        "tracking": POLICY["tracking"]["couriers"],
        "payment_methods": [method.lower() for method in POLICY["payment_methods"]["methods"]],
    }

    for name, values in stated.items():
        text = POLICY[name]["text"]
        assert [value for value in values if value not in text and value not in text.lower()] == []
    assert set(POLICY["returns"]["non_returnable"]) == {"final_sale", "innerwear"}
    assert POLICY["cash_on_delivery"]["payment_method"] in POLICY["payment_methods"]["methods"]


def test_escalation_names_every_situation_that_needs_a_person():
    rules = POLICY["escalation"]

    assert [(rule["id"], rule["enforced_by"]) for rule in rules] == ESCALATION
    for rule in rules:
        backend = rule["enforced_by"] == "backend"
        assert set(rule) == {"id", "enforced_by", "text"} | ({"when"} if backend else set())
        assert rule["text"] == rule["text"].strip() and rule["text"].endswith(".")
        if backend:
            # Machine-checkable: the intents it applies to, and item facts by their names.
            when = rule["when"]
            assert set(when) <= ITEM_FACT_KEYS | {"intent", "refund_overdue"}
            assert set(one_of(when["intent"])) <= set(ORDER_TOPICS) | LEGACY_INTENTS
            assert all(isinstance(value, (str, bool)) for wanted in when.values() for value in one_of(wanted))


@pytest.mark.parametrize(
    "intent, item_id, rule",
    [
        ("track_status", "ITM-1013", "late_delivery"),  # shipped, and its estimated date has passed
        ("track_eta", "ITM-1013", "late_delivery"),
        ("late_delivery", "ITM-1013", "late_delivery"),
        ("not_received", "ITM-1013", "late_delivery"),
        ("track_details", "ITM-1013", "late_delivery"),  # any question about the delivery of a late item
        ("speed_up", "ITM-1013", "late_delivery"),
        ("delivery_options", "ITM-1013", "late_delivery"),
        ("cancel", "ITM-1013", None),  # not about its delivery: the rules can answer
        ("track_details", "ITM-1001", None),
        ("track_status", "ITM-1001", None),  # on time
        ("late_delivery", "ITM-1014", None),  # a failed attempt, but not due until tomorrow
        ("damaged_wrong", "ITM-1003", "damaged_or_wrong_item"),
        ("damaged_wrong", "ITM-1001", "damaged_or_wrong_item"),  # whatever the record says
        ("address_change", "ITM-1002", "address_change"),  # not shipped yet: support changes it
        ("address_change", "ITM-1001", None),  # shipped: it cannot be changed, which the rules can say
        ("return", "ITM-1011", "cod_return"),  # cash on delivery, inside the window
        ("return", "ITM-1003", None),  # prepaid: offered for confirmation
        ("return", "ITM-1010", None),  # cash on delivery, but not delivered yet
        ("exchange", "ITM-1011", None),
        ("human", "ITM-1009", "asks_for_person"),
        ("payment", "ITM-1001", None),  # a payment problem is left to the gate
        ("order_details", "ITM-1008", None),
        (None, "ITM-1013", None),
        # The two rules app/rules.py enforces today, under the intents it detects today (see LEGACY_INTENTS).
        ("missing", "ITM-1003", "delivery_scan_disputed"),
        ("missing", "ITM-1011", "delivery_scan_disputed"),
        ("refund", "ITM-1008", "refund_overdue"),
        ("refund", "ITM-1007", None),
        ("refund", "ITM-1015", None),  # credited
    ],
)
def test_backend_rules_are_decided_by_the_intent_and_the_item_facts(store, intent, item_id, rule):
    assert mandated_rule(intent, store.item(item_id)) == rule


def test_backend_rules_follow_the_clock():
    after_the_window = store_at("2026-11-26T10:00:00+05:30")  # the sweater's last eligible day was 2026-11-25
    due_day, day_after = store_at("2026-11-22T10:00:00+05:30"), store_at("2026-11-23T10:00:00+05:30")

    assert mandated_rule("return", after_the_window.item("ITM-1011")) is None
    assert mandated_rule("track_status", due_day.item("ITM-1014")) is None  # the tunic is due on 2026-11-22
    assert mandated_rule("track_status", day_after.item("ITM-1014")) == "late_delivery"


# --- the order records -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("item_id, name, noun, status, stated", REQUIRED, ids=[row[0] for row in REQUIRED])
def test_required_item(store, item_id, name, noun, status, stated):
    facts = store.item(item_id)

    assert set(facts) == ITEM_FACT_KEYS
    assert (facts["name"], facts["noun"], facts["status"]) == (name, noun, status)
    assert {key: facts[key] for key in stated} == stated
    assert facts["refund"] is None or set(facts["refund"]) == REFUND_KEYS
    assert facts["return"] is None or set(facts["return"]) == RETURN_KEYS
    assert all(set(event) == {"at", "kind", "note"} for event in facts["events"])
    assert facts["events"][0] == {"at": facts["placed_at"], "kind": "placed", "note": "Order placed"}
    assert [event["at"] for event in facts["events"]] == sorted(event["at"] for event in facts["events"])


def test_items_and_orders_keep_fixture_order(store):
    items, orders = store.items(), store.orders()

    assert [item["id"] for item in items] == ALL_IDS
    assert all(set(item) == ITEM_BRIEF_KEYS for item in items)
    assert [order["id"] for order in orders] == [order["id"] for order in FIXTURES["orders"]]
    assert all(set(order) == {"id", "placed_at", "items"} for order in orders)
    listed = [item for order in orders for item in order["items"]]
    assert [item["id"] for item in listed] == ALL_IDS
    assert all(set(item) == ORDER_ITEM_KEYS for item in listed)
    assert all(item["order_id"] == order["id"] for order in orders for item in order["items"])
    assert orders[0]["id"] == "ORD-1001" and [item["id"] for item in orders[0]["items"]] == ["ITM-1001"]
    assert any(len(order["items"]) == 2 for order in orders)


def test_order_list_shows_what_the_new_policy_turns_on(store):
    listed = {item["id"]: item for order in store.orders() for item in order["items"]}
    shown = ("category", "colour", "size", "payment_method", "non_returnable_reason", "courier", "tracking_number",
             "late")

    assert {key: listed["ITM-1001"][key] for key in shown} == {
        "category": "Kurtas", "colour": "Blue", "size": "M", "payment_method": "UPI",
        "non_returnable_reason": None, "courier": "Pavan Express", "tracking_number": "PVX731984126", "late": False,
    }
    assert listed["ITM-1010"]["payment_method"] == "Cash on delivery"
    assert listed["ITM-1012"]["non_returnable_reason"] == "final_sale"
    assert [item_id for item_id, item in listed.items() if item["late"]] == ["ITM-1013"]
    # The list and the item facts are read from the same rows.
    for item_id, item in listed.items():
        facts = store.item(item_id)
        assert item == {key: facts[key] for key in ORDER_ITEM_KEYS}


@pytest.mark.parametrize("item_id, can_cancel, can_return, permitted", ELIGIBILITY, ids=[row[0] for row in ELIGIBILITY])
def test_eligibility_for_each_status(store, item_id, can_cancel, can_return, permitted):
    facts = store.item(item_id)

    assert facts["can_cancel"] is can_cancel
    assert facts["can_return"] is can_return
    assert facts["permitted_actions"] == permitted


def test_order_fields_and_unknown_item(store):
    kurta = store.item("ITM-1001")

    assert (kurta["placed_at"], kurta["payment_method"], kurta["variant"]) == ("2026-11-18", "UPI", "Size M")
    assert (kurta["return_deadline"], kurta["refund"], kurta["return"]) == (None, None, None)
    assert kurta["events"] == [
        {"at": "2026-11-18", "kind": "placed", "note": "Order placed"},
        {"at": "2026-11-20", "kind": "shipped", "note": "Picked up by courier"},
    ]
    assert store.item("ITM-9999") is None


def test_record_carries_the_item_its_order_and_its_shipment(store):
    kurta, shirt, jacket = store.item("ITM-1001"), store.item("ITM-1002"), store.item("ITM-1003")

    assert (kurta["category"], kurta["colour"], kurta["size"]) == ("Kurtas", "Blue", "M")
    assert (kurta["returnable"], kurta["non_returnable_reason"]) == (True, None)
    assert (kurta["prepaid"], kurta["shipping_fee_inr"], kurta["cod_fee_inr"], kurta["order_total_inr"]) == (
        True, 0, 0, 1499)
    assert (kurta["delivery_city"], kurta["delivery_pincode"]) == ("Bengaluru", "560034")
    assert (kurta["courier"], kurta["tracking_number"], kurta["delivery_attempts"], kurta["late"]) == (
        "Pavan Express", "PVX731984126", 0, False)
    # Not shipped yet: no courier and no tracking number.
    assert (shirt["courier"], shirt["tracking_number"], shirt["delivery_attempts"]) == (None, None, 0)
    # The order total is every item of the order plus its fees: the jacket and the dupatta were bought together.
    assert (jacket["price_inr"], jacket["order_total_inr"], jacket["delivery_attempts"]) == (3499, 4398, 1)
    assert store.item("ITM-1004")["order_total_inr"] == 4398


def test_fixture_orders_follow_the_policy():
    shipping, cod = POLICY["shipping_charges"], POLICY["cash_on_delivery"]
    dispatch = POLICY["shipping_times"]["dispatch_business_days"]
    fastest, slowest = POLICY["shipping_times"]["delivery_days"]
    today = FIXTURES["now"][:10]
    for order in FIXTURES["orders"]:
        goods = sum(item["price_inr"] for item in order["items"])
        on_delivery = order["payment_method"] == cod["payment_method"]
        placed = date.fromisoformat(order["placed_at"])
        assert order["payment_method"] in POLICY["payment_methods"]["methods"]
        assert order["shipping_fee_inr"] == (0 if goods >= shipping["free_from_inr"] else shipping["fee_inr"])
        assert order["cod_fee_inr"] == (cod["fee_inr"] if on_delivery else 0)
        assert not on_delivery or goods <= cod["max_order_inr"]
        assert order["delivery_city"] and re.fullmatch(r"\d{6}", order["delivery_pincode"])
        for item in order["items"]:
            shipped, attempts, refund = item["shipped_at"], item["delivery_attempts"], item["refund"]
            assert all(item[key] for key in ("category", "colour", "size"))
            assert item["non_returnable_reason"] in (None, *POLICY["returns"]["non_returnable"])
            assert item["courier"] in (POLICY["tracking"]["couriers"] if shipped else [None])
            assert bool(item["tracking_number"]) is bool(shipped)
            # A delivery took at least one attempt, and nothing that has not shipped was ever attempted.
            assert (1 if item["delivered_at"] else 0) <= attempts <= (
                POLICY["delivery_attempts"]["max_attempts"] if shipped else 0)
            # Dispatched within the promised business days, and due within the promised days of dispatch.
            dispatch_by = _add_business_days(placed, dispatch)
            assert date.fromisoformat(shipped or today) <= dispatch_by or item["status"] == "cancelled"
            due_in = (date.fromisoformat(item["eta"]) - (date.fromisoformat(shipped) if shipped else dispatch_by)).days
            assert (fastest <= due_in <= slowest) if shipped else due_in <= slowest
            if refund:
                assert refund["reason"] in ("cancellation", "return")
                assert refund["amount_inr"] == item["price_inr"]  # the price of the item, never its fees
                assert on_delivery or refund["method"] == order["payment_method"]
            if item["return"]:
                assert item["return"]["pickup_status"] in ("scheduled", "picked_up")


def test_old_style_fixtures_load_with_the_new_fields_defaulted():
    store = Store(OLD_STYLE)
    kurti, blouse, skirt = (store.item(f"ITM-900{number}") for number in (1, 2, 3))

    assert all(set(facts) == ITEM_FACT_KEYS for facts in (kurti, blouse, skirt))
    assert (kurti["category"], kurti["colour"], kurti["size"]) == (None, None, None)
    assert (kurti["returnable"], kurti["non_returnable_reason"], kurti["return_deadline"]) == (True, None, "2026-11-22")
    assert (kurti["delivery_city"], kurti["delivery_pincode"]) == (None, None)
    # No fee was recorded, so none was charged; whether the order was prepaid follows from its payment method.
    assert (kurti["prepaid"], kurti["shipping_fee_inr"], kurti["cod_fee_inr"], kurti["order_total_inr"]) == (
        False, 0, 0, 6000)
    # The shipment was not recorded: unknown, which is not the same as no attempt made.
    assert (kurti["courier"], kurti["tracking_number"], kurti["delivery_attempts"]) == (None, None, None)
    assert kurti["return"] == {
        "status": "requested", "requested_at": "2026-11-19", "pickup_on": "2026-11-22", "pickup_status": "scheduled"}
    assert set(blouse["refund"]) == REFUND_KEYS
    assert (blouse["refund"]["reason"], blouse["refund"]["overdue"], blouse["late"]) == (None, True, False)
    assert (skirt["late"], skirt["permitted_actions"]) == (True, [])
    assert all(set(item) == ORDER_ITEM_KEYS for order in store.orders() for item in order["items"])


def test_a_fixture_still_needs_its_original_fields():
    broken = copy.deepcopy(OLD_STYLE)
    del broken["orders"][0]["items"][0]["status"]

    with pytest.raises(sqlite3.ProgrammingError):
        Store(broken)


@pytest.mark.parametrize(
    "now, dupatta, scarf",
    [
        ("2026-11-20T23:59:00+05:30", True, True),  # the scarf's last eligible day
        ("2026-11-21T00:00:00+05:30", True, False),  # the dupatta's last eligible day
        ("2026-11-22T00:00:00+05:30", False, False),
    ],
)
def test_return_window_boundaries_move_with_the_clock(now, dupatta, scarf):
    store = store_at(now)

    assert store.item("ITM-1004")["can_return"] is dupatta
    assert store.item("ITM-1005")["can_return"] is scarf


def test_refund_is_overdue_only_after_its_expected_day(store):
    assert store.item("ITM-1008")["refund"]["overdue"] is True
    assert store.item("ITM-1007")["refund"]["overdue"] is False
    assert store_at("2026-11-27T10:00:00+05:30").item("ITM-1007")["refund"]["overdue"] is False
    assert store_at("2026-11-28T10:00:00+05:30").item("ITM-1007")["refund"]["overdue"] is True

    store.db.execute("UPDATE refunds SET status = 'credited', credited_at = '2026-11-20' WHERE item_id = 'ITM-1008'")

    assert store.item("ITM-1008")["refund"]["overdue"] is False


@pytest.mark.parametrize(
    "now, hoodie, tunic, shirt",
    [
        ("2026-11-19T23:59:00+05:30", False, False, False),  # the hoodie's estimated day itself
        ("2026-11-20T00:00:00+05:30", True, False, False),
        ("2026-11-22T23:59:00+05:30", True, False, False),  # the tunic's estimated day itself
        ("2026-11-23T00:00:00+05:30", True, True, False),
        ("2026-11-27T00:00:00+05:30", True, True, True),  # a packed item is late too once its date has passed
    ],
)
def test_an_undelivered_item_is_late_once_its_estimated_date_has_passed(now, hoodie, tunic, shirt):
    store = store_at(now)

    assert [store.item(item_id)["late"] for item_id in ("ITM-1013", "ITM-1014", "ITM-1002")] == [hoodie, tunic, shirt]


def test_late_is_read_from_current_rows(store):
    assert [item["id"] for item in store.items() if store.item(item["id"])["late"]] == ["ITM-1013"]

    store.db.execute("UPDATE items SET eta = '2026-11-20' WHERE id = 'ITM-1001'")
    store.db.execute("UPDATE items SET status = 'delivered', delivered_at = '2026-11-21' WHERE id = 'ITM-1013'")
    store.db.execute("UPDATE items SET eta = NULL WHERE id = 'ITM-1014'")

    assert store.item("ITM-1001")["late"] is True
    assert store.item("ITM-1013")["late"] is False  # delivered, however late it came
    assert store.item("ITM-1014")["late"] is False  # no estimate, so nothing to be late against
    # Only an item still on its way can be late: the cancelled skirt's date passed long ago.
    assert store.item("ITM-1015")["eta"] < store.today and store.item("ITM-1015")["late"] is False


def test_a_non_returnable_item_has_no_return_window(store):
    blazer = store.item("ITM-1012")

    assert (blazer["status"], blazer["delivered_at"]) == ("delivered", "2026-11-16")
    assert (blazer["returnable"], blazer["non_returnable_reason"]) == (False, "final_sale")
    assert (blazer["return_deadline"], blazer["can_return"], blazer["permitted_actions"]) == (None, False, [])

    store.db.execute("UPDATE items SET non_returnable_reason = NULL WHERE id = 'ITM-1012'")
    store.db.execute("UPDATE items SET non_returnable_reason = 'innerwear' WHERE id = 'ITM-1003'")

    blazer, jacket = store.item("ITM-1012"), store.item("ITM-1003")
    assert (blazer["returnable"], blazer["return_deadline"], blazer["permitted_actions"]) == (
        True, "2026-11-23", ["return"])
    assert (jacket["returnable"], jacket["return_deadline"], jacket["permitted_actions"]) == (False, None, [])


def test_prepaid_follows_the_payment_method_and_the_policy(store):
    assert [item["id"] for item in store.items() if not store.item(item["id"])["prepaid"]] == ["ITM-1010", "ITM-1011"]

    store.db.execute("UPDATE orders SET payment_method = 'Cash on delivery' WHERE id = 'ORD-1003'")

    jacket = store.item("ITM-1003")
    assert (jacket["prepaid"], jacket["can_return"], jacket["permitted_actions"]) == (False, True, [])

    policy = copy.deepcopy(POLICY)
    policy["cash_on_delivery"]["payment_method"] = "Pay at the door"
    assert Store(policy=policy).item("ITM-1011")["prepaid"] is True


def test_eligibility_is_read_from_current_rows(store):
    store.db.execute("UPDATE items SET status = 'shipped', shipped_at = '2026-11-21' WHERE id = 'ITM-1002'")
    store.db.execute("UPDATE items SET delivered_at = '2026-11-12' WHERE id = 'ITM-1003'")

    shirt, jacket = store.item("ITM-1002"), store.item("ITM-1003")

    assert (shirt["status"], shirt["can_cancel"], shirt["permitted_actions"]) == ("shipped", False, [])
    assert (jacket["return_deadline"], jacket["can_return"], jacket["permitted_actions"]) == ("2026-11-19", False, [])


def test_eligibility_follows_the_policy():
    policy = copy.deepcopy(POLICY)
    policy["returns"]["window_days"] = 3
    policy["cancellation"]["cancellable_statuses"] = []
    store = Store(policy=policy)

    assert (store.item("ITM-1003")["return_deadline"], store.item("ITM-1003")["can_return"]) == ("2026-11-20", False)
    assert store.item("ITM-1002")["permitted_actions"] == []


def test_history_is_per_item_oldest_first_and_limited(store):
    for n in range(4):
        store.add_message("customer", f"kurta question {n}", "ITM-1001")
        store.add_message("agent", f"kurta answer {n}", "ITM-1001")
    store.add_message("customer", "shirt question", "ITM-1002")
    store.add_message("customer", "general question")
    store.add_message("agent", "general answer")

    assert store.history("ITM-1002") == [{"role": "customer", "text": "shirt question"}]
    assert store.history(None) == [
        {"role": "customer", "text": "general question"},
        {"role": "agent", "text": "general answer"},
    ]
    assert [message["text"] for message in store.history("ITM-1001")] == [
        "kurta question 1", "kurta answer 1",
        "kurta question 2", "kurta answer 2",
        "kurta question 3", "kurta answer 3",
    ]
    assert store.history("ITM-1001", limit=2) == [
        {"role": "customer", "text": "kurta question 3"},
        {"role": "agent", "text": "kurta answer 3"},
    ]
    assert store.history("ITM-1003") == []
    assert store.customer_message_count() == 6


def test_message_role_is_customer_or_agent(store):
    """A misspelt role must fail loudly: only "customer" rows count toward the session's message limit."""
    with pytest.raises(sqlite3.IntegrityError):
        store.add_message("user", "hello")

    assert store.customer_message_count() == 0


def test_tickets_get_sequential_references_and_are_stored(store):
    first = store.create_ticket("delivery_scan_disputed", "Jacket not received", "ITM-1003")
    second = store.create_ticket("handoff", "General question")

    assert (first, second) == ("TKT-0001", "TKT-0002")
    rows = store.db.execute("SELECT * FROM tickets ORDER BY ref").fetchall()
    assert [(row["ref"], row["item_id"], row["kind"], row["summary"]) for row in rows] == [
        ("TKT-0001", "ITM-1003", "delivery_scan_disputed", "Jacket not received"),
        ("TKT-0002", None, "handoff", "General question"),
    ]
    assert all(row["created_at"] for row in rows)


def test_failed_ticket_write_raises_ticket_error_and_stores_nothing(store):
    store.db.execute(
        "CREATE TRIGGER ticket_write_fails BEFORE INSERT ON tickets BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )

    with pytest.raises(TicketError):
        store.create_ticket("handoff", "cannot be stored")

    assert store.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
    store.db.execute("DROP TRIGGER ticket_write_fails")
    assert store.create_ticket("handoff", "stored on retry") == "TKT-0001"


def test_offer_records_a_pending_action_and_changes_nothing(store):
    token, other = store.offer_action("ITM-1002", "cancel"), store.offer_action("ITM-1002", "cancel")

    assert token != other and len(token) >= 20
    row = store.db.execute("SELECT * FROM actions WHERE token = ?", (token,)).fetchone()
    assert (row["item_id"], row["action"], row["outcome"]) == ("ITM-1002", "cancel", None)
    assert store.item("ITM-1002")["status"] == "packed"


def test_only_cancel_and_return_can_be_offered(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.offer_action("ITM-1002", "refund")


def test_confirmed_cancellation_cancels_the_item_and_starts_the_refund(store):
    token = store.offer_action("ITM-1002", "cancel")

    outcome = store.confirm_action(token)

    assert set(outcome) == {"status", "action", "item_id", "reply"}
    assert (outcome["status"], outcome["action"], outcome["item_id"]) == ("done", "cancel", "ITM-1002")
    facts = store.item("ITM-1002")
    assert (facts["status"], facts["permitted_actions"]) == ("cancelled", [])
    # Seven business days after Saturday 2026-11-21, skipping two weekends, end on Tuesday 2026-12-01.
    assert refund_rows(store, "ITM-1002") == [{
        "item_id": "ITM-1002", "amount_inr": 1299, "status": "initiated", "initiated_at": "2026-11-21",
        "expected_by": "2026-12-01", "credited_at": None, "method": "UPI", "reason": "cancellation",
    }]
    assert facts["refund"]["overdue"] is False
    assert all(part in outcome["reply"] for part in ("cancelled", "₹1,299", "December 1"))
    stored = store.db.execute("SELECT outcome FROM actions WHERE token = ?", (token,)).fetchone()["outcome"]
    assert json.loads(stored) == outcome


def test_cancelling_a_cash_on_delivery_item_starts_no_refund(store):
    # The policy refunds a cancelled PREPAID item; nothing was paid on a cash-on-delivery order.
    store.db.execute("UPDATE orders SET payment_method = 'Cash on delivery' WHERE id = ?", (store.item("ITM-1002")["order_id"],))

    outcome = store.confirm_action(store.offer_action("ITM-1002", "cancel"))

    assert outcome["status"] == "done"
    assert store.item("ITM-1002")["status"] == "cancelled"
    assert refund_rows(store, "ITM-1002") == []
    assert "cancelled" in outcome["reply"] and "refund" not in outcome["reply"].lower() and "₹" not in outcome["reply"]


def test_cancelling_the_cash_on_delivery_belt_starts_no_refund(store):
    outcome = store.confirm_action(store.offer_action("ITM-1010", "cancel"))

    belt = store.item("ITM-1010")
    assert (outcome["status"], belt["status"], belt["refund"]) == ("done", "cancelled", None)
    assert refund_rows(store, "ITM-1010") == []
    assert "₹" not in outcome["reply"]


def test_cancellation_refund_is_the_price_of_the_item_and_never_its_fees():
    fixtures = copy.deepcopy(FIXTURES)
    belt_order = fixtures["orders"][7]
    belt_order.update(payment_method="UPI", cod_fee_inr=0)  # a prepaid order that paid for shipping
    store = Store(fixtures)

    assert store.confirm_action(store.offer_action("ITM-1010", "cancel"))["status"] == "done"

    belt = store.item("ITM-1010")
    assert (belt["shipping_fee_inr"], belt["order_total_inr"], belt["refund"]["amount_inr"]) == (79, 678, 599)


def test_cancellation_refund_follows_the_clock_the_order_and_the_policy():
    fixtures = copy.deepcopy(FIXTURES)
    fixtures["now"] = "2026-11-23T09:00:00+05:30"  # a Monday
    fixtures["orders"][1]["payment_method"] = "Credit card"
    policy = copy.deepcopy(POLICY)
    policy["refunds"]["business_days"] = 2
    by_default, by_policy = Store(fixtures), Store(fixtures, policy)

    for store in (by_default, by_policy):
        assert store.confirm_action(store.offer_action("ITM-1002", "cancel"))["status"] == "done"

    refund = by_default.item("ITM-1002")["refund"]
    assert (refund["initiated_at"], refund["expected_by"]) == ("2026-11-23", "2026-12-02")
    assert (refund["method"], refund["reason"]) == ("Credit card", "cancellation")
    assert by_policy.item("ITM-1002")["refund"]["expected_by"] == "2026-11-25"


def test_confirmed_return_requests_the_return_and_schedules_a_pickup(store):
    outcome = store.confirm_action(store.offer_action("ITM-1003", "return"))

    assert (outcome["status"], outcome["action"], outcome["item_id"]) == ("done", "return", "ITM-1003")
    facts = store.item("ITM-1003")
    assert (facts["status"], facts["permitted_actions"]) == ("return_requested", [])
    rows = store.db.execute("SELECT * FROM returns WHERE item_id = 'ITM-1003'").fetchall()
    assert [tuple(row) for row in rows] == [("ITM-1003", "requested", "2026-11-21", "2026-11-23", "scheduled")]
    assert facts["return"] == {
        "status": "requested", "requested_at": "2026-11-21", "pickup_on": "2026-11-23", "pickup_status": "scheduled"}
    assert facts["refund"] is None  # the refund starts after inspection, not at confirmation
    assert "November 23" in outcome["reply"]


@pytest.mark.parametrize("item_id", ["ITM-1011", "ITM-1012"], ids=["cash_on_delivery", "final_sale"])
def test_a_return_the_bot_may_not_take_is_never_committed(store, item_id):
    """Support arranges a cash-on-delivery return, and a final-sale item cannot be returned at all."""
    outcome = store.confirm_action(store.offer_action(item_id, "return"))

    assert (outcome["status"], outcome["action"], outcome["item_id"]) == ("rejected", "return", item_id)
    facts = store.item(item_id)
    assert (facts["status"], facts["return"]) == ("delivered", None)
    assert store.db.execute("SELECT COUNT(*) FROM returns WHERE item_id = ?", (item_id,)).fetchone()[0] == 0


def test_repeated_confirmation_returns_the_original_result(store):
    token = store.offer_action("ITM-1002", "cancel")
    first = store.confirm_action(token)

    assert store.confirm_action(token) == first
    assert len(refund_rows(store, "ITM-1002")) == 1

    # Even when the record changes afterwards, the stored outcome is returned and nothing is applied again.
    store.db.execute("DELETE FROM refunds WHERE item_id = 'ITM-1002'")
    store.db.execute("UPDATE items SET status = 'packed' WHERE id = 'ITM-1002'")

    assert store.confirm_action(token) == first
    assert store.item("ITM-1002")["status"] == "packed"
    assert refund_rows(store, "ITM-1002") == []


@pytest.mark.parametrize("shipped_at", ["2026-11-21", None], ids=["ship_date_recorded", "status_only"])
def test_item_that_shipped_after_the_offer_is_not_cancelled(store, shipped_at):
    token = store.offer_action("ITM-1002", "cancel")
    store.db.execute("UPDATE items SET status = 'shipped', shipped_at = ? WHERE id = 'ITM-1002'", (shipped_at,))

    outcome = store.confirm_action(token)

    assert (outcome["status"], outcome["action"], outcome["item_id"]) == ("rejected", "cancel", "ITM-1002")
    assert "shipped" in outcome["reply"] and "no longer be cancelled" in outcome["reply"]
    assert ("November 21" in outcome["reply"]) is (shipped_at is not None)
    facts = store.item("ITM-1002")
    assert (facts["status"], facts["shipped_at"], facts["refund"]) == ("shipped", shipped_at, None)
    assert store.confirm_action(token) == outcome

    # The rejection is the stored result, not a fresh check: it stands even when the item is cancellable again.
    store.db.execute("UPDATE items SET status = 'packed', shipped_at = NULL WHERE id = 'ITM-1002'")

    assert store.confirm_action(token) == outcome
    assert store.item("ITM-1002")["status"] == "packed" and refund_rows(store, "ITM-1002") == []


def test_return_is_rejected_when_the_window_closed_after_the_offer(store):
    token = store.offer_action("ITM-1004", "return")
    store.db.execute("UPDATE items SET delivered_at = '2026-11-13' WHERE id = 'ITM-1004'")

    outcome = store.confirm_action(token)

    assert (outcome["status"], outcome["action"]) == ("rejected", "return")
    assert "November 20" in outcome["reply"]
    facts = store.item("ITM-1004")
    assert (facts["status"], facts["return"]) == ("delivered", None)


def test_second_offer_for_an_item_is_rejected_once_the_first_is_confirmed(store):
    first, second = store.offer_action("ITM-1002", "cancel"), store.offer_action("ITM-1002", "cancel")

    assert store.confirm_action(first)["status"] == "done"
    outcome = store.confirm_action(second)

    assert outcome["status"] == "rejected" and "already been cancelled" in outcome["reply"]
    assert len(refund_rows(store, "ITM-1002")) == 1


def test_unknown_token_and_another_sessions_token_confirm_nothing(store):
    other = Store()
    token = other.offer_action("ITM-1002", "cancel")

    assert store.confirm_action("no-such-token") is None
    assert store.confirm_action(token) is None
    assert store.item("ITM-1002")["status"] == "packed"
    assert other.item("ITM-1002")["status"] == "packed"


def test_change_and_outcome_are_one_transaction(store):
    token = store.offer_action("ITM-1002", "cancel")
    # A refund row that should not exist makes the second write of the cancellation fail.
    store.db.execute(
        "INSERT INTO refunds VALUES ('ITM-1002', 1, 'initiated', '2026-11-01', '2026-11-10', NULL, 'UPI', NULL)"
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.confirm_action(token)

    assert store.item("ITM-1002")["status"] == "packed"
    assert store.db.execute("SELECT outcome FROM actions WHERE token = ?", (token,)).fetchone()["outcome"] is None
    assert not store.db.in_transaction


def test_failed_outcome_write_rolls_the_change_back(store):
    token = store.offer_action("ITM-1002", "cancel")
    store.db.execute(
        "CREATE TRIGGER outcome_write_fails BEFORE UPDATE OF outcome ON actions"
        " BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )

    with pytest.raises(sqlite3.IntegrityError):
        store.confirm_action(token)

    assert store.item("ITM-1002")["status"] == "packed" and refund_rows(store, "ITM-1002") == []
    assert store.db.execute("SELECT outcome FROM actions WHERE token = ?", (token,)).fetchone()["outcome"] is None
    assert not store.db.in_transaction


def test_concurrent_confirmations_commit_once(store):
    token = store.offer_action("ITM-1002", "cancel")
    outcomes = []
    threads = [threading.Thread(target=lambda: outcomes.append(store.confirm_action(token))) for _ in range(8)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 8 and outcomes[0]["status"] == "done"
    assert all(outcome == outcomes[0] for outcome in outcomes)
    assert len(refund_rows(store, "ITM-1002")) == 1


def test_traces_round_trip_oldest_first(store):
    traces = [{"route": "rule", "reply": "₹1,499", "scores": {"supported": 0.9}}, {"route": "handoff", "scores": None}]

    for trace in traces:
        store.add_trace(trace)

    assert store.traces() == traces


def test_stores_are_isolated():
    one, two = Store(), Store()

    one.confirm_action(one.offer_action("ITM-1002", "cancel"))
    one.add_message("customer", "hello", "ITM-1001")
    one.create_ticket("handoff", "only in the first store")
    one.add_trace({"route": "rule"})
    one.policy["returns"]["window_days"] = 1
    one.customer["name"] = "Someone else"

    assert one.db is not two.db and one.lock is not two.lock
    shirt = two.item("ITM-1002")
    assert (shirt["status"], shirt["refund"]) == ("packed", None)
    assert two.history("ITM-1001") == [] and two.customer_message_count() == 0
    assert two.traces() == []
    assert two.create_ticket("handoff", "first in the second store") == "TKT-0001"
    assert two.item("ITM-1003")["can_return"] is True
    assert two.customer == {"name": "Meera Iyer"}
    assert POLICY["returns"]["window_days"] == 7 and FIXTURES["customer"] == {"name": "Meera Iyer"}


def test_dropped_store_closes_its_database():
    store = Store()
    db = store.db

    del store  # what evicting a session does

    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")
