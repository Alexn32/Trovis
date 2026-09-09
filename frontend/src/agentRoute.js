/**
 * Where an agent-shaped row actually points.
 *
 * A row's `agent` is a LABEL: the display name when the operator set one, and
 * for a drift row on a multi-agent service it also carries the sub-agent
 * ("Support Bot · researcher"). Navigating by it opens /agents/Support%20Bot,
 * which 404s into a near-empty page — the blank screen people reported after
 * clicking the work feed.
 *
 * Rows now carry `service_name` + `agent_id` for exactly this. Keep the two
 * apart everywhere: the label is for reading, the route is for routing.
 *
 * The fallback to `agent` is for rows cached before the payload gained the
 * field (attention rows have a 1h TTL); those behave as they did rather than
 * navigating nowhere, and age out on their own.
 */
export function agentRoute(row) {
  return [row?.service_name || row?.agent, row?.agent_id || 'main']
}
