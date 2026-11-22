"""Routes one customer message through the six steps of the routing contract and records how it went."""
import functools
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from app import fallback, jev, rules
from app.store import Store, TicketError

MAX_MESSAGES = 30  # customer messages per session

_log = logging.getLogger("jev_demo.trace")


def decide(scores: dict, thresholds: dict, need_complete: bool = True) -> str:
    """Turn Jev's three scores into "handoff", "accept" or "reject". A required handoff outranks a good draft."""
    if scores["requires_human"] >= thresholds["requires_human_handoff"]:
        return "handoff"
    if (
        scores["supported"] >= thresholds["supported_min"]
        and (scores["complete"] >= thresholds["complete_min"] or not need_complete)
        and scores["requires_human"] < thresholds["requires_human_low"]
    ):
        return "accept"
    return "reject"


def handle(store: Store, message: str, item_id: str | None = None, quick_action: str | None = None) -> dict:
    """Route one customer message. Returns the chat payload and records one trace.

    Fail closed: a rule draft or a fallback text is sent only after decide() returns "accept" on the scores that
    jev.score gave for that exact text. Every other route replies with a fixed text.
    """
    trace = _new_trace(message)
    started = time.monotonic()
    end = functools.partial(_end, store, trace, started)  # every route leaves through end or ticket
    ticket = functools.partial(_ticket, store, trace, started)
    policy, thresholds = store.policy, jev.GATE["thresholds"]

    # 0. The session's message limit. Past it no model is called and nothing is stored.
    if store.customer_message_count() >= MAX_MESSAGES:
        reason = f"The session has reached its limit of {MAX_MESSAGES} customer messages."
        return end("limit", reason, rules.LIMIT_REACHED, limit_reached=True)

    # 1. Resolve the item. When the answer depends on it and it is not clear which one is meant, ask.
    found = rules.topics(message, quick_action)  # every help topic the message raises, in the customer's order
    trace["intent"] = ", ".join(found) or None
    item = None
    if item_id is not None:
        item = store.item(item_id)
        if item is None:
            reason = "The selected item is not in this session's orders, so the customer was asked which one is meant."
            return end("clarify_item", reason, rules.UNKNOWN_ITEM, item_choices=store.items())
    else:
        matches = rules.match_items(message, store.items())
        if len(matches) == 1:
            item = store.item(matches[0]["id"])
        elif rules.needs_item(found, message, named=len(matches)):
            reason = (
                f"The answer depends on the item and the message matches {len(matches)} items,"
                " so the customer was asked to choose one."
            )
            return end("clarify_item", reason, rules.CHOOSE_ITEM, item_choices=matches or store.items())
        # Otherwise a general question, small talk or nothing the rules know: it proceeds without an order.
    if item:
        trace["item_id"] = item["id"]

    # 2. Backend rules that mandate an outcome. No model is consulted. Every topic in the message is tried, not
    # only the first: "Where is it? It says delivered but I never got it" is still a disputed delivery scan.
    mandate = rules.mandated(found, item, policy)
    if mandate:
        return ticket("mandated_ticket", f"Backend rule {mandate['rule']} applies", mandate["rule"], mandate["summary"])

    # 3. Draft a candidate from the records and the policy, one answer to every topic the rules found. If there
    # is one, Jev scores it on the three questions.
    history = store.history(trace["item_id"])  # read before this message is stored
    turn = (message, history, item, policy, store.today)  # what both providers are given
    candidate = trace["candidate"] = rules.candidate(found, item, policy)
    rejected = None
    if candidate:
        draft = candidate["draft"]
        scores = trace["scores"] = _score(trace, turn, draft)
        if scores is None:
            return ticket("gate_error", "The rule draft was not sent because Jev gave no usable scores for it")
        # 4. A required handoff comes first, an accepted draft is sent, and a rejected one goes to the fallback.
        decision = trace["decision"] = decide(scores, thresholds)
        if decision == "handoff":
            return ticket("handoff", "Jev scored the situation as needing a person")
        if decision == "accept":
            reason = "Jev accepted the rule draft, so it was sent."
            return end("rule", reason, draft, **_offer(store, item, candidate["action"]))
        rejected = {"draft": draft, "scores": scores}

    # 5. One fallback attempt: check it against the backend, then score it once.
    try:
        with _timed(trace, "fallback"):
            fb = fallback.draft(*turn, rejected)
    except Exception as error:  # FallbackError, or anything unexpected: both fail closed on a ticket
        trace["error"], trace["cost_usd"] = f"fallback: {_cause(error, fallback.FallbackError)}", None
        return ticket("handoff", "The fallback gave no usable reply")
    trace["cost_usd"] = _add_cost(trace["cost_usd"], fb["cost_usd"])
    try:
        problems = fallback.problems(fb, item, policy, store.today)
    except Exception as error:  # a reply the backend could not check is unverified, so it counts as failing
        logging.exception("The backend check of a fallback reply raised; the route fails closed")
        problems = [f"the backend check failed: {type(error).__name__}"]
    trace["fallback"] = {"kind": fb["kind"], "text": fb["text"], "action": fb["action"], "problems": problems}
    if fb["kind"] not in ("reply", "clarify"):  # "handoff"; any other kind fails closed the same way
        return ticket("handoff", "The fallback asked for a person")
    if problems:
        return ticket("handoff", "The fallback reply failed backend validation")
    scores = trace["fallback_scores"] = _score(trace, turn, fb["text"])
    if scores is None:
        return ticket("gate_error", "The fallback reply was not sent because Jev gave no usable scores for it")
    # Only a question is excused from the completeness cutoff. The fallback's own "clarify" label is not enough:
    # a statement under that label is treated as a reply.
    # ponytail: "is a question" is read off the last character, so a statement with a question tacked on still
    # skips the cutoff. Upgrade path: have Jev judge whether the text is one specific question.
    is_question = fb["kind"] == "clarify" and fb["text"].rstrip().endswith("?")
    decision = trace["fallback_decision"] = decide(scores, thresholds, need_complete=not is_question)
    if decision != "accept":
        return ticket("handoff", "Jev did not accept the fallback reply")
    if is_question:
        return end("fallback_clarify", "Jev accepted the fallback's clarifying question, so it was sent.", fb["text"])
    reason = "Jev accepted the fallback reply, so it was sent."
    return end("fallback", reason, fb["text"], **_offer(store, item, fb["action"]))


