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
- **Work home (lean):** `GET /work/overview` (counts) + `GET /work/items` (paginated named rows). Suggestions: `GET /work/suggestions` + approve/edit/decline. Do **not** call `GET /work/board` or `GET /work/summary` from home — those scan every open loop and starve a single Uvicorn replica. `/health` is a DB-free fastpath.
- **Home snapshot:** `GET /home/snapshot` — the authoritative, LLM-free data foundation for Home (scope, bounded period + IANA timezone, aggregate work counts, completion series, job breakdown, personal attention, seat-gated org-wide cost, completeness metadata). Orchestration in `home_snapshot.py`, SQL in `database.get_home_snapshot_rows`, contract in `HOME_SNAPSHOT.md`. It reuses the lean Work definitions — never add a second definition of "completed work": a completion is `database._WORK_COMPLETED_SQL` (`closed_at` set **and** `cached_state='done'`), because `abandon_loop` and `artifact_close_loop` also stamp `closed_at`. Assignment resolution is bounded twice (500 candidate loops, 20k handoff event rows) and identity is resolved in batches of 400; counts the snapshot cannot establish exactly come back as labeled lower bounds with `exact: false`, and a "none" it cannot assert comes back as `unknown` / `absence_established: false` — never as a confident number.
- **Home findings:** `GET /home/findings` (+ `/{id}`, PATCH for acknowledge/dismiss) — Trovis's evidence-backed investigation layer. It forms a question, RETRIEVES records that could confirm or refute it through a read-only allowlist (`investigation_tools.py`), and publishes only what survives deterministic validation (`findings.py`). Prompts and the flow live in `investigator.py` (versioned by `PROMPT_VERSION`); the durable queue is `analysis_jobs.py`. Contract: `HOME_FINDINGS.md`. **No model runs on a read path** — the read serves what is published and enqueues; a worker in `lifespan` drains the queue. It **executes nothing**: the strongest output is a `next_step` a person takes. Numeric claims must cite `snapshot:<path>` or `calc:<id>` and are checked; evidence ids the session never retrieved are fabrications and rejected; money is gated at retrieval, at validation and at serving. Abstaining is a correct outcome.
- **Distribution:** `trovis-agents/` (pip SDK for OpenAI Agents SDK / Claude Agent SDK /
  Claude Managed Agents), `trovis-openclaw-plugin/` (TS plugin), `mcp_server.py`
  (MCP server for ChatGPT, mounted on the FastAPI app — currently unlisted in the UI).

## Repo map

| Path | Purpose |
|------|---------|
| `main.py` | FastAPI app, auth middleware, all HTTP endpoints |
| `database.py` | Dual SQLite/Postgres data layer, schema, migrations, all SQL |
| `models.py` | Pydantic request/response shapes |
| `describer.py` / `asker.py` | Claude calls (descriptions, workflows, dashboard, Ask) |
| `pricing_sync.py` | Daily model-price sync (LiteLLM list) |
| `frontend/src/*.jsx` | UI: `App.jsx` shell, `Dashboard.jsx`, `Fleet.jsx`, `Workflows.jsx`/`WorkflowCanvas.jsx`, `AddAgent.jsx`, `Settings.jsx`, `AskVisuals.jsx`, `CostPage.jsx` |
| `frontend/src/styles.css` | All styling + the CSS theme variables |
| `trovis-agents/`, `trovis-openclaw-plugin/` | Agent-side integrations |

## Architectural principles

- **Build for the OTEL standard, not one framework.** Anything emitting OTEL spans
  should work; don't hardcode framework-specific schemas. An "agent" is *derived* from
  telemetry (`service.name` / `trovis.agent.id`), never pre-registered.
- **SQLite now, Postgres-shaped.** ISO timestamps, explicit FKs, no SQLite-only quirks.
- **Plain-English descriptions are a first-class surface.** Treat the Claude pipeline as core.

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
