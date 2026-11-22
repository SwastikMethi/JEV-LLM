"""Jev gate client: one POST to TypeSafe asking three yes/no (noul) questions about a proposed reply."""
import functools
import json
import os
import re
import time
from datetime import date
from pathlib import Path

import httpx

GATE: dict = json.loads((Path(__file__).resolve().parents[1] / "data" / "gate.json").read_text(encoding="utf-8"))
QUESTIONS = ("supported", "complete", "requires_human")
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# The two flags that say what can be requested today go out under names that say so. As plain "can_return": false,
# Jev read a shipped item as one that can never be returned and scored "it can be returned within 7 days of
# delivery" at 0.68; under the new name the same reply scored 0.94 and wrong claims stayed below 0.05 (live checks,
# 2026-11-22).
_TODAY_ONLY = {"can_cancel": "can_cancel_today", "can_return": "can_return_today"}


class JevError(Exception):
    """The gate produced no usable scores. Never read it as a "no": the caller hands off."""


@functools.lru_cache(maxsize=1)
def _client() -> httpx.Client:
    # Built on first use and then shared, so connections are reused and tests can swap in a mock transport.
    return httpx.Client()


def _spoken(value):
    """Copy a record, writing each YYYY-MM-DD string as "2026-11-24 (November 24)" so reply wording can be matched."""
    if isinstance(value, dict):
        return {key: _spoken(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_spoken(inner) for inner in value]
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        day = date.fromisoformat(value)
        return f"{value} ({_MONTHS[day.month - 1]} {day.day})"
    return value


def evidence(message: str, history: list[dict], item: dict | None, policy: dict, today: str) -> dict:
    """The evidence shared by Jev and the fallback: what a support agent would have in front of them.

    The whole order record, and the whole policy as the text a customer may be told, section by section. What
    the code reads beside a section's text (topics, windows, fees, rule ids) stays out, and so does anything that
    is unique to one request.
    """
    # ponytail: every section goes out on every call, about 2,750 input tokens for a short message. The largest
    # request the app can build (the longest record, six history turns, a 500-character message and 600-character
    # replies, all in four-byte letters) measured 20,681 of Jev's 32,000 tokens per question. Pick sections by
    # topic if the policy grows several times over.
    return {
        "customer_message": message,
        "conversation_history": [f"{turn['role']}: {turn['text']}" for turn in history],
        "order_record": item and {_TODAY_ONLY.get(name, name): value for name, value in _spoken(item).items()},
        "permitted_actions": list(item["permitted_actions"]) if item else [],
        "store_policy": {
            "version": policy["version"],
            "store": policy["store"],
            **{name: part["text"] for name, part in policy.items() if isinstance(part, dict)},
            # Frozen with the gate: without it Jev cannot tell a correct refusal from an unsupported claim, and
            # it weighs only what stands in the two fields its questions name, order_record and store_policy.
            "assistant_scope": GATE["assistant_scope"],
            "escalation_conditions": [rule["text"] for rule in policy["escalation"]],
        },
        "today": _spoken(today),
    }


def _scores(data: object) -> dict:
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise JevError("malformed scores: no answers object")
    scores = {}
    for name in QUESTIONS:
        answer = answers.get(name)
        noul = answer.get("noul") if isinstance(answer, dict) and answer.get("type") == "noul" else None
        # bool is an int in Python, so it is ruled out by hand; NaN and the infinities fail the range test.
        if isinstance(noul, bool) or not isinstance(noul, (int, float)) or not 0 <= noul <= 1:
            raise JevError(f"malformed scores: {name}")
        scores[name] = float(noul)
    return scores


def score(message: str, history: list[dict], item: dict | None, policy: dict, today: str, reply: str) -> dict:
    """Ask Jev the three gate questions about `reply` in one request. Raises JevError unless every score is valid."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        raise JevError("TYPESAFE_API_KEY is not set")
    if policy.get("version") != GATE["policy_version"]:
        frozen_for = f"gate {GATE['version']} was frozen for policy {GATE['policy_version']}"
        raise JevError(f"{frozen_for}, not {policy.get('version')}")
    body = {
        "model": GATE["model"],
        "state": {**evidence(message, history, item, policy, today), "proposed_reply": reply},
        "questions": {
            name: {"type": "noul", "instructions": GATE["questions"][name]["instructions"],
                   "criteria": GATE["questions"][name]["criteria"]}
            for name in QUESTIONS
        },
    }
    started = time.monotonic()
    try:
        response = _client().post(
            (os.environ.get("TYPESAFE_BASE_URL") or "https://api.typesafe.ai").rstrip("/") + "/v1/systemone",
            json=body,
            headers={"Authorization": f"Bearer {key}"},
            # ponytail: httpx applies this to each phase (connect, write, read), not to the wall clock, so a
            # slow connect plus a slow answer can take longer. Run the POST under a hard deadline if that matters.
            timeout=float(os.environ.get("JEV_TIMEOUT_S") or 5),
        )
    except httpx.TimeoutException as error:
        raise JevError("timeout") from error
    # InvalidURL is not an HTTPError; ValueError covers a timeout, key or state that cannot be parsed or encoded,
    # and OverflowError a timeout the socket cannot take ("inf").
    # Only the class name is reported: the text of these errors can quote the Authorization header.
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, OverflowError) as error:
        raise JevError(f"request failed: {type(error).__name__}") from error
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if response.status_code != 200:
        raise JevError(f"http {response.status_code}")
    try:
        data = response.json()
    except (ValueError, RecursionError) as error:  # RecursionError: a body nested too deep to parse
        raise JevError("malformed scores: the body is not JSON") from error
    scores = _scores(data)
    usage = data.get("usage")
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    if isinstance(tokens, bool) or not isinstance(tokens, int):
        tokens = None  # unknown usage is not a failed call
    model = data.get("model")
    return {
        **scores,
        "model": model if isinstance(model, str) else GATE["model"],
        "input_tokens": tokens,
        "cost_usd": None if tokens is None else tokens * GATE["usd_per_mtok_input"] / 1_000_000,
        "request_id": response.headers.get("x-typesafe-request-id"),
        "elapsed_ms": elapsed_ms,
    }
