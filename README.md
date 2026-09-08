# Trovis — The Agent Management System

The system of record for companies running AI agents. Ingests OpenTelemetry traces from any agent platform and gives operators a unified view of their AI workforce.

## Local development

No Postgres needed locally — runs on SQLite by default. Just:

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...   # required for /describe; everything else works without it
uvicorn main:app --reload --port 8080
```

The server creates `trovis.db` (SQLite) on first run. To use Postgres locally instead, export `DATABASE_URL=postgresql://...` before starting — the storage layer flips automatically.

## Production deployment (Railway)

1. **Install the Railway CLI:**
   ```bash
   npm install -g @railway/cli
   ```
2. **Log in:**
   ```bash
   railway login
   ```
3. **Initialize a project** from this repo:
   ```bash
   railway init
   ```
4. **Add a Postgres plugin** — this also sets `DATABASE_URL` automatically:
   ```bash
   railway add --plugin postgresql
   ```
5. **Set your Anthropic API key:**
   ```bash
   railway variables set ANTHROPIC_API_KEY=sk-ant-...
   ```
6. **Deploy:**
   ```bash
   railway up
   ```
7. **Get the public URL:**
   ```bash
   railway domain
   ```

`DATABASE_URL` is wired into the runtime by Railway when the Postgres plugin is added — no manual config. The build is driven by [`railway.toml`](railway.toml) and [`runtime.txt`](runtime.txt); the dyno entry point is [`Procfile`](Procfile).

## Endpoints

| Method | Path                                 | Description                                                                       |
| ------ | ------------------------------------ | --------------------------------------------------------------------------------- |
| GET    | `/health`                            | Liveness probe. No DB. Never waits on `/work/*`. Returns `{"status": "ok", "version": "..."}`. |
| GET    | `/work/overview`                     | Lean Work home counts: `needs_you`, `needs_attention`, `open`, `completed_week`. Named work only — not `/work/board`. |
| GET    | `/work/items`                        | Paginated named items (`?cursor=&limit=`). Rows: `id`, `title`, `status`, `holder`, `whats_next`, `updated_at`. |
| GET    | `/work/suggestions`                  | Pending suggestions `{ id, title, why, source?, draft_holder? }`. Empty until real rows exist. |
| PATCH  | `/work/suggestions/{id}`             | Edit a pending suggestion in place. Session auth. |
| POST   | `/work/suggestions/{id}/approve`     | Create a named work item (appears in `/work/items`). Optional body = edit-then-approve. Garbage titles 400. |
| POST   | `/work/suggestions/{id}/decline`     | Remove from the strip. No work item (no ghost row). 204. |
| GET    | `/work/items/{id}`                   | Named item + v1.1 spine: `whats_happening`, `process`, `timeline`, `provenance`. |
| GET    | `/work/summary`                      | **Legacy / fat.** Loop-scans the full board. Do not call from home. |
| GET    | `/work/board`                        | **Legacy / fat.** Whole board dump. Do not poll from home. |
| POST   | `/v1/traces`                         | OTLP/JSON trace ingest. Accepts the standard OTEL export body.                    |
| GET    | `/agents`                            | List every agent that has reported telemetry, with their latest description.      |
| GET    | `/agents/{service_name}/summary`     | Aggregate stats for one agent. 404 if unknown.                                    |
| GET    | `/agents/{service_name}/spans`       | Recent spans for one agent. `?limit=N` (1–200, default 50).                       |
| POST   | `/agents/{service_name}/describe`    | Ask Claude to generate a fresh plain-English description. 503 if API key missing. |
| GET    | `/agents/{service_name}/description` | Most recent saved description for the agent. 404 if none yet.                     |

## Sending a test trace

Most agent SDKs ship with an OTLP/HTTP exporter — point it at `http://localhost:8080/v1/traces` and you're done. To smoke-test the endpoint by hand:

```bash
curl -X POST http://localhost:8080/v1/traces \
  -H "Content-Type: application/json" \
  -d '{
    "resourceSpans": [{
      "resource": {
        "attributes": [
          {"key": "service.name", "value": {"stringValue": "demo-agent"}}
        ]
      },
      "scopeSpans": [{
        "spans": [{
          "traceId": "5b8aa5a2d2c872e8321cf37308d69df2",
          "spanId": "051581bf3cb55c13",
          "name": "summarize_document",
          "kind": 1,
          "startTimeUnixNano": "1700000000000000000",
          "endTimeUnixNano":   "1700000001500000000",
          "status": {"code": 1},
          "attributes": [
            {"key": "model", "value": {"stringValue": "claude-opus-4-7"}},
            {"key": "input_tokens", "value": {"intValue": "1342"}}
          ]
        }]
      }]
    }]
  }'
```

Then:

```bash
curl http://localhost:8080/agents
curl http://localhost:8080/agents/demo-agent/summary
curl http://localhost:8080/agents/demo-agent/spans
```

## What's in the box (v1)

- FastAPI server (`main.py`) — routing + OTLP/JSON parsing
- SQLite storage (`database.py`) — single `spans` table, Postgres-shaped schema
- Pydantic response models (`models.py`)

No auth, no rate limiting, no config. This is the demo foundation; those land later.
