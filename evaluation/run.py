"""Replay the labelled cases, compare the bot with the labels and write a metrics report.

    python -m evaluation.run --check
        Validate the case files and print counts. No network.
    python -m evaluation.run gate [--split dev|test|all] [--sweep] [--only ID_PREFIX]
        The gate-specific comparison: Jev scores every candidate reply and router.decide is held against the labels.
    python -m evaluation.run e2e [--split dev|test|all] [--only ID_PREFIX]
        The end-to-end comparison: the rules-only baseline next to the full bot, with the live gate and fallback.
    python -m evaluation.run
        gate, then e2e, on the development split.

Run it from the project root. A provider is given only what the router gives it: labels, kinds, notes, family names,
splits and ids stay in this process. Jev's scores are cached in evaluation/scores; reports go to evaluation/reports.
"""
import argparse
import collections
import hashlib
import itertools
import json
import math
import os
import statistics
import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from app import jev, router, rules
from app.store import Store

HERE = Path(__file__).resolve().parent
CASES, SCORES, REPORTS = HERE / "cases", HERE / "scores", HERE / "reports"
KEYS = ("id", "family", "flow", "split", "fixtures", "item_id", "history", "message", "quick_action",
        "requires_human", "requires_human_note", "candidates", "label_status")
KINDS = ("good", "incomplete", "incorrect_claim", "wrong_item", "unsupported_promise")
STATUSES = ("ai_draft", "ai_agreed", "ai_disputed", "reviewed")
BASELINE = ("rule_draft", "mandated_ticket", "no_rule_reply")
# The sweep's order of preference. Each is a count to keep low; the last one is "most suitable candidates accepted".
SWEEP_ORDER = ("accepted_that_were_unsuitable", "required_handoffs_missed", "suitable_not_accepted")
# ponytail: cutoffs are tried in steps of 0.1 (3,645 settings). Jev's scores sit near 0 or 1, so a finer grid rarely
# changes the outcome; narrow the step around a cutoff if many scores fall next to it.
GRID = [step / 10 for step in range(1, 10)]


def load_cases(directory: Path) -> tuple[list[dict], list[str]]:
    """The situations in the case files, in flow order, and every problem found in the files."""
    cases, errors, family_split, ids = [], [], {}, set()
    for path in sorted(directory.glob("*.jsonl")):
        for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if not line.strip():
                continue
            where, before = f"{path.name}:{number}", len(errors)
            try:
                c = json.loads(line)
            except json.JSONDecodeError as error:
                errors.append(f"{where}: not JSON ({error})")
                continue
            missing = [key for key in KEYS if key not in c] if isinstance(c, dict) else list(KEYS)
            if missing:
                errors.append(f"{where}: missing keys {missing}")
                continue
            found = [
                (c["id"] in ids, f"duplicate id {c['id']}"),
                (c["flow"] not in rules.INTENTS, f"flow {c['flow']!r}"),
                (c["split"] not in ("dev", "test"), f"split {c['split']!r}"),
                (family_split.setdefault(c["family"], c["split"]) != c["split"],
                 f"family {c['family']} appears in both splits"),
                (c["label_status"] not in STATUSES, f"label_status must be one of {', '.join(STATUSES)}"),
                (not isinstance(c["requires_human"], bool), "requires_human must be a boolean"),
                (not (isinstance(c["message"], str) and 0 < len(c["message"].strip()) <= 500),
                 "message must be 1 to 500 characters"),
                (c["quick_action"] is not None and c["quick_action"] not in rules.INTENTS,
                 f"quick_action {c['quick_action']!r}"),
                (not (isinstance(c["history"], list) and all(
                    isinstance(turn, dict) and turn.get("role") in ("customer", "agent")
                    and isinstance(turn.get("text"), str) for turn in c["history"])),
                 "history must be a list of {role: customer|agent, text}"),
            ]
            ids.add(c["id"])
            try:
                store = Store(fixtures=c["fixtures"])
                if c["item_id"] is not None and store.item(c["item_id"]) is None:
                    found.append((True, f"item_id {c['item_id']} is not in the fixtures"))
            except Exception as error:  # noqa: BLE001 - any fixture problem is reported, not raised
                found.append((True, f"fixtures do not load ({type(error).__name__}: {error})"))
            cands = c["candidates"]
            if not (isinstance(cands, list) and 2 <= len(cands) <= 4):
                found.append((True, "needs 2 to 4 candidates"))
                cands = []
            for cand in cands:
                if not isinstance(cand, dict) or any(
                        key not in cand for key in ("id", "kind", "reply", "supported", "complete", "note")):
                    found.append((True, "candidate needs id, kind, reply, supported, complete, note"))
                    continue
                name, kind = cand["id"], cand["kind"]
                labelled = isinstance(cand["supported"], bool) and isinstance(cand["complete"], bool)
                found += [
                    (name in ids, f"duplicate id {name}"),
                    (kind not in KINDS, f"{name} kind {kind!r}"),
                    (not (isinstance(cand["reply"], str) and 0 < len(cand["reply"].strip()) <= 600),
                     f"{name} reply must be 1 to 600 characters"),
                    (not labelled, f"{name} supported and complete must be booleans"),
                    (labelled and kind == "good" and not (cand["supported"] and cand["complete"]),
                     f"{name} kind good must be supported and complete"),
                    (labelled and kind == "incomplete" and cand["complete"],
                     f"{name} kind incomplete must have complete false"),
                    (labelled and kind in ("incorrect_claim", "wrong_item", "unsupported_promise")
                     and cand["supported"], f"{name} kind {kind} must have supported false"),
                ]
                ids.add(name)
            errors += [f"{where}: {problem}" for bad, problem in found if bad]
            # ai_agreed and ai_disputed say whether the second labelling matches the labels of record. They must
            # stay true, so a label that is changed by hand has to move the status on (to reviewed).
            status = c["label_status"]
            if len(errors) == before and status in ("ai_agreed", "ai_disputed"):
                first = [c["requires_human"]] + [cand[label] for cand in cands for label in ("supported", "complete")]
                second = [c.get("second_requires_human")] + [
                    cand["second"].get(label) if isinstance(cand.get("second"), dict) else None
                    for cand in cands for label in ("supported", "complete")]
                if not all(isinstance(label, bool) for label in second):
                    errors.append(f"{where}: label_status {status} needs a second label on the situation and on"
                                  " every candidate")
                elif (status == "ai_agreed") != (first == second):
                    errors.append(f"{where}: label_status {status} does not match the two labellings")
            if len(errors) == before:
                cases.append(c)
    cases.sort(key=lambda c: rules.INTENTS.index(c["flow"]))  # stable: file order within a flow
    return cases, errors


