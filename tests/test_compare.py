"""The comparison runner (evaluation/compare.py). No network: Jev, the LLM, the backend check and the rule layer are
stand-ins, so these tests pin how the arms switch the router's collaborators and not what today's rules say."""
import copy
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from evaluation import compare  # imports no app module until a function needs one

JEV_KEY, LLM_KEY = "jev-key-3f9a1c77d2", "llm-key-8b61e04f55"
ACCEPT = {"supported": 1.0, "complete": 1.0, "requires_human": 0.0}
NOT_UNDERSTOOD = "Sorry, I did not understand that."


def _item(item_id: str, name: str, status: str, **dates: str) -> dict:
    return {"id": item_id, "name": name, "noun": name.split()[-1], "variant": "Size M", "price_inr": 1499,
            "status": status, "shipped_at": None, "eta": None, "delivered_at": None, "events": [], "return": None,
            "refund": None, **dates}


FIXTURES = {"now": "2026-11-21T10:00:00+05:30", "customer": {"name": "Asha"}, "orders": [{
    "id": "ORD-1", "placed_at": "2026-11-15", "payment_method": "UPI", "items": [
        _item("ITM-1", "Blue kurta", "packed", eta="2026-11-25"),
        _item("ITM-2", "Red saree", "shipped", shipped_at="2026-11-19", eta="2026-11-24"),
        _item("ITM-3", "Grey jeans", "delivered", shipped_at="2026-11-16", delivered_at="2026-11-18"),
        _item("ITM-4", "Green scarf", "shipped", shipped_at="2026-11-19", eta="2026-11-24"),
    ]}]}


def case(case_id: str, item_id: str | None, message: str, **changes: object) -> dict:
    return {"id": case_id, "family": f"fam-{case_id}", "split": "dev", "kind": "single",
            "topics": [compare.help_topics()[0]["id"]], "fixtures": FIXTURES, "item_id": item_id, "history": [],
            "message": message, "quick_action": None, "in_scope": True, "requires_human": False,
            "requires_human_note": "", "expected": "A reply that states what the order record shows.",
            "expected_action": None, "label_status": "ai_draft", **changes}


# What the stand-ins give. The rule layer: a draft for two items and a backend-mandated ticket for a third.
RULE = {"ITM-1": {"draft": "RULE one.", "evidence": [], "action": "cancel"},
        "ITM-2": {"draft": "RULE two.", "evidence": [], "action": None}}
MANDATE = {"ITM-3": {"rule": "refund_overdue", "summary": "A person must look at this."}}
SCORES = {"RULE two.": {**ACCEPT, "supported": 0.0}, "LLM three.": {**ACCEPT, "requires_human": 1.0}}
LLM = {"msg one": ("LLM one.", "cancel"), "msg two": ("LLM two.", None), "msg three": ("LLM three.", None),
       "msg four": ("LLM four.", None)}  # "msg five" fails, with an error text that quotes both keys
