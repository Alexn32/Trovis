// One-time localStorage key migration for the oversee→trovis rename.
//
// Imported FIRST in main.jsx so this runs before any module (api.js,
// ThemeProvider, ConnectionsMap) reads these keys. Copies each legacy
// `oversee_*` value to its new `trovis_*` key and removes the old one — so a
// returning user keeps their session, theme, and saved layout (no logout, no
// reset). Idempotent and safe to keep indefinitely.
const PAIRS = [
  ['oversee_api_key', 'trovis_api_key'],
  ['oversee_session_token', 'trovis_session_token'],
  ['oversee_theme', 'trovis_theme'],
  ['oversee_map_positions', 'trovis_map_positions'],
]

try {
  for (const [oldKey, newKey] of PAIRS) {
    if (localStorage.getItem(newKey) === null) {
      const v = localStorage.getItem(oldKey)
      if (v !== null) {
        localStorage.setItem(newKey, v)
        localStorage.removeItem(oldKey)
      }
    }
  }
} catch {
  // localStorage can throw in sandboxed iframes / private mode — ignore;
  // the readers all fall back gracefully.
}
