// The app on a phone.
//
// Until this ship the app had no mobile layout at all. The header assumed a
// desktop row — logo, a four-tab nav, a theme toggle, "+ Add Agent" and a
// named account pill — which needs about 640px. At 390 the pieces sat on top
// of each other AND pushed the document past the viewport, so every page in
// the product scrolled sideways.
//
// Most of the fix is CSS, which unit tests cannot execute: a media query only
// means something in a browser at a width, so the layout itself was verified
// by driving the real app at 360 / 390 / 430 / 768 / 1024 / 1440 and measuring
// document.scrollWidth against clientWidth on every surface.
//
// What IS worth pinning here is the handful of decisions that are invisible in
// a diff and silently undo the whole thing when reverted. Each test below
// exists because breaking it produces no error, no failing build, and a broken
// phone layout that nobody notices until a customer opens the app on a train.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = (f) => readFileSync(new URL('../src/' + f, import.meta.url), 'utf8')
const css = read('styles.css')
const app = read('App.jsx')
const workTab = read('WorkTab.jsx')
const agentDetail = read('AgentDetail.jsx')

// The block a rule lives in, so a test can assert "…and it is inside a media
// query", not merely "…the text appears somewhere in 8000 lines".
// Matches any @media whose prelude names this max-width, so a block that also
// lists another condition — `(max-width: 720px), (pointer: coarse)` — still
// counts as guarded by the width.
function inMediaQuery(source, selector, maxWidth) {
  const re = new RegExp(`@media[^{]*max-width:\\s*${maxWidth}px[^{]*\\{`, 'g')
  let m
  while ((m = re.exec(source))) {
    // Walk braces from the opening one to find this block's extent.
    let depth = 1
    let i = m.index + m[0].length
    while (i < source.length && depth > 0) {
      if (source[i] === '{') depth++
      else if (source[i] === '}') depth--
      i++
    }
    if (source.slice(m.index, i).includes(selector)) return true
  }
  return false
}

// ---------------------------------------------------------------------------
// The containing-block trap
// ---------------------------------------------------------------------------

test('the app header does not create a containing block', () => {
  // THE subtle one. `backdrop-filter` makes an element a containing block for
  // its `position: fixed` descendants. The nav bar lives inside the header in
  // the DOM, so with the filter on .app-header itself, `bottom: 0` pinned the
  // nav to the bottom of the HEADER — a 55px strip under the logo — instead of
  // the bottom of the screen. It looked like a broken nav; it was a CSS
  // containing block. Putting the filter back on .app-header re-breaks it, and
  // nothing else will complain.
  const rule = css.match(/\.app-header \{[^}]*\}/)
  assert.ok(rule, '.app-header rule not found')
  assert.ok(
    !/backdrop-filter/.test(rule[0]),
    '.app-header must not carry backdrop-filter — it traps the fixed nav bar. ' +
      'The frosted glass belongs on .app-header::before.',
  )
  assert.match(css, /\.app-header::before \{[^}]*backdrop-filter/)
})

test('the frosted glass survived the move', () => {
  // The filter moved for a layout reason, not a visual one: the header must
  // still look the same on desktop.
  const before = css.match(/\.app-header::before \{[^}]*\}/)[0]
  assert.match(before, /background: var\(--nav-bg\)/)
  assert.match(before, /blur\(16px\)/)
  assert.match(before, /z-index: -1/)
})

// ---------------------------------------------------------------------------
// One nav, moved — not a second nav
// ---------------------------------------------------------------------------

test('the phone nav is the same nav, repositioned', () => {
  // The tabs are seat-driven (visibleTabs), routed, and carry tablist roles.
  // A second mobile-only nav would be a second copy of all of that, and the
  // two would drift. There is exactly one <nav className="tabs"> and the
  // phone layout only changes where it sits.
  assert.equal(app.match(/className="tabs"/g).length, 1)
  assert.match(app, /role="tablist"/)
  assert.ok(inMediaQuery(css, '.tabs {', 720), '.tabs must be repositioned under the 720px query')
  const block = css.slice(css.indexOf('@media (max-width: 720px) {\n  /* ---- header ---- */'))
  assert.match(block, /position: fixed/)
})

