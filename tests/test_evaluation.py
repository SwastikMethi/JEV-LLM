"""Offline tests for evaluation/run.py. No test reaches a provider or writes into evaluation/scores or reports."""
import collections
import copy
import hashlib
import json
import shutil

import httpx
import pytest

from app import jev, rules
from app.store import Store
from evaluation import run

CUTOFFS = {"supported_min": 0.7, "complete_min": 0.7, "requires_human_handoff": 0.7, "requires_human_low": 0.3}
GOOD = {"supported": 0.97, "complete": 0.99, "requires_human": 0.02}
KURTA_DRAFT = "Your kurta has shipped. It is expected on November 24."


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    """Scores and reports go to a temporary directory, and a stray gate request could only reach a closed port."""
    monkeypatch.setattr(run, "SCORES", tmp_path / "scores")
    monkeypatch.setattr(run, "REPORTS", tmp_path / "reports")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setitem(jev.GATE, "thresholds", CUTOFFS)  # the tests hold whatever cutoffs are frozen later
    return tmp_path


@pytest.fixture(scope="module")
def cases():
    found, errors = run.load_cases(run.CASES)
    assert errors == []
    return found


def row(name, flow, supported, complete, requires_human, scores):
    """A scored candidate. Each flow of a hand-built set is one situation."""
    return {"id": name, "situation": flow, "flow": flow, "supported": supported, "complete": complete,
            "requires_human": requires_human, "scores": dict(zip(jev.QUESTIONS, scores))}


def leaks(cases, sent):
    """Label text found in `sent` (JSON text): ids, family names and notes anywhere, and the short label values
    (kinds, splits, sources) as whole strings, because "good" and "test" are also plain words and the policy has
    an escalation rule named damaged_or_wrong_item."""
    anywhere = {"label_status", "second_requires_human", "requires_human_note", "ai_draft", "ai_agreed", "ai_disputed"}
    for c in cases:
        anywhere |= {c["id"], c["family"], c["requires_human_note"]}
        anywhere |= {value for cand in c["candidates"] for value in (cand["id"], cand["note"])}
    as_sent = [json.dumps(value, ensure_ascii=False)[1:-1] for value in anywhere if value]
    whole = [json.dumps(value) for value in (*run.KINDS, "dev", "test", "rule", "written", "reviewed")]
    return [value for value in (*as_sent, *whole) if value in sent]


# --- the metric arithmetic ---------------------------------------------------------------------------------------

GATE_ROWS = [
    row("a", "track", True, True, False, (0.9, 0.9, 0.1)),   # suitable, accepted
    row("b", "track", True, True, False, (0.9, 0.5, 0.1)),   # suitable, rejected
    row("c", "track", False, True, False, (0.8, 0.9, 0.1)),  # unsupported, accepted
    row("d", "track", True, False, False, (0.9, 0.2, 0.1)),  # incomplete, rejected
    row("e", "refund", True, True, True, (0.9, 0.9, 0.9)),   # needs a person, handed off
    row("f", "refund", True, True, True, (0.9, 0.9, 0.5)),   # needs a person, only rejected: the handoff is missed
    row("g", "refund", True, False, True, (0.9, 0.9, 0.1)),  # needs a person and is incomplete, accepted outright
    # Exactly at the cutoffs, as router.decide reads them: support and completeness count as met, and a
    # requires_human score at the low cutoff is already too high to accept.
    row("h", "track", True, True, False, (0.7, 0.7, 0.3)),   # suitable, rejected
]


