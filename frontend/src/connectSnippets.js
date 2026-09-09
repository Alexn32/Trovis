// Snippet plumbing for the "Set up with AI" guide (ConnectGuide.jsx).
//
// The model never sees the org's real credential: it writes the literal
// placeholders TROVIS_API_KEY / TROVIS_ENDPOINT, and these helpers fill in the
// real values at render time only. `flattenAssistant` is what goes back on the
// wire, so it keeps the placeholders — that is the reason a copied snippet
// works while /connect/ask payloads stay free of the key.

export const KEY_PLACEHOLDER = 'TROVIS_API_KEY'
export const ENDPOINT_PLACEHOLDER = 'TROVIS_ENDPOINT'

// Shown when the session has no key to substitute (see ConnectGuide's note).
export const KEY_FALLBACK = 'ov_sk_…'

// Fill the user's real key/endpoint into a snippet for display. The negative
// lookahead skips a placeholder used as an env-var NAME (`export
// TROVIS_API_KEY=…`) so only value positions are substituted — the copy
// button then copies exactly what's on screen, and the name stays a name.
export function substitute(text, key, endpoint) {
  return (text || '')
    .replace(new RegExp(`${ENDPOINT_PLACEHOLDER}(?!\\s*=)`, 'g'), endpoint)
    .replace(new RegExp(`${KEY_PLACEHOLDER}(?!\\s*=)`, 'g'), key || KEY_FALLBACK)
}

// Flatten an assistant turn (answer + its code snippets) for the wire history,
// so the model recalls exactly what it already handed the user. Placeholders
// are deliberately left intact — substitution is display-only.
export function flattenAssistant(m) {
  const codeText = (m.code || []).map((c) => c.content).join('\n\n')
  return codeText ? `${m.content}\n\n${codeText}` : m.content
}