test('content is padded clear of the bottom bar', () => {
  // A fixed bar covers the end of every page unless the scroll container
  // reserves its height. The last row of the roster sitting under the nav is
  // the classic version of this bug.
  assert.ok(inMediaQuery(css, '.app-main {', 720))
  assert.match(css, /padding-bottom: calc\(66px \+ env\(safe-area-inset-bottom/)
})

test('the bar clears the iOS home indicator', () => {
  // env() resolves to 0 everywhere it does not apply, so this costs nothing
  // off-iPhone and is unfixable-looking on one if omitted.
  const uses = css.match(/env\(safe-area-inset-bottom/g) || []
  assert.ok(uses.length >= 3, `expected the safe-area inset on the bar, the content padding and the pill; found ${uses.length}`)
})

test('Ask moves out of the middle of the page', () => {
  // The pill is centred at the bottom on desktop. On a phone that is directly
  // on top of the content it is meant to help with, and directly on top of the
  // nav bar. It goes to the corner, above the bar.
  assert.ok(inMediaQuery(css, '.dash-ask-pill {', 720))
})

// ---------------------------------------------------------------------------
// Grid tracks with a zero floor
// ---------------------------------------------------------------------------

test('roster grids use minmax(0, 1fr), never a bare 1fr', () => {
  // `1fr` is shorthand for `minmax(AUTO, 1fr)`: the track can never get
  // narrower than its content's min-content width. One roster card with a
  // four-column nowrap stats row was therefore able to stretch the grid, the
  // page, and the viewport. This is why the Agents tab scrolled 41px sideways
  // while the other tabs scrolled 8.
  for (const sel of ['.agents-grid', '.instance-group-grid']) {
    const rule = css.match(new RegExp(`\\${sel} \\{[^}]*\\}`))
    assert.ok(rule, `${sel} not found`)
    assert.match(rule[0], /grid-template-columns: minmax\(0, 1fr\)/, sel)
  }
})

// ---------------------------------------------------------------------------
// The Work board
// ---------------------------------------------------------------------------

test('the job row stacks earlier than the shell', () => {
  // The shell breaks where the header stops fitting (720). A job row's head
  // is two columns — name and verdict beside the state bar — and stops
  // reading well before that: at 768 the bar and its counts are squeezed into
  // about 300px. If these two ever get merged onto one breakpoint, an iPad
  // in portrait gets the squeezed row back.
  assert.ok(inMediaQuery(css, '.wk-job-head { grid-template-columns: minmax(0, 1fr)', 860),
    'the job head must stack at 860px')
  assert.ok(inMediaQuery(css, '.wk-tiles { grid-template-columns: repeat(2, minmax(0, 1fr))', 860),
    'four tiles become two-by-two at 860px')
  assert.ok(inMediaQuery(css, '.wk-done-grid { grid-template-columns: minmax(0, 1fr)', 860))
})

test('the state bar carries its counts in words', () => {
  // A proportional bar cannot say whether it is a bar of 3 runs or 300, and
  // stacked on a phone it is the only thing beside the name. The counts are
  // printed under it in every layout, and the bar itself names them for AT.
  assert.match(workTab, /<p className="wk-counts">\{calmLine\(grouped\)\}<\/p>/)
  assert.match(workTab, /role="img" aria-label=\{countsLine\(grouped\)\}/)
})

// ---------------------------------------------------------------------------
// Inline styles that had to give up their layout
// ---------------------------------------------------------------------------

test('agent-detail stats are laid out in CSS, not inline', () => {
  // An inline style outranks every stylesheet rule, so anything a media query
  // must change cannot be inline. The dividers were drawn with
  // `borderRight: i < last` — which, once the row wrapped, drew a divider
  // after whichever stat happened to end a visual row and indented the next
  // row's first stat by its neighbour's margin.
  assert.match(agentDetail, /className="ad-stats"/)
  assert.match(agentDetail, /className="ad-stat"/)
  // The exact expression, not the phrase: the comment above the fix quotes it
  // to explain what went wrong, and a looser pattern matches the explanation.
  assert.ok(
    !/borderRight: i < stats\.length/.test(agentDetail),
    'the wrap-unaware inline divider is back',
  )
  assert.ok(inMediaQuery(css, '.ad-stats {', 720))
})

// ---------------------------------------------------------------------------
// Touch
// ---------------------------------------------------------------------------

test('fields are 16px on a phone', () => {
  // Under 16px, Safari zooms the page when a field takes focus and does not
  // zoom back out. It is the one iOS quirk with no workaround but the font
  // size, and it costs nothing at this width.
  assert.ok(inMediaQuery(css, 'font-size: 16px', 720))
})

test('tap size follows the input device, not the width', () => {
  // Everything else here breaks at a width, because it is about what fits.
  // How big a control has to be is about what is pointing at it: an iPad in
  // portrait is 768px — wide enough to keep the desktop header, and still a
  // thumb. Dropping the pointer clause silently returns 28px buttons to every
  // tablet.
  assert.match(css, /@media \(max-width: 720px\), \(pointer: coarse\) \{/)
})

test('the labels that drop are spans with an aria-label behind them', () => {
  // "Add Agent" and the account name are dropped to icons on a phone. A
  // control that is only an icon still has to be named, or the header becomes
  // four unlabelled squares to a screen reader.
  assert.match(app, /className="btn-compact-label"/)
  assert.match(app, /aria-label="Add Agent"/)
  assert.ok(inMediaQuery(css, '.btn-compact-label', 720))
  assert.ok(inMediaQuery(css, '.account-badge-label', 720))
})

test('the hover-revealed delete control is visible without hover', () => {
  // There is no hover on a touch screen. A control that only appears on hover
  // is a control that does not exist there.
  assert.ok(inMediaQuery(css, '.card-delete-btn {', 720))
  const block = css.slice(css.indexOf('/* The delete control is hover-revealed'))
  assert.match(block.slice(0, 400), /opacity: 1/)
})

// ---------------------------------------------------------------------------
// Desktop is untouched
// ---------------------------------------------------------------------------

test('every phone rule is inside a max-width query', () => {
  // The whole ship has to be invisible above 860px. A rule that leaked out of
  // its media query would change the product for everyone on a laptop, which
  // is who uses it today.
  for (const sel of ['.wk-head-controls { width: 100%', '.btn-compact-label', '.ad-chip']) {
    const at = css.indexOf(sel)
    assert.notEqual(at, -1, `${sel} not found`)
    const before = css.slice(0, at)
    const opens = (before.match(/@media[^{]*\{/g) || []).length
    assert.ok(opens > 0, `${sel} appears before any media query`)
  }
  // And no min-width query sneaked in that would make the phone rules the
  // default with desktop as the exception.
  assert.ok(!/@media \(min-width/.test(css), 'this stylesheet is max-width only')
})