def confirm(store: Store, token: str) -> dict | None:
    """6. Carry out a confirmed cancellation or return. The store rechecks eligibility and commits it once."""
    return store.confirm_action(token)


def _new_trace(message: str) -> dict:
    """A trace with every key present. The steps fill in what applies to the route taken."""
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "item_id": None,
        "intent": None,
        "route": None,
        "reason": None,
        "candidate": None,
        "scores": None,
        "decision": None,
        "fallback": None,
        "fallback_scores": None,
        "fallback_decision": None,
        "error": None,
        "ticket_ref": None,
        "calls": {"jev": 0, "fallback": 0},
        "elapsed_ms": {"jev": 0, "fallback": 0, "total": 0},
        "cost_usd": 0.0,
        "reply": None,
    }


def _end(store: Store, trace: dict, started: float, route: str, reason: str, reply: str, **extras: object) -> dict:
    """Close the route: store the turn, record the trace and build the chat payload."""
    trace.update(route=route, reason=reason, reply=reply)
    trace["elapsed_ms"]["total"] = int((time.monotonic() - started) * 1000)
    if route != "limit":  # past the limit nothing is stored, so a client that keeps posting cannot grow the session
        store.add_message("customer", trace["message"], trace["item_id"])
        store.add_message("agent", reply, trace["item_id"])
        store.add_trace(trace)
    _log.info(json.dumps(trace))
    return {"reply": reply, "item_id": trace["item_id"], **extras}


def _ticket(
    store: Store, trace: dict, started: float, route: str, why: str, kind: str | None = None, summary: str | None = None
) -> dict:
    """Close the route on a support ticket. Its reference is shown only once the ticket is stored."""
    try:
        ref = store.create_ticket(
            kind or route, summary or f"{why}. Customer wrote: {trace['message']}", trace["item_id"]
        )
    except TicketError as error:
        trace["error"] = "; ".join(filter(None, (trace["error"], f"ticket: {error}")))
        reason = f"{why}, but the support ticket could not be stored, so a retry was offered."
        return _end(store, trace, started, "ticket_failed", reason, rules.TICKET_FAILED, retry=True)
    trace["ticket_ref"] = ref
    reply = rules.ticket_reply(ref, verified=route != "gate_error")
    return _end(store, trace, started, route, f"{why}, so a support ticket was opened.", reply, ticket_ref=ref)


def _offer(store: Store, item: dict | None, action: str | None) -> dict:
    """The payload's pending action, created when an accepted reply offers a cancellation or a return."""
    if not action:
        return {}
    pending = {
        "token": store.offer_action(item["id"], action),
        "action": action,
        "item_id": item["id"],
        "label": f"{action.capitalize()} {item['name']}",
    }
    return {"pending_action": pending}


def _score(trace: dict, turn: tuple, text: str) -> dict | None:
    """Jev's three scores for `text`, from one request. None when Jev gave no usable scores: that is not a "no"."""
    try:
        with _timed(trace, "jev"):
            result = jev.score(*turn, text)
    except Exception as error:  # JevError, or anything unexpected: neither is a "no", and neither is a 500
        trace["error"], trace["cost_usd"] = f"jev: {_cause(error, jev.JevError)}", None
        return None
    trace["cost_usd"] = _add_cost(trace["cost_usd"], result["cost_usd"])
    return {name: result[name] for name in jev.QUESTIONS}


def _cause(error: Exception, typed: type[Exception]) -> str:
    """What the trace says about a failed provider call. The provider's own error type speaks for itself.

    Anything else is unexpected: it is logged with its traceback and the trace names only its class, because
    its text could quote a key.
    """
    if isinstance(error, typed):
        return str(error)
    logging.exception("Unexpected error in a provider call; the route fails closed")
    return f"unexpected {type(error).__name__}"


@contextmanager
def _timed(trace: dict, provider: str) -> Iterator[None]:
    """Count one provider call and its time, whether it succeeds or fails."""
    trace["calls"][provider] += 1
    started = time.monotonic()
    try:
        yield
    finally:
        trace["elapsed_ms"][provider] += int((time.monotonic() - started) * 1000)


def _add_cost(total: float | None, cost: float | None) -> float | None:
    """The running cost of a message. It is None, unknown, as soon as one call's cost is unknown."""
    return None if total is None or cost is None else total + cost
