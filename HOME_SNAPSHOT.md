# `GET /home/snapshot` — the authoritative Home snapshot

The server-side data foundation for the rebuilt Home. One request, one set of
numbers, every definition written down. Home is about to make claims about
what work got done, what needs someone's attention, what it cost and where it
could be better — and a page that assembles those claims from six list
endpoints ends up with claims that disagree with each other.

This PR is the foundation only. It does not redesign Home and adds no AI
generation. Later analysis is expected to read *this* snapshot rather than
re-derive its own numbers.

- Endpoint: `main.home_snapshot_endpoint`
- Orchestration, period math, policy: `home_snapshot.py`
- SQL: `database.get_home_snapshot_rows` / `database.home_snapshot_totals_sql`
- Response shape: `models.HomeSnapshot`
- Tests: `test_home_snapshot.py` (contract), `test_home_snapshot_integrity.py`
  (completion semantics, truncation, query shape, scope metadata)

## Three rules the shape encodes

1. **Current state and period totals are different kinds of measure.** "Two
   items are moving right now" and "five finished this week" answer different
   questions. They live in `current_state` and `period`, never merged.
2. **Missing is not zero.** An org four days old did not complete zero last
   week — it has no last week. Anything unestablished is `null` plus a named
   reason.
3. **Recorded is not verified.** A closed work item is a *recorded
   completion*. Nothing here calls it a confirmed business outcome, a success,
   or a saving.

## Request

```
GET /home/snapshot?days=7&tz=America/Chicago&whose=team
```

| Param | Meaning |
|---|---|
| `days` | Period length in **local calendar days**. 1–90, default 7. Out of range → 422. |
| `tz` | IANA timezone for the boundaries and the buckets. Default `UTC`. Unknown zone → 400. |
| `whose` | The existing whose-work selector: `everyone` \| `me` \| `team` \| `person`. An unreadable value narrows to `everyone` rather than 400-ing, matching `/work/items`. |
| `person_id` | Required when `whose=person`. Someone outside your reporting line → 403. |

The period runs from local midnight `days - 1` days ago up to *now*, so the
last bucket is a partial local day ("this week, so far"). Boundaries come back
explicitly, in local time and in UTC.

## Response

Full field list is `models.HomeSnapshot`; each model there carries its own
docstring. Summary:

### `scope`
Four distinct things, deliberately not collapsed into each other:

| field | meaning |
|---|---|
| `requested` / `effective` | the selector asked for, and the one the server **applied**. They differ only when the raw value was unreadable (`request_unreadable: true`), which narrows nothing. |
| `breadth`, `people_in_scope`, `clamped_by_seat` | the permission **ceiling** and what it removed. |
| `choices`, `selector_offered` | what a **control** should offer. Advisory display affordance only. |
| `membership_complete` | whether the resolved membership is the whole truth. |

Everything comes from `main._resolve_whose_selection`, the one place a request
is intersected with the seat; nothing is re-derived here, so the metadata
cannot describe a different query than the one that produced the numbers beside
it. In particular **`effective` is never inferred from `choices`**. A
company-breadth person with no reports who asks for `team` gets their own work:
`effective` is `team`, `filtered` is true, `people_in_scope` is 1, and
`selector_offered` is false because a control would not have drawn that option.
Reporting `everyone` there — as an earlier version did, because `team` was
absent from `choices` — describes the opposite of the filter that ran.

`breadth`, `scope_level_id` and `role_id` are seat **atoms**; nothing branches
on a scope level's name or a role title, so a custom level behaves exactly like
a preset composed the same way. `filtered: false` with `people_in_scope: null`
means company breadth: no person filter is applied at all. That is **not**
"nobody" — work can be held by a person with no login, and an id list would
silently drop those rows.

### `period`
`start`/`end` (local, with offset), `start_utc`/`end_utc`, `timezone`, `days`,
and `completed`.

**`completed` counts recorded work completions**: rows in `loops` that are
*named work* (`title_source = 'provided'`, non-empty, not a `Task from …` or
template shell — the same `_work_named_scope_sql` predicate `/work/overview`
and `/work/items` use), whose lifecycle state is `done`, and whose `closed_at`
falls in `[start_utc, end_utc)`. It is counted once per work item. Spans, tool
calls, nested child activity inside an item, and agent registrations are
**not** rows in that table and are never counted.

The lifecycle-state half is load-bearing. `closed_at IS NOT NULL` is a
**terminal close**, not a completion, and two kinds of terminal close are the
opposite of work getting done:

