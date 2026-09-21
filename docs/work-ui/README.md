# Work UI — verification screenshots

**These are TEST FIXTURES, not live customer data.** The account, the jobs,
the runs and the people were written by a seeding script against a throwaway
SQLite database, purely to photograph the page in states that are otherwise
hard to reach on demand. Every completion in the fixture landed on the day it
was seeded, which is why the Completed chart shows one bar.

Captured against the real backend (`uvicorn main:app`) and the real dev
server, Chromium at 1280×900 (390×900 for the narrow shot).

| file | state |
|---|---|
| `1-work-by-job-dark.png` | the landing: situation strip, then one row per declared job, dark theme |
| `2-work-by-job-light.png` | the same, light theme |
| `3-work-all-open.png` | All open: one urgency-sorted table of the same rows |
| `4-work-completed.png` | Completed: recorded completions charted from `/home/snapshot`, by job, then the closed runs |
| `5-work-narrow.png` | 390px wide — tiles two-by-two, job head stacked |

The page reads `/work/overview`, `/work/items`, `/workflows` and
`/work/suggestions`; Completed adds `/work/items?status=done` and
`/home/snapshot`, fetched only while that view is open. It never calls
`/work/board` or `/work/summary`.
