# Home v1 — integration verification

**FIXTURE DATA on a LOCAL app. This is not a production smoke test.**

One seeded account (`Demo Co`) in a throwaway SQLite database, driven through
a real browser (Chromium via Playwright) against a real backend
(`uvicorn main:app`) and the real production frontend build. The findings on
screen were published through the actual investigation path — a `/home/findings`
read enqueued a job, `analysis_jobs.drain()` claimed and ran it — with the
provider **scripted**, so no live model and no network call was involved. The
numbers are therefore real outputs of the real code over invented records.

Nothing here was verified against production: no production data, no
production deployment, and no live model.

The app under test was `main` **plus the merged-but-not-on-`main` commits from
#201**, because that is the integrated Home the work assumes. See the PR body.

| file | what it shows |
|---|---|
| `01-home-dark.png` | the whole page: Work scope + Period controls, work totals, completion chart, by-job breakdown with unclassified work visible, Right-now counts, the published finding, the cost card |
| `05-home-light.png` | the same in light theme |
| `02-cost-card-dark.png`, `06-cost-card-light.png` | the cost card close up — the Sep 9 **unknown day** drawn as a hatched gap with `?` and the line broken across it, "1 day has no priced calls — cost unknown, not zero", "recorded month to date", "3 calls this month carry no stored price", "Some calls are unpriced · 73% priced" |
| `03-finding-detail-dark.png` | the finding panel: evidence, uncertainty, and the actions (Open this work item, Ask about this, Mark seen, Dismiss) |
| `12-after-acknowledge-dark.png` | the same finding after **Mark seen** — it carries an `Acknowledged` chip and survives a reload |
| `07-home-ic-no-cost-dark.png` | a reader **without the Cost surface**: no cost section at all, the "record is complete for this scope" empty state, and the queued-investigation note |
| `08-home-narrow-dark.png`, `09-cost-card-narrow-dark.png` | 390px |

Re-capture by seeding a fixture account, serving the built frontend against a
local backend, and driving it with Playwright. There is no committed harness:
a screenshot rig that rots is worse than one rebuilt in ten minutes.