def test_gate_metrics_on_a_hand_built_set():
    metrics = run.gate_metrics(GATE_ROWS, CUTOFFS)
    assert (metrics["situations"], metrics["candidates"]) == (2, 8)
    assert metrics["accepted_that_were_unsuitable"] == {"n": 2, "d": 3, "rate": 0.6667}  # c and g, of a, c and g
    assert metrics["unsuitable_that_were_accepted"] == {"n": 2, "d": 5, "rate": 0.4}  # of c, d, e, f and g
    assert metrics["failed_label"] == {"supported": 1, "complete": 1, "requires_human": 1}
    assert metrics["suitable_not_accepted"] == {"n": 2, "d": 3, "rate": 0.6667}  # b and h, of a, b and h
    assert metrics["situations_that_require_a_human"] == 1  # e, f and g are candidates of one situation
    assert metrics["required_handoffs_missed"] == {"n": 2, "d": 3, "rate": 0.6667}  # f and g, of e, f and g
    assert metrics["required_handoffs_accepted"] == {"n": 1, "d": 3, "rate": 0.3333}  # g
    assert metrics["confusion"] == {
        "supported >= 0.7": {"tp": 7, "fn": 0, "fp": 1, "tn": 0},
        "complete >= 0.7": {"tp": 5, "fn": 1, "fp": 1, "tn": 1},
        "requires_human >= 0.7": {"tp": 1, "fn": 2, "fp": 0, "tn": 5},
        "requires_human >= 0.3": {"tp": 2, "fn": 1, "fp": 1, "tn": 4},  # the false positive is h
    }
    assert metrics["ids"] == {
        "accepted_that_were_unsuitable": ["c (supported)", "g (complete, requires_human)"],
        "suitable_not_accepted": ["b", "h"],
        "required_handoffs_missed": ["f", "g"],
        "required_handoffs_accepted": ["g"],
    }


def test_metrics_are_given_overall_and_by_flow_with_no_rate_for_an_empty_denominator():
    scopes = run.by_flow(GATE_ROWS, lambda part: run.gate_metrics(part, CUTOFFS))
    assert list(scopes) == ["overall", "track", "refund"]
    assert scopes["track"]["accepted_that_were_unsuitable"] == {"n": 1, "d": 2, "rate": 0.5}
    assert scopes["track"]["required_handoffs_missed"] == {"n": 0, "d": 0, "rate": None}
    assert scopes["refund"]["required_handoffs_missed"] == {"n": 2, "d": 3, "rate": 0.6667}
    lines = dict(run.leaves(scopes["track"]))
    assert lines["accepted_that_were_unsuitable"] == "1/2 (50.0%)"
    assert lines["required_handoffs_missed"] == "0/0"
    assert lines["confusion complete >= 0.7 (tp/fn/fp/tn)"] == "3/1/0/1"


def test_sweep_prefers_fewest_bad_accepts_then_fewest_missed_handoffs_then_most_accepted():
    rows = [
        row("u", "track", False, True, False, (0.75, 0.9, 0.1)),   # unsuitable: supported_min 0.8 keeps it out
        row("s1", "track", True, True, False, (0.72, 0.9, 0.1)),   # suitable, and lost when u is kept out
        row("s2", "track", True, True, False, (0.95, 0.9, 0.1)),   # suitable
        row("h", "track", True, True, True, (0.05, 0.05, 0.45)),   # needs a person: caught by a handoff cutoff of 0.4
        row("s3", "track", True, True, False, (0.95, 0.9, 0.42)),  # suitable, and handed off when h is caught
        row("s4", "track", True, True, False, (0.95, 0.65, 0.1)),  # suitable: accepted once complete_min is 0.6
    ]

    def outcome(setting):  # named here, not read from the module: the order of preference is what is under test
        return tuple(setting[name]["n"] for name in (
            "accepted_that_were_unsuitable", "required_handoffs_missed", "suitable_not_accepted"))

    found = run.sweep(rows, CUTOFFS)
    assert outcome(found["current"]) == (1, 1, 2)
    assert found["current"]["cutoffs"] == CUTOFFS
    chosen = found["best"][0]
    assert outcome(chosen) == (0, 0, 2)
    # Of the settings with that outcome, the one nearest the current cutoffs.
    assert chosen["cutoffs"] == {
        "supported_min": 0.8, "complete_min": 0.6, "requires_human_handoff": 0.4, "requires_human_low": 0.3}
    assert chosen["suitable_not_accepted"] == {"n": 2, "d": 4, "rate": 0.5}
    # Accepting nothing (0, 0, 4) still ranks above missing one handoff to accept three of four (0, 1, 1).
    assert [outcome(setting) for setting in found["best"][:4]] == [(0, 0, 2), (0, 0, 3), (0, 0, 4), (0, 1, 1)]
    # A setting that accepts three of the four suitable candidates exists, and loses: it lets u through.
    looser = run.gate_metrics(rows, {**CUTOFFS, "complete_min": 0.6, "requires_human_handoff": 0.4})
    assert (looser["accepted_that_were_unsuitable"]["n"], looser["suitable_not_accepted"]["n"]) == (1, 1)


