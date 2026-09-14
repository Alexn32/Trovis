# Home cost card — fixture screenshots

**These are FIXTURE DATA, not a real account.** They were captured headlessly
(Chromium via Playwright) against the real `CostCard` and `DailySpendChart`
components fed hand-written props, so the numbers are invented and the shapes
are chosen to show each state clearly.

| file | state |
|---|---|
| `normal-{dark,light}.png` | fully priced spend, budget under |
| `partial-{dark,light}.png` | partial pricing coverage — "Some calls are unpriced" |
| `budget-over-{dark,light}.png` | month-to-date over the monthly budget |
| `narrow-{dark,light}.png` | 390px viewport |

Re-capture by rendering `CostCard` with fixture props and screenshotting
`.hv-cost`; there is no committed harness, because a screenshot rig that rots
is worse than one you rebuild in five minutes.
