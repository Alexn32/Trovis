# Evaluating Trovis's findings: does the AI discover anything?

**Base commit:** `51f5908` (`main`, after #192 and #194 merged)
**Evaluated commit:** this branch
**Model configured:** `claude-opus-5` · **Prompt version:** `home-investigation-2026-09-v2`
**Database backend exercised:** SQLite only. Postgres was not run.

---

## The headline

**The live evaluation did not run. There is no API key in this environment, so
no model was called, and this document contains no evidence about whether
Trovis's AI is insightful.**

What it does contain is everything needed to get that evidence in one command,
plus two defects found *without* a model — both in the product, not the
prompts, and both of which would distort a live evaluation if it ran today.

| question | answer |
|---|---|
| Does the AI discover useful patterns? | **Not measured.** No live run. |
| Is the evidence it would need reachable? | **Measured.** 8 of 9 scenarios yes, 1 no (finding 2). |
| Do the fixtures establish what the rubric claims? | **Measured.** 98 checks, all pass. |
| Does the pipeline run end to end? | **Measured**, with a scripted model. |
| Does the rubric catch a bad finding? | **Measured**, against hand-written bad drafts. |

---

## What already existed, and what was missing

| | |
|---|---|
| `test_home_findings.py` and the three round files | The machinery, with a **scripted** model. Prove the pipeline's plumbing. |
| `test_home_findings_eval.py` | A rubric applied to **canned candidate findings**. Its own docstring says it measures the guard, not the model. |
| **Nothing** | Any evaluation that ran a real model against a known situation. |

So the gap was not "is there a rubric" — it was that every existing rubric
scored text somebody had written by hand. This PR adds the part where real work
records go in one end and whatever the model actually says comes out the other.

Existing infrastructure is reused rather than replaced: the same
`TestClient`-over-isolated-SQLite pattern, the same `investigator._client`
override point, the same `analysis_jobs.drain()` the round tests use.

---

## Files

| file | what it is |
|---|---|
| `home_eval_scenarios.py` | Nine seeded scenarios and the rubric for each, written before any model ran. |
| `test_home_eval_scenarios.py` | Network-free. Verifies the fixtures' ground truth and that the evidence is retrievable. CI-safe. |
| `run_home_eval.py` | The pipeline runner: `plan` (default, no calls), `stub` (scripted), `live` (real, opt-in twice). |
| `eval_stub_model.py` | The scripted model for `--mode stub`. Harness only. |

---

## Reproduction

```bash
# ground truth + discoverability. No network, no key, no spend.
TROVIS_DISABLE_PRICING_SYNC=1 python3 test_home_eval_scenarios.py

# what a live run would do, and under what limits. Makes no calls.
python3 run_home_eval.py

# the whole pipeline with a scripted model. Proves the harness, nothing else.
python3 run_home_eval.py --mode stub

# the real evaluation (needs a key; spends money)
ANTHROPIC_API_KEY=sk-... python3 run_home_eval.py \
    --mode live --yes-spend-money --scenarios B,F,I --repeat 2 --max-usd 5
```

`--mode live` refuses without **both** `--yes-spend-money` and a key. There is
no default that spends.

### Limits, and actual usage

| bound | value | where it comes from |
|---|---|---|
| model calls, whole run | 60 (`--max-calls`) | this runner |
| estimated spend | $5 (`--max-usd`), checked before each scenario | this runner, priced from the repo's own pricing table |
| wall clock per investigation | 240s | `TROVIS_INVESTIGATION_WALL_S`, the product's own |
| per-call timeout | 60s | `TROVIS_INVESTIGATION_TIMEOUT_S` |
| candidates per analysis | 4 | `investigator.MAX_CANDIDATES` |
| investigation turns | 6 | `investigator.MAX_INVESTIGATION_TURNS` |
| findings published | 5 | `investigator.MAX_FINDINGS_PUBLISHED` |
| retrieval | 14 calls / 400 rows / 600 events | `investigation_tools.ToolBudget` |

**Actual usage: 0 model calls, 0 tokens, $0.00.** Nothing was spent because
nothing ran. Token counts in a live run come from the provider's own `usage`
block; the dollar figure is this repository's pricing table applied to them and
is an estimate, labelled as such in the transcript.

Isolated throwaway SQLite per run, `DATABASE_URL` unset unconditionally. No
production records are read and nothing is written outside the temp database.

---

## The scenarios

Each seeds real work through `database.ingest_spans_with_loops` — the ordinary
ingest path — into its own account. **No finding is ever hand-written into the
findings table**; doing so would measure the validator and be presented as
measuring the model.

Every rubric was written before any run and states five things: what the
records establish, what they do **not** establish, which discoveries would be
acceptable, which claims would be false, and whether abstention is correct.

| | scenario | records establish | abstention | a false claim would be |
|---|---|---|---|---|
| A | ordinary successful work | 6 items, all completed, no failing step | **preferred** | any failure, any business impact, "healthy" |
| B | repeated unsuccessful work | 4 abandoned at the *same* `approval_service` step; 2 others completed | no | a cause, "all refunds", any % |
| C | recovered errors | 3 items failed a step then completed | ok | "failed", "work was lost" |
| D | human handoff delay | 2 items waiting on the reader, 1 on someone else | ok | an SLA, blame, "overdue" |
| E | plausible optimization | 4 runs × 8 searches; a 5th with 2 also completed | ok | "wasteful", a dollar saving, "cheaper model" |
| F | supported positive change | 9 started each window; completed 4 → 8, same job and agent | no | a cause, a projection, "doubled" |
| G | incomplete visibility | 3 items in 2 days; 5 days with **no record**; one quiet agent | **preferred** | "fell", "nothing happened", "is down", "healthy" |
| H | cost and permission | org-wide spend, partial pricing coverage, a reader without Cost | ok | team-level cost attribution; **any** money to the restricted reader |
| I | counterexample | 3 abandoned recently — but 3 of 10 last window too, at 3 *different* steps | **preferred** | "new", "regression", "same step", "worse than last week" |

### Known limits of these fixtures

1. **One account per scenario.** Clean rubrics, unrealistic quiet. A real
   workspace has nine things competing for four candidate slots, and a pattern
   obvious in isolation may be crowded out. This is the fixture set's biggest
   weakness.
2. **Synthetic records.** Titles, error strings and step names are invented.
   Real telemetry is messier, and messiness is where discovery gets hard.
3. **`align_to_spans` backdates the work items** (see finding 1). Real
   accounts do not get that correction.
4. **Nine situations is not a distribution.** Passing all nine says nothing
   about the tenth.
5. **Synthetic evaluation establishes behaviour on these scenarios. It does
   not establish production accuracy.**

---

## Findings — discovered without a model

Both are product defects, found while building the fixtures. Neither is a
prompt problem, and both would distort a live evaluation run today.

### 1. Ingest time is not event time, and the period windows on ingest time

`INSERT INTO loops` lets `created_at` default to the insert clock, and closing
a work item stamps `closed_at` with `NOW()`. Neither uses the span timestamps
the telemetry actually carries — `last_event_unix` does, but the two columns
every period window reads do not.

**Consequence.** Telemetry that arrives later than the work happened — a
buffered agent, a replayed export, an overnight batch, a reconnect after an
outage — is filed into the period it *arrived* in. Home's "recorded
completions", the completions chart, the by-job breakdown and
`compare_outcome_mix` all window on these columns, so a backfill lands as a
spike today and the week it belongs to stays empty.

**How it showed up.** Seeding a fortnight of work put all of it in the current
window: `compare_outcome_mix` returned `started: 18, previous: 0` for a fixture
built to be 9 and 9. `home_eval_scenarios.align_to_spans()` corrects the
fixtures by moving each item onto its own span times; the docstring explains
why it exists.

**Severity: high.** It silently misattributes the product's headline number,
and the user cannot see it happening.

### 2. A successful repeated tool call is invisible to the investigation

`inspect_run` returns lifecycle events and *failed* spans. Nothing in the
allowlist returns the tool calls a run made when they succeeded. Scenario E
seeds four runs of eight `web_search` calls each and one run of two — and the
only thing an investigation can retrieve is that one run has more spans than
another, with no way to learn what those spans were.

**Consequence.** "This agent repeats the same lookup eight times" is not a
discoverable pattern, so the entire optimization category is largely
unreachable. `OPTIMIZATION_PROMPT` asks for a proposal "tied to an OBSERVED
MECHANISM" — and for repeated work, the mechanism cannot be observed.

Asserted in `test_home_eval_scenarios.py` so the gap cannot close silently:

```
PASS  repeated tool calls are NOT retrievable through inspect_run
PASS  only the span count distinguishes a heavy run from a light one
```

**Severity: high for the product goal.** "Evidence-backed opportunities" is one
of Home's five promises, and this is the most common shape of one.

### 3. Two different populations are both called "completed"

The snapshot counts completions by `closed_at` **within the period** — work
that *finished* in the window. `investigation_outcome_mix` counts by
`created_at` — work that *started* in the window and has since finished. Both
are defensible; they are not the same number.

A finding may cite `calc:mix.current.completed`, and it is rendered directly
beside the snapshot's completion total. A reader comparing the two is comparing
different populations, with nothing on screen saying so.

**Severity: medium.** Wrong only at the boundaries, but Home's contract is that
the numbers and the interpretation describe the same slice.

---

## What actually ran

### Ground truth and discoverability — 98 checks, all pass

```
TROVIS_DISABLE_PRICING_SYNC=1 python3 test_home_eval_scenarios.py
→ ALL PASS (98 checks)
```

Per scenario, the retrieval allowlist was run against the seeded records and
what it returned was checked against the rubric. Highlights:

* **B** — six refunds retrievable in one listing; four abandoned, two
  completed (so "every refund" is refutable); each abandoned run's
  `approval_service` failure is reachable via `inspect_run`.
* **C** — all three completed **and** still carry `error_span_count ≥ 1`, so
  the distinction between friction and failure is visible rather than implied.
* **D** — three waits retrievable with their holders; the snapshot's
  `needs_you` is 2, counting only the reader's own.
* **F** — 9/9 started, 4 → 8 completed, equal-length non-overlapping windows,
  each number carrying the `calculation_id` a claim must cite.
* **G** — the previous window is genuinely empty and the snapshot reports
  `comparison_available: false`; the quiet agent has no error spans, so
  silence is a gap and not a recorded failure.
* **H** — org-wide spend retrievable with `coverage_ratio 0.57` and 3 unpriced
  spans; the permitted reader's block is labelled `organization_wide`; the
  restricted reader's is `visible: false` with
  `unavailable_reason: seat_excludes_financial_surface` and every field null —
  **while still seeing work in scope**, so the gate is genuinely under test
  rather than passing on an empty page.
* **I** — 3 abandoned recently, 3 abandoned previously, 10 started in each,
  and the three recent failures are at three different steps. Every fact that
  refutes "newly broken" is retrievable.
* **E** — **fails discoverability**, by design, and that failure is finding 2.

### The rubric catches bad findings

Hand-written drafts of the shape a model could produce were run through
`score()` — as a test of the scorer, not of any model:

| draft | outcome |
|---|---|
| "Refunds are failing **because** the approval service is down" | flagged (asserted cause) |
| "**All** refunds stopped" | flagged (over-scoped) |
| "Three shipments **failed to complete**" (they completed) | flagged |
| "**This team** spent $40" | flagged (attribution) |
| "Spend was $40" shown to the restricted reader | flagged (any money at all) |
| "Four refunds stopped at the same approval step" | passes, counts as a discovery |
| publishing nothing on A | not a miss |
| publishing nothing on B | **counted as a miss** |

That last pair is the point: abstention is scored as correct or incorrect per
scenario, so "say nothing" cannot quietly score well everywhere.

### The pipeline, with a scripted model

```
python3 run_home_eval.py --mode stub
→ 9/9 scenarios reach analysis state `current`, outcome `complete`
→ snapshot → enqueue → retrieval → validation → publication → Home read
```

**This says nothing about model quality.** Its value is that the live run is
now one command rather than a debugging session — and it did surface one real
property: the naive scripted answer scored `discovered: False` on B, C, D, E, F
and H, which is what a weak model looks like through this rubric. The rubric
discriminates.

---

## What was NOT done, and why

* **No live model run.** `ANTHROPIC_API_KEY` is absent;
  `ANTHROPIC_BASE_URL` points at the real API with no credential. An earlier
  session in this environment confirmed it end-to-end: the worker's request was
  rejected `401 authentication_error`. **No scripted output is presented as
  live evidence anywhere in this PR.**
* **No repeated-run consistency check.** That needs a live model. `--repeat`
  exists and is wired; it has never been exercised against a real one.
* **No UI inspection of live findings.** There were no live findings to
  inspect. PR #192's browser checks used *hand-seeded* findings rows and are
  not re-claimed here as evidence about model output.
* **No prompt, retrieval or validation changes.** Section 6 asks for
  before-and-after behaviour on any correction. Without a model there is no
  "before", so tuning the prompts now would be guessing dressed as evidence.
  Findings 1–3 are documented and proposed as separate work below.
* **No Postgres run.** SQLite only.
* **No frontend change**, so no frontend suite, build or lint was required.

---

## Remaining gaps, ranked by user impact

1. **The core question is unanswered.** Nobody has yet observed Trovis's AI
   discover anything. Everything shipped here is the apparatus for finding out.
2. **Finding 2 — repeated tool calls are unretrievable.** An entire promised
   category ("evidence-backed opportunities") is mostly unreachable. Proposed
   fix: a `tool_usage` summary on `inspect_run` (name → count, success/failure
   split), or a `tool_concentration` tool. Small, additive, and it needs the
   live evaluation to confirm the model then uses it.
3. **Finding 1 — late telemetry lands in the wrong period.** Silently wrong
   headline numbers. Fix: set `created_at`/`closed_at` from span timestamps at
   ingest, with a migration decision for existing rows. Not small, and not
   this PR's to make.
4. **Finding 3 — two "completed" populations.** Either align the windows or
   label them where they meet on screen.
5. **Fixture isolation.** Nine quiet accounts are not one noisy one. A
   tenth scenario combining several patterns in one account would test
   candidate selection, which is where a four-slot budget actually bites.
6. **No adversarial scenario.** Nothing yet tests an agent emitting text that
   tries to steer the investigation. `_untrusted()` fences retrieved content
   and `test_home_findings.py` covers the fencing, but no *evaluation*
   scenario probes it.
