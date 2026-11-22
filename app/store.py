"""One demo session's records: orders, messages, tickets, pending actions and traces in a private SQLite database."""
import copy
import json
import secrets
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from app import rules

_DATA = Path(__file__).resolve().parent.parent / "data"
FIXTURES: dict = json.loads((_DATA / "fixtures.json").read_text(encoding="utf-8"))
POLICY: dict = json.loads((_DATA / "policy.json").read_text(encoding="utf-8"))

_SCHEMA = """
CREATE TABLE orders(id TEXT PRIMARY KEY, placed_at TEXT, payment_method TEXT, shipping_fee_inr INTEGER,
                    cod_fee_inr INTEGER, delivery_city TEXT, delivery_pincode TEXT);
CREATE TABLE items(id TEXT PRIMARY KEY, order_id TEXT, name TEXT, noun TEXT, variant TEXT, price_inr INTEGER,
                   status TEXT, shipped_at TEXT, eta TEXT, delivered_at TEXT, category TEXT, colour TEXT, size TEXT,
                   non_returnable_reason TEXT, courier TEXT, tracking_number TEXT, delivery_attempts INTEGER);
CREATE TABLE events(id INTEGER PRIMARY KEY, item_id TEXT, at TEXT, kind TEXT, note TEXT);
CREATE TABLE returns(item_id TEXT PRIMARY KEY, status TEXT, requested_at TEXT, pickup_on TEXT, pickup_status TEXT);
CREATE TABLE refunds(item_id TEXT PRIMARY KEY, amount_inr INTEGER, status TEXT, initiated_at TEXT,
                     expected_by TEXT, credited_at TEXT, method TEXT, reason TEXT);
CREATE TABLE tickets(ref TEXT PRIMARY KEY, item_id TEXT, kind TEXT, summary TEXT, created_at TEXT);
CREATE TABLE actions(token TEXT PRIMARY KEY, item_id TEXT,
                     action TEXT NOT NULL CHECK (action IN ('cancel', 'return')), created_at TEXT, outcome TEXT);
CREATE TABLE messages(id INTEGER PRIMARY KEY, role TEXT NOT NULL CHECK (role IN ('customer', 'agent')),
                      text TEXT, item_id TEXT);
CREATE TABLE traces(id INTEGER PRIMARY KEY, data TEXT);
"""


# The fields the 2026-11-22 policy added, and what a fixture written before it (the archived evaluation cases)
# is taken to hold: a fee that was not recorded was not charged, a return on record has its pickup scheduled,
# and the rest is unknown.
_DEFAULTS = {
    "order": {"shipping_fee_inr": 0, "cod_fee_inr": 0, "delivery_city": None, "delivery_pincode": None},
    "item": {"category": None, "colour": None, "size": None, "non_returnable_reason": None, "courier": None,
             "tracking_number": None, "delivery_attempts": None},
    "return": {"pickup_status": "scheduled"},
    "refund": {"reason": None},
}


class TicketError(Exception):
    """A ticket could not be stored, so there is no reference to show."""


def _add_business_days(start: date, days: int) -> date:
    # ponytail: skips Saturdays and Sundays only. Add a public-holiday calendar if refund dates must match
    # bank working days.
    day = start
    while days > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            days -= 1
    return day