def outcome_row(name, flow, route, ms, cost, **more):
    return {
        "id": name, "flow": flow, "requires_human": False, "route": route, "reply": f"final {name}",
        "ticket_ref": None, "suitable": None, "needs_review": False, "baseline": "rule_draft",
        "baseline_reply": f"draft {name}", "baseline_suitable": None, "calls": {"jev": 1, "fallback": 0},
        "error": None, "elapsed_ms": {"total": ms}, "cost_usd": cost, **more,
    }


E2E_ROWS = [
    # An accepted rule draft that the labels call suitable, and one they call unsuitable.
    outcome_row("r1", "track", "rule", 100, 0.00005, reply="draft r1", suitable=True, baseline_suitable=True),
    outcome_row("r2", "track", "rule", 200, 0.00005, reply="draft r2", suitable=False, baseline_suitable=False),
    # An unsuitable draft that the gate stopped; no label covers the fallback's reply.
    outcome_row("r3", "track", "fallback", 3000, 0.0001, needs_review=True, baseline_suitable=False,
                calls={"jev": 2, "fallback": 1}),
    # A suitable draft that the gate did not accept; the fallback failed, so the customer got a ticket.
    outcome_row("r4", "refund", "handoff", 400, None, ticket_ref="TKT-0001", baseline_suitable=True,
                calls={"jev": 1, "fallback": 1}, error="fallback: timeout"),
    # Two situations that require a person: a mandated ticket, and a fallback reply that misses the handoff. Its
    # text is a labelled candidate, so the labels call it unsuitable and nobody has to review it.
    outcome_row("r5", "refund", "mandated_ticket", 5, 0.0, requires_human=True, ticket_ref="TKT-0001",
                baseline="mandated_ticket", baseline_reply=None, calls={"jev": 0, "fallback": 0}),
    outcome_row("r6", "refund", "fallback", 9000, 0.0001, requires_human=True, suitable=False,
                baseline="no_rule_reply", baseline_reply=None, calls={"jev": 1, "fallback": 1}),
]


