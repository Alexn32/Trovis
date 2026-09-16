# Home v1 — verification pass on `main`

**FIXTURE DATA on a LOCAL app. This is not a production smoke test.**

Captured against `main` at `dfdef0e` (which carries #195, #202 and #203), in a
throwaway SQLite database seeded with invented records, driven through a real
browser (Chromium via Playwright) against a real backend (`uvicorn main:app`)
and the real production frontend build. The findings on screen were published
through the actual investigation path — a `/home/findings` read enqueued a job,
the in-process worker claimed and ran it — with the provider **scripted**
(`eval_stub_model`), so no live model, no network call and no credential was
involved.

**Nothing here was verified against production.** The production hosts
(`web-production-e6bc4.up.railway.app`, `oversee-pi.vercel.app`) are outside
this session's network policy — every request is refused at the proxy with a
403 — so the deployed commit, the deployed Home, real findings and real
spending are all unverified. See the closeout report.

The two seats shown are fixture users of one fixture org (`Demo Co`):

* **Cleo Demo** — `Exec` scope level, so her seat carries the `Cost` surface.
* **Ira Analyst** — `IC` scope level, `self` breadth, **no `Cost` surface**.

| file | what it shows |
|---|---|
| `01-home-dark.png` | the whole page for an exec seat: Work scope + Period selects, work totals, completion chart, by-job breakdown, right-now counts, personal attention, the published finding, and the cost card |
| `05-home-light.png` | the same in light theme |
| `03-finding-detail-dark.png` | the finding panel: what it claims, its evidence, its confidence, and the suggested next step |
| `06-home-narrow-dark.png` | 390px — no sideways scroll, and the tab bar fits |
| `07-home-ic-no-cost-dark.png` | a reader **without** the `Cost` surface: no money anywhere on Home, the "record is complete for this scope" empty state, and the honest `incomplete` investigation notice |
| `09-ic-agents-cost-leak.png` | **a defect, not a feature.** The same seat on the **Agents** page, which still shows `$0.13 this week`. Home withholds money correctly; Agents and Work do not. Reported separately and not fixed in this pass. |

The finding's wording is the **scripted stub's**, which is deliberately naive
and says nothing about model quality. Re-capture by seeding a fixture account,
serving the built frontend against a local backend, and driving it with
Playwright; there is no committed harness.
