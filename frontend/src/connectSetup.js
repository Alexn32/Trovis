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

import { CATEGORY_LABELS, CONNECTORS, TILE_SETUP_TYPES, getConnector, tileConnectors } from './connectors.js'

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

/** Available work systems (OAuth doors), registry order. */
export function workSystemConnectors() {
  return CONNECTORS.filter((c) => c.availability === 'available' && c.setup_type === 'oauth')
}

/** The work systems' chips for the guide's opening turn — their names. */
export function workSystemOptions() {
  return workSystemConnectors().map((c) => c.name)
}

/**
 * Registry connector for an opening chip label — an AI connector's
 * `guide_label` or a work system's name; unknown → null.
 */
export function connectorForGuideOption(label) {
  const want = String(label || '').trim().toLowerCase()
  if (!want) return null
  return (
    CONNECTORS.find((c) => (c.guide_label || '').trim().toLowerCase() === want) ||
    workSystemConnectors().find((c) => c.name.trim().toLowerCase() === want) ||
    null
  )
}

/**
 * The Connect landing's intents — "what do you want Trovis to see?" — each
 * with where it sends the person. Every path lands in the guide; the intent
 * only decides the first thing the guide hears, so the wording stays with
 * the registry's category labels and the guide keeps one conversation.
 *
 *   message   the sentence shown as the first user turn
 *   local     when set, the guide answers that turn itself ({content,
 *             options}) instead of asking the model — the registry already
 *             holds the list of doors
 */
export function intentChips() {
  return [
    {
      id: 'ai',
      label: 'An AI worker or agent platform',
      hint: [CATEGORY_LABELS.ai_worker, CATEGORY_LABELS.agent_platform].join(' · '),
      message: 'I want to connect an AI worker or agent platform.',
      // Nothing to ask a model yet: the next question is "which one?", and
      // the registry already knows the answers. A local turn keeps the guide
      // instant and never blocks on the model for a list it has.
      local: { content: 'Which platform is it built with?', options: guideOpeningOptions() },
    },
    {
      id: 'work_system',
      label: 'A work system',
      hint: workSystemConnectors().map((c) => c.name).join(' · '),
      message: 'I want to connect a work system like Stripe, HubSpot or Shopify.',
      local: { content: 'Which work system? Pick one and a Connect button appears here.', options: workSystemOptions() },
    },
    {
      id: 'custom',
      label: 'Something custom',
      hint: 'Anything that emits OpenTelemetry',
      message: 'I have a custom agent that can emit OpenTelemetry.',
    },
    {
      id: 'unsure',
      label: "I'm not sure",
      hint: 'Trovis will ask',
      message: "I'm not sure what I should connect first.",
    },
  ]
}

export { TILE_SETUP_TYPES }
