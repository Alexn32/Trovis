# AGENTS.md — Trovis

Guidance for AI coding agents (and humans) working in this repo. Pairs with
`CLAUDE.md` (product north-star) and `README.md`.

## What this is

Trovis is the **system of record for companies running AI agents**. It ingests
OpenTelemetry (OTEL) traces from any agent platform, uses Claude to describe what
each agent does in plain English, and surfaces a unified dashboard: fleet health,
cost, workflows, and conversational Q&A. Multi-tenant SaaS.

## Stack

- **Backend:** Python + FastAPI (`main.py`).
- **Database:** SQLite in dev, Postgres in prod — one dual-backend layer (`database.py`).
- **AI layer:** Claude API (`describer.py` = descriptions/workflows/dashboard insights,
  `asker.py` = fleet Q&A). Model lives in module-level `MODEL` constants.
- **Frontend:** React + Vite (`frontend/`), CSS-variable theming (light/dark).
- **Ingest:** OTLP/HTTP receiver at `POST /v1/traces`.
- **Work home (lean):** `GET /work/overview` (counts) + `GET /work/items` (paginated named rows) + `GET /workflows` (declared jobs). Three views of the same rows — By job (`WorkTab.jsx` `JobRow`: a job row shows only the runs that need a person, the rest is a state bar, a count and a fold), All open (the one `WorkTable`) and Completed (`GET /work/items?status=done` + the 7-day `GET /home/snapshot` series, fetched only while that view is open); presentation rules in `frontend/src/workPage.js`. Suggestions: `GET /work/suggestions` + approve/edit/decline. Do **not** call `GET /work/board` or `GET /work/summary` from home — those scan every open loop and starve a single Uvicorn replica. `/health` is a DB-free fastpath.
- **Home snapshot:** `GET /home/snapshot` — the authoritative, LLM-free data foundation for Home (scope, bounded period + IANA timezone, aggregate work counts, completion series, job breakdown, personal attention, seat-gated org-wide cost, completeness metadata). Orchestration in `home_snapshot.py`, SQL in `database.get_home_snapshot_rows`, contract in `HOME_SNAPSHOT.md`. It reuses the lean Work definitions — never add a second definition of "completed work": a completion is `database._WORK_COMPLETED_SQL` (`closed_at` set **and** `cached_state='done'`), because `abandon_loop` and `artifact_close_loop` also stamp `closed_at`. Assignment resolution is bounded twice (500 candidate loops, 20k handoff event rows) and identity is resolved in batches of 400; counts the snapshot cannot establish exactly come back as labeled lower bounds with `exact: false`, and a "none" it cannot assert comes back as `unknown` / `absence_established: false` — never as a confident number.
- **Home findings:** `GET /home/findings` (+ `/{id}`, PATCH for acknowledge/dismiss) — Trovis's evidence-backed investigation layer. It forms a question, RETRIEVES records that could confirm or refute it through a read-only allowlist (`investigation_tools.py`), and publishes only what survives deterministic validation (`findings.py`). Prompts and the flow live in `investigator.py` (versioned by `PROMPT_VERSION`); the durable queue is `analysis_jobs.py`. Contract: `HOME_FINDINGS.md`. **No model runs on a read path** — the read serves what is published and enqueues; a worker in `lifespan` drains the queue. It **executes nothing**: the strongest output is a `next_step` a person takes. Numeric claims must cite `snapshot:<path>` or `calc:<id>` and are checked; evidence ids the session never retrieved are fabrications and rejected; money is gated at retrieval, at validation and at serving. Abstaining is a correct outcome — but a BROKEN analysis is not an abstention: every run reports an `analysis_outcome`, DERIVED from what happened rather than asserted, and only `complete` with no coverage gap may retire (`superseded`) a finding it did not republish. Two questions are kept apart: COMPLETION (did the run answer what it raised?) sets the outcome and whether the read may say `current`; COVERAGE (could it have seen the whole slice?) decides retirement. The five `COMPLETION_GAPS` are `candidates_not_examined`, `candidate_undecided`, `candidate_uncomposed`, `wording_withheld` and `validation_rejected` — a draft the deterministic validator refuses is a FAILED REPLACEMENT, never evidence the condition ended — and any one of them makes the run `incomplete` even when other candidates published. Coverage gaps are those plus `retrieval_incomplete`, which is deliberately NOT a completion gap: a bounded search that answered its questions publishes, reads `current`, carries `qualified` confidence, and still retires nothing. Only the four `RETRYABLE_OUTCOMES` (transport/parse failures) are retried; `incomplete` is not, because the same validator would refuse the same draft. New evidence and the freshness window are the routes back to `current`. An assessment verdict of `narrow` REWRITES the finding (bounded, re-assessed, re-validated) rather than relabelling it. Coverage is server-derived and can never be upgraded by model output; it comes from the snapshot **and** `InvestigationSession.retrieval_report()`, which sees budget exhaustion, a capped query, a capped event history, incomplete assignment resolution, size trimming, a dropped result and a failed tool — `derive_coverage` fails closed, so a report that does not say it was complete is incomplete. The evidence ledger holds what was DELIVERED, contents included: `_record` only STAGES a response's rows and `_settle_delivery` promotes the ones that reached the model, so a re-fetched row that gets trimmed away leaves the delivered title, outcome and digest untouched (writing on retrieval meant the ledger held a payload nobody had been shown, and every claim, assessment and stored digest read that). A delivered update does replace it; a wholly dropped response promotes nothing; promoted payloads are rebuilt, so later mutation cannot reach them; provenance is keyed on the payload handed to `fit`, so out-of-order or overlapping responses each settle against their own rows. Retrieval alone records nothing citable — pair `run` with `fit`, or use `session.retrieve()`. Nothing is written while a model call is outstanding — the investigation stages its findings and `run_one` commits publish + retire + finish in ONE transaction fenced on `attempts = claim_token AND status='running'`, and `requeue_analysis_job` is fenced too. Freshness is reported from `analyzed_evidence_version` (what a completed run actually read, persisted on the job and re-read at execution, not at enqueue) against the observed `evidence_version`, with `newer_evidence_available` when they differ — `current` means a completed analysis read THESE records. Pending work is coalesced per AUDIENCE (`uq_analysis_jobs_pending_scope`), not per job key, so a moved bucket or period slot joins the job in flight and stamps `pending_evidence_version`; follow-up is triggered by the next read, never chained automatically.
- **Distribution:** `trovis-agents/` (pip SDK for OpenAI Agents SDK / Claude Agent SDK /
  Claude Managed Agents), `trovis-openclaw-plugin/` (TS plugin), `mcp_server.py`
  (MCP server for ChatGPT, mounted on the FastAPI app — currently unlisted in the UI).
  **Execution structure in the doors we own:** where the runtime itself knows that
  activity happened inside a run (OpenClaw's `runId` per hook, ended by `agent_end`;
  one Managed Agents `stream()`; one Claude Agent SDK `query()`), the door opens one
  `agent_run` span and starts the run's hook/event spans in its OTEL context, so ingest
  stores the real `parent_span_id` and `work_execution.py` reconstructs the tree with no
  heuristics. Nothing deeper is encoded than the runtime asserts (tool and model spans
  are siblings under the run); hooks with no run id stay roots; the root carries only
  the run id / loop key its children carry, never the one-shot title/handoff/close
  signals, so Work correlation is unchanged. The Grok Bot and ChatGPT MCP doors, the xAI
  SDK and the OpenAI adapter are deliberately untouched (see PR 214's audit).

## Repo map

| Path | Purpose |
|------|---------|
| `main.py` | FastAPI app, auth middleware, all HTTP endpoints |
| `database.py` | Dual SQLite/Postgres data layer, schema, migrations, all SQL |
| `models.py` | Pydantic request/response shapes |
| `describer.py` / `asker.py` | Claude calls (descriptions, workflows, dashboard, Ask) |
| `pricing_sync.py` | Daily model-price sync (LiteLLM list) |
| `work_evidence.py` | `GET /work/items/{id}/evidence`: provenance for one item's claims — a read model over its spans and loop events (types `execution` / `action_reported` / `external_state` / `handoff` / `completion` / `cost`; source connector via `connect_health.identify_connector`; correlation `explicit_key` / `time_adjacency` / `direct`). Persists nothing; `loop_events.span_id/trace_id` (ingest-written events only) and the provider ids on a SaaS clear are the two facts kept so it can. Not verification, not coverage. |
| `work_coverage.py` | `GET /work/items/{id}/coverage`: which dimensions of one item are observed — `execution` / `actions` / `external_outcomes` / `handoffs` / `cost`, each `observed` / `unknown` (cost also `partial` / `not_observed`, the one dimension with a known denominator: model-usage spans vs priced spans). A read model over that item's evidence; absence is `unknown`, never "nothing happened". No score, no `not_applicable`, no `verified`, not Connection Health. |
| `work_execution.py` | `GET /work/items/{id}/execution`: the **Execution Graph** — the technical execution underneath one item, a read model over its spans and loop events. Nodes typed `worker` / `model` / `tool` / `system` / `handoff` / `wait` / `completion` / `other`, each with a stated `classification_basis` (the run root the doors emit, `trovis.event.type=agent_run`, is `worker` by that explicit event type alone — never by span name); STRUCTURE from the recorded `parent_span_id` (attached only when the parent is in the run's read set; cycles broken deterministically; nothing invented from timing) kept separate from CHRONOLOGY (node ids by persisted time). Worker ≠ connector; cost/usage per PR 211 (covered = known, not priced alone; unknown = None). Persists nothing, no model, no retry or success inference, no business steps. Per item only — never Home, the Work table or a job roll-up. |
| `work_graph.py` | `GET /work/items/{id}/graph`: the **Work Graph** — the deterministic operational projection of the same run, a read model over the same rows as Execution. A record becomes a Work Step (`handoff` / `wait` / `exception` / `completed`; `progress` is defined but nothing persisted earns it yet) only from an explicit lifecycle record: `handoff_initiated` (to_human / to_agent → handoff; to_system → wait, or exception when the SaaS spine recorded `stuck`), `handoff_declined`, `stall_detected`, `loop_closed` ("Work record closed", never an outcome). `loop_opened`, `agent_run`, model calls, generic/HTTP spans, trace boundaries, tool spans and `action_reported` are NOT steps — `stripe.refunds.create` is never "Refund issued"; accept/complete/SaaS clear move possession without a "wait resolved" step. Possession is `loops.compute_loop_segments` over the same merged stream the Run detail uses — no second state machine. Every step carries `work-step:event:<id>` and its source `event_id`; `evidence_id` / `execution_node_id` (`event:<id>`) are set only when Evidence / Execution actually hold that record (a stall has an Execution node and no Evidence record → `evidence_id` null), never by naming convention. Sparse is correct; absence of a step never means nothing happened. Per item only; bounded like Execution (events read whole). |
| `connectors.py` | The **canonical connector registry**: identity (id / category / methods / availability), setup shape (`setup_type`, tile and guide labels, `variants`) and capabilities (`observes` — Coverage dimensions a connection CAN contribute, never a promise; `discovers_agents`, `supports_multiple_instances`, `management`). `connect_health` derives its id tuples from it, `asker` builds its setup roster from it, `GET /connect/connectors` serves it, and the frontend reads a **committed snapshot** — after editing, run `python3 connectors.py > frontend/src/connectors.registry.json` (`test_connectors_registry.py` fails while it is stale; `frontend/test/connectors.test.mjs` fails if `connectors.js` stops mirroring it). Never add a connector list anywhere else: `connectionsPage.SETUP_TILE_IDS`, the wizard tiles and the guide chips all derive from `setup_type` via `frontend/src/connectSetup.js`. |
| `connect_health.py` | `GET /connect/health`: normalized connection state per connector (`not_connected` / `waiting_for_data` / `connected`), a read model over `saas_connections` + `saas_events` and stored spans. Identity comes from the stamp a Trovis-owned door writes (`trovis.connector.id`, or the legacy `trovis.platform` / `trovis.sdk.platform` / OpenClaw stamps) — never from `service.name`. No `degraded`: nothing records a concrete failure yet. Not Work coverage. |
| `frontend/src/*.jsx` | UI: `App.jsx` shell, `Dashboard.jsx`, `Fleet.jsx`, `Workflows.jsx`/`WorkflowCanvas.jsx`, `AddAgent.jsx`, `Settings.jsx`, `AskVisuals.jsx`, `CostPage.jsx`. `JobDetail.jsx` is the Run (page + Home desk panel; the file name predates the Job/Run split); its page has two views under one quiet header (breadcrumb Work / job / title, the title, one recorded meta line "Run #id · Started … · elapsed so far / closed after …" from `workGraph.runTiming`, "View job") — **Activity** and **Execution** — and no third tab. **Activity** is a story column beside a record rail. The column: the **Current situation** (`workGraph.situationFor`: one statement from the lean status plus `possession.current_holder` — "Waiting for Alex", "Chief of Staff is working on this", "Needs attention", "Work record closed" — an eyebrow word from the status, at most one supporting sentence built from the LAST Work Step only when that record itself names the holder, the real Approve / Send back beside it when the work is on you, and a holder strip — current holder, since when, previously held by — from `workGraph.holderStrip`, i.e. `current_holder`, its `start`, and the preceding segment; never a holder inferred from a segment, an actor, a step or the status), the **Activity** timeline (`WorkGraphView.jsx` + pure `workGraph.js`: exactly `graph.steps`, one row each — time · "who → whom" · the endpoint's label or recorded reason; a row opens "View in Execution" / "View evidence" only when the step's own `execution_node_id` / `evidence_id` is non-null), then two closed folds, **Evidence** and **How this job ran**. The rail (stacked under the column below 1100px): **Run details** (status, job, run id, started, elapsed, the worker the runs name, the connector Evidence names as source, recorded totals), **Visibility** (coverage, unchanged meanings), **Who held the work** (`possession.segments` as history only, most recent first, with recorded lengths — no client-side "now"). Zero steps is a calm sparse state, never "no activity"; `lifecycle` is never read; no step is generated client-side; nothing in the header or rail restates the situation. **Execution** (`ExecutionView.jsx` + pure `execution.js`): a vertical tree drawn from `GET /work/items/{id}/execution` parent ids only, a right-side type-aware inspector (a Work Step's deep link selects exactly its node, or nothing when the read set lacks it), and a chronology fallback used solely when the endpoint recorded no `attached` parent among two or more nodes. The client never infers parentage, types, retries, success or cost; several roots and traces are normal. The panel variant keeps its steps, passes and runs fold and fetches neither graph. |
| `frontend/src/styles.css` | All styling + the CSS theme variables |
| `trovis-agents/`, `trovis-openclaw-plugin/` | Agent-side integrations |

## Work vocabulary (pin this before touching setup copy or ingest docs)

- **Run** = one occurrence of work — "Approve refund for order #4821". Persisted as one
  `loops` row; grouped on the wire by `trovis.loop.external_id` (falling back to
  `trovis.run.id`); **titled by `trovis.loop.title`**. `JobDetail.jsx` and `/work/runs/:id`
  render a run.
- **Job** = the recurring kind of work those runs belong to — "Process customer returns".
  Persisted as a versioned `workflows` declaration whose `match_hints` (service_name /
  agent_id / title) recognise its runs; `loops.workflow_id` is the sticky match.
  `/work/jobs/:id` renders a job. **Nothing on the wire names the job** — instrumentation,
  SDK docs and the Connect guide teach builders to title the run, and say the job is
  declared in Work.
- Wire names that predate the split keep their spelling for compatibility but are
  documented as run-level: the GPT Actions `job_title` field (alias `run_title`), the Grok
  Bot `report_job_*` MCP tools (one call = one run) and the OpenClaw plugin's
  `trovis.run.job_id` (the gateway's own id, passed through). `frontend/test/connectSetup.test.mjs`
  and `test_connect_ask.py` pin the vocabulary in the setup surfaces.

## Architectural principles

- **Cost on Work is never gated.** The Cost *surface* gates organization-wide totals only (the Cost page, Home's spend card), because a person with narrowed breadth would read a partial total as the company's. A job a person can see on Work carries its cost per run and its window cost, and a run they open from it carries its own cost, for **every** seat — a person should know what the work they are looking at costs. Never add a seat check to a cost field on Work, the job page or the run page. What Work does owe is the basis: the average is over `cost_runs` of `started_runs` (same window), "No data" when there is none, and the unpriced-usage note from `/work/items/{id}/coverage` on a run.
- **Every run belongs to a job.** Ingest matches each new run against the declared jobs' hint sets; when none claims it, `database._ensure_derived_workflow` files it under a job DERIVED from its agent's `service.name` (one per account + service, enforced by the partial unique index `idx_workflows_derived`; `workflows.derived_from` names the service, NULL means declared). A derived job is named after the agent, carries one hint (`service_name equals`) and NO expectation, so it is observed and never graded; `WorkflowSummary.derived` is what lets the UI say "not yet described". `loops.match_workflow` prefers a declaration over a derived job at equal specificity, so declaring a job takes its runs on the next pass (open runs re-match; closed runs stay frozen where they closed). Promotion is `POST /workflows/{id}/versions` with `name`: the person's description clears `derived_from` and names the job — the only path that ever changes a job's name, and 400 on a declared one. Archiving a derived job is respected: that agent's new runs stay unfiled rather than re-creating it. Telemetry is never refused for lacking a job.
- **Build for the OTEL standard, not one framework.** Anything emitting OTEL spans
  should work; don't hardcode framework-specific schemas. An "agent" is *derived* from
  telemetry (`service.name` / `trovis.agent.id`), never pre-registered.
- **SQLite now, Postgres-shaped.** ISO timestamps, explicit FKs, no SQLite-only quirks.
- **Plain-English descriptions are a first-class surface.** Treat the Claude pipeline as core.
- **Two representations of the same work, not two sources of truth.** A run has one
  persisted record (its spans and loop events) and two read models over it. The
  **Execution Graph** (`work_execution.py`) is the technical resolution — which worker ran,
  which model activity and tool calls it recorded, what called what, in what order, with
  what usage and cost, each node carrying its provenance. The **Work Graph** (future) is the
  operational resolution — what happened, who had it, where it waited, how it ended. The
  Execution Graph must preserve technical truth and provenance verbatim; the **Work Graph**
  (`work_graph.py`) is the deterministic operational projection of the same run, built ON TOP
  of the same records, never instead of them, and every Work Step keeps provenance back to
  the loop event it came from; its references into Evidence and Execution are exposed only
  when those read models actually contain that record (a `stall_detected` step has an
  Execution node and no Evidence record, so `evidence_id` is null — never a reference by
  naming convention). The Work Graph is
  intentionally sparse: a record enters it only on evidence of a meaningful change in the
  work, never on evidence that computation occurred; the absence of a step does NOT mean
  nothing happened, and unknown or incomplete information stays unknown or incomplete.
  Neither graph may invent structure, outcomes or causes the record does not hold.

## Backend conventions (important)

- **Dual backend.** Use the module helpers, never raw connections:
  - `PH` (`%s` on Postgres, `?` on SQLite) for every placeholder; `USE_POSTGRES` to branch.
  - `with _connect() as conn, _cursor(conn) as cur:` for all access (dict-like rows on both).
  - `_ns_to_iso()` / `_ts_to_str()` to normalize timestamps in responses.
- **Account scoping.** Every per-tenant query filters `account_id`; endpoints read it via
  `account_id = getattr(request.state, "account_id", None)`. Scope new queries the same way (IDOR-safe).
- **Migrations are idempotent on boot.** Add columns with `_try_add_column(cur, table, col, decl)`;
  add new tables to **both** `ddls` lists (PG + SQLite) in FK order, plus indexes in `_INDEXES`.
- **Auth.** `accounts` = org/tenant; `users` + `sessions` (opaque token, `Authorization: Bearer`);
  org `api_keys` (machine credential, `X-Trovis-Api-Key`). Agents authenticate with the API key.
- **Seats.** Everyone in an org queries the *same* Work truth; a seat decides which slice they see
  and how far each row unfolds. It resolves `scope_levels → org_roles.scope_level_id →
  org_role_members.user_id` (`database.resolve_seat`, surfaced on `GET /auth/me`). The atoms are a
  closed set — breadth `self|subtree|company`, depth `glance|technical`, surfaces `Home|Work|Fleet|
  Ask|Cost|Connect|Org`; a custom scope level composes them and can never add an axis. **Every
  preset carries Fleet and Connect and ships `technical`** — a seat narrows whose work you see, it
  never takes the product away from the person who bought it, and depth is a reader's preference
  rather than a rank. Changing `SCOPE_LEVEL_PRESETS` fixes nothing on its own: existing orgs move
  via `_migrate_scope_level_presets`, which only touches a row still matching a legacy shape
  exactly. The `Fleet` atom keeps its name in the DB; the nav label reads **Agents**. Filter lists
  with `visible_user_ids_for_breadth` (**`None` = company-wide, skip the filter** — not "empty").
  `/work/items` and `/work/overview` take `whose` (`everyone|me|team|person` + `person_id`);
  `main._resolve_whose_work` INTERSECTS the request with the seat, so the query string can only
  ever narrow. Attribution is two things (`database._work_person_filter`): work run by an agent
  you own, plus work waiting on you. The ownership leg is SQL so it filters BEFORE the cursor;
  the waiting-on leg is a bounded fold injected as an id list. **`needs_you` is never narrowed** —
  that count is the desk, and the desk answers to the session identity, not to a Whose-work choice.
  **Breadth narrows Work and nothing else** — decided, not overlooked, and pinned by
  `test_surface_breadth.py`. Agents is shared infrastructure (health and drift are org facts, and
  a narrowed roster would show an empty Agents tab to anyone who owns nothing); Cost is gated by
  the *surface atom* instead, so only Exec/VP/Manager see it at all rather than everyone seeing a
  partial figure that reads like the company's; Ask answers from the same telemetry the roster
  shows, so it follows the roster. Don't narrow one of the three on its own.
  Chart edits go through `can_edit_chart`: Org builder anywhere, everyone else strictly *below*
  their own role. Org builder is a separate ladder from view breadth — a company-breadth Exec is
  not a builder. Enforce all of it server-side; the client renders the seat, it never asserts one.
- **Org API.** `GET /org/chart` (already filtered to what the caller may see — never a full chart
  with a client-side mask), roles CRUD at `/org/roles`, people at `/org/roles/{id}/members`,
  `/org/scope-levels`, `PUT /org/members/{id}/org-builder`, `POST /org/graduate` (Path A→B).
  Two distinct rungs: `can_edit_chart(role)` for changing an existing box (strictly *below* you),
  `can_add_child_role(parent)` for hanging a new one (your own box or below). Invites carry
  `role_id` and are minted under the same ladder; accept seats the user in the same transaction
  that creates them. Cross-account ids are **404, never 403** — a 403 confirms the id exists.
- **Cost is computed at ingest** (`insert_spans` → `_compute_cost` via the pricing table) and stored
  on the span; aggregates sum the stored value. Re-pricing history needs an explicit recompute.

## Frontend conventions

- **Theme via CSS variables only** — no hardcoded hex in components. Update values in `:root`
  (dark) and `:root[data-theme="light"]`; pages inherit automatically. Inter is scoped to the
  `.dash` / `.wf2` wrappers; the app otherwise uses DM Sans.
- **Phones.** Three breakpoints, each with its own reason, and they are deliberately not
  the same number. **720px — the shell**: the header keeps the logo and three icon-only
  actions, and the nav leaves the header to become a fixed bottom bar (same `<nav
  className="tabs">`, same tablist roles, same seat-driven list — only `position` changes;
  never add a second mobile nav). **860px — the Work board**: four state columns stop being
  readable well before the header stops fitting, so `.jb-rowgrid` and `.jb-flat .jb-grid`
  collapse to one column and each `.jb-cell` names itself from `data-col` (sourced from
  `COLUMNS`, not a second copy of the strings); empty cells are dropped, reversing the wide
  board's "empty cells keep their height" rule because stacked there is no row shape to
  read. **`(pointer: coarse)` — tap size**: how big a control must be is about what is
  pointing at it, not how wide the screen is; an iPad in portrait is 768px and still a
  thumb. Fields go to 16px there too — under 16px Safari zooms on focus and never zooms
  back. Inline text buttons inside a sentence ("0 waiting") stay small on purpose.
  Two traps worth knowing: **`backdrop-filter` makes an element a containing block for its
  `position: fixed` children** — it is on `.app-header::before`, never on `.app-header`, or
  the bottom nav pins to the bottom of the header; and **a bare `1fr` track is
  `minmax(auto, 1fr)`**, so one card with a wide min-content stretches the grid and the
  page (use `minmax(0, 1fr)`). Anything a media query must change cannot be an inline
  style — inline wins over every rule — which is why `AgentDetail.jsx` hands its stat row
  and chips a class. Verify layout by driving the app at 360/390/430/768/1024/1440 and
  measuring `scrollWidth` vs `clientWidth`; `test/responsive.test.mjs` pins only the
  decisions a media query cannot express.
- `api.js` `request()` attaches `Authorization: Bearer` and/or `X-Trovis-Api-Key` headers
  (never cookies). It returns parsed JSON.
- View switching is `useState` tab state in `App.jsx` (no router); overlays via `setOverlay`.
- **Seat in the chrome.** Nav comes from `visibleTabs(seatOf(me).surfaces)` (`tabs.js` + `seat.js`),
  never from `account_type`. `seat.js` is the only place the client reads a seat, and its rule is
  **widen on doubt**: a missing, empty or malformed seat falls back to every surface. A seat
  arrives late, can fail, and is absent for API-key sessions — failing closed would blank a working
  product and buy nothing, because the server re-checks every request anyway.
- **Org is the one place people live.** `Org.jsx` (chart, roles, people, invites, scope-on-role,
  Path A→B graduation); `org.js` holds its pure logic. The chart is a real top-down hierarchy —
  nested `<ul>`/`<li>` with connector pseudo-elements (`.oc-*`), no layout library. Children fan
  out horizontally **unless every one of them is a leaf**, in which case they stack down an elbow;
  without that a manager with twelve reports is wider than any screen. It takes the full page
  width and scrolls sideways; the role detail sits under it. Affordances come from the server's
  per-role `can_edit` / `can_add_child` — the client never derives permissions. Settings shows
  members read-only and links to Org; there is deliberately no second invite form.
- **Agent ownership is a `users` assignment.** `agent_owners.user_id`, and the label beside the
  name is their **chart role title** (`org_roles.title`) — never `users.role`, which is an account
  permission. Every owner read goes through the one shared resolver (`_OWNER_JOIN_SQL` /
  `_OWNER_COLS_SQL`), which prefers `users` and falls back to `team_members` for legacy rows;
  don't hand-roll that join again. The roster **names the gap**: agents are derived from
  telemetry, so they arrive owned by nobody, and Whose work's team view stays thin until
  someone assigns them. `unowned.js` decides what counts — per **sub-agent** (the unit an
  owner is assigned to, so a gateway with five gaps reads as five), and **locked agents are
  excluded** because their card won't open, so the count would never reach zero. The toggle
  filters the grid and hides itself once the gap is closed; it never touches the summary
  counts, which describe the fleet.
- **Naming a person with no login.** `_resolve_human_name` resolves an email to: `users` → a
  **named pending invite** (`invites.display_name`, account-scoped, read regardless of
  `accepted_at`/`expires_at` — the token expires, the name is just a record) → the legacy
  `team_members` directory → **nameless**. That last step is load-bearing: echoing a raw address
  back as a name would render any address an agent emits, including another org's, as a
  colleague. `POST /team` is **410** — invite people with `POST /org/invites {email, name,
  role_id}`. Existing `team_members` rows still resolve and need no backfill; minting invite
  tokens for them would create redeemable links nobody asked for.

## Running locally

```bash
# Backend (isolated SQLite, no network price sync)
# The SQLite path env var is TROVIS_DB_PATH (legacy: OVERSEE_DB_PATH) — see
# database.py. DATABASE_PATH is read by nothing and silently gives you a
# trovis.db in the repo root instead.
TROVIS_DB_PATH=/tmp/dev.db TROVIS_DISABLE_PRICING_SYNC=1 \
  uvicorn main:app --port 8099 --reload

# Frontend (point it at the backend)
echo 'VITE_API_URL=http://localhost:8099' > frontend/.env.local
cd frontend && npm run dev
```

Postgres is enabled by setting `DATABASE_URL`. `TROVIS_MONTHLY_BUDGET` sets the default
cost budget; `TROVIS_CORS_ORIGINS` can lock CORS down from `*`.

## Testing

- **Backend:** isolated SQLite via `DATABASE_PATH` + `TROVIS_DISABLE_PRICING_SYNC=1` on a
  throwaway DB; FastAPI `TestClient`. Stub Claude by monkeypatching
  `describer.anthropic.Anthropic` / `asker.anthropic.Anthropic` (or the individual functions)
  so tests never hit the network. Seed data with `database.insert_spans(parsed_spans, account_id=...)`.
- **Frontend:** `cd frontend && npm run build` must pass; do a browser smoke for visual changes.
- `insert_spans` expects **already-parsed** span dicts (`trace_id`, `service_name`,
  `start_time_unix` ns, `attributes` dict, …) — not raw OTLP. The OTLP→parsed step lives in the
  `/v1/traces` handler.

## Shipping

Work on a branch, then `gh pr create` and squash-merge — don't push directly to `main`.
Don't commit secrets or the gitignored demo artifacts (`frontend/.env.local`,
`.claude/launch.json`).
