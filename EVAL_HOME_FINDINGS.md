# Evaluating Trovis's findings: does the AI discover anything?

**Base commit:** `51f5908` (`main`, after #192 and #194 merged)
**Evaluated commit:** this branch
**Model configured:** `claude-opus-5` · **Prompt version:** `home-investigation-2026-09-v2`
**Python 3.11.15 · SQLite 3.45.1. SQLite only — Postgres was not exercised.**

---

## The headline

**The live evaluation did not run.** There is no API key in this environment,
so no model was called, and **this document contains no evidence about whether
Trovis's AI is insightful.**

Missing credentials were never the real blocker, though. The first version of
this harness would have produced untrustworthy numbers if a key had appeared:
repetitions re-read a cache, a cautiously-worded finding was scored as two
false claims, discoverability was measured at three times the production
allowance, and the spend ceiling could not stop a single request. Those are
fixed here, and the corrected measurements are below.

| question | answer |
|---|---|
| Does the AI discover useful patterns? | **Not measured.** No live run. |
| Is the required evidence delivered under the **production** budget? | **Measured.** 9 of 9 — see the correction to the old claim. |
| Is the *repetition* pattern retrievable at all? | **No** (scenario E) — finding 2. |
| Did the model actually receive the evidence it cited? | **Verifiable now** — captured per investigation, reported `unavailable` when it cannot be. |
| What does Home generation actually cost? | **Unmeasured.** No live run, and `claude-opus-5` has no price in the table. |
| Do the fixtures establish what the rubric claims? | **Measured.** 120 checks. |
| Does the harness itself behave? | **Measured.** 153 checks. |
| Does the pipeline run end to end? | **Measured**, with a scripted model. |

---

## What this evaluation may conclude, and what it may only flag

The single most important change in this pass. Results come in three kinds and
they are never mixed:

| kind | what it is | can it fail a scenario? |
|---|---|---|
| **Deterministic** | Facts needing no interpretation: how many findings published; whether the analysis completed, failed or was unavailable; whether every cited evidence id was **actually delivered to the model during that investigation**; whether a numeric claim carries the `calc:`/`snapshot:` reference the contract requires; whether a monetary **figure** reached a reader whose seat excludes Cost. | **Yes** |
| **Unavailable** | A deterministic check that could not be run — most often because delivery instrumentation was not active. Never counted as a pass. | No |
| **Review flag** | A regex hit, carried with the clause it matched and whether that clause **asserts**, **denies** or **hedges** the thing. An invitation to read a sentence. | No |
| **Requires review** | Everything semantic, including *"did it discover the pattern?"*. No automated judgement is made; the rubric and the supporting evidence are attached and the question is left open. | No |

Two rules follow, and the previous scorer broke both:

* **The absence of a flag is not proof of truth.** A finding can be entirely
  wrong in words no regex anticipated.
* **The presence of an expected keyword is not proof of discovery.**
  `approval` appears in *"no approval step was involved"* too.

`discovered: true` and `false_claims` no longer exist. The worked example that
forced this: the sentence

> "The root cause is not established; there is no evidence of an outage."

is the model behaving *well* — declining to assert two things — and the old
scorer recorded it as two false claims. It now raises two review flags, both
with `disposition: negated`, and fails nothing.

Failure, incompleteness, unavailability and abstention are also kept apart.
**An empty response after a failure is not an abstention**; only a *completed*
analysis that published nothing is.

### Why a reviewer can tell the five cases apart

Every attempt carries a `diagnosis`:

| code | meaning |
|---|---|
| `published` | Findings reached the reader; judge them against the rubric. |
| `no_candidate_raised` | Discovery proposed nothing to investigate. |
| `investigated_and_withheld` | A candidate ran and was not published; the abstention reasons say why. |
| `draft_blocked_by_validation` | A draft existed and the deterministic validator refused it. |
| `required_evidence_unreachable` | The evidence the scenario turns on did not reach the investigation. |
| `pipeline_failed_before_decision` | No decision was reached. **Not** an abstention. |
| `incomplete_execution` | It did not finish what it started. **Not** an abstention. |
| `outcome_not_reported` | No worker report carried a candidate count, so why nothing was published cannot be established. |
| `not_run` | The harness declined to run this attempt (budget). |

The candidate **count** decides `no_candidate_raised`, not the abstention
message. The real pipeline reports `candidates: 0` *alongside*
`abstained: ["no candidate worth investigating"]`, and reading the message
first labelled that `investigated_and_withheld` — a candidate investigation
that never happened. A validator rejection still outranks the count, because a
rejection proves a draft existed.

---

## Five separate questions about evidence and discovery

The old report said "8 of 9 discoverable". That claim was **withdrawn**: it was
measured with 40 tool calls and 4,000 rows against production defaults of 14
and 400, and it conflated levels that have to stay apart.

1. **The evidence exists in the records** — asserted directly against the DB.
2. **It is retrievable through the allowlist** at all.
3. **It is delivered within the PRODUCTION budget** by an independent,
   optimally-targeted probe (`investigation_tools.ToolBudget()`).
4. **It was delivered during THIS investigation** — captured from the real
   `InvestigationSession.delivered` by test-only instrumentation, and used to
   check that published citations match what the model was actually shown.
5. **The model discovered a supported finding** — requires semantic review.
   Not measured.

Levels 3 and 4 are different questions and are never substituted for each
other. A successful probe says the budget *could* reach the evidence; it says
nothing about what this run retrieved. Comparing citations against the
publication's own evidence list — which the first version did — only checks
that the publication agrees with itself.

When the instrumentation is not active, the delivery check reports
**unavailable with a reason**. It is never reported as passed, and never
inferred from the publication. An **observed empty** delivery ledger stays
distinct from **unavailable**: the first says the model was shown nothing, the
second says we do not know what it was shown.

### Corrected measurement — level 3, production budget

Every scenario's required evidence is delivered within the product's own
allowance, with room to spare:

| scenario | tool calls | rows | events | delivered |
|---|---|---|---|---|
| A ordinary success | 8 / 14 | 6 / 400 | 12 / 600 | ✅ |
| B shared failing step | 8 / 14 | 6 / 400 | 12 / 600 | ✅ |
| C recovered errors | 5 / 14 | 3 / 400 | 6 / 600 | ✅ |
| D waiting on a person | 5 / 14 | 6 / 400 | 4 / 600 | ✅ |
| E optimization | 8 / 14 | 5 / 400 | 10 / 600 | ✅ rows — ❌ **pattern** (see below) |
| F positive change | 7 / 14 | 18 / 400 | 10 / 600 | ✅ |
| G incomplete visibility | 6 / 14 | 3 / 400 | 6 / 600 | ✅ |
| H cost and permission | 3 / 14 | 4 / 400 | 0 / 600 | ✅ |
| I counterexample | 8 / 14 | 20 / 400 | 12 / 600 | ✅ |

Nothing was refused for want of budget and no tool errored. The expanded
diagnostic probe still exists but is labelled separately and runs **only** when
the production probe comes up short; it may never support a production claim.

**Scenario E's rows do not mean its pattern arrived.** E's required run rows
are delivered, and its *repeated successful tool calls* are retrievable through
no tool at all. The scenario carries `pattern_retrievable: false` and a note
that travels with every report, so a ✅ in the delivery column can never be read
as "the repetition reached the investigation".

**The one thing this measurement flatters, stated plainly.** The probe already
knows which run ids matter, so it spends its allowance perfectly. A real
investigation must work out what to look at and will spend calls on questions
that lead nowhere. So a ✅ here means *"the production budget is sufficient for
an optimally targeted search"* — a lower bound on the difficulty, not a promise
that a model gets there. A ❌ would be the stronger result.

To show the measurement can fail at all, the budget is squeezed to 2 calls /
1 row / 1 event against scenario I: retrievals are refused, the required
evidence does not arrive, and the probe reports it as **missing** rather than
as absent from the records.

---

## Files

| file | what it is |
|---|---|
| `home_eval_scenarios.py` | Nine seeded scenarios, their rubrics, and the production/diagnostic probes |
| `home_eval_scoring.py` | The three-way contract: deterministic / review flag / requires review |
| `test_home_eval_scenarios.py` | Network-free ground truth + production-budget discoverability (120 checks) |
| `test_home_eval_harness.py` | The harness itself: repetition, scoring semantics, spending (90 checks) |
| `run_home_eval.py` | `plan` (default, no calls) · `stub` (scripted) · `live` (opt-in twice) |
| `eval_stub_model.py` | The scripted model for `--mode stub`. Harness only |

No product code is changed.

---

## Reproduction

```bash
# ground truth + production-budget discoverability. No network, no key, no spend.
TROVIS_DISABLE_PRICING_SYNC=1 python3 test_home_eval_scenarios.py

# the harness's own regressions: independent repeats, scoring, spending.
TROVIS_DISABLE_PRICING_SYNC=1 python3 test_home_eval_harness.py

# what a live run would do, and under what limits. Makes no calls.
python3 run_home_eval.py

# the whole pipeline with a scripted model. Proves the harness, nothing else.
python3 run_home_eval.py --mode stub

# two INDEPENDENT investigations of one scenario
python3 run_home_eval.py --mode stub --scenarios B --repeat 2

# the real evaluation (needs a key; spends money)
ANTHROPIC_API_KEY=sk-... python3 run_home_eval.py \
    --mode live --yes-spend-money --scenarios B,F,I --repeat 2 --max-usd 5
```

### Repetitions are independent experiments

`--repeat 2` seeds **two equivalent-but-distinct fixture accounts** and runs
each. The previous version re-read one account, and the product correctly
returned its cached analysis — so attempt 2 reported the same finding id, zero
jobs drained and `enqueued: false`. That measured nothing.

No production behaviour is bypassed. Declining to re-analyse an unchanged
audience is right, and a forced-refresh switch would have made the evaluation
measure something the product never does. A repetition is a **different
audience**, not a refresh. Proven by `test_home_eval_harness.py`:

```
PASS  two attempts use two different fixture accounts
PASS  each attempt drained its own job
PASS  each attempt produced a distinct analysis id
PASS  the two analyses cover different audiences
PASS  each attempt actually invoked the model
PASS  no job from another fixture was attributed to an attempt
PASS  the product declines to re-analyse an unchanged audience   ← still true
PASS  and that re-read drains no job / invokes the model zero times
```

Each attempt records its scenario and repetition, fixture id and version,
account, job ids, analysis ids, scope keys, model calls, model and prompt
version, and whether it completed, failed, abstained or was skipped. A skipped
attempt carries no fixture and no findings, so it can never be mistaken for a
run.

### Limits and spending control

| bound | value | source |
|---|---|---|
| model calls, whole run | 60 (`--max-calls`) | this runner |
| estimated spend | $5 (`--max-usd`), checked **before each request** | this runner |
| provider retries | **0** | set explicitly on the live client |
| wall clock per investigation | 240s | `TROVIS_INVESTIGATION_WALL_S` |
| per-call timeout | 60s | `TROVIS_INVESTIGATION_TIMEOUT_S` |
| candidates / turns / published | 4 / 6 / 5 | `investigator` |
| retrieval | 14 calls / 400 rows / 600 events | `investigation_tools.ToolBudget` |

Each request is bounded before it is sent:

* **Input** is counted by the **provider** (`messages.count_tokens`), plus
  documented headroom of **10% and 512 tokens** for the server-side additions
  the caller's arguments do not show. `len(serialized)/3 + 1000` was a
  heuristic wearing the words "upper bound" — it has no guarantee behind it and
  under-counts exactly where it matters (dense non-English text, base64, long
  tool schemas). A request whose tokens **cannot** be counted is **refused**,
  not sent on the assumption the guess was close.
* **Output** is the request's own `max_tokens`, which the server enforces, so
  it is a true ceiling. A request with no `max_tokens` is refused.

The reservation is then reconciled against the provider's reported usage —
**but only when every field is reported**. Partial usage is unknown
consumption and keeps its reservation: a response carrying `input_tokens=100`
with `output_tokens` missing previously turned a $0.031 reservation into
$0.0001 of accounted spend and left `usage_missing` at zero. Absent, partial,
invalid and negative usage all now keep the reservation and are recorded in
`partial_usage_responses`.

A request that **raises** keeps its reservation too — it was sent, it may have
been served and billed, and releasing it would make failures free and a retry
storm invisible. Provider retries are disabled, so one counted call is one
request. The report separates `of_which_measured` from
`of_which_unreconciled_reservations`, so a reader can see how much of the
figure is observation and how much is allowance.

**Unknown pricing stops a dollar-budgeted live run outright**, and this is not
hypothetical: **`claude-opus-5` has no row in this repository's pricing table**,
so `--mode live` currently refuses with

> refusing: no price is known for claude-opus-5, so a dollar budget cannot be
> enforced. Nothing was run and nothing was spent.

A price has to be added before a bounded live evaluation is possible. That is
recorded as a gap rather than worked around.

This bounds an **estimate**. It is not a statement about the provider's bill:
the prices are this repository's, and only the provider knows what it charged.

**Actual usage: 0 model calls, 0 tokens, $0.00.** No live evaluation ran.

### Usage reconciliation — SCRIPTED run, no model, no spend

`python3 run_home_eval.py --mode stub --scenarios A,H --repeat 2` (scripted
model; the call counts are real, the answers are hand-written):

```
usage:          "model_calls": 28
reconciliation: {"global_model_calls": 28,
                 "sum_of_attempt_totals": 28,
                 "unattributed_model_calls": 0,
                 "reconciles": true}

A#1 total 5   (primary 5)
A#2 total 5   (primary 5)
H#1 total 9   (primary 5 + restricted reader 4)
H#2 total 9   (primary 5 + restricted reader 4)
```

Before this pass the same command reported 28 global against 20 attributed:
scenario H's restricted reader is a separate audience with its own scope key,
its own queued job and its own model calls, and those eight calls were spent
globally and recorded nowhere. Each attempt now carries `usage_readers` (per
reader), `usage_attempt_total` (the sum), and a note saying to reconcile with
the totals and **not** to add the subtotals on top. A non-zero
`unattributed_model_calls` is printed as a harness defect.

Job ownership is resolved from `analysis_jobs.account_id` — the job record —
rather than from a `account_id` field the worker reports usually omit. The
previous version read a missing field as "ours", so any job draining during an
attempt was attributed to it. An id that cannot be resolved is attributed to
**nobody**.

Isolated throwaway SQLite per run; `DATABASE_URL` is unset unconditionally; no
production records are read and nothing is written outside the temp database.
Only synthetic fixture data and diagnostics are persisted — no credentials, no
environment dump.

---

## The scenarios

Each seeds real work through `database.ingest_spans_with_loops` — the ordinary
ingest path — into its own account. **No finding is ever hand-written into the
findings table.** Every rubric was written before any run.

| | scenario | records establish | abstention | a false claim would be |
|---|---|---|---|---|
| A | ordinary successful work | 6 items, all completed, no failing step | **preferred** | any failure, business impact, "healthy" |
| B | repeated unsuccessful work | 4 abandoned at the *same* step; 2 others completed | no | a cause, "all refunds", any % |
| C | recovered errors | 3 failed a step then completed | ok | "failed", "work was lost" |
| D | human handoff delay | 2 waiting on the reader, 1 on someone else | ok | an SLA, blame, "overdue" |
| E | plausible optimization | 4 runs × 8 searches; a 5th with 2 also completed | ok | "wasteful", a dollar saving |
| F | supported positive change | 9 started each window; completed 4 → 8 | no | a cause, a projection |
| G | incomplete visibility | 3 items in 2 days; 5 days with **no record** | **preferred** | "fell", "is down", "healthy" |
| H | cost and permission | org-wide spend, partial coverage, a reader without Cost | ok | team cost attribution; **any** money to the restricted reader |
| I | counterexample | 3 abandoned — but 3 of 10 last window too, at 3 *different* steps | **preferred** | "new", "regression", "same step" |

### Known limits of these fixtures

1. **One account per scenario instance.** Clean rubrics, unrealistic quiet. A
   real workspace has many things competing for four candidate slots.
2. **Synthetic records.** Titles, error strings and step names are invented.
3. **`align_to_spans` backdates the work items** (finding 1). Real accounts get
   no such correction.
4. **The probe spends its budget perfectly**, as above.
5. **Nine situations is not a distribution.**
6. **Synthetic evaluation establishes behaviour on these scenarios. It does not
   establish production accuracy.**

---

## Findings — discovered without a model

Both are product defects found while building the fixtures. Neither is fixed
here: showing before-and-after needs the model this environment cannot call,
and both were explicitly scoped out of this correction pass.

### 1. Ingest time is not event time, and the period windows on ingest time

`INSERT INTO loops` lets `created_at` default to the insert clock, and closing
stamps `closed_at` with `NOW()`. Neither uses the span timestamps the telemetry
carries. Home's completions, the chart, the by-job breakdown and
`compare_outcome_mix` all window on these columns.

Telemetry arriving later than the work happened — a buffered agent, a replayed
export, an overnight batch — is filed into the period it *arrived* in. Seeding
a fortnight put all of it in the current window: `compare_outcome_mix` returned
`started: 18, previous: 0` for a fixture built to be 9 and 9.

**Severity: high.** It silently misattributes the headline number.

### 2. A successful repeated tool call is invisible to the investigation

`inspect_run` returns lifecycle events and *failed* spans. Nothing in the
allowlist returns the tool calls a run made when they succeeded. Scenario E
seeds four runs of eight `web_search` calls and one run of two, and all an
investigation can retrieve is that one run has more spans than another.

This sits at **level 2**, not level 3: it is not a budget problem, and no
budget fixes it. Asserted in the fixture test so the gap cannot close silently.

**Severity: high for the product goal** — "evidence-backed opportunities" is
one of Home's five promises.

### 3. Two different populations are both called "completed"

The snapshot counts completions by `closed_at` in the period (work that
*finished*). `investigation_outcome_mix` counts by `created_at` (work that
*started* and has since finished). A finding may cite
`calc:mix.current.completed` and it renders beside the snapshot total with
nothing saying they are different populations. **Severity: medium.**

---

## Tests run, and actual results

```
python3 test_home_eval_scenarios.py   ALL PASS (120 checks)   no network
python3 test_home_eval_harness.py     ALL PASS (153 checks)   no network
python3 run_home_eval.py --mode stub  9/9 reach execution=completed
python3 run_home_eval.py --mode stub --scenarios B --repeat 2
                                      2 accounts, 2 jobs, 2 analysis ids, 5 calls each
python3 run_home_eval.py --mode stub --scenarios A,H --repeat 2
                                      28 global calls = 28 attributed, 0 unattributed

test_home_findings.py                 PASS
test_home_findings_eval.py            PASS
test_home_findings_review.py          PASS
test_home_findings_round2.py          PASS
test_home_findings_round3.py          PASS
test_home_snapshot.py                 PASS
test_home_snapshot_bounds.py          PASS
test_home_snapshot_integrity.py       PASS
test_home_desk.py                     PASS
test_surface_breadth.py               PASS
```

Python 3.11.15, SQLite 3.45.1. **SQLite only — Postgres not exercised.** All
external services are stubbed; no frontend code changed, so no frontend suite,
build or lint was required.

The scripted run also shows the evidence retention working: each attempt's
transcript carries the worker's own `candidates`, `rejected`, `abstained`,
`completion_gaps`, `coverage_gaps`, `retrieval` (with limitations), per-finding
`validation`, and the full publication records including claims and evidence.
**The pipeline does not persist a model transcript, so none is included** and
`transcript_available: false` says so rather than implying one exists.

---

## What was NOT done, and why

* **No live model run.** No key; `ANTHROPIC_BASE_URL` points at the real API
  with no credential. An earlier session confirmed it: `401
  authentication_error`. No scripted output is presented as live evidence.
* **No repeated-run consistency measurement.** The mechanism is now correct and
  tested, but it has never been exercised against a real model.
* **No UI inspection.** There were no live findings to inspect.
* **No prompt, retrieval or validation changes**, and none of findings 1–3 were
  fixed — all explicitly out of scope for this pass.
* **No judge-model call** was added for semantic assessment; those results are
  labelled `requires_review` with a rubric instead.

---

## Remaining gaps and what needs human review

1. **The core question is unanswered.** Nobody has watched Trovis's AI discover
   anything, and **what Home generation actually costs is unmeasured** — there
   has been no live run, and `claude-opus-5` has no price in the table, so even
   an estimate has nothing to stand on. The apparatus is now trustworthy enough
   to find out.
2. **`claude-opus-5` has no price in the pricing table**, so a dollar-bounded
   live run is refused. This must be fixed before a live evaluation.
3. **Every semantic judgement requires a human.** `discovery` is always
   `requires_review`; the harness supplies the finding, its claims, its
   evidence, the scenario's `establishes` / `not_established` lists and the
   review flags with their dispositions, and a person decides.
4. **Delivery instrumentation is test-only and subclass-based.** It captures
   `InvestigationSession.delivered` for sessions created during an attempt. If
   the product ever constructs a session by another route, the check reports
   `unavailable` rather than silently missing it — but a reviewer should read
   that field rather than assume it ran.
5. **The input bound rests on `count_tokens` plus 10% and 512 tokens.** That
   headroom is a judgement, not a proof; it covers the server-side additions
   observed in this pipeline's requests and has never been validated against a
   live bill, because there has been no live run.
6. **Review flags are heuristic in both directions.** Negation and hedging are
   read from cue words at clause granularity; sarcasm, double negatives and
   long-range scope are not handled, and a false claim phrased in unanticipated
   words raises no flag at all.
7. **Finding 2** blocks an entire promised category.
8. **Finding 1** silently misfiles late telemetry.
9. **Fixture isolation and perfect probe targeting** both make the scenarios
   easier than production.
10. **No adversarial scenario** — nothing evaluates an agent emitting text that
   tries to steer the investigation.