def check(cases: list[dict], errors: list[str]) -> int:
    """Print the counts by flow and split, then every problem. Returns the exit status."""
    names = ("situations", "candidates", *KINDS, "follow_ups", "requires_human", "disputed")
    counts = collections.defaultdict(collections.Counter)
    for c in cases:
        for flow in (c["flow"], "all"):
            count = counts[flow, c["split"]]
            count.update(cand["kind"] for cand in c["candidates"])
            count.update(situations=1, candidates=len(c["candidates"]), follow_ups=bool(c["history"]),
                         requires_human=c["requires_human"], disputed=c["label_status"] == "ai_disputed")
    print("flow     split  " + "  ".join(names))
    for flow, split in itertools.product((*rules.INTENTS, "all"), ("dev", "test")):
        if (flow, split) in counts:
            print(f"{flow:8} {split:5}  " + "  ".join(f"{counts[flow, split][name]:>{len(name)}}" for name in names))
    families = collections.Counter({c["family"]: c["split"] for c in cases}.values())
    print(f"families: {sum(families.values())} | dev {families['dev']} | test {families['test']}")
    for error in errors:
        print(" -", error)
    print(f"{len(errors)} PROBLEMS" if errors else "OK")
    return 1 if errors else 0


def situation(c: dict) -> tuple[Store, tuple]:
    """A fresh store holding the situation's records and earlier turns, and the turn as the router hands it to a
    provider: (message, history, item, policy, today)."""
    store = Store(fixtures=c["fixtures"])
    for earlier in c["history"]:
        store.add_message(earlier["role"], earlier["text"], c["item_id"])
    item = store.item(c["item_id"]) if c["item_id"] is not None else None
    return store, (c["message"], store.history(c["item_id"]), item, store.policy, store.today)


def failed(labels: dict) -> list[str]:
    """The labels that make a reply unsuitable. It is suitable when it is supported and complete and the situation
    does not require a human."""
    unmet = [name for name in ("supported", "complete") if not labels[name]]
    return unmet + ["requires_human"] * labels["requires_human"]


def ratio(n: int, d: int) -> dict:
    return {"n": n, "d": d, "rate": round(n / d, 4) if d else None}


