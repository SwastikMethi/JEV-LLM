---
title: Ruleset + Jev Demo
emoji: 🧵
colorFrom: purple
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

# Ruleset + Jev demo

A demo of the ruleset + Jev idea, shown through one small use case: a support bot for a fictional
fashion store, Saanjh Clothing. A customer can track an order, check a refund, request a
cancellation or a return, and report an undelivered package. Simple rules draft routine replies from
order records, and every draft is judged by **Jev**, TypeSafe's hosted decision model, before it is sent.
When there is no draft, or Jev rejects it, a fallback LLM gets one attempt. Anything that cannot be
verified becomes a support ticket. Every customer, order and policy here is fictional.

## How a message is routed

`app/router.py` follows the six steps of the routing contract in order.

1. **Resolve the item.** The item the chat was started from, or the one item the message names. If
   several fit, the customer is asked to choose. A general policy question proceeds without an order.
2. **Backend-mandated outcomes.** `data/policy.json` lists escalation rules the backend enforces itself,
   for example a customer disputing a delivery scan. The ticket is stored before its reference is shown.
3. **Rule draft, scored by Jev.** `app/rules.py` drafts the routine reply. `app/jev.py` sends it with three
   yes/no ("noul") questions in one request: *supported* (every claim follows from the record and the
   policy), *complete* (every part of the message is covered) and *requires_human* (an escalation
   condition in the policy applies).
4. **Decide.** A high *requires_human* score opens a ticket. Otherwise the draft is sent when *supported*
   and *complete* meet their cutoffs and *requires_human* is low. Cutoffs, the pinned model, the question
   wording and the policy version live together in `data/gate.json`.
5. **Fallback, once.** `app/fallback.py` asks the fallback LLM for a reply, a clarifying question or a
   handoff. The backend checks any proposed action against the permitted actions and every quoted date,
   amount and status against the order record, then Jev scores the new reply once. If it still fails, a
   ticket is opened. A Jev timeout or malformed score also opens a ticket: an unverified reply is never
   sent. If the ticket cannot be stored, the customer gets a retry option.
6. **Confirm before acting.** A cancellation or return runs only after the customer confirms that item
   and action. The confirmation is bound to the session, eligibility is rechecked, the change commits
   once, and a repeated confirmation returns the original result.

## Run it locally

