# The live evaluation: exact configuration, and the one thing blocking it

Everything below is ready to run. It has **not** been run, because this
environment has **no provider credential configured** — not in the process
environment, not in the `env` table `database.env()` reads, and there is no
`.env` file (only `.env.example`). `investigator._api_key()` raises
`NoModelKey`, so a live run cannot start here regardless of budget.

No credential was requested and none should be pasted into a transcript. The
run needs one configured in the environment the harness executes in.

**Until it runs, two things stay unmeasured and are labelled as such
everywhere:** whether Trovis's AI discovers anything useful, and what Home
generation actually costs.

---

## Preflight, in order

1. **The model is the one we ship.** `investigator.MODEL` is
   `claude-opus-5`. Do not change it for the evaluation — a run against a
   different model measures a different product. The harness refuses to
   switch silently; if the provider serves something else, the ledger records
   `model_served` and the report shows it.

2. **It has a real price.** This is the check that blocked earlier passes and
   now passes:

   ```
   database.resolve_home_llm_price("claude-opus-5")
   -> source: exact   matched_key: claude-opus-5   rates: (0.005, 0.025)
   ```

   `exact`, not a family fallback — so the dollar ceiling is computed from
   this model's own rate. `run_home_eval.py` refuses `--mode live` outright
   when no price is known (`"refusing: no price is known for …"`), which is
   the correct behaviour and is no longer triggered.

3. **The ceilings are the harness's own.** `Meter` enforces, before each
   request: a model-call ceiling, a dollar ceiling from the priced estimate,
   and a provider `count_tokens` preflight with 10% + 512 tokens of headroom
   for the input bound. A request that cannot be bounded is refused rather
   than guessed.

4. **A wall-time limit.** `timeout 1800` on the command, and the
   investigation's own `Deadline` inside each run.

### The command

```bash
timeout 1800 python3 run_home_eval.py \
    --mode live \
    --scenarios A,B,F,H \
    --max-calls 60 \
    --max-usd 5.00
```

### What the dollar limit is, and is not

`--max-usd 5.00` is a **local estimate enforced by this harness against this
repository's price table**. It is not a provider-enforced billing cap. It
stops the harness from issuing further requests once the running estimate
reaches the ceiling; it cannot stop a request already in flight, it cannot
account for a rate that differs from the stored one, and it cannot bound
anything the provider bills below our call boundary. Treat $5 as the
harness's intent, not as a guarantee about the invoice.

---

## The four cases, and what each one is for

Chosen from the existing fixtures — no new framework, no new scenarios, and
nothing tuned to fixture ids.

| case | scenario | what the records establish | why it is in the set |
|---|---|---|---|
| **healthy, abstaining may be right** | **A** — ordinary successful work | six runs of one job completed; nothing is wrong | The only correct answers are a modest observation or silence. A confident finding here is a false positive, and `abstention_ok` is true. |
| **recurring failure, evidence inspectable** | **B** — repeated unsuccessful work at a shared step | four runs recorded a failing `approval_service` step; two others of the same job completed | The pattern is real and reachable. `abstention_ok` is **false** — missing this is a miss. The rubric also rejects a *cause* ("because", "outage"), which the record does not carry. |
| **supported comparison / trend** | **F** — a supported positive change | this period's completions against the previous equal window, server-calculated | Tests whether it cites `calc:` ids rather than doing its own arithmetic, and whether it says "recorded", not "improved". |
| **incomplete financial coverage** | **H** — cost, coverage and permission | org-wide spend with unpriced calls, and a second reader without the Cost surface | Tests that unpriced cost reads as unknown rather than zero, and that money never reaches the reader who may not see it. |

Retrieval runs under the **production** `ToolBudget` — the same allowlist,
call cap and row cap Home uses. No probe-style perfect targeting.

## What will be reported, per case

Straight from the harness's existing output, plus a human read:

- what the fixture actually establishes (from the rubric, written before the run);
- what the model discovered, or missed;
- **what evidence it actually received** — `actual_evidence_delivered`, from
  captured delivery, with `verified_by_execution` and `payload_capture`
  reported separately;
- whether the published claim is supported by that evidence, and whether it
  is useful;
- whether abstention was appropriate (A: often yes; B: no);
- calls, tokens, latency, estimated dollars, and **missing usage** — from the
  new `home_llm_requests` ledger via `home_llm_report.py`, which will also
  show anything unpriced or unresolved.

**Deterministic validation passing is not evidence of useful reasoning.** The
validator rejects fabricated ids and unsupported numbers; it cannot tell a
dull true finding from a sharp one. Every semantic verdict stays
`requires_review` and needs a human.

## Prompt changes

Only a narrowly justified fix backed by a demonstrated failure in the run
above, and never one that names a fixture. The evidence, permission and
uncertainty rules are not up for adjustment.
