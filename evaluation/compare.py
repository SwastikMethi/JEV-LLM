"""Compare four ways of answering customers on the same cases, every one of them through app.router.handle.

    python -m evaluation.compare --check
        Validate the case files in evaluation/comparison/cases and print counts by split and kind. No network.
    python -m evaluation.compare run --split dev|test [--only ID_PREFIX] [--workers 4] [--system-prompt-file PATH]
        Run the four arms on every case and save evaluation/comparison/runs/<UTC timestamp>-<split>.json.
    python -m evaluation.compare sheet RUN.json
        Write the blind judging sheet RUN-sheet.json and its key RUN-key.json, which no judge is given.
    python -m evaluation.compare report RUN.json --judgements FILE [FILE ...]
        Print the comparison and save it as RUN-report.txt and RUN-report.json.

The arms differ only in what is switched off, by replacing module attributes that the router reads at call time:
    A  rules only          no gate (jev.score gives perfect scores) and no LLM (fallback.draft gives the fixed
                           reply rules.NOT_UNDERSTOOD, which fallback.problems lets through)
    B  rules + Jev + LLM   the application as built; its two providers are only watched, to split time and cost
    C  LLM alone           no rules (rules.candidate, rules.mandated and rules.needs_item give nothing, so the LLM
                           is asked about every message), no gate, no backend check
    D  LLM + Jev           no rules; the backend check and the gate decide on the text the LLM gave in arm C
    B-no-LLM is derived in the report: arm B with every outcome of its fallback step replaced by a ticket.

Run it from the project root. A provider is given only what the router gives it: labels, kinds and ids stay here.
The app's modules are imported inside the functions that need them, after `run` has mapped the LLM's settings onto
the variables they read.
"""
import argparse
import collections
import concurrent.futures
import contextlib
import hashlib
import importlib
import itertools
import json
import logging
import math
import os
import random
import statistics
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CASES, RUNS = HERE / "comparison" / "cases", HERE / "comparison" / "runs"
KEYS = ("id", "family", "split", "kind", "topics", "fixtures", "item_id", "history", "message", "quick_action",
        "in_scope", "requires_human", "requires_human_note", "expected", "expected_action", "label_status")
KINDS = ("single", "multi", "follow_up", "policy", "handoff", "irrelevant")
QUICK = ("track", "refund", "cancel", "return", "missing")
STATUSES = ("ai_draft", "ai_agreed", "ai_disputed", "reviewed")  # as evaluation/run.py; the writers' files say ai_draft
ARMS = ("A", "B", "C", "D")  # the order of the phases: C runs before D, which replays its answers
REPORTED = ("A", "B", "B-no-LLM", "C", "D")
PROVIDERS = ("jev", "llm")
SENT = ("reply", "clarification")  # the outcomes in which a text reached the customer, so a person judges it
SECRETS = ("TYPESAFE_API_KEY", "FALLBACK_API_KEY", "COMPARE_LLM_API_KEY", "LITELLM_API_KEY")
SEED = 20261122  # the judging sheet's shuffle
# What the arms replace. `run` stops before anything is called if the app no longer has one of these.
SURFACE = (("rules", "NOT_UNDERSTOOD"), ("rules", "candidate"), ("rules", "mandated"), ("rules", "needs_item"),
           ("jev", "score"), ("fallback", "draft"), ("fallback", "problems"), ("fallback", "SYSTEM_PROMPT"))
JUDGING = (
    "Judge each item on its own. evidence is everything a support agent has in front of them. expected describes the"
    " correct outcome, and expected_action is the action a correct reply offers for confirmation (null: none)."
    " Mark an item suitable only when the reply says nothing that the evidence does not support, deals with what the"
    " customer asked in the way expected describes, and offers exactly the expected action. For a message that has"
    " nothing to do with the store's orders or policies, a suitable reply briefly declines without inventing"
    ' anything. Answer in one JSON file, with an entry for every item: {"<item id>": {"suitable": true or false,'
    ' "note": "why"}}.'
)

_now = threading.local()  # the case a worker thread is answering (.case) and the provider requests made for it


def clean(text: str) -> str:
    """`text` without the value of any provider key in the environment: nothing printed or saved may quote one."""
    for name in SECRETS:
        if key := os.environ.get(name, "").strip():
            text = text.replace(key, "[key removed]")
    return text