Requirements: Python 3.13, [Ollama](https://ollama.com) with `qwen3.5:9b` for the fallback, and a
TypeSafe API key from `console.typesafe.ai` for the gate.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
echo 'TYPESAFE_API_KEY=your-key' > .env          # .env is git-ignored; write the value without quotes
set -a; . ./.env; set +a
.venv/bin/python -m uvicorn app.main:app --port 7860
```

Open http://localhost:7860. Without a key the app still runs but fails closed: every gated reply
becomes a ticket.

- **No key yet?** `.venv/bin/python tests/stub_gate_server.py` serves the same app on port 7865 with a
  keyword stand-in for Jev. It is only for clicking through the flows and proves nothing about the gate.
- **Fallback speed.** A warm reply takes about 1.5 seconds. Ollama unloads the model after about five
  minutes idle, and the next reply then takes 9 to 17 seconds. Start Ollama with a longer
  `OLLAMA_KEEP_ALIVE` to avoid that.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TYPESAFE_API_KEY` | none | Key for the Jev gate. Missing means every gated reply becomes a ticket. |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` | Jev API host. |
| `JEV_TIMEOUT_S` | `5` | Timeout for one Jev request. |
| `FALLBACK_BASE_URL` | `http://localhost:11434/v1` | Any OpenAI-compatible chat-completions endpoint. |
| `FALLBACK_MODEL` | `qwen3.5:9b` | Model name sent to that endpoint. |
| `FALLBACK_API_KEY` | none | Sent as a bearer token only when set. Ollama needs none. |
| `FALLBACK_REASONING_EFFORT` | `none` | Switches off the model's thinking. Set it empty to omit the field. |
| `FALLBACK_TIMEOUT_S` | `30` | Timeout for one fallback request. |
| `FALLBACK_USD_PER_MTOK_IN`, `FALLBACK_USD_PER_MTOK_OUT` | `0` | Prices used for cost reporting. A local model is free. |

Limits built into the app: 30 customer messages per session, 200 sessions in memory, at most two Jev
calls and one fallback call per message, 500 characters per message, 16 KB per request body.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The suite makes no network calls. It covers the routing contract, confirmation, session isolation, both
provider clients, the evaluation arithmetic and static guards on the web files.

## Four-arm comparison (2026-11-22)

Is rules plus Jev better than an LLM for support? Four ways of answering were run on the same 120
held-out cases, each through the application's own `router.handle`:

- **A** rules only, no check
- **B** rules + Jev + LLM fallback (the application as built), and **B without the LLM** (every fallback
  outcome becomes a ticket)
- **C** the LLM alone, with the full policy and order record
- **D** the LLM gated by Jev, with no rules

The LLM is Claude Haiku 5.5 through an OpenAI-compatible gateway. The cases were written from the public
help-topics catalog and the policy by writers who never saw the rules code: 57 single-topic questions, 14
multi-part, 10 follow-ups, 15 policy questions, 16 that need a person and 8 irrelevant. Every reply a
customer received was judged twice, blind to the arm; the two judges agreed on 213 of 215.

| | A | B | B without LLM | C | D |
|---|---|---|---|---|---|
| Resolved correctly without a person | 66/120 (55.0%) | 77/120 (64.2%) | 50/120 (41.7%) | 83/120 (69.2%) | 76/120 (63.3%) |
| Wrong reply sent | 30/120 (25.0%) | 9/120 (7.5%) | 8/120 (6.7%) | 12/120 (10.0%) | 7/120 (5.8%) |
| Required handoffs missed | 4/16 | 1/16 | 1/16 | 0/16 | 0/16 |
| Unnecessary tickets | 7/104 | 17/104 | 46/104 | 8/104 | 20/104 |
| Latency, median / 95th percentile | 4 ms / 9 ms | 0.31 s / 3.3 s | 0.28 s / 0.40 s | 2.3 s / 3.1 s | 2.5 s / 3.5 s |
| Model cost per 1,000 requests | $0 | $0.31 | $0.08 | $0.56 | $0.66 |
| Model cost per correctly resolved request | $0 | $0.00048 | $0.00020 | $0.00081 | $0.00104 |

Resolved correctly, by kind of request:

| | A | B | B without LLM | C | D |
|---|---|---|---|---|---|
| Single topic (57) | 41 | 42 | 35 | 44 | 43 |
| Multi-part (14) | 7 | 13 | 7 | 12 | 11 |
| Follow-up (10) | 4 | 9 | 4 | 8 | 8 |
| Policy question (15) | 9 | 10 | 4 | 13 | 10 |
| Irrelevant (8) | 5 | 3 | 0 | 6 | 4 |

What it shows:

- **Rules alone are unsafe.** One reply in four was wrong and four of sixteen required handoffs were missed.
- **Jev makes rules safe.** Wrong replies fell from 30 to 9 and missed handoffs from 4 to 1.
- **Rules + Jev is cheaper and faster than the LLM, but resolves less.** Arm B costs 45% less per request
  than Haiku alone and answers in a third of a second at the median, but it resolved 77 requests to
  Haiku's 83 and opened 17 unnecessary tickets to Haiku's 8.
- **Without a generating model the idea does not hold up.** Rules + Jev alone resolved 50 of 120 and sent
  46 requests to a person that did not need one.
- **Tickets decide the economics.** Counting model cost plus one ticket for every request left unresolved,
  arm B is cheaper than Haiku alone only while a ticket costs less than about half a cent ($0.0051).
- **Jev over the LLM buys safety at a price.** Arm D sent the fewest wrong replies (7) but opened 20
  unnecessary tickets and cost the most.

Limits of this result: 120 cases; expected outcomes and judgements are AI-drafted and not yet reviewed by
a person; one LLM; gate cutoffs carried over from the pilot; the 989-line ruleset was written in a day and
its planned review was cut short. Arm B used Haiku on 40 of the 120 cases.

```bash
.venv/bin/python -m evaluation.compare --check
set -a; . ./.env; set +a          # TYPESAFE_API_KEY, LITELLM_BASE_URL, LITELLM_API_KEY
export FALLBACK_REASONING_EFFORT= FALLBACK_TIMEOUT_S=60
.venv/bin/python -m evaluation.compare run --split test --system-prompt-file evaluation/comparison/haiku_prompt.txt
.venv/bin/python -m evaluation.compare sheet evaluation/comparison/runs/<run>.json
.venv/bin/python -m evaluation.compare report evaluation/comparison/runs/<run>.json --judgements JUDGE1.json JUDGE2.json
```

The run, the blind sheet, both judges' verdicts and the full report (with break-even prices for every pair
of arms) are in `evaluation/comparison/`.