def test_e2e_metrics_on_a_hand_built_set():
    metrics = run.e2e_metrics(E2E_ROWS)
    third, half, sixth = 0.3333, 0.5, 0.1667
    assert metrics["situations"] == 6
    assert metrics["resolved_on_rules_path"] == {"n": 2, "d": 6, "rate": third}
    assert metrics["routes"] == {
        "fallback": {"n": 2, "d": 6, "rate": third}, "handoff": {"n": 1, "d": 6, "rate": sixth},
        "mandated_ticket": {"n": 1, "d": 6, "rate": sixth}, "rule": {"n": 2, "d": 6, "rate": third},
    }
    assert metrics["required_handoffs_missed"] == {"n": 1, "d": 2, "rate": half}
    assert metrics["accepted_rule_drafts_unsuitable"] == {"n": 1, "d": 2, "rate": half}  # r2, of r1 and r2
    assert metrics["other_labelled_replies_unsuitable"] == {"n": 1, "d": 1, "rate": 1.0}  # r6
    assert metrics["final_replies_for_human_review"] == {"n": 1, "d": 6, "rate": sixth}  # r3
    assert metrics["baseline"] == {
        "rule_draft": {"n": 4, "d": 6, "rate": 0.6667}, "mandated_ticket": {"n": 1, "d": 6, "rate": sixth},
        "no_rule_reply": {"n": 1, "d": 6, "rate": sixth},
    }
    assert metrics["baseline_on_required_handoffs"] == {
        "rule_draft": {"n": 0, "d": 2, "rate": 0.0}, "mandated_ticket": {"n": 1, "d": 2, "rate": half},
        "no_rule_reply": {"n": 1, "d": 2, "rate": half},
    }
    assert metrics["baseline_drafts_unsuitable"] == {"n": 2, "d": 4, "rate": half}
    assert metrics["unsuitable_baseline_drafts_stopped"] == {"n": 1, "d": 2, "rate": half}  # r3; r2 was sent
    assert metrics["suitable_baseline_drafts_not_accepted"] == {"n": 1, "d": 2, "rate": half}  # r4
    assert metrics["provider_calls"] == {"jev": 6, "fallback": 3}
    assert metrics["provider_errors"] == 1
    assert metrics["elapsed_ms"] == {"median": 300, "p95": 9000, "max": 9000}
    assert metrics["cost_usd"] == {"mean": 0.00006, "total": 0.0003, "unknown": 1}
    assert metrics["ids"] == {
        "required_handoffs_missed": ["r6"], "accepted_rule_drafts_unsuitable": ["r2"],
        "other_labelled_replies_unsuitable": ["r6"], "final_replies_for_human_review": ["r3"],
        "unsuitable_baseline_drafts_stopped": ["r3"], "suitable_baseline_drafts_not_accepted": ["r4"],
    }
    assert run.by_flow(E2E_ROWS, run.e2e_metrics)["track"]["elapsed_ms"] == {"median": 200, "p95": 3000, "max": 3000}


# --- the real case files -----------------------------------------------------------------------------------------

def test_real_case_files_keep_every_family_in_one_split(cases):
    assert len(cases) == 100
    assert collections.Counter(c["flow"] for c in cases) == dict.fromkeys(rules.INTENTS, 20)
    splits = collections.defaultdict(set)
    for c in cases:
        splits[c["family"]].add(c["split"])
    assert [family for family, found in splits.items() if len(found) != 1] == []
    assert {split for found in splits.values() for split in found} == {"dev", "test"}


def test_check_accepts_the_real_files(capsys):
    assert run.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("OK")
    header = out.splitlines()[0].split()
    assert header[:3] == ["flow", "split", "situations"] and header[-3:] == ["follow_ups", "requires_human", "disputed"]
    totals = [line.split() for line in out.splitlines() if line.startswith("all ")]
    assert [line[1] for line in totals] == ["dev", "test"] and sum(int(line[2]) for line in totals) == 100


@pytest.mark.parametrize("problem, change", [
    ("track.jsonl:1: not JSON", None),
    ("kind good must be supported and complete", lambda c: c["candidates"][0].update(supported=False)),
    ("appears in both splits", lambda c: c.update(split="test")),
    ("label_status must be one of", lambda c: c.update(label_status="done")),
    ("requires_human must be a boolean", lambda c: c.update(requires_human="no")),
    ("needs 2 to 4 candidates", lambda c: c.update(candidates=c["candidates"][:1])),
    ("label_status ai_agreed does not match the two labellings",
     lambda c: c["candidates"][0]["second"].update(complete=False)),
    ("label_status ai_agreed needs a second label", lambda c: c.pop("second_requires_human")),
])
def test_check_rejects_a_broken_line(problem, change, tmp_path, monkeypatch, capsys):
    broken = shutil.copytree(run.CASES, tmp_path / "cases")
    lines = (broken / "track.jsonl").read_text(encoding="utf-8").split("\n")
    if change is None:
        lines[0] = lines[0][:-1]  # the closing brace is gone
    else:
        case = json.loads(lines[0])
        change(case)
        lines[0] = json.dumps(case, ensure_ascii=False)
    (broken / "track.jsonl").write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(run, "CASES", broken)
    assert run.main(["--check"]) == 1
    out = capsys.readouterr().out
    assert problem in out and "PROBLEMS" in out
    # A mode refuses to run on broken files: it reports the problems, calls no provider and writes no report.
    assert run.main(["gate"]) == 1
    assert not (tmp_path / "reports").exists()