| close | written by | `cached_state` |
|---|---|---|
| operator / agent close | `close_loop`, `trovis.loop.close` | `done` |
| sweep gave up (idle past ABANDON_THRESHOLD) | `abandon_loop` | `abandoned` |
| phantom loop from straggler telemetry | `artifact_close_loop` | `abandoned` |

`loops.compute_loop_state` rules 1–2 keep both abandonment shapes at
`abandoned` through any recompute (`reason='ingestion_artifact'` maps there on
purpose), so the state is authoritative and the timestamp is not. Counting the
timestamp credits an org for work it abandoned.

The predicate is written once, as `database._WORK_COMPLETED_SQL`, and shared.
`period.abandoned` reports the terminal-but-not-completed closes in the window
so abandoned work stays visible as itself rather than being dropped or
rewritten as success. Abandoned records are never modified.

`period.exact` is False (and `qualifier` becomes `at_least`) when the scope's
membership is incomplete — see **Incompleteness** below.

`period.comparison` is the **equal-duration** window immediately preceding
(`[start - (end - start), start)`), anchored to the period start rather than to
local midnight — anchoring to midnight while the current period is mid-day
would make the two windows different lengths, and a comparison between unequal
windows is not a comparison. It returns `available: false` with
`no_recorded_work_before_previous_period` when the record holds nothing from
before that window: a zero previous period in a workspace that did not exist
yet is not a decline.

### `current_state`
Open work **right now**, split by `loops.cached_state` — the same engine state
`/work/overview` counts `needs_attention` from, not a second opinion about what
"moving" means:

| bucket | states |
|---|---|
| `waiting_on_person` | `awaiting_human` |
| `blocked` | `stalled`, `awaiting_system` |
| `moving` | everything else still open |

`moving + waiting_on_person + blocked == open`. There is **no trend** for these
and `trend_available` is permanently `false`: the record does not retain what
these values were last Tuesday, and reconstructing a stuck/waiting history from
today's numbers would be a chart of nothing.

### `completions_series`
Recorded completions per **local calendar day**, bucketed on the *completion*
timestamp (`closed_at`) — never on start time or last-touched time. A
daylight-saving day is one bucket of 23 or 25 hours. `reconciles` is checked,
not assumed: the bucket sum is compared against the independently computed
aggregate `COUNT` and a mismatch is reported.

Past `SERIES_ROW_CAP` (50,000) completions in one period the series comes back
`available: false` with `too_many_completions_to_bucket`; the aggregate total
stays exact.

### `by_job`
Completions grouped by **existing job identity** — `loops.workflow_id` and the
workflow's own name. No outcome, quality or success taxonomy is invented.
Unmatched work is reported as `unclassified_completed`, never dropped or folded
into a neighbour. The tail past `row_limit` is reported as `other_completed` so
a chart built from `rows` still adds up; `reconciles` asserts it.

### `attention`
`needs_you` — work whose latest unresolved handoff targets the **signed-in
person**. It answers to the session identity and nothing else: changing the
whose-work selection changes the work counts and leaves this one exactly where
it was. Same rule `/work/overview` keeps for `needs_you`, same bounded assignee
scan (`_loops_assigned_to`, `named_only=True`), same handoff semantics
(`loops._unresolved_handoffs`, so accepted / completed / declined handoffs
resolve exactly as everywhere else).

`needs_you` is `null` — never 0 — whenever the exact count cannot be
established:

| case | `unavailable_reason` |
|---|---|
| machine session, no person | `no_personal_identity_for_machine_session` |
| assignee scan hit its cap | `assignment_scan_truncated` |

`needs_you_at_least` carries what the (possibly capped) scan did find, as an
explicitly labeled lower bound. The distinction matters: with 501 open
handoffs where yours is the oldest, the id-DESC candidate scan fills entirely
with other people's work, and a confident "0 needs you" would be the single
worst thing this endpoint could say.

### `financial`
Present only when the **resolved seat carries the `Cost` surface atom**.
Otherwise `visible: false` with `seat_excludes_financial_surface`, or
`no_seat_for_machine_session` for an API key.

When visible, everything is **organization-wide**, and says so:

- `scope: "organization_wide"`, `attributable_to_shown_work: false`.
- `spend_usd` is the sum of cost **computed at ingest and stored on the span**
  (`insert_spans` → `_compute_cost`). Nothing is re-priced here.
- There is **no cost-per-completion**. Stored span cost hangs off the account
  and the agent, not off a work scope, so dividing org-wide spend by a narrowed
  completion count would produce a number that describes nothing.