## Pilot evaluation (2026-11-21, on the earlier five-flow policy)

This section records the first evaluation. It was run against policy version 2026-11-01 and the original
five-flow rules; the policy and rules have since been expanded, so its numbers describe that earlier build.

`evaluation/cases/` holds 100 fictional situations (20 per flow) with 332 candidate replies. Whole
scenario families are assigned to the development split (60 situations) or the held-out test split (40).

```bash
.venv/bin/python -m evaluation.run --check                    # validate the case files; no network
set -a; . ./.env; set +a
.venv/bin/python -m evaluation.run gate --split dev --sweep   # score candidates, compare cutoffs
.venv/bin/python -m evaluation.run e2e --split dev            # full bot next to the rules-only baseline
.venv/bin/python -m evaluation.run gate --split test          # held-out; --sweep is refused here
.venv/bin/python -m evaluation.run e2e --split test
```

Jev scores are cached in `evaluation/scores/` by a hash of the exact request, so a re-run costs nothing
and any change to the wording, the evidence or the policy re-scores automatically. Reports go to
`evaluation/reports/`. The runner never edits `data/gate.json`: a person freezes the cutoffs.

### Labels

Every candidate is labelled *supported* and *complete*, and every situation *requires_human*, using
only what the gate sees at that turn. A candidate is **suitable** when it is supported and complete and
the situation does not require a person.

- **All labels are AI drafts.** Each was labelled twice, the second time blind. The two labellings
  disagree on 4 of 332 candidates and on none of the 100 handoff labels. Three situations are marked `ai_disputed`
  (`refund-019`, `return-003`, `missing-002`) and six were relabelled after rule wording changed
  (`ai_draft`). A reviewer should resolve those first and set `label_status` to `reviewed`.
- Two rulings the reviewer should apply consistently: a reply about the wrong item is not complete, and a
  bare handoff is complete only when the message leaves no open question.

### Results on 2026-11-21

Gate `pilot-1`, model `jev-1.14.0`, cutoffs supported 0.7, complete 0.7, requires-human handoff 0.7 and
low 0.3. The cutoffs were chosen on the development split and then run once on the test split.

Gate-specific comparison, counting candidates:

| | Development | Held-out test |
|---|---|---|
| Candidates (suitable / unsuitable) | 198 (45 / 153) | 134 (29 / 105) |
| Unsuitable candidates accepted | 1/153 (0.7%) | 1/105 (0.9%) |
| Suitable candidates not accepted | 6/45 (13.3%) | 9/29 (31.0%) |
| Required handoffs not flagged at 0.7 | 0/59 | 3/44, one situation; none was accepted |

End to end, counting situations:

| | Development | Held-out test |
|---|---|---|
| Resolved on the rules path | 20/60 (33.3%) | 9/40 (22.5%) |
| Fallback reply sent | 9/60 (15.0%) | 6/40 (15.0%) |
| Ticket forced by a backend rule | 9/60 (15.0%) | 1/40 (2.5%) |
| Ticket from the gate or the fallback | 22/60 (36.7%) | 24/40 (60.0%) |
| Required handoffs missed | 0/18 | 0/14 |
| Accepted rule drafts that were unsuitable | 0/20 | 0/9 |
| Rule drafts the baseline would have sent that were unsuitable | 15/38 (39.5%) | 21/34 (61.8%) |
| ...of those, stopped by the gate | 15/15 | 21/21 |
| Suitable rule drafts the gate did not accept | 3/23 (13.0%) | 4/13 (30.8%) |
| Ordinary requests that still ended in a ticket | 13/42 (31.0%) | 11/26 (42.3%) |
| Response time, median / 95th percentile / maximum | 0.35 s / 2.8 s / 3.4 s | 1.0 s / 3.4 s / 4.1 s |
| Jev cost per request, mean | $0.000051 | $0.000063 |

Held-out test by flow:

| | Track | Refund | Cancel | Return | Missing |
|---|---|---|---|---|---|
| Resolved on the rules path | 1/8 | 1/8 | 3/8 | 3/8 | 1/8 |
| Fallback reply sent | 0/8 | 4/8 | 1/8 | 1/8 | 0/8 |
| Ticket | 7/8 | 3/8 | 4/8 | 4/8 | 7/8 |
| Required handoffs missed | 0/3 | 0/3 | 0/2 | 0/4 | 0/2 |
| Accepted rule drafts that were unsuitable | 0/1 | 0/1 | 0/3 | 0/3 | 0/1 |

