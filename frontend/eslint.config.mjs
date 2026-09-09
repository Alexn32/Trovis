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
]
