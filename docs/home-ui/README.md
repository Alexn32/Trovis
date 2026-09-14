# Home UI — verification screenshots

**These are TEST FIXTURES, not live customer data.** The account, the work
items, the job names and all four findings were written by a seeding script
against a throwaway SQLite database; the findings in particular are
hand-written rows inserted directly into the `findings` table, **not output
from a live model**. They exist to photograph the UI in states that are
otherwise hard to reach on demand.

Captured against the real backend (`uvicorn main:app`) and the real dev
server, Chromium at 1280×900 (400×900 for the narrow shot).

| file | state |
|---|---|
| `1-home-dark.png` | populated Home, dark theme |
| `2-home-light.png` | the same, light theme |
| `3-home-narrow.png` | 400px wide |
| `4-empty-workspace.png` | an account with no recorded work at all |
| `5-restricted-no-cost.png` | a seat without the Cost surface — no financial content anywhere |
| `6-finding-detail.png` | the finding panel: evidence, stale evidence, uncertainty, navigation |
| `7-incomplete-analysis.png` | `analysis.state: incomplete` with earlier findings retained |

`7-incomplete-analysis.png` is captured with the `/home/findings` response
rewritten to the `incomplete` lifecycle state the backend really produces
(see `HOME_FINDINGS.md`) — the UI is genuine, the response was forced.