PROBLEMS = {"LLM four.": ["a date the record does not have"]}
CASES = [
    case("zq-rule", "ITM-1", "msg one", expected_action="cancel"),
    case("zq-rejected", "ITM-2", "msg two"),
    case("zq-mandate", "ITM-3", "msg three", kind="handoff", requires_human=True, requires_human_note="zq-note"),
    case("zq-unknown", "ITM-4", "msg four"),
    case("zq-llm-down", "ITM-4", "msg five"),
]
# Per arm and case: the outcome, the reply sent (None: a ticket), the action offered and the provider requests.
EXPECTED = {
    "A": [("reply", "RULE one.", "cancel", []), ("reply", "RULE two.", None, []), ("ticket", None, None, []),
          ("reply", NOT_UNDERSTOOD, None, []), ("reply", NOT_UNDERSTOOD, None, [])],
    "B": [("reply", "RULE one.", "cancel", ["jev"]), ("reply", "LLM two.", None, ["jev", "llm", "jev"]),
          ("ticket", None, None, []), ("ticket", None, None, ["llm"]), ("ticket", None, None, ["llm"])],
    "C": [("reply", "LLM one.", "cancel", ["llm"]), ("reply", "LLM two.", None, ["llm"]),
          ("reply", "LLM three.", None, ["llm"]), ("reply", "LLM four.", None, ["llm"]),
          ("ticket", None, None, ["llm"])],
    "D": [("reply", "LLM one.", "cancel", ["llm", "jev"]), ("reply", "LLM two.", None, ["llm", "jev"]),
          ("ticket", None, None, ["llm", "jev"]), ("ticket", None, None, ["llm"]), ("ticket", None, None, ["llm"])],
}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Recording stand-ins where the router reaches its collaborators, the settings `run` needs, and empty
    directories for cases and runs. Returns what each stand-in was called with."""
    from app import fallback, jev, rules

    calls = {"candidate": [], "mandated": [], "jev": [], "llm": [], "problems": [], "llm_saw": set()}

    def candidate(found: list, item: dict | None, *rest: object) -> dict | None:
        calls["candidate"].append(item["id"])
        return RULE.get(item["id"])

    def mandated(found: list, item: dict | None, *rest: object) -> dict | None:
        calls["mandated"].append(item["id"])
        return MANDATE.get(item["id"])

    def score(message: str, history: list, item: dict | None, policy: dict, today: str, reply: str) -> dict:
        calls["jev"].append(reply)
        return {**SCORES.get(reply, ACCEPT), "model": "stub", "input_tokens": 1000, "cost_usd": 0.001,
                "request_id": None, "elapsed_ms": 1}

    def draft(message: str, history: list, item: dict | None, policy: dict, today: str, rejected=None) -> dict:
        calls["llm"].append(message)
        calls["llm_saw"].add((fallback.SYSTEM_PROMPT, os.environ["FALLBACK_MODEL"], os.environ["FALLBACK_API_KEY"],
                              os.environ["FALLBACK_BASE_URL"], os.environ["FALLBACK_USD_PER_MTOK_IN"],
                              os.environ["FALLBACK_USD_PER_MTOK_OUT"]))
        time.sleep(0.02)  # long enough to show up in the milliseconds
        if message not in LLM:
            raise fallback.FallbackError(f"http 401 for {LLM_KEY} and {JEV_KEY}")
        text, action = LLM[message]
        return {"kind": "reply", "text": text, "action": action, "input_tokens": 2000, "output_tokens": 100,
                "cost_usd": 0.01, "elapsed_ms": 20}

    def problems(result: dict, *rest: object) -> list[str]:
        calls["problems"].append(result["text"])
        return PROBLEMS.get(result["text"], [])

    for module, name, stand_in in ((rules, "candidate", candidate), (rules, "mandated", mandated),
                                   (jev, "score", score), (fallback, "draft", draft), (fallback, "problems", problems)):
        monkeypatch.setattr(module, name, stand_in)
    monkeypatch.setattr(rules, "NOT_UNDERSTOOD", NOT_UNDERSTOOD)
    monkeypatch.setattr(compare, "CASES", tmp_path / "cases")
    monkeypatch.setattr(compare, "RUNS", tmp_path / "runs")
    compare.CASES.mkdir()
    for name in ("BASE_URL", "API_KEY", "MODEL", "USD_PER_MTOK_IN", "USD_PER_MTOK_OUT"):
        monkeypatch.setenv(f"FALLBACK_{name}", "")  # so that what `run` writes there is undone after the test
    monkeypatch.setenv("TYPESAFE_API_KEY", JEV_KEY)
    monkeypatch.setenv("COMPARE_LLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("COMPARE_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    for name in ("LITELLM_API_KEY", "LITELLM_BASE_URL", "COMPARE_LLM_MODEL", "COMPARE_LLM_USD_PER_MTOK_IN",
                 "COMPARE_LLM_USD_PER_MTOK_OUT"):
        monkeypatch.delenv(name, raising=False)  # a developer's own settings play no part
    return calls


def write_cases(cases: list[dict]) -> None:
    (compare.CASES / "cases.jsonl").write_text("".join(json.dumps(c) + "\n" for c in cases), encoding="utf-8")


def seen(records: list[dict]) -> list[tuple]:
    return [(record["outcome"], None if record["outcome"] == "ticket" else record["reply"], record["action"],
             [request["provider"] for request in record["requests"]]) for record in records]


# The arms


def test_what_each_arm_replaces(world: dict) -> None:
    replaced = {arm: {f"{module.__name__}.{name}" for module, name, _ in compare.switches(arm, {})}
                for arm in compare.ARMS}
    assert replaced == {
        "A": {"app.jev.score", "app.fallback.draft", "app.fallback.problems"},
        "B": {"app.jev.score", "app.fallback.draft"},  # watched, not switched off: see the test of arm B
        "C": {"app.rules.candidate", "app.rules.mandated", "app.rules.needs_item", "app.jev.score",
              "app.fallback.problems", "app.fallback.draft"},
        "D": {"app.rules.candidate", "app.rules.mandated", "app.rules.needs_item", "app.jev.score",
              "app.fallback.draft"},
    }


def test_arm_a_sends_the_rule_draft_unchecked_and_calls_no_provider(world: dict) -> None:
    records = compare.run_arm("A", CASES, 4, {})
    assert seen(records) == EXPECTED["A"]
    assert world["jev"] == world["llm"] == world["problems"] == []  # no gate, no LLM, no backend check
    assert sorted(world["candidate"]) == ["ITM-1", "ITM-2", "ITM-4", "ITM-4"]  # the rules were asked
    assert all(record["calls"] == {"jev": 0, "llm": 0} and record["cost_usd"] == {"jev": 0, "llm": 0}
               for record in records)
    assert records[2]["ticket_ref"] and records[2]["source"] is None  # a mandated ticket still happens


def test_arm_b_is_the_application_as_built(world: dict) -> None:
    from app import fallback, jev, router, rules

    def as_built(c: dict) -> tuple:
        store = compare.stored(c)
        payload = router.handle(store, c["message"], c["item_id"], c["quick_action"])
        trace = store.traces()[-1]
        return (trace["route"], trace["reply"], trace["ticket_ref"], trace["error"], trace["item_id"],
                (payload.get("pending_action") or {}).get("action"))

    direct = [as_built(c) for c in CASES]
    direct_calls = {name: list(made) for name, made in world.items() if name != "llm_saw"}
    for made in world.values():
        made.clear()
    untouched = (rules.candidate, rules.mandated, fallback.problems, jev.score, fallback.draft)
    records = compare.run_arm("B", CASES, 1, {})
    assert [(r["route"], r["reply"], r["ticket_ref"], r["error"], r["item_id"], r["action"]) for r in records] == direct
    assert {name: made for name, made in world.items() if name != "llm_saw"} == direct_calls  # same calls, in order
    assert untouched == (rules.candidate, rules.mandated, fallback.problems, jev.score, fallback.draft)  # put back
    assert seen(records) == EXPECTED["B"]
    assert [record["source"] for record in records] == ["rule", "fallback", None, "fallback", "fallback"]
    rejected = records[1]  # rule draft rejected by Jev, LLM reply accepted: the cost is split between the two
    assert rejected["calls"] == {"jev": 2, "llm": 1}
    assert rejected["cost_usd"] == pytest.approx({"jev": 0.002, "llm": 0.01})
    made = [(r["provider"], r["cost_usd"], r["input_tokens"], r.get("output_tokens")) for r in rejected["requests"]]
    assert made == [("jev", 0.001, 1000, None), ("llm", 0.01, 2000, 100), ("jev", 0.001, 1000, None)]  # with tokens
    assert records[4]["cost_usd"] == {"jev": 0, "llm": None}  # a failed LLM request has no known cost
    assert records[4]["error"].startswith("fallback: http 401")


def test_arm_c_asks_only_the_llm_and_arm_d_gates_exactly_what_it_said(world: dict) -> None:
    kept = {}
    c = compare.run_arm("C", CASES, 4, kept)
    assert seen(c) == EXPECTED["C"]
    assert sorted(world["llm"]) == sorted(case["message"] for case in CASES)  # every message went to the LLM
    assert world["jev"] == world["problems"] == world["candidate"] == world["mandated"] == []  # and nowhere else
    assert all(record["calls"]["jev"] == 0 and record["cost_usd"]["jev"] == 0 for record in c)

    d = compare.run_arm("D", CASES, 4, kept)
    assert seen(d) == EXPECTED["D"]
    assert len(world["llm"]) == len(CASES)  # arm D did not pay the LLM again
    assert world["candidate"] == world["mandated"] == []  # still no rules
    assert sorted(world["problems"]) == ["LLM four.", "LLM one.", "LLM three.", "LLM two."]  # the backend check ran
    assert sorted(world["jev"]) == ["LLM one.", "LLM three.", "LLM two."]  # Jev scored arm C's own texts
    for case_c, case_d in zip(c, d):
        llm_c, llm_d = case_c["requests"][0], case_d["requests"][0]
        assert llm_d == llm_c and llm_c["ms"] >= 15  # the remembered time and cost are arm D's too
        assert case_d["cost_usd"]["llm"] == case_c["cost_usd"]["llm"]
        assert case_d["latency_ms"] >= llm_c["ms"]
    assert d[4]["error"] == c[4]["error"] and d[4]["cost_usd"] == {"jev": 0, "llm": None}  # the failure is replayed


def test_remembered_llm_answers_do_not_cross_cases_between_threads(world: dict, monkeypatch) -> None:
    many = [case(f"zq-{n}", "ITM-4", f"msg {n}") for n in range(24)]
    for n in range(24):
        monkeypatch.setitem(LLM, f"msg {n}", (f"LLM {n}.", None))
    kept = {}
    compare.run_arm("C", many, 8, kept)
    d = compare.run_arm("D", many[::-1], 8, kept)  # another order, so a lookup by position or by thread would fail
    assert [record["reply"] for record in d] == [f"LLM {n}." for n in range(24)][::-1]


def test_the_llm_arms_answer_where_the_rules_would_ask_which_item(world: dict, monkeypatch) -> None:
    from app import rules

    def unclear(found: list, message: str, named: int = 0) -> bool:
        return True  # the rules cannot tell which item is meant, so the application asks the customer

    monkeypatch.setattr(rules, "needs_item", unclear)
    which = [case("zq-which", None, "msg two")]
    kept = {}
    a, b, c, d = (compare.run_arm(arm, which, 1, kept) for arm in compare.ARMS)
    assert seen(a) == seen(b) == [("clarification", rules.CHOOSE_ITEM, None, [])]  # the rules' own fixed question
    assert seen(c) == [("reply", "LLM two.", None, ["llm"])]  # the LLM is asked all the same: once, with no item
    assert seen(d) == [("reply", "LLM two.", None, ["llm", "jev"])]
    assert world["llm"] == ["msg two"] and world["jev"] == world["problems"] == ["LLM two."]
    assert world["candidate"] == world["mandated"] == []
    assert [records[0]["item_id"] for records in (a, b, c, d)] == [None] * 4  # the same item in every arm: none
    assert rules.needs_item is unclear  # put back


def routed(cost: float | None, candidate: dict | None = None, asks: int = 0) -> Callable:
    """A router that answers by some other way than the arm's switches: it reports `cost` (None: unknown) for
    providers that no switch saw and the rule draft `candidate`, and it asks the LLM `asks` times."""
    def handle(store, message: str, item_id: str | None = None, quick_action: str | None = None) -> dict:
        from app import fallback

        spent = sum(fallback.draft(message, [], None, store.policy, store.today)["cost_usd"] for _ in range(asks))
        store.add_trace({"route": "rule", "reason": "", "reply": "a reply", "ticket_ref": None, "item_id": item_id,
                         "candidate": candidate, "error": None, "calls": {"jev": 0, "fallback": asks},
                         "cost_usd": None if cost is None else cost + spent})
        return {"reply": "a reply"}
    return handle


def test_an_arm_that_is_not_what_it_claims_stops_the_run(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import router

    monkeypatch.setattr(router, "handle", routed(0.0))  # a reply, and no provider was asked
    assert seen(compare.run_arm("A", CASES[:1], 1, {})) == [("reply", "a reply", None, [])]  # fine for the rule arms
    assert seen(compare.run_arm("B", CASES[:1], 1, {})) == [("reply", "a reply", None, [])]
    for arm in "CD":  # an LLM arm in which the LLM was not asked
        with pytest.raises(SystemExit, match=f"Arm {arm} is not what it claims.*zq-rule.*Nothing was saved"):
            compare.run_arm(arm, CASES[:1], 1, {})
    monkeypatch.setattr(router, "handle", routed(0.0, asks=1))
    assert seen(compare.run_arm("C", CASES[:1], 1, {})) == [("reply", "a reply", None, ["llm"])]  # asked once: fine
    # In arm C: the LLM asked twice, a rule draft beside the LLM's answer, and a gate that was paid.
    drafted = routed(0.0, candidate={"draft": "RULE one."}, asks=1)
    for not_arm_c in (routed(0.0, asks=2), drafted, routed(0.001, asks=1)):
        monkeypatch.setattr(router, "handle", not_arm_c)
        with pytest.raises(SystemExit, match="Arm C is not what it claims.*zq-rule"):
            compare.run_arm("C", CASES[:1], 1, {})
    for cost in (0.001, None):  # a provider was paid, or failed, that no switch saw
        monkeypatch.setattr(router, "handle", routed(cost))
        for arm in "AB":
            with pytest.raises(SystemExit, match=f"Arm {arm} is not what it claims.*zq-rule"):
                compare.run_arm(arm, CASES[:1], 1, {})


def test_the_switches_are_put_back_when_a_phase_fails(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import fallback, jev, rules

    def stored(c: dict) -> None:
        raise RuntimeError("the fixtures did not load")

    switched = [(rules, "candidate"), (rules, "mandated"), (rules, "needs_item"), (jev, "score"),
                (fallback, "draft"), (fallback, "problems")]
    before = [getattr(module, name) for module, name in switched]
    monkeypatch.setattr(compare, "stored", stored)
    for arm in compare.ARMS:
        with pytest.raises(RuntimeError, match="the fixtures did not load"):
            compare.run_arm(arm, CASES, 2, {})
        assert [getattr(module, name) for module, name in switched] == before


def test_a_case_the_application_fails_on_is_an_error_and_the_run_goes_on(world: dict, monkeypatch) -> None:
    from app import router

    as_built = router.handle

    def handle(store, message: str, *rest: object) -> dict:
        if message == "msg two":
            raise RuntimeError(f"no reply, and the text quotes {LLM_KEY}")
        return as_built(store, message, *rest)

    monkeypatch.setattr(router, "handle", handle)
    for arm in "AB":
        records = compare.run_arm(arm, CASES, 2, {})
        assert [r["outcome"] for r in records] == [outcome for outcome, *_ in EXPECTED[arm][:1]] + ["error"] + [
            outcome for outcome, *_ in EXPECTED[arm][2:]]
        failed = records[1]  # not a reply, not a ticket, and nothing was asked of a provider
        assert (failed["reply"], failed["action"], failed["ticket_ref"], failed["requests"]) == (None, None, None, [])
        assert failed["error"].startswith("handle raised RuntimeError: no reply")
    with pytest.raises(SystemExit, match="Arm C is not what it claims.*zq-rejected"):  # the LLM was never asked
        compare.run_arm("C", CASES, 2, {})


def test_b_without_the_llm_turns_every_fallback_outcome_into_a_ticket() -> None:
    requests = [{"provider": "jev", "ms": 5, "cost_usd": 0.001}, {"provider": "llm", "ms": 100, "cost_usd": 0.01},
                {"provider": "jev", "ms": 6, "cost_usd": 0.001}]
    sent = {"outcome": "reply", "reply": "LLM text", "action": "cancel", "ticket_ref": None, "error": None,
            "route": "fallback", "source": "fallback", "latency_ms": 120, "requests": requests,
            **compare.totals(requests)}
    derived = compare.without_llm(sent)
    assert (derived["outcome"], derived["reply"], derived["action"]) == ("ticket", None, None)
    assert derived["requests"] == requests[:1]  # the rule draft was still scored; nothing after it happened
    assert derived["calls"] == {"jev": 1, "llm": 0} and derived["cost_usd"] == {"jev": 0.001, "llm": 0}
    assert derived["latency_ms"] == 120 - 100 - 6
    failed = {**sent, "outcome": "ticket", "error": "fallback: timeout",
              "requests": [{"provider": "llm", "ms": 30000, "cost_usd": None}], "latency_ms": 30001}
    derived = compare.without_llm(failed)
    assert derived["error"] is None and derived["cost_usd"] == {"jev": 0, "llm": 0} and derived["latency_ms"] == 1
    for source in ("rule", None):  # a rule draft that was sent and a mandated ticket are left as they are
        kept = {**sent, "source": source}
        assert compare.without_llm(kept) is kept


# run, sheet and report through the command line


def test_run_then_sheet_then_report_and_no_key_in_any_output(world: dict, tmp_path: Path, capsys) -> None:
    from app import fallback, jev

    write_cases(CASES + [case("zq-test-split", "ITM-4", "msg four", split="test")])
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("A prompt for this comparison only.", encoding="utf-8")
    original = fallback.SYSTEM_PROMPT
    with pytest.raises(SystemExit, match="No case .* starts with 'nope'"):
        compare.main(["run", "--split", "dev", "--only", "nope"])
    assert compare.main(["run", "--split", "dev", "--workers", "2", "--system-prompt-file", str(prompt_file)]) == 0
    assert fallback.SYSTEM_PROMPT == original  # put back after the run
    # What the LLM client reads while arms B and C call it: the replaced prompt and the comparison's settings.
    assert world["llm_saw"] == {("A prompt for this comparison only.", "claude-haiku-5-5", LLM_KEY,
                                 "http://127.0.0.1:9/v1", "0.10", "0.50")}

    (run_path,) = compare.RUNS.glob("*-dev.json")
    saved = json.loads(run_path.read_text(encoding="utf-8"))
    assert [c["id"] for c in saved["cases"]] == [c["id"] for c in CASES]  # the test split was left out
    assert saved["llm"]["model"] == "claude-haiku-5-5" and saved["gate"] == jev.GATE
    assert (saved["llm"]["usd_per_mtok_in"], saved["llm"]["usd_per_mtok_out"]) == (0.10, 0.50)
    assert saved["not_understood"] == NOT_UNDERSTOOD  # arm A's fixed reply, for the report to tell it apart
    root = Path(compare.__file__).resolve().parents[1]
    assert saved["engineering_surface"] == {  # measured when the run is made
        "rules_py_lines": len((root / "app" / "rules.py").read_text(encoding="utf-8").splitlines()),
        "help_topics": len(compare.help_topics()),
        "policy_json_bytes": (root / "data" / "policy.json").stat().st_size}
    for arm in compare.ARMS:
        assert seen([c["arms"][arm] for c in saved["cases"]]) == EXPECTED[arm]
    assert set(saved["cases"][0]["arms"]["B"]) == {
        "outcome", "reply", "action", "ticket_ref", "item_id", "route", "reason", "error", "source", "latency_ms",
        "requests", "calls", "cost_usd"}

    assert compare.main(["sheet", str(run_path)]) == 0
    sheet_text = compare.beside(run_path, "sheet.json").read_text(encoding="utf-8")
    sheet = json.loads(sheet_text)
    key = json.loads(compare.beside(run_path, "key.json").read_text(encoding="utf-8"))
    assert set(sheet) == {"instructions", "items"} and len(sheet["items"]) == len(key) == 8
    assert "zq-" not in sheet_text and "arms" not in sheet_text  # the judges' file names no case and no arm
    judges = []
    for number, doubts in enumerate(([], [sheet["items"][0]["id"]])):  # the second judge rejects one reply
        judges.append(tmp_path / f"judge{number}.json")
        judges[-1].write_text(json.dumps({item["id"]: {"suitable": item["id"] not in doubts, "note": ""}
                                          for item in sheet["items"]}), encoding="utf-8")
    assert compare.main(["report", str(run_path), "--judgements", *map(str, judges)]) == 0
    report = json.loads(compare.beside(run_path, "report.json").read_text(encoding="utf-8"))
    assert report["replies_judged"] == {"suitable": len(key) - 1, "disputed": 1}
    assert set(report["metrics"]) == {"overall", "single", "handoff"}
    assert set(report["metrics"]["overall"]) == set(compare.REPORTED)
    assert report["metrics"]["handoff"]["C"]["required_handoffs_missed"] == {"n": 1, "d": 1, "rate": 1.0}

    printed = capsys.readouterr()
    assert compare.beside(run_path, "report.txt").read_text(encoding="utf-8") in printed.out
    everything = printed.out + printed.err + "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file() and path not in judges)
    assert "fallback: http 401 for [key removed] and [key removed]" in everything  # the error was kept, without keys
    assert JEV_KEY not in everything and LLM_KEY not in everything


def test_the_sheet_is_blind_and_merges_identical_replies(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import jev, rules

    def match_items(message: str, items: list[dict]) -> list[dict]:
        return [item for item in items if item["name"] in message]

    monkeypatch.setattr(rules, "match_items", match_items)
    monkeypatch.setitem(LLM, "msg same", ("RULE one.", None))  # the rule draft's words, without its action
    monkeypatch.setitem(LLM, "Where is my Green scarf", ("LLM scarf.", None))
    cases = CASES + [case("zq-same", "ITM-1", "msg same", expected_action="cancel"),
                     case("zq-named", None, "Where is my Green scarf")]  # no item selected: the message names it
    kept = {}
    arms = {arm: compare.run_arm(arm, cases, 1, kept) for arm in compare.ARMS}
    run_data = {"cases": [{**c, "arms": {arm: arms[arm][index] for arm in compare.ARMS}}
                          for index, c in enumerate(cases)]}
    items, key = compare.sheet_items(run_data)
    # One item per distinct text and action that reached the customer of a case; several arms share an item.
    assert sorted((where["case"], *where["arms"]) for where in key.values()) == [
        ("zq-llm-down", "A"), ("zq-mandate", "C"), ("zq-named", "A"), ("zq-named", "B", "C", "D"),
        ("zq-rejected", "A"), ("zq-rejected", "B", "C", "D"), ("zq-rule", "A", "B"), ("zq-rule", "C", "D"),
        ("zq-same", "A", "B"), ("zq-same", "C", "D"),  # the same words with and without the action: two items
        ("zq-unknown", "A"), ("zq-unknown", "C")]
    by_case = {c["id"]: c for c in cases}
    for item in items:
        c = by_case[key[item["id"]]["case"]]
        store = compare.stored(c)
        resolved = "ITM-4" if c["id"] == "zq-named" else c["item_id"]  # the record of the item the router resolved
        assert item == {  # nothing but these six fields, and the evidence is the gate's own
            "id": item["id"],
            "evidence": jev.evidence(c["message"], [], store.item(resolved), store.policy, store.today),
            "expected": c["expected"], "expected_action": c["expected_action"],
            "reply": item["reply"], "action": item["action"]}
    assert "zq-" not in json.dumps(items) and "fam-" not in json.dumps(items)  # no case id, family or label note
    # An item id is the sheet's own tag and the item's place in the shuffled order.
    tag = items[0]["id"][:-5]
    assert [item["id"] for item in items] == [f"{tag}-{number:04d}" for number in range(1, 13)] and len(tag) == 6
    order = [(key[item["id"]]["case"], key[item["id"]]["arms"][0]) for item in items]
    place = {c["id"]: number for number, c in enumerate(cases)}
    assert order != sorted(order, key=lambda where: (place[where[0]], where[1]))  # not the order of the run file
    assert compare.sheet_items(run_data) == (items, key)  # in the same order every time
    # Judgements belong to one sheet: another run's replies get other ids, so its judgements cannot be read here.
    other = copy.deepcopy(run_data)
    other["cases"][0]["arms"]["A"]["reply"] = "RULE one, in other words."
    assert not {item["id"] for item in compare.sheet_items(other)[0]} & {item["id"] for item in items}


def test_what_the_application_logs_during_a_run_is_printed_without_keys(world: dict, monkeypatch, capsys) -> None:
    from app import fallback

    root, stand_in = logging.getLogger(), fallback.draft
    handlers = list(root.handlers)

    def draft(*args: object, **kwargs: object) -> dict:
        # As the router logs an unexpected provider error: through the root logger, with the error's own text.
        logging.error("Unexpected error in a provider call: Illegal header value b'Bearer %s'", LLM_KEY)
        return stand_in(*args, **kwargs)

    monkeypatch.setattr(fallback, "draft", draft)
    write_cases(CASES)
    assert compare.main(["run", "--split", "dev"]) == 0
    printed = capsys.readouterr().err
    assert "Unexpected error in a provider call: Illegal header value b'Bearer [key removed]'" in printed
    assert LLM_KEY not in printed
    assert root.handlers == handlers  # the application's logging is as it was before the run


def test_a_run_can_be_repeated(world: dict) -> None:
    write_cases(CASES)
    asked = []
    for _ in range(2):
        assert compare.main(["run", "--split", "dev", "--workers", "3"]) == 0
        newest = max(compare.RUNS.glob("*-dev.json"), key=lambda path: path.stat().st_mtime_ns)
        saved = json.loads(newest.read_text(encoding="utf-8"))
        for arm in compare.ARMS:
            assert seen([c["arms"][arm] for c in saved["cases"]]) == EXPECTED[arm]
        asked.append((len(world["llm"]), len(world["jev"])))
    assert asked[1] == (2 * asked[0][0], 2 * asked[0][1])  # nothing was remembered from the first run


# The report's arithmetic, on numbers worked out by hand


def record(outcome: str, reply: str | None = None, action: str | None = None, ms: int = 0, jev: float | None = 0,
           llm: float | None = 0, **more: object) -> dict:
    return {"outcome": outcome, "reply": reply, "action": action, "latency_ms": ms, "error": None, "source": None,
            "requests": [], "cost_usd": {"jev": jev, "llm": llm}, **more}


def by_hand(tmp_path: Path) -> Path:
    """Four cases and what each arm did with them, the sheet's key, and the path of the run file."""
    def request(provider: str, ms: int, cost: float | None) -> dict:
        return {"provider": provider, "ms": ms, "cost_usd": cost}

    down = {"error": "fallback: timeout"}
    arms = {
        "k1": {"A": record("reply", "a1", ms=10),
               "B": record("reply", "a1", ms=100, jev=0.001, source="rule", requests=[request("jev", 90, 0.001)]),
               "C": record("reply", "c1", ms=900, llm=0.01),
               "D": record("reply", "c1", ms=1500, jev=0.001, llm=0.01)},
        "k2": {"A": record("reply", "a2", ms=20),  # offers nothing although a cancellation was expected
               "B": record("reply", "b2", "cancel", ms=120, jev=0.002, llm=0.01, source="fallback", requests=[
                   request("jev", 5, 0.001), request("llm", 100, 0.01), request("jev", 5, 0.001)]),
               "C": record("reply", "b2", "cancel", ms=800, llm=0.01),
               "D": record("reply", "b2", "cancel", ms=1400, jev=0.001, llm=0.01)},
        "k3": {"A": record("reply", "nu", ms=30),  # the fixed reply where a person was required: a missed handoff
               "B": record("ticket", ms=50, jev=0.001, source="rule", requests=[request("jev", 40, 0.001)]),
               "C": record("reply", "c3", ms=700, llm=0.01),
               "D": record("ticket", ms=1300, jev=0.001, llm=0.01)},
        "k4": {"A": record("reply", "nu", ms=40),
               "B": record("ticket", ms=200, jev=0.001, llm=0.01, source="fallback", requests=[
                   request("llm", 150, 0.01), request("jev", 40, 0.001)]),
               "C": record("ticket", ms=30000, llm=None, **down),
               "D": record("ticket", ms=30000, llm=None, **down)},
    }
    labels = {"k1": ("single", False, None), "k2": ("single", False, "cancel"), "k3": ("handoff", True, None),
              "k4": ("irrelevant", False, None)}
    run_path = tmp_path / "20261122T000000Z-dev.json"
    run_path.write_text(json.dumps({
        "split": "dev", "llm": {"model": "m", "usd_per_mtok_in": 0.1, "usd_per_mtok_out": 0.5},
        "not_understood": "nu",
        "gate": {"version": "g", "model": "j"},
        "engineering_surface": {"rules_py_lines": 1234, "help_topics": 35, "policy_json_bytes": 10484},
        "cases": [{"id": name, "kind": kind, "requires_human": human, "expected_action": action,
                   "label_status": "ai_draft", "arms": arms[name]} for name, (kind, human, action) in labels.items()],
    }), encoding="utf-8")
    compare.beside(run_path, "key.json").write_text(json.dumps({
        "R1": {"case": "k1", "arms": ["A", "B"]}, "R2": {"case": "k2", "arms": ["A"]},
        "R3": {"case": "k3", "arms": ["A"]}, "R4": {"case": "k4", "arms": ["A"]},
        "R5": {"case": "k2", "arms": ["B", "C", "D"]}, "R6": {"case": "k1", "arms": ["C", "D"]},
        "R7": {"case": "k3", "arms": ["C"]}}), encoding="utf-8")
    return run_path


