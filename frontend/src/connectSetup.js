// Connect setup surfaces, derived from the canonical connector registry.
// Pure; no React.
//
// Three surfaces used to carry their own copy of "which platforms can you
// connect": the manual wizard's tiles (AddAgent.jsx PLATFORMS /
// RECIPE_PLATFORMS / CLAUDE_VARIANTS), the AI guide's opening chips
// (ConnectGuide.jsx OPENING_TURN.options) and the backend's setup prose.
// Each one could drift from the registry on its own. Now they all read the
// registry (connectors.js ← connectors.registry.json ← connectors.py): add
// a connector on the backend with a `setup_type` and labels, regenerate the
// snapshot, and every surface shows it.

import { CONNECTORS, TILE_SETUP_TYPES, getConnector, tileConnectors } from './connectors.js'

const AI_CATEGORIES = ['ai_worker', 'agent_platform', 'custom']

function tile(c) {
  return {
    id: c.id,
    label: c.tile_label || c.name,
    subtitle: c.tile_subtitle || c.description,
    // No live door asks for an LLM provider; the generic Python pages that
    // did are not reachable from the picker.
    needsProvider: false,
  }
}

/** Live doors in the wizard's main grid — every tile type except `recipe`. */
export function pickerTiles() {
  return tileConnectors().filter((c) => c.setup_type !== 'recipe').map(tile)
}

/** "Or send traces yourself" — the OTEL recipe tiles. */
export function recipeTiles() {
  return tileConnectors().filter((c) => c.setup_type === 'recipe').map(tile)
}

/** Every picker tile, main grid first. */
export function allTiles() {
  return [...pickerTiles(), ...recipeTiles()]
}

export function isTileId(id) {
  return allTiles().some((t) => t.id === id)
}

/**
 * Implementation variants shown on a sub-step under one tile (the Claude
 * tile splits into Agent SDK vs Managed Agents). Each {id, label, subtitle};
 * the ids are the instructions-page ids the wizard dispatches on.
 */
export function variantsFor(connectorId) {
  return (getConnector(connectorId)?.variants || []).map((v) => ({ ...v }))
}

/**
 * The AI guide's opening chips: one per available AI connector that carries
 * a `guide_label`, in registry order. `custom-otel` ("Custom Python / other")
 * is last because the registry lists it last — the catch-all closes the list.
 */
export function guideOpeningOptions() {
  return CONNECTORS.filter(
    (c) =>
      c.availability === 'available' &&
      AI_CATEGORIES.includes(c.category) &&
      typeof c.guide_label === 'string' &&
      c.guide_label.trim(),
  ).map((c) => c.guide_label)
}

/** Registry connector for an opening chip label; unknown → null. */
export function connectorForGuideOption(label) {
  const want = String(label || '').trim().toLowerCase()
  if (!want) return null
  return CONNECTORS.find((c) => (c.guide_label || '').trim().toLowerCase() === want) || null
}

export { TILE_SETUP_TYPES }
