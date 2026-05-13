# Oversee — Frontend

Single-page React dashboard for Oversee.

## Install

```bash
cd frontend
npm install
```

## Run (dev server)

```bash
npm run dev
```

Opens at http://localhost:5173. The dashboard expects the Oversee API at `http://localhost:8080`. To point at a different host:

```bash
VITE_API_URL=http://my-server:8080 npm run dev
```

If the backend has `OVERSEE_INGEST_KEY` set (production), the dashboard needs to send the matching key on every request. Provide it as a build-time env var:

```bash
VITE_API_URL=https://your-backend \
VITE_OVERSEE_API_KEY=your_key \
  npm run dev
```

Both vars are baked into the bundle at build time, so on Vercel they have to be set in **Settings → Environment Variables** before the build runs.

## Build for production

```bash
npm run build
```

Outputs to `dist/`. Serve with any static file host, or preview locally:

```bash
npm run preview
```

## Views

- **Agent Registry** (default) — one card per agent, with the Claude-generated description, key stats, and a status dot indicating freshness. Click an agent to drill in.
- **Agent Detail** — full description, stats, and a table of the 50 most recent spans. Click a span row to expand its full attributes JSON.