def judge(path: Path, **marks: bool) -> str:
    path.write_text(json.dumps({item: {"suitable": mark, "note": ""} for item, mark in marks.items()}),
                    encoding="utf-8")
    return str(path)


def test_report_arithmetic(tmp_path: Path, capsys) -> None:
    run_path = by_hand(tmp_path)
    # The judges agree on all but R2. R7 is a reply both find suitable, sent on a case that requires a person.
    first = judge(tmp_path / "j1.json", R1=True, R2=True, R3=False, R4=False, R5=True, R6=True, R7=True)
    second = judge(tmp_path / "j2.json", R1=True, R2=False, R3=False, R4=False, R5=True, R6=True, R7=True)
    assert compare.main(["report", str(run_path), "--judgements", first, second]) == 0
    out = capsys.readouterr().out
    saved = json.loads(compare.beside(run_path, "report.json").read_text(encoding="utf-8"))
    assert saved["replies_judged"] == {"suitable": 4, "disputed": 1, "unsuitable": 2}
    overall = saved["metrics"]["overall"]

    def counts(arm: str, scope: dict = overall) -> dict:
        return {name: f"{value['n']}/{value['d']}" for name, value in scope[arm].items()
                if isinstance(value, dict) and "rate" in value}

    assert counts("A") == {  # the one wrong reply is the fixed reply to a message the rules do not understand
        "resolved_correctly_without_a_person": "1/4", "wrong_reply_sent": "1/4",
        "of_which_the_fixed_not_understood_reply": "1/1", "disputed_reply_sent": "1/4",
        "required_handoffs_missed": "1/1", "necessary_tickets": "0/1", "unnecessary_tickets": "0/3",
        "ended_in_an_error": "0/4", "not_resolved": "3/4", "action_errors": "1/4", "provider_errors": "0/4"}
    assert counts("B") == {
        "resolved_correctly_without_a_person": "2/4", "wrong_reply_sent": "0/4",
        "of_which_the_fixed_not_understood_reply": "0/0", "disputed_reply_sent": "0/4",
        "required_handoffs_missed": "0/1", "necessary_tickets": "1/1", "unnecessary_tickets": "1/3",
        "ended_in_an_error": "0/4", "not_resolved": "2/4", "action_errors": "0/2", "provider_errors": "0/4"}
    assert counts("B-no-LLM") == {
        "resolved_correctly_without_a_person": "1/4", "wrong_reply_sent": "0/4",
        "of_which_the_fixed_not_understood_reply": "0/0", "disputed_reply_sent": "0/4",
        "required_handoffs_missed": "0/1", "necessary_tickets": "1/1", "unnecessary_tickets": "2/3",
        "ended_in_an_error": "0/4", "not_resolved": "3/4", "action_errors": "0/1", "provider_errors": "0/4"}
    assert counts("C") == {  # the suitable reply on the case that requires a person is a missed handoff
        "resolved_correctly_without_a_person": "2/4", "wrong_reply_sent": "0/4",
        "of_which_the_fixed_not_understood_reply": "0/0", "disputed_reply_sent": "0/4",
        "required_handoffs_missed": "1/1", "necessary_tickets": "0/1", "unnecessary_tickets": "1/3",
        "ended_in_an_error": "0/4", "not_resolved": "2/4", "action_errors": "0/3", "provider_errors": "1/4"}
    assert counts("D")["necessary_tickets"] == "1/1" and counts("D")["provider_errors"] == "1/4"
    assert overall["A"]["resolved_correctly_without_a_person"]["rate"] == 0.25
    assert counts("A", saved["metrics"]["single"]) == {
        "resolved_correctly_without_a_person": "1/2", "wrong_reply_sent": "0/2",
        "of_which_the_fixed_not_understood_reply": "0/0", "disputed_reply_sent": "1/2",
        "required_handoffs_missed": "0/0", "necessary_tickets": "0/0", "unnecessary_tickets": "0/2",
        "ended_in_an_error": "0/2", "not_resolved": "1/2", "action_errors": "1/2", "provider_errors": "0/2"}
    assert counts("A", saved["metrics"]["irrelevant"])["wrong_reply_sent"] == "1/1"

    assert overall["A"]["latency_ms"] == {"median": 25, "p95": 40, "max": 40}
    assert overall["B"]["latency_ms"] == {"median": 110, "p95": 200, "max": 200}
    assert overall["B-no-LLM"]["latency_ms"] == {"median": 32, "p95": 100, "max": 100}  # 10, 15, 50, 100
    cost = {arm: overall[arm]["cost_usd_per_request"] for arm in compare.REPORTED}
    assert cost["A"] == {"jev": 0, "llm": 0, "total": 0, "unknown": 0}
    assert cost["B"] == pytest.approx({"jev": 0.00125, "llm": 0.005, "total": 0.00625, "unknown": 0})
    assert cost["B-no-LLM"] == pytest.approx({"jev": 0.00075, "llm": 0, "total": 0.00075, "unknown": 0})
    assert cost["C"] == pytest.approx({"jev": 0, "llm": 0.01, "total": 0.01, "unknown": 1})  # mean of the 3 known
    assert cost["D"] == pytest.approx({"jev": 0.00075, "llm": 0.01, "total": 0.01075, "unknown": 1})
    per_resolved = {arm: overall[arm]["cost_usd_per_correctly_resolved_request"] for arm in compare.REPORTED}
    assert per_resolved == pytest.approx({"A": 0, "B": 0.0125, "B-no-LLM": 0.003, "C": 0.02, "D": 0.0215})

    # Per 1,000 requests: A $0 and 750 not resolved, B $6.25 and 500, B-no-LLM $0.75 and 750, C $10 and 500.
    pairs = {tuple(pair["pair"]): (pair["ticket_price_usd"], pair["cheaper_below"], pair["cheaper_above"])
             for pair in saved["break_even"]}
    assert len(pairs) == 10
    assert pairs["A", "B"] == (pytest.approx(0.025), "A", "B")
    assert pairs["A", "B-no-LLM"] == (None, "A", "A")  # as many tickets, and A costs nothing to run
    assert pairs["A", "C"] == (pytest.approx(0.04), "A", "C")
    assert pairs["B", "B-no-LLM"] == (pytest.approx(0.022), "B-no-LLM", "B")
    assert pairs["B", "C"] == (None, "B", "B")
    assert pairs["C", "D"] == (None, "C", "C")
    assert "A vs B: equal at $0.0250 per ticket; below that A is cheaper, above it B" in out
    assert "B vs C: B is cheaper at every ticket price" in out
    assert "a wrong reply is counted as costing one ticket" in out and "Assumption" in out
    assert "resolved correctly without a person" in out and "1/4 (25.0%)" in out
    assert "of which the fixed not understood reply  1/1 (100.0%)" in out

    assert "app/rules.py 1,234 lines | 35 help topics | data/policy.json 10,484 bytes" in out  # as the run recorded