# --- labels stay out of provider requests ------------------------------------------------------------------------

def test_the_leak_detector_finds_label_text(cases):
    assert leaks(cases, json.dumps(cases[0], ensure_ascii=False))
    assert leaks(cases, json.dumps({"kind": "good"}))
    assert not leaks(cases, json.dumps({"customer_message": "Not good enough. I want a second test."}))


def test_no_label_reaches_jev(cases, monkeypatch, scratch):
    """The real jev.score runs on a mock transport for every candidate of every real case."""
    bodies, calls = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        answers = {name: {"type": "noul", "noul": 0.5} for name in jev.QUESTIONS}
        usage = {"input_tokens": 1200, "output_tokens": 3}
        return httpx.Response(200, json={"model": "jev-1.14.0", "answers": answers, "usage": usage})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(jev, "_client", lambda: client)
    real_score = jev.score
    monkeypatch.setattr(jev, "score", lambda *args: calls.append(args) or real_score(*args))
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    # The real jev.score refuses a policy the gate was not frozen for. data/gate.json is refrozen for each
    # policy version once it is re-validated; which version it holds today is not what this test is about.
    monkeypatch.setitem(jev.GATE, "policy_version", Store().policy["version"])

    rows = run.gate_rows(cases)

    # What jev.score is given is the turn and the reply, built here from the fields a reviewer sees as evidence.
    expected = []
    for c in cases:
        store = Store(fixtures=c["fixtures"])
        item = store.item(c["item_id"]) if c["item_id"] else None
        expected += [(c["message"], c["history"], item, store.policy, store.today, cand["reply"])
                     for cand in c["candidates"]]
    assert calls == expected
    assert len(bodies) == len(rows) == sum(len(c["candidates"]) for c in cases)
    assert all(set(body) == {"model", "state", "questions"} for body in bodies)
    assert leaks(cases, json.dumps([calls, bodies], ensure_ascii=False)) == []

    # The cache key is the hash of what was sent: the pinned model, the question wording and the state.
    def sent_key(body: dict) -> str:
        wording = {name: {key: value for key, value in question.items() if key != "type"}
                   for name, question in body["questions"].items()}
        content = {"model": body["model"], "questions": wording, "state": body["state"]}
        return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    saved = (scratch / "scores" / f"{jev.GATE['version']}.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["key"] for line in saved] == [sent_key(body) for body in bodies]


def test_e2e_compares_the_baseline_with_the_full_bot(cases, providers, monkeypatch, scratch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    picked = [c for c in cases if c["id"] in ("track-001", "track-002", "track-008")]
    # The kurta draft is accepted for the first message. The second message now gets a rule draft that answers both
    # of its parts, which no archived label covers; the fake gate scores it incomplete here, and the fallback reply
    # is then accepted. The third situation is a disputed delivery scan: a mandated ticket, no model.
    both_parts = (f"{KURTA_DRAFT} It shipped on November 20, so it can no longer be cancelled."
                  " Once it is delivered, it can be returned within 7 days of the delivery date.")
    providers.scores = [GOOD, {**GOOD, "complete": 0.04}, GOOD]
    unlabelled = "Your kurta has shipped and is due on November 24. It can no longer be cancelled."
    providers.draft_result = {**providers.draft_result, "text": unlabelled}

    run.e2e(picked, "dev", "track-00")

    saved = json.loads(next((scratch / "reports").glob("*-e2e-dev.json")).read_text(encoding="utf-8"))
    rows = {found["id"]: found for found in saved["rows"]}
    first, second, third = rows["track-001"], rows["track-002"], rows["track-008"]
    assert (first["baseline"], first["baseline_reply"], first["baseline_suitable"]) == ("rule_draft", KURTA_DRAFT, True)
    assert (first["route"], first["reply"], first["matched"], first["suitable"], first["needs_review"]) == (
        "rule", KURTA_DRAFT, "track-001-a", True, False)
    assert (second["baseline"], second["baseline_reply"], second["baseline_suitable"]) == (
        "rule_draft", both_parts, None)
    assert (second["route"], second["reply"], second["matched"], second["suitable"], second["needs_review"]) == (
        "fallback", unlabelled, None, None, True)
    assert second["calls"] == {"jev": 2, "fallback": 1} and second["pending_action"] is None
    assert (third["baseline"], third["baseline_reply"], third["route"], third["ticket_ref"]) == (
        "mandated_ticket", None, "mandated_ticket", "TKT-0001")
    assert third["calls"] == {"jev": 0, "fallback": 0} and third["needs_review"] is False

    overall = saved["metrics"]["overall"]
    assert overall["resolved_on_rules_path"] == {"n": 1, "d": 3, "rate": 0.3333}
    assert overall["unsuitable_baseline_drafts_stopped"] == {"n": 0, "d": 0, "rate": None}
    assert overall["suitable_baseline_drafts_not_accepted"] == {"n": 0, "d": 1, "rate": 0.0}
    assert overall["required_handoffs_missed"] == {"n": 0, "d": 1, "rate": 0.0}
    assert overall["final_replies_for_human_review"] == {"n": 1, "d": 3, "rate": 0.3333}
    assert (saved["mode"], saved["split"], saved["only"]) == ("e2e", "dev", "track-00")

    sheet = next((scratch / "reports").glob("*-e2e-dev-review.jsonl")).read_text(encoding="utf-8").splitlines()
    assert len(sheet) == 1
    line = json.loads(sheet[0])
    assert (line["id"], line["route"], line["reply"]) == ("track-002", "fallback", unlabelled)
    assert line["evidence"]["customer_message"] == "When will it arrive, and can I still cancel it?"
    assert line["evidence"]["order_record"]["id"] == "ITM-1001"
    assert [line[field] for field in ("supported", "complete", "action_ok", "reviewer", "note")] == [
        None, None, None, "", ""]

    assert leaks(cases, json.dumps([providers.score_calls, providers.draft_calls], ensure_ascii=False)) == []
    assert "final replies for human review" in capsys.readouterr().out


# --- the score cache and the commands ----------------------------------------------------------------------------

def test_a_second_run_makes_no_call_and_new_wording_scores_again(cases, providers, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    picked = cases[:3]
    count = sum(len(c["candidates"]) for c in picked)

    first = run.gate_rows(picked)
    assert len(providers.score_calls) == count and not any(found["cached"] for found in first)
    second = run.gate_rows(picked)
    assert len(providers.score_calls) == count  # nothing was scored again
    assert all(found["cached"] for found in second)
    assert [found["scores"] for found in second] == [found["scores"] for found in first]
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert len(run.gate_rows(picked)) == count  # cached scores need no key

    _, turn = run.situation(picked[0])
    before = run.cache_key(turn, KURTA_DRAFT)
    assert run.cache_key(turn, KURTA_DRAFT + " Thank you.") != before  # another reply is another request
    questions = copy.deepcopy(jev.GATE["questions"])
    questions["complete"]["criteria"]["false"] += " A reply to a different question does not count."
    monkeypatch.setitem(jev.GATE, "questions", questions)
    assert run.cache_key(turn, KURTA_DRAFT) != before
    with pytest.raises(SystemExit, match="TYPESAFE_API_KEY is not set"):
        run.gate_rows(picked)  # the scores for the old wording no longer count
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    assert not any(found["cached"] for found in run.gate_rows(picked))
    assert len(providers.score_calls) == 2 * count


@pytest.mark.parametrize("mode", ["gate", "e2e"])
def test_without_a_key_and_cached_scores_the_command_stops(mode, scratch, capsys):
    with pytest.raises(SystemExit) as stopped:
        run.main([mode, "--only", "track-001"])
    assert "TYPESAFE_API_KEY is not set" in str(stopped.value)
    assert "Nothing was" in str(stopped.value)
    assert not (scratch / "reports").exists() and not (scratch / "scores").exists()  # no partial metrics
    assert capsys.readouterr().out == ""


def test_a_failed_score_stops_the_run_and_is_never_a_no(cases, providers, monkeypatch, scratch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    providers.scores = [GOOD, providers.JevError("timeout")]
    with pytest.raises(SystemExit, match="no usable scores for track-001-b"):
        run.main(["gate", "--only", "track-001"])
    assert not (scratch / "reports").exists()
    kept = (scratch / "scores" / f"{jev.GATE['version']}.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(kept) == 1  # the score that did arrive is there for the next run


@pytest.mark.parametrize("argv", [
    ["gate", "--sweep", "--split", "test"], ["gate", "--sweep", "--split", "all"], ["e2e", "--sweep"],
])
def test_sweep_is_refused_off_the_development_split(argv, providers, capsys):
    with pytest.raises(SystemExit) as stopped:
        run.main(argv)
    assert stopped.value.code == 2
    assert "development split only" in capsys.readouterr().err
    assert providers.score_calls == []


def test_gate_command_reports_and_sweeps_without_writing_gate_json(providers, monkeypatch, scratch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    gate_json = run.HERE.parent / "data" / "gate.json"
    before = gate_json.read_bytes()

    assert run.main(["gate", "--sweep", "--only", "track-001"]) == 0

    assert gate_json.read_bytes() == before
    out = capsys.readouterr().out
    assert "current" in out and "chosen" in out and "data/gate.json is unchanged" in out
    saved = json.loads(next((scratch / "reports").glob("*-gate-dev.json")).read_text(encoding="utf-8"))
    assert (saved["mode"], saved["split"], saved["only"]) == ("gate", "dev", "track-001")
    assert saved["gate"]["model"] == jev.GATE["model"] and saved["gate"]["questions"] == jev.GATE["questions"]
    assert [found["id"] for found in saved["rows"]] == ["track-001-a", "track-001-b", "track-001-c"]
    assert {found["decision"] for found in saved["rows"]} == {"accept"}  # the fake scores are all good
    # The two written candidates are labelled unsupported, so the fake gate accepted two unsuitable replies.
    assert saved["metrics"]["overall"]["accepted_that_were_unsuitable"] == {"n": 2, "d": 3, "rate": 0.6667}
    assert saved["sweep"]["current"]["cutoffs"] == CUTOFFS


def test_no_arguments_runs_gate_then_e2e_on_the_development_split(cases, providers, monkeypatch, scratch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    assert run.main([]) == 0
    dev = [c for c in cases if c["split"] == "dev"]
    gate = json.loads(next((scratch / "reports").glob("*-gate-dev.json")).read_text(encoding="utf-8"))
    e2e = json.loads(next((scratch / "reports").glob("*-e2e-dev.json")).read_text(encoding="utf-8"))
    assert gate["metrics"]["overall"]["candidates"] == sum(len(c["candidates"]) for c in dev)
    assert [found["id"] for found in e2e["rows"]] == [c["id"] for c in dev]
    assert e2e["metrics"]["overall"]["situations"] == len(dev)
