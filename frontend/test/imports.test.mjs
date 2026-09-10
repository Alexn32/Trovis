// Every name a component uses from a module is actually imported from it.
//
// This exists because 298 green tests and a clean build shipped a Home that
// crashed on render: `fallbackInsight` was used and never imported. Nothing
// caught it — the suites here read source with regexes rather than executing
// it, and Vite does not resolve free identifiers at build time. The error
// surfaced only in a browser, as the error boundary's "Something went wrong".
//
// So this checks the one thing a regex CAN check soundly: that the set of
// helper names a file references is a subset of what it imported.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const src = (f) => readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8')

/** Named exports of a module, as declared. */
function exportsOf(file) {
  return new Set(
    [...src(file).matchAll(/^export (?:function|const|class)\s+([A-Za-z_$][\w$]*)/gm)]
      .map((m) => m[1]),
  )
}

/** Names a file pulls in from one relative module. */
function importedFrom(code, module) {
  const re = new RegExp(`import\\s*\\{([^}]*)\\}\\s*from\\s*'${module.replace('.', '\\.')}'`, 's')
  const m = code.match(re)
  if (!m) return new Set()
  return new Set(
    m[1].split(',').map((s) => s.trim().split(/\s+as\s+/)[0].trim()).filter(Boolean),
  )
}

// Consumers that lean on a helper module heavily enough for a miss to break
// the page rather than a corner of it.
const PAIRS = [
  ['Dashboard.jsx', './home.js', 'home.js'],
  ['WorkTab.jsx', './home.js', 'home.js'],
  ['WorkTab.jsx', './workBoard.js', 'workBoard.js'],
  ['Dashboard.jsx', './board.js', 'board.js'],
  ['JobDetail.jsx', './jobDetail.js', 'jobDetail.js'],
]

for (const [consumer, spec, moduleFile] of PAIRS) {
  test(`${consumer} imports every ${moduleFile} name it uses`, () => {
    const code = src(consumer)
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '')
    const available = exportsOf(moduleFile)
    const imported = importedFrom(code, spec)
    // Anything the file defines itself is not a missing import.
    const local = new Set(
      [...code.matchAll(/(?:^|\s)(?:function|const|let|class)\s+([A-Za-z_$][\w$]*)/g)].map((m) => m[1]),
    )
    for (const name of available) {
      if (imported.has(name) || local.has(name)) continue
      // Used as a call, as JSX, or as a bare reference.
      const used = new RegExp(`\\b${name}\\s*[(<]`).test(code)
      assert.ok(
        !used,
        `${consumer} uses ${name}() from ${moduleFile} without importing it`,
      )
    }
  })
}

test('the check would have caught the bug it was written for', () => {
  // Guard against the assertion going vacuous: a file that calls a helper it
  // never imported must fail the same logic this test applies.
  const broken = "import { askChips } from './home.js'\nconst x = fallbackInsight(packet)\n"
  const imported = importedFrom(broken, './home.js')
  assert.ok(!imported.has('fallbackInsight'))
  assert.ok(/\bfallbackInsight\s*[(<]/.test(broken))
})