class Store:
    """The records of one session. The caller holds .lock for the whole of a request."""

    def __init__(self, fixtures: dict | None = None, policy: dict | None = None) -> None:
        # isolation_level=None: each statement commits at once, so SQL run through .db takes effect immediately.
        # confirm_action opens the one explicit transaction.
        self.db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        fixtures = FIXTURES if fixtures is None else fixtures
        # Copies, so nothing a session changes can reach another session or the module constants.
        self.policy: dict = copy.deepcopy(POLICY if policy is None else policy)
        self.customer: dict = copy.deepcopy(fixtures["customer"])
        self.now = datetime.fromisoformat(fixtures["now"])
        self.today = self.now.date().isoformat()
        self.db.executescript(_SCHEMA)
        for order in fixtures["orders"]:
            self.db.execute(
                "INSERT INTO orders VALUES (:id, :placed_at, :payment_method, :shipping_fee_inr, :cod_fee_inr,"
                " :delivery_city, :delivery_pincode)",
                {**_DEFAULTS["order"], **order},
            )
            for item in order["items"]:
                key = {"item_id": item["id"]}
                self.db.execute(
                    "INSERT INTO items VALUES (:id, :order_id, :name, :noun, :variant, :price_inr, :status,"
                    " :shipped_at, :eta, :delivered_at, :category, :colour, :size, :non_returnable_reason,"
                    " :courier, :tracking_number, :delivery_attempts)",
                    {**_DEFAULTS["item"], **item, "order_id": order["id"]},
                )
                self.db.executemany(
                    "INSERT INTO events(item_id, at, kind, note) VALUES (:item_id, :at, :kind, :note)",
                    [{**key, **event} for event in item["events"]],
                )
                if item["return"]:
                    self.db.execute(
                        "INSERT INTO returns VALUES (:item_id, :status, :requested_at, :pickup_on, :pickup_status)",
                        {**key, **_DEFAULTS["return"], **item["return"]},
                    )
                if item["refund"]:
                    self.db.execute(
                        "INSERT INTO refunds VALUES (:item_id, :amount_inr, :status, :initiated_at, :expected_by,"
                        " :credited_at, :method, :reason)",
                        {**key, **_DEFAULTS["refund"], **item["refund"]},
                    )

    def __del__(self) -> None:
        # Python 3.13 warns about a connection that is dropped while open, and every evicted session drops one.
        self.db.close()

    def _rows(self, sql: str, *params: object) -> list[dict]:
        return [dict(row) for row in self.db.execute(sql, params)]

    def _late(self, item: dict) -> bool:
        """Still on its way although its estimated delivery date has passed."""
        return item["status"] in ("packed", "shipped") and item["eta"] is not None and item["eta"] < self.today

    def orders(self) -> list[dict]:
        """Every order with its items, in fixture order."""
        # ponytail: one items query per order, fine for a demo account. Join and group if order counts grow.
        return [
            {**order, "items": [{**item, "late": self._late(item)} for item in self._rows(
                "SELECT i.id, i.order_id, i.name, i.noun, i.status, i.variant, i.price_inr, i.eta, i.delivered_at,"
                " i.category, i.colour, i.size, o.payment_method, i.non_returnable_reason, i.courier,"
                " i.tracking_number FROM items i JOIN orders o ON o.id = i.order_id"
                " WHERE i.order_id = ? ORDER BY i.rowid",
                order["id"],
            )]}
            for order in self._rows("SELECT id, placed_at FROM orders ORDER BY rowid")
        ]

    def items(self) -> list[dict]:
        """Every item in brief, in fixture order."""
        return self._rows("SELECT id, order_id, name, noun, status FROM items ORDER BY rowid")

    def item(self, item_id: str) -> dict | None:
        """The facts of one item, computed from the rows as they are now. None when it is not in this session.

        Beside the stored fields of the item, its order and its shipment, these are worked out here: returnable,
        prepaid, order_total_inr, late, return_deadline, can_cancel, can_return, permitted_actions and a refund's
        overdue. delivery_attempts counts the courier's attempts so far, the successful one included; like the
        other shipment and product fields it is None when an old-style fixture did not record it.
        """
        # ponytail: order_total_inr is what the order came to at checkout, every item and both fees. On a
        # cash-on-delivery order with a cancelled item it is more than is still payable; subtract the cancelled
        # items if such an order must quote the amount due.
        found = self._rows(
            "SELECT i.id, i.order_id, i.name, i.noun, i.variant, i.category, i.colour, i.size, i.price_inr,"
            " i.non_returnable_reason, i.status, o.placed_at, o.payment_method, o.shipping_fee_inr, o.cod_fee_inr,"
            " (SELECT SUM(price_inr) FROM items WHERE order_id = o.id) + o.shipping_fee_inr + o.cod_fee_inr"
            " AS order_total_inr, o.delivery_city, o.delivery_pincode, i.shipped_at, i.eta, i.delivered_at,"
            " i.courier, i.tracking_number, i.delivery_attempts"
            " FROM items i JOIN orders o ON o.id = i.order_id WHERE i.id = ?",
            item_id,
        )
        if not found:
            return None
        facts = found[0]
        returnable = facts["non_returnable_reason"] is None
        # Cash on delivery is paid to the courier; every other payment method is charged when the order is placed.
        prepaid = facts["payment_method"] != self.policy["cash_on_delivery"]["payment_method"]
        deadline = None  # an item that cannot be returned has no return window
        if facts["delivered_at"] and returnable:
            window = timedelta(days=self.policy["returns"]["window_days"])
            deadline = (date.fromisoformat(facts["delivered_at"]) + window).isoformat()
        can_cancel = facts["status"] in self.policy["cancellation"]["cancellable_statuses"]
        # ISO dates compare correctly as strings. The last eligible day is included.
        can_return = facts["status"] == "delivered" and deadline is not None and self.today <= deadline
        refunds = self._rows(
            "SELECT amount_inr, status, initiated_at, expected_by, credited_at, method, reason"
            " FROM refunds WHERE item_id = ?",
            item_id,
        )
        for refund in refunds:
            refund["overdue"] = refund["status"] == "initiated" and self.today > refund["expected_by"]
        returns = self._rows(
            "SELECT status, requested_at, pickup_on, pickup_status FROM returns WHERE item_id = ?", item_id
        )
        facts.update({
            "returnable": returnable,
            "prepaid": prepaid,
            "late": self._late(facts),
            "return_deadline": deadline,
            "can_cancel": can_cancel,
            "can_return": can_return,
            # What the customer can confirm here. Support arranges the return of a cash-on-delivery item, whose
            # refund needs bank details (policy: returns, escalation cod_return), so it is never offered.
            "permitted_actions": ["cancel"] if can_cancel else ["return"] if can_return and prepaid else [],
            "refund": refunds[0] if refunds else None,
            "return": returns[0] if returns else None,
            "events": self._rows("SELECT at, kind, note FROM events WHERE item_id = ? ORDER BY at, id", item_id),
        })
        return facts

    def history(self, item_id: str | None, limit: int = 6) -> list[dict]:
        """The last `limit` messages about this item (None: about no item), oldest first."""
        newest_first = self._rows(
            "SELECT role, text FROM messages WHERE item_id IS ? ORDER BY id DESC LIMIT ?", item_id, limit
        )
        return newest_first[::-1]

    def add_message(self, role: str, text: str, item_id: str | None = None) -> None:
        self.db.execute("INSERT INTO messages(role, text, item_id) VALUES (?, ?, ?)", (role, text, item_id))

    def customer_message_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE role = 'customer'").fetchone()[0]

    def create_ticket(self, kind: str, summary: str, item_id: str | None = None) -> str:
        """Store a ticket, then return its reference. Raises TicketError when the write fails."""
        try:
            # ponytail: the next number is the row count, which holds while tickets are never deleted and the
            # session lock serialises writers; a clash fails the insert and surfaces as TicketError.
            # Upgrade path: a counter row.
            count = self.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
            ref = f"TKT-{count + 1:04d}"
            self.db.execute(
                "INSERT INTO tickets VALUES (?, ?, ?, ?, ?)", (ref, item_id, kind, summary, self.now.isoformat())
            )
        except sqlite3.Error as error:
            raise TicketError(f"ticket was not stored: {error}") from error
        return ref

    def offer_action(self, item_id: str, action: str) -> str:
        """Record a pending cancel or return and return the unguessable token that confirms it."""
        token = secrets.token_urlsafe(16)
        self.db.execute("INSERT INTO actions VALUES (?, ?, ?, ?, NULL)", (token, item_id, action, self.now.isoformat()))
        return token

    def confirm_action(self, token: str) -> dict | None:
        """Apply a pending action once. Every later call with the same token returns the stored outcome."""
        # Re-entrant, so the check and the commit stay one step even for a caller that does not hold the lock.
        with self.lock:
            pending = self._rows("SELECT item_id, action, outcome FROM actions WHERE token = ?", token)
            if not pending:
                return None
            item_id, action, stored = pending[0]["item_id"], pending[0]["action"], pending[0]["outcome"]
            if stored is not None:
                return json.loads(stored)
            today = date.fromisoformat(self.today)
            with self.db:  # commits the transaction opened below, or rolls all of it back on an error
                self.db.execute("BEGIN")
                facts = self.item(item_id)
                done = action in facts["permitted_actions"]  # rechecked against the rows as they are now
                if done and action == "cancel":
                    self.db.execute("UPDATE items SET status = 'cancelled' WHERE id = ?", (item_id,))
                    # Nothing has been paid on cash on delivery. A refund is the price of the item, never a fee.
                    if facts["prepaid"]:
                        expected_by = _add_business_days(today, self.policy["refunds"]["business_days"])
                        self.db.execute(
                            "INSERT INTO refunds VALUES (?, ?, 'initiated', ?, ?, NULL, ?, 'cancellation')",
                            (item_id, facts["price_inr"], self.today, expected_by.isoformat(), facts["payment_method"]),
                        )
                elif done:
                    pickup_on = today + timedelta(days=2)
                    self.db.execute("UPDATE items SET status = 'return_requested' WHERE id = ?", (item_id,))
                    self.db.execute(
                        "INSERT INTO returns VALUES (?, 'requested', ?, ?, 'scheduled')",
                        (item_id, self.today, pickup_on.isoformat()),
                    )
                outcome = {
                    "status": "done" if done else "rejected",
                    "action": action,
                    "item_id": item_id,
                    "reply": rules.confirmation_reply(action, done, self.item(item_id), self.policy, self.today),
                }
                self.db.execute("UPDATE actions SET outcome = ? WHERE token = ?", (json.dumps(outcome), token))
            return outcome

    def add_trace(self, trace: dict) -> None:
        self.db.execute("INSERT INTO traces(data) VALUES (?)", (json.dumps(trace),))

    def traces(self) -> list[dict]:
        """Every trace, oldest first."""
        return [json.loads(row["data"]) for row in self.db.execute("SELECT data FROM traces ORDER BY id")]
