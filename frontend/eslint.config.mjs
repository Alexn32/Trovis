import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'

// One job: catch the crash class that has taken agent pages down twice —
// an identifier that doesn't exist at runtime. `serviceName is not defined`
// in AgentDetail's header blanked the page for every agent whose telemetry
// carries a platform, and a build passes straight over it because Vite only
// transforms JSX; nothing resolves free variables.
//
// Deliberately narrow: `no-undef` and nothing else. The react-hooks plugin is
// registered (not enabled) so the repo's existing
// `// eslint-disable-next-line react-hooks/exhaustive-deps` comments resolve
// to a real rule instead of erroring as unknown. Turning more rules on is a
// separate decision — this config exists to keep pages from going blank, not
// to start a style debate.
export default [
  {
    files: ['src/**/*.{js,jsx}', 'test/**/*.mjs'],
    plugins: { 'react-hooks': reactHooks },
    // Those exhaustive-deps disable comments are notes for whoever turns the
    // rule on; with it off they'd otherwise all report as unused.
    linterOptions: { reportUnusedDisableDirectives: 'off' },
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      'no-undef': 'error',
    },
  },
  // The second crash class, and the one that keeps coming back: a missing
  // value coerced into a measured zero. `Number(null)` and `Number('')` are
  // both 0, so `Number(x) || 0` turns "we were not told" into "it was free"
  // — which is how $0.00 reached an unpriced run (#160), $0.00/run reached a
  // job (#164), and 0s/0% reached a job that had never run (#167).
  //
  // Three occurrences means the fourth is already being typed. `numOrNull`
  // (workBoard.js) rejects absent BEFORE the cast; use it.
  //
  // Scoped to the Work surface, where honesty rules 1-6 apply and where all
  // three occurrences happened. Cost, Fleet and Dashboard still hold ~11 of
  // these; some are legitimate (a zero-filled cost series, a bar height) and
  // some are the same bug. Widening the scope means auditing each, which is
  // its own change, not a rider on this one.
  {
    files: [
      'src/workBoard.js', 'src/jobPage.js', 'src/board.js', 'src/jobDetail.js',
      'src/home.js', 'src/WorkTab.jsx', 'src/JobDetail.jsx',
    ],
    rules: {
      'no-restricted-syntax': ['error', {
        selector: 'LogicalExpression[operator="||"] > CallExpression.left[callee.name="Number"]',
        message:
          'Number(x) || 0 turns a missing value into a measured zero. Use '
          + 'numOrNull(x) from workBoard.js and handle null explicitly.',
      }, {
        selector: 'LogicalExpression[operator="??"] > CallExpression.left[callee.name="Number"]',
        message:
          'Number(x) ?? fallback never fires — Number(null) is 0, not '
          + 'nullish. Use numOrNull(x) from workBoard.js.',
      }],
    },
  },
]