def save(path: Path, content: object) -> Path:
    """Write text as it is, anything else as JSON. Every file this module writes goes through clean()."""
    text = content if isinstance(content, str) else json.dumps(content, indent=1, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(clean(text), encoding="utf-8")
    return path


def beside(run_path: Path, name: str) -> Path:
    """The file that belongs to a run: RUN.json -> RUN-<name>."""
    return run_path.with_name(f"{run_path.stem}-{name}")


def help_topics() -> list[dict]:
    topics = json.loads((ROOT / "data" / "help_topics.json").read_text(encoding="utf-8"))
    return topics["topics"] if isinstance(topics, dict) else topics


def surface() -> dict:
    """The size of what the rule arms are built from, as plain facts."""
    return {"rules_py_lines": len((ROOT / "app" / "rules.py").read_text(encoding="utf-8").splitlines()),
            "help_topics": len(help_topics()),
            "policy_json_bytes": (ROOT / "data" / "policy.json").stat().st_size}


def stored(case: dict):
    """A fresh Store holding the case's records, with its earlier turns loaded the way evaluation/run.py loads them."""
    from app.store import Store

    store = Store(fixtures=case["fixtures"])
    for earlier in case["history"]:
        store.add_message(earlier["role"], earlier["text"], case["item_id"])
    return store


# --check


def load_cases(directory: Path) -> tuple[list[dict], list[str]]:
    """The cases that pass every check, in file order, and every problem found in the files."""
    from app.store import Store

    topic_ids = {topic["id"] for topic in help_topics()}
    cases, errors, family_split, ids = [], [], {}, set()
    paths = sorted(directory.glob("*.jsonl"))
    if not paths:
        errors.append(f"no case files (*.jsonl) in {directory}")
    for path in paths:
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
            try:
                kind, topics = c["kind"], c["topics"]
                found = [
                    (c["id"] in ids, f"duplicate id {c['id']}"),
                    (c["split"] not in ("dev", "test"), f"split {c['split']!r}"),
                    (family_split.setdefault(c["family"], c["split"]) != c["split"],
                     f"family {c['family']} appears in both splits"),
                    (kind not in KINDS, f"kind {kind!r}"),
                    (not (isinstance(topics, list) and all(topic in topic_ids for topic in topics)),
                     f"topics must be ids from data/help_topics.json, got {topics}"),
                    (kind == "irrelevant" and bool(topics or c["in_scope"]),
                     "an irrelevant case has no topics and in_scope false"),
                    (kind != "irrelevant" and not topics, f"a {kind} case needs at least one topic"),
                    (kind == "multi" and len(topics) < 2, "a multi case needs two or more topics"),
                    (kind == "follow_up" and not c["history"], "a follow_up case needs history"),
                    (kind == "handoff" and not c["requires_human"], "a handoff case must have requires_human true"),
                    (not isinstance(c["in_scope"], bool), "in_scope must be a boolean"),
                    (not isinstance(c["requires_human"], bool), "requires_human must be a boolean"),
                    (bool(c["requires_human"]) and not c["requires_human_note"].strip(),
                     "requires_human_note must name the escalation condition"),
                    (not (isinstance(c["message"], str) and 0 < len(c["message"].strip()) <= 500),
                     "message must be 1 to 500 characters"),
                    (not (isinstance(c["expected"], str) and len(c["expected"].strip()) >= 20),
                     "expected must describe the correct outcome"),
                    (c["expected_action"] not in (None, "cancel", "return"),
                     f"expected_action {c['expected_action']!r}"),
                    (c["quick_action"] is not None and c["quick_action"] not in QUICK,
                     f"quick_action {c['quick_action']!r}"),
                    (c["label_status"] not in STATUSES, f"label_status must be one of {', '.join(STATUSES)}"),
                    (not (isinstance(c["history"], list) and all(
                        isinstance(turn, dict) and turn.get("role") in ("customer", "agent")
                        and isinstance(turn.get("text"), str) for turn in c["history"])),
                     "history must be a list of {role: customer|agent, text}"),
                ]
                ids.add(c["id"])
                try:
                    facts = Store(fixtures=c["fixtures"]).item(c["item_id"]) if c["item_id"] is not None else None
                    wanted, allowed = c["expected_action"], facts["permitted_actions"] if facts else []
                    found += [
                        (c["item_id"] is not None and facts is None, f"item_id {c['item_id']} is not in the fixtures"),
                        (bool(wanted) and c["item_id"] is None, "expected_action needs an item"),
                        (bool(wanted) and facts is not None and wanted not in allowed,
                         f"expected_action {wanted} is not permitted for this item (permitted: {allowed})"),
                    ]
                except Exception as error:  # noqa: BLE001 - any fixture problem is reported, not raised
                    found.append((True, f"fixtures do not load ({type(error).__name__}: {error})"))
                errors += [f"{where}: {problem}" for bad, problem in found if bad]
            except Exception as error:  # noqa: BLE001 - a value of the wrong type is a problem with the line
                errors.append(f"{where}: malformed ({type(error).__name__}: {error})")
            if len(errors) == before:
                cases.append(c)
    return cases, errors


def check(cases: list[dict], errors: list[str]) -> int:
    """Print the counts of the valid cases by kind and split, then every problem. Returns the exit status."""
    counts = collections.Counter()
    for c in cases:
        for name in (c["kind"], "all", *["requires_human"] * c["requires_human"],
                     *["quick_action"] * bool(c["quick_action"])):
            counts[name, c["split"]] += 1
    print(f"{'':16}{'dev':>6}{'test':>6}")
    for name in (*KINDS, "all", "requires_human", "quick_action"):
        print(f"{name:16}{counts[name, 'dev']:>6}{counts[name, 'test']:>6}")
    families = collections.Counter({c["family"]: c["split"] for c in cases}.values())
    print(f"families: {sum(families.values())} | dev {families['dev']} | test {families['test']}")
    for error in errors:
        print(" -", error)
    print(f"{len(errors)} PROBLEMS" if errors else "OK")
    return 1 if errors else 0


# run


def configure_llm() -> list[str]:
    """Point the app's fallback client at the comparison's LLM, in this process only. Returns the settings that are
    missing or unusable."""
    env = os.environ
    env["FALLBACK_BASE_URL"] = env.get("COMPARE_LLM_BASE_URL") or env.get("LITELLM_BASE_URL") or ""
    env["FALLBACK_API_KEY"] = env.get("COMPARE_LLM_API_KEY") or env.get("LITELLM_API_KEY") or ""
    env["FALLBACK_MODEL"] = env.get("COMPARE_LLM_MODEL") or "claude-haiku-5-5"
    env["FALLBACK_USD_PER_MTOK_IN"] = env.get("COMPARE_LLM_USD_PER_MTOK_IN") or "0.10"
    env["FALLBACK_USD_PER_MTOK_OUT"] = env.get("COMPARE_LLM_USD_PER_MTOK_OUT") or "0.50"
    needed = {
        "TYPESAFE_API_KEY": env.get("TYPESAFE_API_KEY", ""),
        "COMPARE_LLM_BASE_URL (or LITELLM_BASE_URL)": env["FALLBACK_BASE_URL"],
        "COMPARE_LLM_API_KEY (or LITELLM_API_KEY)": env["FALLBACK_API_KEY"],
    }
    problems = [name for name, value in needed.items() if not value.strip()]
    for side in ("IN", "OUT"):  # a price that is not a number would fail every LLM call
        try:
            float(env[f"FALLBACK_USD_PER_MTOK_{side}"])
        except ValueError:
            problems.append(f"COMPARE_LLM_USD_PER_MTOK_{side} (a number)")
    return problems


def _perfect(*args: object, **kwargs: object) -> dict:
    """jev.score with the gate switched off: the best scores, with no request, no time and no cost."""
    return {"supported": 1.0, "complete": 1.0, "requires_human": 0.0, "model": None, "input_tokens": 0,
            "cost_usd": 0.0, "request_id": None, "elapsed_ms": 0}


def _watched(provider: str, call: Callable, kept: dict | None = None) -> Callable:
    """`call` itself, with each request's time and cost noted for the case the calling thread is answering.

    With `kept`, what the call returned or raised is also remembered under that case's id, for arm D to replay.
    """
    def watched(*args: object, **kwargs: object) -> dict:
        request = {"provider": provider, "ms": 0, "cost_usd": None}  # the cost of a failed request stays unknown
        _now.requests.append(request)
        started = time.monotonic()
        try:
            result = call(*args, **kwargs)
        except Exception as error:  # noqa: BLE001 - noted, then raised again below
            result = error
        else:
            request["cost_usd"] = result["cost_usd"]
            # The token counts behind the cost, so that a run can be priced again without being repeated.
            request.update({name: result[name] for name in ("input_tokens", "output_tokens") if name in result})
        request["ms"] = int((time.monotonic() - started) * 1000)
        if kept is not None:
            kept[_now.case] = (result, request)
        if isinstance(result, Exception):
            raise result
        return result
    return watched


def _replayed(kept: dict) -> Callable:
    """fallback.draft for arm D: what the LLM gave this case in arm C, at the time and the cost it took there."""
    def draft(*args: object, **kwargs: object) -> dict:
        result, request = kept[_now.case]
        _now.requests.append(dict(request))
        if isinstance(result, Exception):
            raise result
        return dict(result)
    return draft


def switches(arm: str, kept: dict) -> list[tuple]:
    """What an arm replaces, as (module, attribute, replacement). `kept` carries the LLM's answers from C to D."""
    from app import fallback, jev, rules

    def nothing(*args: object, **kwargs: object) -> None:
        return None

    def no_problems(*args: object, **kwargs: object) -> list:
        return []

    # No rule draft, no backend-mandated ticket and no rule that asks the customer which item is meant: the LLM is
    # asked about every message, with the record of the item the router resolved or with none.
    no_rules = [(rules, name, nothing) for name in ("candidate", "mandated", "needs_item")]
    if arm == "A":
        fixed = {"kind": "reply", "text": rules.NOT_UNDERSTOOD, "action": None, "input_tokens": 0,
                 "output_tokens": 0, "cost_usd": 0.0, "elapsed_ms": 0}
        return [(jev, "score", _perfect), (fallback, "problems", no_problems),
                (fallback, "draft", lambda *args, **kwargs: dict(fixed))]
    if arm == "B":  # nothing is switched off
        return [(jev, "score", _watched("jev", jev.score)), (fallback, "draft", _watched("llm", fallback.draft))]
    if arm == "C":
        return [*no_rules, (jev, "score", _perfect), (fallback, "problems", no_problems),
                (fallback, "draft", _watched("llm", fallback.draft, kept))]
    return [*no_rules, (jev, "score", _watched("jev", jev.score)), (fallback, "draft", _replayed(kept))]


@contextlib.contextmanager
def swapped(changes: list[tuple]) -> Iterator[None]:
    """Replace attributes, of modules mostly, and put them back afterwards. Process-wide, so two phases never overlap.

    getattr fails on a name the module does not have, rather than adding an attribute that nothing reads.
    """
    before = [(module, name, getattr(module, name)) for module, name, _ in changes]
    try:
        for module, name, replacement in changes:
            setattr(module, name, replacement)
        yield
    finally:
        for module, name, original in before:
            setattr(module, name, original)


def totals(requests: list[dict]) -> dict:
    """Requests per provider and what they cost. A cost is None, unknown, as soon as one request's cost is."""
    costs = {name: [request["cost_usd"] for request in requests if request["provider"] == name] for name in PROVIDERS}
    return {"calls": {name: len(own) for name, own in costs.items()},
            "cost_usd": {name: None if None in own else sum(own) for name, own in costs.items()}}


def breach(arm: str, trace: dict | None, requests: list[dict]) -> str | None:
    """How a route departs from the arm it was run as, or None. `trace` is the router's own account of the route
    (None: handle raised) and `requests` is what the arm's switches saw.

    In arms C and D the LLM answers every message: it is asked exactly once and there is no rule draft. In every arm
    the router's total cost is the cost of the watched requests. Anything else means that the router reached a
    provider some other way: arm A paid one, arm C paid the gate, or arm B's costs are not all counted.
    """
    made = totals(requests)
    if arm in "CD" and made["calls"]["llm"] != 1:
        return f"the LLM was asked {made['calls']['llm']} times"
    if trace is None:
        return None
    if arm in "CD" and trace["candidate"]:
        return "the rules drafted a reply"
    counted, watched = trace["cost_usd"], list(made["cost_usd"].values())
    unknown = None in watched
    if (counted is None) != unknown or not unknown and not math.isclose(counted, sum(watched), abs_tol=1e-12):
        return "the router counted provider costs that the arm's switches did not see"
    return None


def answer(case: dict, arm: str) -> tuple[dict, str | None]:
    """One case through router.handle on a fresh store, under the switches in place: what the customer got and what
    it took, and how the route departs from the arm it was run as (None: it does not)."""
    from app import router

    _now.case, _now.requests = case["id"], []
    store = stored(case)
    started = time.monotonic()
    trace = None
    try:
        payload = router.handle(store, case["message"], case["item_id"], case["quick_action"])
        trace = store.traces()[-1]
    except Exception as error:  # noqa: BLE001 - the application raised, so the customer got nothing
        record = {"outcome": "error", "reply": None, "action": None, "ticket_ref": None, "item_id": None,
                  "route": None, "reason": None, "error": f"handle raised {type(error).__name__}: {error}",
                  "source": None}
    else:
        route = trace["route"]
        record = {
            # ponytail: a clarification is known by its route name ("clarify_item", "fallback_clarify"). Both
            # outcomes are judged alike, so a new route name only moves a count between two columns of the run file.
            "outcome": ("ticket" if trace["ticket_ref"] else "error" if route == "ticket_failed"
                        else "clarification" if "clarify" in route else "reply"),
            "reply": trace["reply"],
            "action": (payload.get("pending_action") or {}).get("action"),
            "ticket_ref": trace["ticket_ref"],
            "item_id": payload.get("item_id"),  # the item the router resolved
            "route": route,
            "reason": trace["reason"],
            "error": trace["error"],
            # Where the route ended: on a rule draft, in the fallback step, or before either (None).
            "source": "fallback" if trace["calls"]["fallback"] else "rule" if trace["candidate"] else None,
        }
    # The time handle took. Arm D replays the LLM's answer in no time; a deployment would wait for it, so the
    # time it took in arm C is added.
    waited = sum(request["ms"] for request in _now.requests if request["provider"] == "llm") if arm == "D" else 0
    record["latency_ms"] = int((time.monotonic() - started) * 1000) + waited
    print(clean(f"  {arm} {case['id']}: {record['outcome']} ({record['route']}) in {record['latency_ms']} ms"),
          file=sys.stderr)
    return {**record, "requests": _now.requests, **totals(_now.requests)}, breach(arm, trace, _now.requests)


def run_arm(arm: str, cases: list[dict], workers: int, kept: dict) -> list[dict]:
    """One arm's phase: its switches in place, the cases answered concurrently, the records in case order. The run
    stops when a route was not the arm it was run as."""
    with swapped(switches(arm, kept)), concurrent.futures.ThreadPoolExecutor(workers) as pool:
        answers = list(pool.map(lambda case: answer(case, arm), cases))
    broken = [f"{case['id']} ({why})" for case, (_, why) in zip(cases, answers) if why]
    if broken:
        sys.exit(f"Arm {arm} is not what it claims: {'; '.join(broken)}. Nothing was saved."
                 " Read app/router.py for how it reaches the rules and its providers now.")
    return [record for record, _ in answers]


def run(args: argparse.Namespace) -> int:
    missing = configure_llm()
    if missing:
        sys.exit("The comparison calls the live gate and the live LLM, and these are missing or unusable: "
                 + ", ".join(missing) + ". Nothing was run.")
    cases, errors = load_cases(CASES)
    if errors:
        return check(cases, errors)
    cases = [c for c in cases if c["split"] == args.split and c["id"].startswith(args.only)]
    if not cases:
        sys.exit(f"No case in {CASES} is in split {args.split} with an id that starts with {args.only!r}.")
    from app import fallback, jev, rules

    gone = [f"app.{module}.{name}" for module, name in SURFACE
            if not hasattr(importlib.import_module(f"app.{module}"), name)]
    if gone:
        sys.exit(f"The arms replace {', '.join(gone)}, which the application no longer has. Nothing was run."
                 " Read app/router.py for how it reaches its collaborators now.")
    prompt = fallback.SYSTEM_PROMPT
    if args.system_prompt_file:
        prompt = Path(args.system_prompt_file).read_text(encoding="utf-8")
    # ponytail: the records are kept in memory, so a run that stops half way saves nothing and a rerun repeats every
    # live call. Upgrade path: append each record to the run file as it ends.
    kept, arms, at = {}, {}, datetime.now(timezone.utc)
    # The application logs an unexpected provider error with its text, which can quote a key, and Python prints
    # that as it is. During the run the root logger has one handler, which prints through clean().
    printed = logging.StreamHandler()
    printed.setLevel(logging.WARNING)
    printed.format = lambda line: clean(logging.Formatter(logging.BASIC_FORMAT).format(line))
    with swapped([(fallback, "SYSTEM_PROMPT", prompt), (logging.getLogger(), "handlers", [printed])]):
        for arm in ARMS:
            started = time.monotonic()
            arms[arm] = run_arm(arm, cases, args.workers, kept)
            outcomes = collections.Counter(record["outcome"] for record in arms[arm])
            print(f"arm {arm}: {len(cases)} cases in {time.monotonic() - started:.1f} s: "
                  + ", ".join(f"{name} {count}" for name, count in sorted(outcomes.items())))
    path = save(RUNS / f"{at:%Y%m%dT%H%M%SZ}-{args.split}.json", {
        "at": at.isoformat(), "split": args.split, "only": args.only or None, "workers": args.workers,
        "llm": {"model": os.environ["FALLBACK_MODEL"],
                "usd_per_mtok_in": float(os.environ["FALLBACK_USD_PER_MTOK_IN"]),
                "usd_per_mtok_out": float(os.environ["FALLBACK_USD_PER_MTOK_OUT"]),
                "system_prompt_file": args.system_prompt_file,
                "system_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                # The client's other settings, where the environment sets them (the key and the URL stay out).
                "settings": {name: os.environ[name] for name in (
                    "FALLBACK_REASONING_EFFORT", "FALLBACK_TIMEOUT_S", "JEV_TIMEOUT_S") if name in os.environ}},
        "gate": jev.GATE,
        "not_understood": rules.NOT_UNDERSTOOD,  # arm A's fixed reply, which the report counts apart
        "engineering_surface": surface(),
        "cases": [{**case, "arms": {arm: arms[arm][index] for arm in ARMS}} for index, case in enumerate(cases)],
    })
    print(f"run: {path}\nnext: python -m evaluation.compare sheet {path}")
    return 0


# sheet


def sheet_items(run_data: dict) -> tuple[list[dict], dict]:
    """The blind items and their key: one item per distinct reply text and action that any arm sent for a case."""
    from app import jev

    found = {}
    for case in run_data["cases"]:
        for arm, record in case["arms"].items():
            if record["outcome"] in SENT:
                same = (case["id"], record["reply"], record["action"])
                found.setdefault(same, (case, record, []))[2].append(arm)
    entries = list(found.values())
    random.Random(SEED).shuffle(entries)
    # Every id carries a tag of what is on this sheet, so that judgements of another run's sheet are never read as
    # judgements of this one.
    tag = hashlib.sha256(json.dumps(list(found)).encode("utf-8")).hexdigest()[:6]
    items, key = [], {}
    for number, (case, record, arms) in enumerate(entries, 1):
        # The evidence as the gate sees it: the item the router resolved and that item's earlier turns.
        store, item_id = stored(case), record["item_id"]
        item = store.item(item_id) if item_id is not None else None
        name = f"{tag}-{number:04d}"  # numbered after the shuffle, so the id says nothing about the case or the arm
        items.append({
            "id": name,
            "evidence": jev.evidence(case["message"], store.history(item_id), item, store.policy, store.today),
            "expected": case["expected"],
            "expected_action": case["expected_action"],
            "reply": record["reply"],
            "action": record["action"],
        })
        key[name] = {"case": case["id"], "arms": arms}
    return items, key


def sheet(args: argparse.Namespace) -> int:
    run_path = Path(args.run_file)
    items, key = sheet_items(json.loads(run_path.read_text(encoding="utf-8")))
    sheet_path = save(beside(run_path, "sheet.json"), {"instructions": JUDGING, "items": items})
    key_path = save(beside(run_path, "key.json"), key)
    print(f"sheet: {sheet_path} ({len(items)} replies to judge)\nkey: {key_path} (not for the judges)")
    return 0


# report


def standings(key: dict, judges: dict[str, dict]) -> dict:
    """Every sheet item as "suitable" (every judge said so), "unsuitable" (none did) or "disputed" (they disagree)."""
    problems = []
    for name, marks in judges.items():
        if not isinstance(marks, dict):
            problems.append(f"{name} is not a JSON object of item ids")
            continue
        unjudged = [item for item in key
                    if not (isinstance(marks.get(item), dict) and isinstance(marks[item].get("suitable"), bool))]
        unknown = [item for item in marks if item not in key]
        if unjudged:
            problems.append(f"{name} has no true or false for {len(unjudged)} items: {', '.join(unjudged[:5])} ...")
        if unknown:
            problems.append(f"{name} judges {len(unknown)} items that are not on this run's sheet:"
                            f" {', '.join(unknown[:5])} ...")
    if problems:
        sys.exit("Nothing was reported, because a missing judgement is never a yes.\n - " + "\n - ".join(problems))
    found = {}
    for item in key:
        said = {marks[item]["suitable"] for marks in judges.values()}
        found[item] = "disputed" if len(said) > 1 else "suitable" if said == {True} else "unsuitable"
    return found


def without_llm(record: dict) -> dict:
    """Arm B with no generating model at all: where the route reached the fallback step, the customer gets a ticket
    instead, and the LLM request and everything after it never happen."""
    if record["source"] != "fallback":
        return record
    made = list(itertools.takewhile(lambda request: request["provider"] != "llm", record["requests"]))
    spared = sum(request["ms"] for request in record["requests"][len(made):])
    return {**record, "outcome": "ticket", "reply": None, "action": None, "error": None, "route": "no_llm_ticket",
            "requests": made, **totals(made), "latency_ms": max(record["latency_ms"] - spared, 0)}


def bucket(case: dict, record: dict, standing: str | None) -> str:
    """The one thing that happened to a request. A case that requires a person is settled by that label alone: a
    reply sent on it is a missed handoff, whatever the judges said about its text."""
    if record["outcome"] == "error":
        return "error"
    if record["outcome"] == "ticket":
        return "necessary_ticket" if case["requires_human"] else "unnecessary_ticket"
    if case["requires_human"]:
        return "missed_handoff"
    return {"suitable": "resolved", "unsuitable": "wrong_reply", "disputed": "disputed"}[standing]


def ratio(n: int, d: int) -> dict:
    return {"n": n, "d": d, "rate": round(n / d, 4) if d else None}


def metrics(rows: list[tuple], not_understood: str) -> dict:
    """One arm over one group of cases. `rows` holds (case, record, standing of the reply it sent or None), and
    `not_understood` is arm A's fixed reply."""
    n = len(rows)
    human = sum(case["requires_human"] for case, _, _ in rows)
    count = collections.Counter(bucket(*row) for row in rows)
    fixed = sum(bucket(*row) == "wrong_reply" and row[1]["reply"] == not_understood for row in rows)
    sent = [(case, record) for case, record, _ in rows if record["outcome"] in SENT]
    times = sorted(record["latency_ms"] for _, record, _ in rows)
    known = {name: [record["cost_usd"][name] for _, record, _ in rows if record["cost_usd"][name] is not None]
             for name in PROVIDERS}
    mean = {name: sum(costs) / len(costs) if costs else None for name, costs in known.items()}
    total = None if None in mean.values() else sum(mean.values())
    resolved = count["resolved"]
    return {
        "requests": n,
        "resolved_correctly_without_a_person": ratio(resolved, n),
        "wrong_reply_sent": ratio(count["wrong_reply"], n),
        # A fixed reply that the judges found unsuitable did not help, but it stated nothing false.
        "of_which_the_fixed_not_understood_reply": ratio(fixed, count["wrong_reply"]),
        "disputed_reply_sent": ratio(count["disputed"], n),
        "required_handoffs_missed": ratio(count["missed_handoff"], human),
        "necessary_tickets": ratio(count["necessary_ticket"], human),
        "unnecessary_tickets": ratio(count["unnecessary_ticket"], n - human),
        "ended_in_an_error": ratio(count["error"], n),
        "not_resolved": ratio(n - resolved, n),  # the sum of the six lines above: what the break-even prices
        # An action offered when none or another was expected, or none offered with a reply when one was expected.
        "action_errors": ratio(sum(record["action"] != case["expected_action"] for case, record in sent), len(sent)),
        # A Jev or LLM request failed and the router closed the route on a ticket. Not the application's own errors.
        "provider_errors": ratio(sum(record["error"] is not None and record["outcome"] != "error"
                                     for _, record, _ in rows), n),
        "latency_ms": {"median": round(statistics.median(times)),
                       "p95": times[math.ceil(0.95 * n) - 1],  # nearest rank
                       "max": times[-1]},
        # Means over the requests whose cost is known; "unknown" counts the rest (a provider call that failed).
        "cost_usd_per_request": {"jev": mean["jev"], "llm": mean["llm"], "total": total,
                                 "unknown": sum(None in record["cost_usd"].values() for _, record, _ in rows)},
        "cost_usd_per_correctly_resolved_request": total * n / resolved if resolved and total is not None else None,
    }


def break_even(first: str, second: str, found: dict) -> dict:
    """The price of one ticket at which two arms cost the same per 1,000 requests, when a request costs its model
    calls plus one ticket if it was not resolved, and the arm that is cheaper below and above that price."""
    pair = (first, second)
    model = {arm: found[arm]["cost_usd_per_request"]["total"] for arm in pair}
    model = {arm: None if cost is None else 1000 * cost for arm, cost in model.items()}
    tickets = {arm: 1000 * found[arm]["not_resolved"]["n"] / found[arm]["requests"] for arm in pair}
    price = below = above = None
    if None not in model.values():
        lean, sure = min(pair, key=model.get), min(pair, key=tickets.get)  # cheaper to run; fewer tickets
        if tickets[first] == tickets[second]:
            below = above = None if model[first] == model[second] else lean
        else:
            price = (model[second] - model[first]) / (tickets[first] - tickets[second])
            # A price that is not positive means one arm costs less to run and leaves fewer tickets.
            price, below, above = (price, lean, sure) if price > 0 else (None, sure, sure)
    return {"pair": list(pair), "model_usd_per_1000": model, "tickets_per_1000": tickets,
            "ticket_price_usd": price, "cheaper_below": below, "cheaper_above": above}


def cells(found: dict) -> Iterator[tuple[str, str]]:
    """A metrics dict as (label, text): a ratio reads 3/80 (3.8%), a cost 0.000123, an unknown n/a."""
    def text(value: object) -> str:
        return "n/a" if value is None else f"{value:.6f}" if isinstance(value, float) else str(value)

    for name, value in found.items():
        label = name.replace("_", " ")
        if not isinstance(value, dict):
            yield label, text(value)
        elif "rate" in value:
            yield label, f"{value['n']}/{value['d']}" + (f" ({value['rate']:.1%})" if value["d"] else "")
        else:
            yield from ((f"{label} {inner}", text(number)) for inner, number in value.items())


def table(by_arm: dict) -> list[str]:
    """One line per metric and one column per arm."""
    columns = {arm: dict(cells(found)) for arm, found in by_arm.items()}
    wide = max(len(cell) for column in columns.values() for cell in column.values()) + 2
    labels = list(columns[REPORTED[0]])
    label_wide = max(map(len, labels)) + 2
    lines = [" " * label_wide + "".join(f"{arm:<{wide}}" for arm in columns)]
    lines += [f"{label:<{label_wide}}" + "".join(f"{column[label]:<{wide}}" for column in columns.values())
              for label in labels]
    return [line.rstrip() for line in lines]


def report(args: argparse.Namespace) -> int:
    run_path = Path(args.run_file)
    run_data = json.loads(run_path.read_text(encoding="utf-8"))
    key_path = beside(run_path, "key.json")
    if not key_path.exists():
        sys.exit(f"{key_path} is missing. Write the judging sheet first: python -m evaluation.compare sheet {run_path}")
    key = json.loads(key_path.read_text(encoding="utf-8"))
    judges = {name: json.loads(Path(name).read_text(encoding="utf-8")) for name in args.judgements}
    judged = standings(key, judges)
    standing = {(where["case"], arm): judged[item] for item, where in key.items() for arm in where["arms"]}
    rows = {arm: [] for arm in REPORTED}
    for case in run_data["cases"]:
        for arm in REPORTED:
            origin = "B" if arm == "B-no-LLM" else arm
            record = without_llm(case["arms"]["B"]) if arm == "B-no-LLM" else case["arms"][arm]
            if record["outcome"] in SENT and (case["id"], origin) not in standing:
                sys.exit(f"{key_path} does not cover the reply arm {origin} sent for {case['id']}: it belongs to"
                         " another run. Write the sheet again and have it judged.")
            rows[arm].append((case, record, standing.get((case["id"], origin))))
    cases = run_data["cases"]
    scopes = ["overall", *(kind for kind in KINDS if any(case["kind"] == kind for case in cases))]
    found = {scope: {arm: metrics([row for row in rows[arm] if scope in ("overall", row[0]["kind"])],
                                  run_data["not_understood"])
                     for arm in REPORTED} for scope in scopes}
    pairs = [break_even(first, second, found["overall"]) for first, second in itertools.combinations(REPORTED, 2)]
    size = run_data["engineering_surface"]  # as it was when the run was made

    llm, tally = run_data["llm"], collections.Counter(judged.values())
    labels = collections.Counter(case["label_status"] for case in cases)
    out = [
        f"COMPARISON  {run_path.name}  split {run_data['split']}  {len(cases)} cases,"
        f" {sum(case['requires_human'] for case in cases)} requiring a person",
        f"LLM {llm['model']} at ${llm['usd_per_mtok_in']} in and ${llm['usd_per_mtok_out']} out per million tokens;"
        f" gate {run_data['gate']['version']} ({run_data['gate']['model']})",
        f"Judges: {len(judges)}. {len(judged)} distinct replies were judged blind: {tally['suitable']} suitable,"
        f" {tally['unsuitable']} unsuitable, {tally['disputed']} disputed."
        " A reply is suitable only when every judge marked it suitable.",
        "The expected outcomes and the requires-a-person labels are "
        + ", ".join(f"{status} ({count})" for status, count in sorted(labels.items())) + ".",
        "A  rules only | B  rules + Jev + LLM, the application as built | B-no-LLM  arm B with every outcome of its"
        " fallback step replaced by a ticket | C  LLM alone | D  LLM + Jev",
        "Each request is counted once: resolved correctly without a person (a suitable reply or clarification was"
        " sent), wrong reply sent, disputed reply sent, required handoff missed, necessary ticket, unnecessary ticket"
        " or ended in an error; not resolved is the sum of the last six. A reply sent on a case that requires a"
        " person is a missed handoff, whatever the judges said about its text. Action errors are counted over the"
        " replies and clarifications that were sent.",
        "Arm A answers a message its rules do not understand with one fixed reply. Where the judges found that"
        " unsuitable it is a wrong reply sent, and the line below says how many of the wrong replies it was.",
        "Arm A makes no request. In arms C and D the LLM is asked about every message, also where arms A and B ask"
        " the customer which item is meant. Arm D's latency and cost include the LLM request made in arm C, whose"
        " answer it gates. Costs are means over the requests whose cost is known.",
    ]
    for scope in scopes:
        out += ["", scope.upper().replace("_", " "), *table(found[scope])]
    out += [
        "",
        "BREAK-EVEN  No price is assumed for a ticket. Cost per 1,000 requests = model cost + requests not resolved"
        " x the price of one ticket.",
        "Assumption: a wrong reply is counted as costing one ticket, like a ticket itself and a missed handoff. So"
        " is a disputed reply, so that it never counts as good, and a request that ended in an error.",
    ]
    for arm in REPORTED:
        total = found["overall"][arm]["cost_usd_per_request"]["total"]
        out.append(f"  {arm:9} model cost {'n/a' if total is None else f'${1000 * total:,.4f}'} per 1,000 requests,"
                   f" {1000 * found['overall'][arm]['not_resolved']['rate']:,.1f} of them not resolved")
    for pair in pairs:
        (first, second), price, below = pair["pair"], pair["ticket_price_usd"], pair["cheaper_below"]
        if price is not None:
            verdict = (f"equal at ${price:,.4f} per ticket; below that {below} is cheaper,"
                       f" above it {pair['cheaper_above']}")
        elif below:
            verdict = f"{below} is cheaper at every ticket price"
        elif None in pair["model_usd_per_1000"].values():
            verdict = "not worked out: a model cost is unknown"
        else:
            verdict = "the same cost at every ticket price"
        out.append(f"  {first} vs {second}: {verdict}")
    out += ["", f"ENGINEERING SURFACE  app/rules.py {size['rules_py_lines']:,} lines | {size['help_topics']}"
                f" help topics | data/policy.json {size['policy_json_bytes']:,} bytes"]
    text = clean("\n".join(out))
    print(text)
    saved = save(beside(run_path, "report.json"), {
        "run": run_path.name, "judgements": [Path(name).name for name in judges],
        "replies_judged": dict(tally), "metrics": found, "break_even": pairs, "engineering_surface": size,
    })
    print(f"report: {save(beside(run_path, 'report.txt'), text + chr(10))} and {saved}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation.compare", description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="validate the case files, print counts and stop")
    commands = parser.add_subparsers(dest="command")
    run_parser = commands.add_parser("run", help="run the four arms on every case and save the run")
    run_parser.add_argument("--split", choices=("dev", "test"), required=True)
    run_parser.add_argument("--only", default="", metavar="ID_PREFIX", help="only cases whose id starts with this")
    run_parser.add_argument("--workers", type=int, default=4, help="cases answered at once (default 4: more hits"
                            " the gate's rate limit)")
    run_parser.add_argument("--system-prompt-file", metavar="PATH", help="replaces the LLM's system prompt")
    sheet_parser = commands.add_parser("sheet", help="write the blind judging sheet and its key next to the run")
    sheet_parser.add_argument("run_file", metavar="RUN.json")
    report_parser = commands.add_parser("report", help="print and save the comparison")
    report_parser.add_argument("run_file", metavar="RUN.json")
    report_parser.add_argument("--judgements", nargs="+", required=True, metavar="FILE", help="one file per judge")
    args = parser.parse_args(argv)
    if args.check:
        return check(*load_cases(CASES))
    if args.command is None:
        parser.error("give --check or one of the commands run, sheet, report")
    return {"run": run, "sheet": sheet, "report": report}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