def text(found: dict) -> str:
    return f"{found['n']}/{found['d']}" + (f" ({found['rate']:.1%})" if found["d"] else "")


def by_flow(rows: list[dict], metrics: Callable[[list[dict]], dict]) -> dict:
    """The metrics over every row ("overall") and over each flow's rows."""
    parts = {flow: [row for row in rows if row["flow"] == flow] for flow in rules.INTENTS}
    return {"overall": metrics(rows), **{flow: metrics(part) for flow, part in parts.items() if part}}


def leaves(value: object, label: str = "") -> Iterator[tuple[str, str]]:
    """The numbers of a metrics dict as (label, text): a ratio reads 3/80 (3.8%), a group of counts 38/2/1/40."""
    if not isinstance(value, dict):
        yield label, "n/a" if value is None else f"{value:.6f}" if isinstance(value, float) else str(value)
    elif "rate" in value:
        yield label, text(value)
    elif value and all(isinstance(count, int) for count in value.values()):
        yield f"{label} ({'/'.join(value)})", "/".join(map(str, value.values()))
    else:
        for key, inner in value.items():
            if key != "ids":
                yield from leaves(inner, f"{label} {key}".strip())


def show(scopes: dict) -> None:
    """Print one line per metric and one column per scope (overall, then each flow), then the ids behind the counts."""
    columns = {scope: dict(leaves(metrics)) for scope, metrics in scopes.items()}
    wide = max(len(cell) for column in columns.values() for cell in column.values()) + 2
    label_wide = max(map(len, columns["overall"])) + 2
    print((" " * label_wide + "".join(f"{scope:<{wide}}" for scope in columns)).rstrip())
    for label in columns["overall"]:
        cells = "".join(f"{column.get(label, '-'):<{wide}}" for column in columns.values())
        print(f"{label.replace('_', ' '):<{label_wide}}{cells}".rstrip())
    for name, ids in scopes["overall"]["ids"].items():
        print(f"{name.replace('_', ' ')}: {', '.join(ids) or 'none'}")


