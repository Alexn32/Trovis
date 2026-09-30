// The guided setup's lifecycle words, from the verification read. Pure; no
// React.
//
// GET /connect/connections/:id/status says what arrived for THIS setup since
// it began: its state, how the traffic was attributed (the instance's own
// key, or connector-level traffic without it), and which kinds of data the
// spans carried. This module only words that; it never infers a state the
// endpoint did not return, and it says "cannot see" only about what a
// connector kind can never observe on its own (the registry's `observes`).

import { CONNECTORS, getConnector } from './connectors.js'

export const SEES_LABELS = Object.freeze({
  execution: 'execution',
  actions: 'tool activity',
  model_usage: 'model usage',
  named_work: 'named runs',
})

/** The observed kinds, in a fixed order, as short labels. */
export function seesList(sees) {
  return Object.keys(SEES_LABELS).filter((k) => sees && sees[k] === true).map((k) => SEES_LABELS[k])
}

/**
 * The instance-level lifecycle line for the guide and the wizard strip.
 * Returns { phase, title, detail } where phase is one of
 * 'setup_started' | 'waiting_for_data' | 'connected' | 'connector_only' |
 * 'disconnected' | 'unknown'.
 *
 * `connector_only` is the honest middle: telemetry for this connector arrived
 * without this setup's id (an SDK without connection_id, a door that cannot
 * carry it yet). Trovis is receiving from the platform; it cannot tie that
 * traffic to this setup.
 */
export function lifecycle(status, connector, rel = (iso) => iso) {
  const name = connector?.name || status?.connector_id || 'This connector'
  if (!status || !status.state) return { phase: 'unknown', title: `Checking ${name}…`, detail: null }
  const when = status.last_observed_at ? rel(status.last_observed_at) : null
  if (status.state === 'connected') {
    const kinds = seesList(status.sees)
    return {
      phase: 'connected',
      title: `${name} connected`,
      detail: [when ? `Last data ${when}` : null, kinds.length ? `Trovis can see: ${kinds.join(', ')}` : null]
        .filter(Boolean).join(' · ') || null,
    }
  }
  if (status.attribution === 'connector') {
    const kinds = seesList(status.sees)
    const who = (status.services || []).map((s) => s.service_name).filter(Boolean)
    return {
      phase: 'connector_only',
      title: `${name} telemetry is arriving${who.length ? ` from ${who.slice(0, 3).join(', ')}` : ''}`,
      detail: [
        'It does not carry this setup’s id, so Trovis cannot tie it to this connection.',
        kinds.length ? `Trovis can see: ${kinds.join(', ')}` : null,
      ].filter(Boolean).join(' '),
    }
  }
  if (status.state === 'waiting_for_data') {
    return { phase: 'waiting_for_data', title: `Waiting for the first data from ${name}`, detail: 'Run your agent once and this updates by itself.' }
  }
  if (status.state === 'disconnected') {
    return { phase: 'disconnected', title: `${name} disconnected`, detail: when ? `Last data ${when}` : null }
  }
  return { phase: 'setup_started', title: `Set up ${name}, then run it`, detail: 'Trovis is listening for the first data from this setup.' }
}

/**
 * What a connection of this kind can never observe on its own, and which
 * connectors could add it — capability language, never a verdict about a
 * run. Only the work systems (external outcomes) today: an AI connector never
 * sees the provider's own outcome, and a work system is the only kind that
 * contributes `external_outcomes`.
 */
export function cannotSee(connector) {
  if (!connector || (connector.observes || []).includes('external_outcomes')) return null
  const adders = CONNECTORS.filter(
    (c) => c.availability === 'available' && c.setup_type === 'oauth' && (c.observes || []).includes('external_outcomes'),
  )
  if (!adders.length) return null
  return {
    text: 'Trovis cannot independently see what happens in your work systems (a payment clearing, an order fulfilled).',
    connectors: adders.map((c) => c.id),
  }
}

/** Convenience: the registry connector for a status row. */
export function connectorOf(status) {
  return status?.connector_id ? getConnector(status.connector_id) : null
}