def test_a_request_that_ended_in_an_error_is_counted_once_and_stays_in_every_denominator(tmp_path: Path) -> None:
    run_path = by_hand(tmp_path)
    run_data = json.loads(run_path.read_text(encoding="utf-8"))
    # The application raised: in arm C on k2, where it had sent a reply, and in arm D on k3, which requires a person.
    run_data["cases"][1]["arms"]["C"] = record("error", ms=5, llm=0.01, error="handle raised TypeError: no item")
    run_data["cases"][2]["arms"]["D"] = record("error", ms=1300, jev=0.001, llm=0.01, error="handle raised KeyError")
    run_path.write_text(json.dumps(run_data), encoding="utf-8")
    marks = dict(R1=True, R2=True, R3=False, R4=False, R5=True, R6=True, R7=True)
    assert compare.main(["report", str(run_path), "--judgements", judge(tmp_path / "j.json", **marks)]) == 0
    overall = json.loads(compare.beside(run_path, "report.json").read_text(encoding="utf-8"))["metrics"]["overall"]

    def counts(arm: str) -> dict:
        return {name: f"{value['n']}/{value['d']}" for name, value in overall[arm].items()
                if isinstance(value, dict) and "rate" in value}

    assert counts("C") == {  # k1 resolved, k2 the error, k3 a reply where a person was required, k4 a ticket
        "resolved_correctly_without_a_person": "1/4", "wrong_reply_sent": "0/4",
        "of_which_the_fixed_not_understood_reply": "0/0", "disputed_reply_sent": "0/4",
        "required_handoffs_missed": "1/1", "necessary_tickets": "0/1", "unnecessary_tickets": "1/3",
        "ended_in_an_error": "1/4", "not_resolved": "3/4", "action_errors": "0/2", "provider_errors": "1/4"}
    assert counts("D") == {  # the error on k3 is neither a missed handoff nor a necessary ticket
        "resolved_correctly_without_a_person": "2/4", "wrong_reply_sent": "0/4",
        "of_which_the_fixed_not_understood_reply": "0/0", "disputed_reply_sent": "0/4",
        "required_handoffs_missed": "0/1", "necessary_tickets": "0/1", "unnecessary_tickets": "1/3",
        "ended_in_an_error": "1/4", "not_resolved": "2/4", "action_errors": "0/2", "provider_errors": "1/4"}
    # In both arms the one provider error is the LLM request that failed on k4; the application's own error is not one.
    assert overall["C"]["latency_ms"] == {"median": 800, "p95": 30000, "max": 30000}  # 5, 700, 900, 30000
    assert overall["C"]["cost_usd_per_request"] == pytest.approx({"jev": 0, "llm": 0.01, "total": 0.01, "unknown": 1})
    assert overall["C"]["cost_usd_per_correctly_resolved_request"] == pytest.approx(0.04)  # 4 x 0.01 for one resolved


