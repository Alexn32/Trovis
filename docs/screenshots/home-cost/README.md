# Home cost card — fixture screenshots

**These are FIXTURE DATA, not a real account.** They were captured headlessly
(Chromium via Playwright, `deviceScaleFactor: 2`) against the real `CostCard`
and `DailySpendChart` components fed hand-written props, so the numbers are
invented and the shapes are chosen to show each state clearly.

| file | state |
|---|---|
| `normal-{dark,light}.png` | fully priced spend, budget under |
| `unknown-days-{dark,light}.png` | a day whose calls carry no stored price — the line breaks, a hatched `?` band marks it, and nothing is drawn at zero |
| `partial-{dark,light}.png` | partial pricing coverage; the partly priced day keeps its recorded value, ringed, as a floor |
| `month-incomplete-{dark,light}.png` | a **fully priced week inside a partly unpriced month** — the period carries no caveat, the month carries its own |
| `budget-over-{dark,light}.png` | month-to-date over the monthly budget |
| `narrow-{dark,light}.png`, `narrow-unknown-dark.png` | 390px viewport |

Re-capture by rendering `CostCard` with fixture props and screenshotting
`.hv-cost`; there is no committed harness, because a screenshot rig that rots
is worse than one you rebuild in five minutes.