What the numbers say:

- **What the gate caught.** The rules-only baseline would have sent an unsuitable draft in 36 of 72
  cases across both splits; the gate stopped all 36. No required handoff was missed end to end.
- **What the gate cost.** 7 of 36 suitable rule drafts were not accepted, and 24 of 68 ordinary requests
  ended in a ticket. Most of those are requests the evidence does not cover (a different refund card, an
  exchange, faster delivery) or fallback replies Jev judged incomplete.
- **One unsuitable fallback reply got through.** 15 fallback replies were sent (9 development, 6 test).
  Two independent AI reviewers judged 14 of them suitable and both flagged `refund-015` in the test split:
  Jev rejected a correct rule draft as incomplete (0.38), then accepted a fallback reply that offered a
  return the customer had not asked for and misstated the refund timing. The backend check passed it
  because a return was permitted. A person has not judged these yet; the sheets are the `*-review.jsonl`
  files in `evaluation/reports/`, with the AI judgements under `ai_review`.
- **Sample sizes are small.** The test split has 14 situations that require a person and 13 suitable rule
  drafts, so these rates will move with more cases.

## Docker and Hugging Face Space

```bash
docker build -t jev-support-demo .
docker run --rm -p 7860:7860 --env-file .env \
  -e FALLBACK_BASE_URL=http://host.docker.internal:11434/v1 jev-support-demo
```

The image runs as UID 1000, listens on 7860 and contains only `app/`, `data/` and `requirements.txt`.
It uses one worker because sessions live in memory; a restart rebuilds every session from the seed data.

To host it as a Space:

1. Create a Space with the Docker SDK. This README's front matter already sets `sdk: docker` and
   `app_port: 7860`. Creating a Docker Space currently needs a paid plan (PRO for a personal account,
   Team or Enterprise for an organisation).
2. Add `TYPESAFE_API_KEY` as a Space secret. Secrets reach the container as environment variables.
3. Point the fallback at a hosted model: a Space cannot reach Ollama on your machine. Set
   `FALLBACK_BASE_URL`, `FALLBACK_MODEL` and `FALLBACK_API_KEY` to any OpenAI-compatible endpoint, and
   re-run the fallback tuning cases, because the system prompt was tuned against `qwen3.5:9b`.
4. Push this repository to the Space.

Each session allows 30 fallback calls and sessions are free to create, so set provider-side spend limits
before making a Space public.

## Layout

| Path | Responsibility |
|---|---|
| `app/main.py`, `app/web/` | HTTP routes, sessions, the customer chat and the developer trace drawer. |
| `app/store.py`, `app/rules.py` | Per-session SQLite store, item-level eligibility, tickets, confirmations, rule drafts. |
| `app/router.py` | The routing contract: clarification, rule reply, fallback, handoff. |
| `app/jev.py`, `app/fallback.py` | Provider requests and validation of their responses. |
| `data/` | Order fixtures with a fixed demo clock, the versioned policy, the frozen gate configuration. |
| `evaluation/`, `tests/` | The four-arm comparison (`compare.py`, `comparison/`), the pilot evaluation (`run.py`, `cases/`), and the tests. |

## Known limits

- The app's own fallback prompt (for local `qwen3.5:9b`) was tuned against the earlier five-flow policy and
  has not been re-tuned for the expanded one. The comparison used a separate prompt written for Haiku.
- The redesigned web UI works, but its last round of minor fixes and its acceptance pass were not completed.
- The rules now answer multi-part questions by combining templates, so "When will it arrive, and can I still
  cancel it?" is answered by a gated rule reply instead of the fallback.
- The backend check on fallback replies is a heuristic. It matches quoted dates, amounts, weekday names
  and a few status phrasings; Jev's *supported* score is the second line of defence.
- A reply that names a weekday becomes a ticket even when the weekday is right, because the order record
  holds dates only and Jev scores right and wrong weekdays alike.
- For a shipped item whose estimated date has passed, the fallback can still say "is expected on" that
  past date. A policy rule that opens a ticket for late shipments would close this.
- The backend accepts a fallback action when it is permitted for the item, without checking that the
  customer asked for it (see `refund-015` above).
- Follow-up questions in a long thread score lower on *complete* than the same question in a fresh thread.
- Jev is not fully deterministic: the same request can score a few hundredths apart.
- TypeSafe publishes no formal deprecation policy for pinned model IDs; watch for notices on `jev-1.14.0`.