def test_p95_is_the_nearest_rank_and_not_the_slowest_request() -> None:
    plain = {"requires_human": False, "expected_action": None}
    rows = [(plain, record("reply", "a reply", ms=ms), "suitable") for ms in range(200, 0, -10)]  # 10, 20 ... 200
    assert compare.metrics(rows, "nu")["latency_ms"] == {"median": 105, "p95": 190, "max": 200}  # the 19th of 20


def test_break_even_when_one_arm_wins_at_every_price_or_a_cost_is_unknown() -> None:
    def arm(total: float | None, not_resolved: int) -> dict:
        return {"requests": 1000, "not_resolved": {"n": not_resolved}, "cost_usd_per_request": {"total": total}}

    found = {"cheap-and-good": arm(0.001, 100), "dear-and-bad": arm(0.002, 300), "twin": arm(0.001, 100),
             "unknown": arm(None, 100)}
    better = compare.break_even("dear-and-bad", "cheap-and-good", found)
    assert (better["ticket_price_usd"], better["cheaper_below"], better["cheaper_above"]) == (
        None, "cheap-and-good", "cheap-and-good")
    assert better["model_usd_per_1000"] == pytest.approx({"dear-and-bad": 2, "cheap-and-good": 1})
    assert better["tickets_per_1000"] == {"dear-and-bad": 300, "cheap-and-good": 100}
    same = compare.break_even("cheap-and-good", "twin", found)
    assert (same["ticket_price_usd"], same["cheaper_below"], same["cheaper_above"]) == (None, None, None)
    unknown = compare.break_even("cheap-and-good", "unknown", found)
    assert (unknown["ticket_price_usd"], unknown["cheaper_below"]) == (None, None)
    assert unknown["model_usd_per_1000"] == {"cheap-and-good": 1, "unknown": None}


