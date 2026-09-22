// Snippet plumbing for the "Set up with AI" guide (ConnectGuide.jsx).
//
// The model never sees the org's real credential: it writes the literal
// placeholders TROVIS_API_KEY / TROVIS_ENDPOINT, and these helpers fill in the
// real values at render time only. `flattenAssistant` is what goes back on the
// wire, so it keeps the placeholders — that is the reason a copied snippet
// works while /connect/ask payloads stay free of the key.

export const KEY_PLACEHOLDER = 'TROVIS_API_KEY'
export const ENDPOINT_PLACEHOLDER = 'TROVIS_ENDPOINT'
// The Grok Bot report door. A Bot is pointed at the MCP server, not the OTLP
// ingest endpoint, so it needs its own placeholder — substituting the traces
// URL there would send a customer's Bot somewhere that speaks no MCP.
export const MCP_URL_PLACEHOLDER = 'TROVIS_MCP_URL'

// The connection instance this setup belongs to (POST /connect/connections →
// `connection_key`, stamped on the wire as `trovis.connection.id`). When the
// guide has one, snippets carry it; when it has none, the line that would
// carry it is dropped rather than shipped with a placeholder.
export const CONNECTION_PLACEHOLDER = 'TROVIS_CONNECTION_ID'

// Shown when the session has no key to substitute (see ConnectGuide's note).
export const KEY_FALLBACK = 'ov_sk_…'

// Fill the user's real key/endpoint into a snippet for display. The negative
// lookahead skips a placeholder used as an env-var NAME (`export
// TROVIS_API_KEY=…`) so only value positions are substituted — the copy
// button then copies exactly what's on screen, and the name stays a name.
export function substitute(text, key, endpoint, mcpUrl, connectionKey = null) {
  // A Grok Bot's MCP URL names the instance too (`?connection=cn_…`), so its
  // reports attribute to this setup; the server ignores a key it does not own.
  const mcp = mcpUrl && connectionKey ? `${mcpUrl}?connection=${connectionKey}` : mcpUrl || ''
  return withConnectionKey(text || '', connectionKey)
    // Longest placeholder first: TROVIS_MCP_URL shares no prefix with the
    // others today, but replacing the specific before the general is the
    // habit that keeps it safe if one ever does.
    .replace(new RegExp(`${MCP_URL_PLACEHOLDER}(?!\\s*=)`, 'g'), mcp)
    .replace(new RegExp(`${ENDPOINT_PLACEHOLDER}(?!\\s*=)`, 'g'), endpoint)
    .replace(new RegExp(`${KEY_PLACEHOLDER}(?!\\s*=)`, 'g'), key || KEY_FALLBACK)
}

/**
 * Fill TROVIS_CONNECTION_ID with the instance key — or, when there is none,
 * remove what would have carried it: a trailing `, connection_id="…"` on a
 * one-line init(), or the whole line otherwise (an env var, a resource
 * attribute, its own kwarg line). A snippet never ships the placeholder.
 */
export function withConnectionKey(text, connectionKey) {
  const src = text || ''
  if (connectionKey) return src.replaceAll(CONNECTION_PLACEHOLDER, connectionKey)
  if (!src.includes(CONNECTION_PLACEHOLDER)) return src
  return src
    .replace(new RegExp(`,\\s*connection_id\\s*=\\s*"${CONNECTION_PLACEHOLDER}"`, 'g'), '')
    .split('\n')
    .filter((line) => !line.includes(CONNECTION_PLACEHOLDER))
    .join('\n')
}

// Flatten an assistant turn (answer + its code snippets) for the wire history,
// so the model recalls exactly what it already handed the user. Placeholders
// are deliberately left intact — substitution is display-only.
export function flattenAssistant(m) {
  const codeText = (m.code || []).map((c) => c.content).join('\n\n')
  return codeText ? `${m.content}\n\n${codeText}` : m.content
}