def report(mode: str, split: str, only: str, body: dict) -> Path:
    """Write a report under evaluation/reports, with the gate it was measured against, and return its path."""
    now = datetime.now(timezone.utc)
    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"{now:%Y%m%dT%H%M%SZ}-{mode}-{split}.json"
    head = {"mode": mode, "split": split, "only": only or None, "at": now.isoformat(), "gate": jev.GATE}
    path.write_text(json.dumps({**head, **body}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def cache_key(turn: tuple, reply: str) -> str:
    """SHA-256 of what a scoring request carries: the pinned model, the question wording and the state object."""
    content = {"model": jev.GATE["model"], "questions": jev.GATE["questions"],
               "state": {**jev.evidence(*turn), "proposed_reply": reply}}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def gate_rows(cases: list[dict]) -> list[dict]:
    """One row per candidate: its labels and Jev's three scores, read from the cache when that request was scored
    before. Stops the run when a score is needed and cannot be had: a missing score is never a "no"."""
    path = SCORES / f"{jev.GATE['version']}.jsonl"
    cache = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            saved = json.loads(line)
            cache[saved.pop("key")] = saved
    wanted = []
    for c in cases:
        _, turn = situation(c)
        wanted += [(c, cand, turn, cache_key(turn, cand["reply"])) for cand in c["candidates"]]
    missing = sum(key not in cache for *_, key in wanted)
    if missing and not os.environ.get("TYPESAFE_API_KEY", "").strip():
        sys.exit(f"{missing} of {len(wanted)} scores are not cached in {path} and TYPESAFE_API_KEY is not set."
                 " Nothing was scored or reported: load the key and run again.")
    rows = []
    for c, cand, turn, key in wanted:
        cached = key in cache
        if not cached:
            try:
                cache[key] = jev.score(*turn, cand["reply"])  # exactly what the router sends: no label, kind or note
            except jev.JevError as error:
                sys.exit(f"Jev gave no usable scores for {cand['id']} ({error}). Nothing was reported. The scores so"
                         f" far are cached in {path}: run again to continue.")
            SCORES.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as out:
                out.write(json.dumps({"key": key, **cache[key]}) + "\n")
            print(f"  scored {cand['id']}", file=sys.stderr)
        rows.append({
            "id": cand["id"], "situation": c["id"], "flow": c["flow"], "label_status": c["label_status"],
            "supported": cand["supported"], "complete": cand["complete"], "requires_human": c["requires_human"],
            "scores": {name: cache[key][name] for name in jev.QUESTIONS}, "cached": cached,
        })
    return rows


def gate_metrics(rows: list[dict], cutoffs: dict) -> dict:
    """The gate against the labels at these cutoffs. The prediction is router.decide on a candidate's scores."""
    decision = {row["id"]: router.decide(row["scores"], cutoffs) for row in rows}
    accepted = [row for row in rows if decision[row["id"]] == "accept"]
    wrong = [row for row in accepted if failed(row)]
    suitable = [row for row in rows if not failed(row)]
    lost = [row for row in suitable if decision[row["id"]] != "accept"]
    human = [row for row in rows if row["requires_human"]]
    missed = [row for row in human if decision[row["id"]] != "handoff"]  # the score is below the handoff cutoff
    outright = [row for row in human if decision[row["id"]] == "accept"]
    confusion = {}
    for name, cutoff in cutoffs.items():
        question = name.rsplit("_", 1)[0]  # supported_min -> supported, requires_human_low -> requires_human
        cells = collections.Counter((row[question], row["scores"][question] >= cutoff) for row in rows)
        confusion[f"{question} >= {cutoff}"] = {  # positive: the score is at or above the cutoff
            "tp": cells[True, True], "fn": cells[True, False], "fp": cells[False, True], "tn": cells[False, False]}
    return {
        "situations": len({row["situation"] for row in rows}),
        "candidates": len(rows),
        "accepted_that_were_unsuitable": ratio(len(wrong), len(accepted)),
        "unsuitable_that_were_accepted": ratio(len(wrong), len(rows) - len(suitable)),
        "failed_label": {name: sum(name in failed(row) for row in wrong) for name in jev.QUESTIONS},
        "suitable_not_accepted": ratio(len(lost), len(suitable)),
        # Candidates of one situation share its message and records, so they are not independent trials.
        "situations_that_require_a_human": len({row["situation"] for row in human}),
        "required_handoffs_missed": ratio(len(missed), len(human)),
        "required_handoffs_accepted": ratio(len(outright), len(human)),
        "confusion": confusion,
        "ids": {
            "accepted_that_were_unsuitable": [f"{row['id']} ({', '.join(failed(row))})" for row in wrong],
            "suitable_not_accepted": [row["id"] for row in lost],
            "required_handoffs_missed": [row["id"] for row in missed],
            "required_handoffs_accepted": [row["id"] for row in outright],
        },
    }


def sweep(rows: list[dict], current: dict) -> dict:
    """Try a grid of the four cutoffs and rank the outcomes: fewest accepted-unsuitable, then fewest required
    handoffs missed, then most suitable candidates accepted. Of the settings that reach an outcome, the one nearest
    the current cutoffs stands for it."""
    def outcome(cutoffs: dict) -> dict:
        metrics = gate_metrics(rows, cutoffs)
        return {"cutoffs": cutoffs, **{name: metrics[name] for name in SWEEP_ORDER}}

    best = {}
    # Strict settings come first (high support and completeness, low handoff), so they win a tie on distance.
    for supported, complete, handoff, low in itertools.product(GRID[::-1], GRID[::-1], GRID, GRID):
        if low > handoff:
            continue  # decide() tests the handoff cutoff first, so a higher low cutoff acts like an equal one
        cutoffs = {"supported_min": supported, "complete_min": complete,
                   "requires_human_handoff": handoff, "requires_human_low": low}
        found = outcome(cutoffs)
        rank = tuple(found[name]["n"] for name in SWEEP_ORDER)
        distance = round(sum(abs(cutoffs[name] - current[name]) for name in cutoffs), 6)
        if rank not in best or distance < best[rank][0]:
            best[rank] = (distance, found)
    return {"grid": GRID, "current": outcome(current), "best": [best[rank][1] for rank in sorted(best)[:5]]}


def gate(cases: list[dict], split: str, only: str, with_sweep: bool) -> None:
    """The gate-specific comparison: print it and save it."""
    cutoffs = jev.GATE["thresholds"]
    rows = gate_rows(cases)
    for row in rows:
        row["decision"] = router.decide(row["scores"], cutoffs)
    metrics = by_flow(rows, lambda part: gate_metrics(part, cutoffs))
    found = sweep(rows, cutoffs) if with_sweep else None
    path = report("gate", split, only, {"metrics": metrics, "sweep": found, "rows": rows})
    new = sum(not row["cached"] for row in rows)
    print(f"\nGATE  split {split}  {len(rows)} candidates ({len(rows) - new} scores from the cache, {new} new)."
          " Every ratio below counts candidates.")
    print(f"gate {jev.GATE['version']}, model {jev.GATE['model']}, cutoffs "
          + ", ".join(f"{name} {value}" for name, value in cutoffs.items()))
    show(metrics)
    if found:
        print(f"\nSWEEP  {len(GRID)} values per cutoff; fewest accepted-unsuitable, then fewest required handoffs"
              " missed, then most suitable accepted")
        settings = [("current", found["current"]), ("chosen", found["best"][0])]
        for label, setting in settings + [("next best", setting) for setting in found["best"][1:]]:
            print(f"{label:10}" + "  ".join(f"{name} {value}" for name, value in setting["cutoffs"].items()))
            print(" " * 10 + "  ".join(f"{name.replace('_', ' ')} {text(setting[name])}" for name in SWEEP_ORDER))
        print("data/gate.json is unchanged: a person freezes the cutoffs.")
    print(f"report: {path}")


def e2e_rows(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    """One row per situation: what the rules-only baseline would send, the full bot's trace, and whether the labels
    cover the final reply. Also the review sheet: the final replies that no label covers."""
    rows, sheet = [], []
    # ponytail: rows are kept in memory, so a run that stops half way reports nothing and a rerun repeats every
    # live call (about a cent and ten minutes for all 100 situations). Upgrade path: append each row as it ends.
    for c in cases:
        store, turn = situation(c)
        labels = {cand["reply"]: (cand["id"], not failed({**cand, "requires_human": c["requires_human"]}))
                  for cand in c["candidates"]}
        print(f"  {c['id']}: ", end="", file=sys.stderr, flush=True)  # named first, in case the bot raises on it
        payload = router.handle(store, c["message"], c["item_id"], c["quick_action"])
        trace = store.traces()[-1]
        # The baseline has no gate and no fallback. It is read from the router's own rule steps, which run before
        # any model is asked: the ticket rules.mandated called for, else the draft from rules.candidate as it is.
        draft = trace["candidate"]["draft"] if trace["candidate"] else None
        baseline = ("mandated_ticket" if trace["route"] == "mandated_ticket"
                    else "rule_draft" if draft else "no_rule_reply")
        sent = None if trace["ticket_ref"] else trace["reply"]  # the customer got a ticket or this text
        matched, suitable = labels.get(sent, (None, None))
        rows.append({
            "id": c["id"], "flow": c["flow"], "label_status": c["label_status"], "requires_human": c["requires_human"],
            "baseline": baseline, "baseline_reply": draft, "baseline_suitable": labels.get(draft, (None, None))[1],
            "matched": matched, "suitable": suitable, "needs_review": sent is not None and matched is None,
            "pending_action": payload.get("pending_action", {}).get("action"),
            **trace,
        })
        if rows[-1]["needs_review"]:
            # ponytail: the evidence is built from the item the case selects. For a case that selects none, the
            # router can resolve an item the message names; no case does that today. Upgrade path: build it from
            # trace["item_id"] and that item's earlier turns.
            sheet.append({
                "id": c["id"], "route": trace["route"], "evidence": jev.evidence(*turn), "reply": sent,
                "pending_action": rows[-1]["pending_action"],
                "supported": None, "complete": None, "action_ok": None, "reviewer": "", "note": "",
            })
        print(f"{trace['route']} in {trace['elapsed_ms']['total']} ms", file=sys.stderr)
    return rows, sheet


def e2e_metrics(rows: list[dict]) -> dict:
    """The end-to-end comparison for one group of situations."""
    ids = {}

    def part(name: str, picked: list[dict], of: list[dict]) -> dict:
        ids[name] = [row["id"] for row in picked]
        return ratio(len(picked), len(of))

    human = [row for row in rows if row["requires_human"]]
    labelled = [row for row in rows if row["suitable"] is not None]  # final replies that a label covers
    accepted = [row for row in labelled if row["route"] == "rule"]
    others = [row for row in labelled if row["route"] != "rule"]
    bad = [row for row in rows if row["baseline_suitable"] is False]
    good = [row for row in rows if row["baseline_suitable"]]
    routes = collections.Counter(row["route"] for row in rows)
    times = sorted(row["elapsed_ms"]["total"] for row in rows)
    costs = [row["cost_usd"] for row in rows if row["cost_usd"] is not None]
    return {
        "situations": len(rows),
        "resolved_on_rules_path": ratio(routes["rule"], len(rows)),
        "routes": {route: ratio(count, len(rows)) for route, count in sorted(routes.items())},
        "required_handoffs_missed": part(
            "required_handoffs_missed", [row for row in human if not row["ticket_ref"]], human),
        "accepted_rule_drafts_unsuitable": part(
            "accepted_rule_drafts_unsuitable", [row for row in accepted if not row["suitable"]], accepted),
        # A fallback or fixed reply whose text happens to be a labelled candidate: its labels apply too.
        "other_labelled_replies_unsuitable": part(
            "other_labelled_replies_unsuitable", [row for row in others if not row["suitable"]], others),
        "final_replies_for_human_review": part(
            "final_replies_for_human_review", [row for row in rows if row["needs_review"]], rows),
        "baseline": {kind: ratio(sum(row["baseline"] == kind for row in rows), len(rows)) for kind in BASELINE},
        "baseline_on_required_handoffs": {
            kind: ratio(sum(row["baseline"] == kind for row in human), len(human)) for kind in BASELINE},
        "baseline_drafts_unsuitable": ratio(len(bad), len(bad) + len(good)),
        # Errors caught: the customer did not get the unsuitable draft that the rules alone would have sent.
        "unsuitable_baseline_drafts_stopped": part(
            "unsuitable_baseline_drafts_stopped", [row for row in bad if row["reply"] != row["baseline_reply"]], bad),
        # Errors or cost introduced: the gate did not accept a draft that the labels call suitable.
        "suitable_baseline_drafts_not_accepted": part(
            "suitable_baseline_drafts_not_accepted", [row for row in good if row["route"] != "rule"], good),
        "provider_calls": {name: sum(row["calls"][name] for row in rows) for name in ("jev", "fallback")},
        "provider_errors": sum(row["error"] is not None for row in rows),
        "elapsed_ms": {"median": round(statistics.median(times)),
                       "p95": times[math.ceil(0.95 * len(times)) - 1],  # nearest rank
                       "max": times[-1]},
        "cost_usd": {"mean": round(sum(costs) / len(costs), 8) if costs else None,
                     "total": round(sum(costs), 8), "unknown": len(rows) - len(costs)},
        "ids": ids,
    }


def e2e(cases: list[dict], split: str, only: str) -> None:
    """The end-to-end comparison: print it, save it and write the review sheet."""
    if not os.environ.get("TYPESAFE_API_KEY", "").strip():
        sys.exit("e2e runs the live gate and the live fallback, and TYPESAFE_API_KEY is not set."
                 " Nothing was run or reported.")
    rows, sheet = e2e_rows(cases)
    metrics = by_flow(rows, e2e_metrics)
    fallback_model = os.environ.get("FALLBACK_MODEL") or "the app's default"
    path = report("e2e", split, only, {"fallback_model": fallback_model, "metrics": metrics, "rows": rows})
    review = path.with_name(f"{path.stem}-review.jsonl")
    review.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n" for line in sheet), encoding="utf-8")
    print(f"\nE2E  split {split}  {len(rows)} situations: the full bot (live gate, live fallback) next to the"
          " rules-only baseline, which calls no provider and costs nothing")
    show(metrics)
    print(f"review sheet: {review} ({len(sheet)} final replies need a human judgement)")
    print(f"report: {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.run", description=__doc__.splitlines()[0])
    parser.add_argument("mode", nargs="?", choices=("gate", "e2e"), help="default: gate, then e2e")
    parser.add_argument("--check", action="store_true", help="validate the case files, print counts and stop")
    parser.add_argument("--split", choices=("dev", "test", "all"), default="dev", help="default: dev")
    parser.add_argument("--sweep", action="store_true", help="gate mode on the dev split: try a grid of cutoffs")
    parser.add_argument("--only", default="", metavar="ID_PREFIX", help="only situations whose id starts with this")
    args = parser.parse_args(argv)
    cases, errors = load_cases(CASES)
    if args.check or errors:
        return check(cases, errors)
    if args.sweep and (args.split != "dev" or args.mode == "e2e"):
        parser.error("--sweep selects cutoffs, so it runs in gate mode on the development split only")
    cases = [c for c in cases if args.split in ("all", c["split"]) and c["id"].startswith(args.only)]
    if not cases:
        parser.error(f"no situation in split {args.split} has an id that starts with {args.only!r}")
    if args.mode != "e2e":
        gate(cases, args.split, args.only, args.sweep)
    if args.mode != "gate":
        e2e(cases, args.split, args.only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