def test_a_missing_judgement_stops_the_report(tmp_path: Path, capsys) -> None:
    run_path = by_hand(tmp_path)
    full = dict(R1=True, R2=True, R3=False, R4=False, R5=True, R6=True, R7=True)
    partial = judge(tmp_path / "partial.json", **{**full, "R7": None})  # an item the judge left open
    with pytest.raises(SystemExit, match="partial.json has no true or false for 1 items: R7"):
        compare.main(["report", str(run_path), "--judgements", judge(tmp_path / "full.json", **full), partial])
    other = judge(tmp_path / "other.json", **full, R99=True)  # a judgement of another run's sheet
    with pytest.raises(SystemExit, match="other.json judges 1 items that are not on this run's sheet: R99"):
        compare.main(["report", str(run_path), "--judgements", other])
    key_path = compare.beside(run_path, "key.json")
    key = json.loads(key_path.read_text(encoding="utf-8"))
    key["R6"]["arms"] = ["C"]  # another run's key: no item stands for the reply that arm D sent for k1
    key_path.write_text(json.dumps(key), encoding="utf-8")
    with pytest.raises(SystemExit, match="does not cover the reply arm D sent for k1"):
        compare.main(["report", str(run_path), "--judgements", judge(tmp_path / "full.json", **full)])
    assert not compare.beside(run_path, "report.json").exists() and capsys.readouterr().out == ""