- Missing cost is **unknown, not free**. `coverage.unpriced_token_spans` counts
  spans that carried usage but got no price.
- `coverage.ratio` is `priced_spans / (priced_spans + unpriced_token_spans)`:
  the share of **cost-bearing spans** carrying a stored price. It is *not* "we
  know 92% of the money" — the value of the unpriced spans is exactly what is
  unknown. When the denominator is zero the ratio is `null` with
  `no_cost_bearing_spans_in_period`; no percentage is invented.
- No agent or connection count is derived from financial access. Source
  freshness (`freshness.latest_telemetry_at`) is computed independently of the
  cost gate for the same reason.

### `freshness`
Newest recorded completion, newest work activity, newest telemetry, and the
first recorded work — so Home can say how current the picture is rather than
implying "live". `null` when the record holds nothing of that kind.

### `completeness`
`workspace_state` (`empty` | `populated`) distinguishes an empty workspace from
a quiet period, plus per-block completeness flags, `counts_exact`, and an
`unavailable` list of `{field, reason}` so a renderer can hide a visual without
re-inspecting every block.

### Incompleteness: lower bounds vs totals

The whose-work filter has two legs. Ownership is a plain column join and is
always complete. The waiting-on leg is a stream fold over a **capped** scan
(`_ASSIGNEE_SCAN_LIMIT`, 500 candidate loops). When that cap is hit the scope's
membership is a SUBSET of the truth, and every count built on it is a lower
bound rather than a total. The snapshot says so rather than presenting a
partial count as exact:

- `scope.membership_complete: false` + `membership_incomplete_reason`.
- `period`, `current_state`, `completions_series` and `by_job` each carry
  `exact: false` and `qualifier: "at_least"`.
- `period.comparison` becomes **unavailable** with
  `scope_membership_incomplete`. A delta between two lower bounds is not a
  lower bound on the delta — the missing rows could sit on either side.
- `completeness.unavailable` names every affected field.

**`completions_series.reconciles` is not a completeness claim.** When `exact`
is false the buckets and the aggregate are drawn from the same subset, so they
agree with each other while both under-count. That is exactly why `exact` is
reported separately.

A company-breadth seat applies no person filter at all, so its membership is
always complete regardless of the assignee cap. `attention` is separate: it is
capped independently of the scope, and reports its own truncation.

### `navigation`
`account_id`, and `carry_query` — the query params a Home link **must** carry
into a drill-in so the scope and period survive the click. A drill-in that
silently resets to "everyone, last 7 days" turns an investigation into a
different question.

`carry_query.whose` is the **effective** selector, not the raw query string: an
unreadable selector is not echoed back (it would re-resolve to `everyone` and
read as a deliberate widening), and a selector that ran is carried exactly as
it ran. It cannot widen the view in any case — the destination re-resolves it
against the same seat. `/work/items` today accepts `whose`, `person_id`,
`workflow_id` and `status`; it has no period filter yet (see gaps).

## Query sources and performance

One connection, a fixed number of bounded aggregates, no N+1:

| # | Query | Index |
|---|---|---|
| 1 | Totals (period, previous, open), history floor, freshness extremes — one pass | `idx_loops_account_title_closed` (account_id, title_source, closed_at) |
| 2 | `GROUP BY cached_state` over open named work | same prefix |
| 3 | `GROUP BY workflow_id` over the period's completions | same prefix; `workflows` PK for the join |
| 4 | The period's `closed_at` values, `LIMIT 50_001`, for the local-day fold | same prefix |
| 5 | The whose-work filter's waiting-on leg and `needs_you`, from **one** shared assignee resolution (below) | `idx_loops_account_state`, `idx_loop_events_loop_type` |
| 6 | Newest telemetry; and, when the seat allows, the period's cost aggregate | `idx_spans_account_started` (new, idempotent, both backends) |

**Assignee resolution** (`database.assigned_handoff_targets`) answers "who is
this waiting on?" once for the whole account, not once per person and once per
loop:

1. One candidate query, capped at `_ASSIGNEE_SCAN_LIMIT` (500 open
   attention-state loops that have a handoff).
2. Those candidates' **handoff events only**, fetched
   `_ASSIGNEE_EVENT_CHUNK` (200) loops at a time and folded through the same
   `loops._unresolved_handoffs` the state machine uses. Fetching only the four
   handoff types keeps the retrieved volume proportional to handoffs rather
   than to every activity event a busy loop accumulated; the fold ignores every
   other type, so the answer is identical. Only the resolved target survives
   each chunk, so memory is bounded by one chunk, not by the whole scan.
