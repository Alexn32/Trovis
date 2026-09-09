// V1 Connect brand catalog + resolver.
//
// Architecture is many transports → one Work Event. A logo is recognition,
// not a connector. Roles:
//   live    — a Connect door that already works today
//   recipe  — a real path (OTEL), not a first-party plugin
//   coming  — recognition chrome only; never a live OAuth/webhook door
//
// Zendesk is intentionally out of V1.

export const BRAND_ORDER = [
  'openclaw',
  'claude',
  'cursor',
  'chatgpt',
  'slack',
  'github',
  'hubspot',
  'stripe',
  'intercom',
  'shopify',
]

export const BRANDS = {
  openclaw: {
    id: 'openclaw',
    label: 'OpenClaw',
    role: 'live',
    color: null, // self-colored lobster
    aliases: ['openclaw', 'open claw'],
  },
  claude: {
    id: 'claude',
    label: 'Claude',
    role: 'live',
    color: '#d97757',
    aliases: ['claude', 'anthropic'],
  },
  cursor: {
    id: 'cursor',
    label: 'Cursor',
    role: 'recipe',
    color: null, // currentColor
    aliases: ['cursor'],
  },
  chatgpt: {
    id: 'chatgpt',
    label: 'ChatGPT',
    role: 'live',
    color: '#10a37f',
    aliases: ['chatgpt', 'chat gpt', 'openai', 'openai agents'],
  },
  slack: {
    id: 'slack',
    label: 'Slack',
    role: 'coming',
    color: null, // self-colored hash
    aliases: ['slack'],
  },
  github: {
    id: 'github',
    label: 'GitHub',
    role: 'coming',
    color: null,
    aliases: ['github', 'git hub'],
  },
  hubspot: {
    id: 'hubspot',
    label: 'HubSpot',
    role: 'live',
    color: '#ff7a59',
    aliases: ['hubspot', 'hub spot'],
  },
  stripe: {
    id: 'stripe',
    label: 'Stripe',
    role: 'live',
    color: '#635bff',
    aliases: ['stripe'],
  },
  intercom: {
    id: 'intercom',
    label: 'Intercom',
    role: 'coming',
    color: '#1f8ded',
    aliases: ['intercom'],
  },
  shopify: {
    id: 'shopify',
    label: 'Shopify',
    role: 'coming',
    color: '#96bf48',
    aliases: ['shopify'],
  },
}

export const LIVE_BRAND_IDS = BRAND_ORDER.filter((id) => BRANDS[id].role === 'live')
export const RECIPE_BRAND_IDS = BRAND_ORDER.filter((id) => BRANDS[id].role === 'recipe')
export const COMING_BRAND_IDS = BRAND_ORDER.filter((id) => BRANDS[id].role === 'coming')

/** Honest tooltip — never says "connected" for recognition-only marks. */
export function brandTooltip(id) {
  const b = BRANDS[id]
  if (!b) return ''
  if (b.role === 'live') return `${b.label} — connect today`
  if (b.role === 'recipe') return `${b.label} — send traces over OpenTelemetry`
  return `${b.label} — recognized when it shows up in work. A direct connect is coming.`
}

function normalize(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/[_./:+-]+/g, ' ')
    .replace(/[^a-z0-9 ]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

function hasPhrase(haystack, phrase) {
  if (!haystack || !phrase) return false
  return ` ${haystack} `.includes(` ${phrase} `)
}

/**
 * Map free text (platform label, holder name, tool, to_system target, …)
 * to a V1 brand id. Unknown → null. Prefer the longest alias when several
 * match so "OpenAI Agents SDK" wins over a bare "gpt" if we add one later.
 */
export function resolveBrand(...texts) {
  const hay = normalize(texts.filter(Boolean).join(' '))
  if (!hay) return null
  let best = null
  let bestLen = 0
  for (const id of BRAND_ORDER) {
    for (const alias of BRANDS[id].aliases) {
      if (alias.length > bestLen && hasPhrase(hay, alias)) {
        best = id
        bestLen = alias.length
      }
    }
  }
  return best
}