# Keys and --check


def test_run_stops_before_anything_when_a_key_is_missing(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    write_cases(CASES)
    monkeypatch.delenv("TYPESAFE_API_KEY")
    monkeypatch.delenv("COMPARE_LLM_API_KEY")
    monkeypatch.setenv("COMPARE_LLM_USD_PER_MTOK_OUT", "half a dollar")
    with pytest.raises(SystemExit) as stopped:
        compare.main(["run", "--split", "dev"])
    message = str(stopped.value)
    for name in ("TYPESAFE_API_KEY", "COMPARE_LLM_API_KEY (or LITELLM_API_KEY)", "COMPARE_LLM_USD_PER_MTOK_OUT"):
        assert name in message
    assert "COMPARE_LLM_BASE_URL" not in message and "Nothing was run" in message
    assert not any(world.values()) and not compare.RUNS.exists()


def test_the_llm_settings_fall_back_to_the_gateway_variables(world: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COMPARE_LLM_API_KEY", "COMPARE_LLM_BASE_URL"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("LITELLM_API_KEY", LLM_KEY)
    monkeypatch.setenv("LITELLM_BASE_URL", "http://gateway.test/v1")
    monkeypatch.setenv("COMPARE_LLM_MODEL", "another-model")
    monkeypatch.setenv("COMPARE_LLM_USD_PER_MTOK_IN", "0.25")
    assert compare.configure_llm() == []
    assert {name: os.environ[f"FALLBACK_{name}"] for name in (
        "BASE_URL", "API_KEY", "MODEL", "USD_PER_MTOK_IN", "USD_PER_MTOK_OUT")} == {
        "BASE_URL": "http://gateway.test/v1", "API_KEY": LLM_KEY, "MODEL": "another-model",
        "USD_PER_MTOK_IN": "0.25", "USD_PER_MTOK_OUT": "0.50"}


def test_check_passes_good_cases_and_counts_them(world: dict, capsys) -> None:
    write_cases(CASES + [case("zq-test-split", None, "Tell me a joke about cricket.", split="test",
                              kind="irrelevant", topics=[], in_scope=False)])
    assert compare.main(["--check"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["dev", "test"]
    assert [line.split() for line in out[1:]] == [
        ["single", "4", "0"], ["multi", "0", "0"], ["follow_up", "0", "0"], ["policy", "0", "0"],
        ["handoff", "1", "0"], ["irrelevant", "0", "1"], ["all", "5", "1"], ["requires_human", "1", "0"],
        ["quick_action", "0", "0"], ["families:", "6", "|", "dev", "5", "|", "test", "1"], ["OK"]]
    assert not any(world.values())  # no provider and no rule was called


def test_check_names_every_broken_line(world: dict, capsys) -> None:
    good, topics = CASES[0], [topic["id"] for topic in compare.help_topics()]
    broken = {  # one way of being wrong per line: what the line changes and what --check must say about it
        "duplicate id zq-rule": {},
        "split 'train'": {"id": "b1", "split": "train"},
        "family fam-zq-rule appears in both splits": {"id": "b2", "split": "test", "family": "fam-zq-rule"},
        "kind 'chat'": {"id": "b3", "kind": "chat"},
        "topics must be ids from data/help_topics.json, got ['weather']": {"id": "b4", "topics": ["weather"]},
        "an irrelevant case has no topics and in_scope false": {"id": "b5", "kind": "irrelevant"},
        "a single case needs at least one topic": {"id": "b6", "topics": []},
        "a multi case needs two or more topics": {"id": "b7", "kind": "multi"},
        "a follow_up case needs history": {"id": "b8", "kind": "follow_up"},
        "a handoff case must have requires_human true": {"id": "b9", "kind": "handoff"},
        "requires_human must be a boolean": {"id": "b10", "requires_human": "no", "requires_human_note": "why"},
        "requires_human_note must name the escalation condition": {"id": "b11", "requires_human": True},
        "message must be 1 to 500 characters": {"id": "b12", "message": "x" * 501},
        "expected must describe the correct outcome": {"id": "b13", "expected": "Say hello."},
        "expected_action 'refund'": {"id": "b14", "expected_action": "refund"},
        "quick_action 'exchange'": {"id": "b15", "quick_action": "exchange"},
        "label_status must be one of": {"id": "b16", "label_status": "final"},
        "history must be a list of {role: customer|agent, text}": {"id": "b17", "history": [{"role": "bot"}]},
        "item_id ITM-9 is not in the fixtures": {"id": "b18", "item_id": "ITM-9"},
        "expected_action needs an item": {"id": "b19", "item_id": None},
        "expected_action return is not permitted for this item (permitted: ['cancel'])": {
            "id": "b20", "expected_action": "return"},
        "fixtures do not load (KeyError: 'orders')": {
            "id": "b21", "fixtures": {"now": FIXTURES["now"], "customer": {}}},
        "malformed (TypeError": {"id": "b22", "topics": 7, "kind": "multi"},
    }
    lines = [json.dumps(good)] + [json.dumps({**good, "family": f"fam-{number}", **change})
                                  for number, change in enumerate(broken.values())]
    lines += ["{not json", json.dumps({"id": "b23"}), json.dumps(["a", "list"])]
    lines.append(json.dumps({**good, "id": "ok-multi", "family": "fam-ok", "kind": "multi", "topics": topics[:2]}))
    (compare.CASES / "cases.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert compare.main(["--check"]) == 1
    out = capsys.readouterr().out
    for number, problem in enumerate(broken, 2):
        assert f" - cases.jsonl:{number}: {problem}" in out
    assert f" - cases.jsonl:{len(broken) + 2}: not JSON" in out
    assert f" - cases.jsonl:{len(broken) + 3}: missing keys ['family', 'split'," in out
    assert f" - cases.jsonl:{len(broken) + 4}: missing keys ['id', 'family'," in out
    listed = sum(line.startswith(" - ") for line in out.splitlines())
    assert listed >= len(broken) + 3 and out.rstrip().endswith(f"{listed} PROBLEMS")
    assert [line.split() for line in out.splitlines() if line.startswith(("single", "multi", "all"))] == [
        ["single", "1", "0"], ["multi", "1", "0"], ["all", "2", "0"]]  # only the two good lines are counted
    with pytest.raises(SystemExit):  # the parser also refuses to do nothing
        compare.main([])
    assert compare.main(["run", "--split", "dev"]) == 1 and not compare.RUNS.exists()  # broken files are not run
