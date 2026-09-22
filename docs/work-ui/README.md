# Work UI — verification screenshots

**These are TEST FIXTURES, not live customer data.** The account, the jobs,
the runs and the people were written by a seeding script against a throwaway
SQLite database, purely to photograph the page in states that are otherwise
hard to reach on demand. Every completion in the fixture landed on the day it
was seeded, which is why the Completed chart shows one bar.

Every run belongs to a job: the `ops-bot` row on the board is a derived job, not an
"Unmatched" bucket.

Captured against the real backend (`uvicorn main:app`) and the real dev
server, Chromium at 1280×900 (390×900 for the narrow shot).

| file | state |
|---|---|
| `1-work-by-job-dark.png` | the landing: situation strip, then one row per declared job, dark theme |
| `2-work-by-job-light.png` | the same, light theme |
| `3-work-all-open.png` | All open: one urgency-sorted table of the same rows |
| `4-work-completed.png` | Completed: recorded completions charted from `/home/snapshot`, by job, then the closed runs |
| `5-work-narrow.png` | 390px wide — tiles two-by-two, job head stacked |
| `7-work-job-list.png` | By job in List density: one line per job — name, verdict, open, last run, runs per day, cost per run |
| `8-job-page.png` | the job page, in the agent detail page's language: name and verdict pill, the window strip (last run, runs per day, typical time to finish, cost per run — each with its basis), runs as a feed (recent few → all loaded → next page), **How this job runs** — the declared steps drawn as boxes with today's open runs placed on them and Done at the end — then Expectations (only the declared targets), settings and history, archive |
| `6-derived-job.png` | the job page of a DERIVED job: `ops-bot` was never declared, so Trovis filed its runs under a job named after it, tagged "not yet described", with the Describe door that promotes it; with one declared step there is no drawing, so the card says every run stayed with `ops-bot` |

The page reads `/work/overview`, `/work/items`, `/workflows` and
`/work/suggestions`; Completed adds `/work/items?status=done` and
`/home/snapshot`, fetched only while that view is open. It never calls
`/work/board` or `/work/summary`.