3. One identity lookup per **distinct target**, however many people are asked
   about.

The result is cached for the life of the request, so the whose-work filter and
the personal attention count share it. Measured on the reported fixture (500
candidate handoffs, two people): **1,006 statements before, 6 after**; a whole
team-scope snapshot request over that fixture stays under 40 statements. The
statement count does not grow with the number of people and grows only per
200-loop chunk with the number of candidates — see
`test_home_snapshot_integrity.py`, which counts them rather than asserting a
docstring. The 500-candidate cap remains, and is reported (see
**Incompleteness**).

- Nothing scans full trace histories, and nothing loops over open work items.
- `GET /work/board` and `GET /work/summary` are **not** called (asserted in
  `test_home_snapshot.py`).
- Statement timeout `_HOME_SNAPSHOT_TIMEOUT_MS` (8s) → 504, matching
  `/work/overview`. The statement count is small and bounded, but it is not a
  fixed number: it varies with the number of candidate chunks and distinct
  handoff targets.
- **No LLM.** Nothing on this path calls or waits on a model; the test
  booby-traps the Anthropic SDK constructors so a call would raise.
- **No cache.** The queries are cheap enough and a cache is the easiest way to
  serve a stale permission result. If one is ever added, the key must include
  account, effective visibility, viewer identity, scope, period and timezone.

Timestamp handling: `closed_at` is written at second granularity, so the
exclusive upper bounds are rounded **up** to the next whole second
(`database._home_ts(..., ceil=True)`). Without that, a period ending "now"
would silently omit work that finished in the current second. Period starts are
local midnight and already second-aligned, so the two windows never overlap.

## Permission behaviour

- Identity and seat are resolved **server-side** (`database.resolve_seat`).
- The whose-work request goes through the *same* `main._resolve_whose_work` the
  Work list uses. It intersects the request with the seat: a request can narrow
  and can never widen. A `person_id` outside the caller's reporting line is a
  403.
- Account isolation is the middleware's `request.state.account_id`; every query
  filters on it. Cross-account ids stay 404/403 per existing convention.
- Unplaced users get the existing default seat (company breadth, all surfaces) —
  seats only ever narrow what an account could already see.
- API-key sessions keep the account-wide work view they have always had, and
  get `attention` and `financial` explicitly unavailable. No personal identity
  is invented for a machine session.
- The intentional organization-wide Agents/Ask visibility policy
  (`test_surface_breadth.py`) is **untouched** by this PR.

## Audit: existing financial visibility paths

Filtering this new endpoint **does not fix the existing ones.** As of this
commit, no endpoint in `main.py` gates on seat surfaces at all — the `Cost`
atom is honoured by the client's nav (`seat.js` / `tabs.js`) and by nothing on
the server. These endpoints return spend to any authenticated caller in the
account, including one whose seat excludes `Cost`:

| Endpoint | Exposes |
|---|---|
| `GET /cost/overview` | today, MTD, budget, per-agent and per-model breakdown |
| `GET /cost/audit` | per-day per-model cost and unpriced tokens |
| `GET /dashboard/cost` | fleet cost summary + trend |
| `GET /agents/{service_name}/costs` | per-agent cost |
| `GET /agents`, `GET /agents/{service_name}/summary`, `GET /agents/{service_name}/weekly` | `estimated_cost_usd` / `cost_today` / `cost_7d` on the agent record |
| `PUT /cost/budget`, `PUT /cost/agent-budget` | read back a full `CostOverview` |

This is recorded, not fixed. A product-wide permission migration is a real
decision with real blast radius (it would change what existing dashboards
return for existing users) and does not belong in a foundation PR. The new
endpoint is correct on its own terms; the rest is a follow-up.

## How later Home UI must link

- Send `days` and `tz` from the snapshot's own `navigation.carry_query`, not
  from fresh client state — the snapshot is the authority on what period is on
  screen.
- Carry `whose` and `person_id` through every drill-in.
- Use `by_job.rows[].workflow_id` for a job drill-in, and
  `navigation.work_items_path` with `workflow_id` + `status=done` for the list
  behind a bar.
- Never re-count on the client. If a visual needs a number this snapshot does
  not carry, add it here rather than folding `/work/items` pages.

## Known gaps (what these block)

1. **No period filter on `/work/items`.** A click on a completion bucket can
   filter by job and `status=done` but cannot restrict to the bucket's dates,
   so a drill-in list will be wider than the bar it came from. Blocks an exact
   "show me these 12" affordance.
2. **Cost cannot be attributed to a work scope.** Spans carry `account_id` and
   `service_name`, not the seat or the work item's owner. Blocks any per-team
   spend visual, any cost-per-completion figure, and any "this work cost X"
   claim. Would need cost rolled up per loop at ingest.
3. **Coverage is a span share, not a money share.** Blocks "we can account for
   N% of spend"; the honest statement is the one the field already makes.
4. **No history of current state.** Blocks any stuck/waiting-over-time chart.
   Would need a periodic state snapshot table.
5. **No outcome or success signal.** `loops.closed_at` records that work
   finished, not that it worked. Blocks success rates, quality trends, and
   every "impact" or "savings" claim.
6. **Named work only.** Untitled OTel loops are excluded, matching the rest of
   Work home. An org whose agents emit no `trovis.loop.title` sees an honest
   zero here and a populated Agents tab — confusing, but not wrong.
7. **The assignee scan is still capped at 500 candidates.** Past that, scoped
   counts and personal attention are reported as incomplete rather than exact.
   Removing the cap needs the "latest unresolved handoff" fold to become a
   SQL-expressible predicate (a materialized per-loop assignee column,
   maintained at ingest) — a schema change, not a query change.
8. **`/work/items` rows still carry the locked five-value `status` enum**, so a
   terminal close that was an abandonment arrives as `status: "done"`. Its
   `whats_next` now reads "Closed — not completed" rather than "Done", and
   `status=done` filters it out, so the count, the filter and the visible label
   agree; only the wire enum value does not. Adding a sixth value touches
   `board.js`, `home.js`, `workBoard.js`, `jobDetail.js` and `loops.js` — a
   product change, deliberately not made in a data-foundation PR.
9. **`/work/overview` has no incompleteness channel.** Its response keys are a
   locked contract (`test_work_lean.py`), so a truncated assignee scan there is
   logged rather than returned. `/home/snapshot` is the surface that reports it
   structurally.
10. **Postgres is not runtime-verified.** See Testing below.

## Next PR: evidence-backed AI findings

The analysis layer that reads this snapshot must:

- Take this snapshot as **input**, not re-query. If a finding needs a number
  this endpoint does not carry, add the field here first.
- Cite the field it came from. A finding with no `{field, value, period}`
  provenance is not shippable.
- Be gated by `completeness`: never make a claim about a block whose
  `available` is false, and never treat `null` as zero.
- Never state money the viewer's seat cannot see, and never attribute
  organization-wide spend to a narrowed scope.
- Never call a recorded completion a verified outcome, a business impact, or a
  saving.
- Follow `pulse.py`'s existing pattern: entailment-checked, fails closed, and
  off the first-paint path — the snapshot must keep answering without it.

## Testing

`test_home_snapshot.py` (contract) covers company / reporting-branch /
personal / custom scopes, broadening attempts, cross-account access, attention
staying personal across scope selections, >50 items proving totals are not
preview-derived, bucket/total and job/total reconciliation, registrations and
nested activity not inflating completions, period boundaries, a DST boundary,
missing history vs a real zero, financial surface present vs absent, org-wide
cost alongside a narrower work scope, unpriced cost, an empty workspace,
malformed input, and the absence of any model call.

`test_home_snapshot_integrity.py` covers the four semantic defects this
document describes: completion vs abandonment vs ingestion-artifact vs open
work from one fixture (checking every completion-related field, plus
`/work/overview` and `/work/items` as affected consumers); the assignee cap at
501 items with the matching one oldest, for both personal attention and scoped
membership; **counted** statements proving no per-person or per-item fan-out,
including a team-scope request end to end; and scope metadata across
company-breadth-without-reports, person-is-self, personal-breadth-asking-
everyone, reporting-branch, custom levels, machine sessions and malformed
selectors.

**Backend exercised: SQLite 3.45.1 on CPython 3.11.15.** No Postgres server
was available in this environment, so Postgres behaviour is verified
*statically* by `test_work_pg_placeholders.py`, which binds the snapshot's
totals SQL the way psycopg2 would and asserts the LIKE-wildcard escaping. That
is **not** a runtime Postgres verification and should not be described as one.

The example response in this repo's PR description is generated by
`example_home_snapshot.py` from a throwaway seeded database — fixture data, not
production data. Its clock is pinned **before** anything runs (`_FrozenClock`
installed into `database`, `loops` and `home_snapshot`), so the seeding, the
lifecycle states, the period arithmetic and `generated_at` all read the same
instant and the output is byte-identical on every run. Nothing is overwritten
after querying.
