import {
  BRANDS,
  BRAND_ORDER,
  COMING_BRAND_IDS,
  LIVE_BRAND_IDS,
  RECIPE_BRAND_IDS,
  brandTooltip,
  resolveBrand,
} from './brandMarks.js'
import { AnthropicIcon, OpenAIIcon, OpenClawIcon } from './Icons.jsx'

// Shared V1 brand marks. Simple, recognizable SVGs — not dumped brand kits.
// OpenClaw / Claude / ChatGPT reuse the existing Icons.jsx marks.

export function CursorIcon({ size = 18 }) {
  // Two-shade diamond — the commonly recognized Cursor mark, not a plugin badge.
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <path d="M12 2.2 21 8.4v7.2L12 21.8 3 15.6V8.4L12 2.2z" fill="currentColor" />
      <path d="M12 2.2 21 8.4 12 12 3 8.4 12 2.2z" fill="currentColor" opacity="0.45" />
    </svg>
  )
}

export function GrokIcon({ size = 18 }) {
  // Angular slashes — the monochrome xAI/Grok mark, drawn not dumped.
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <path d="M5.1 20.4 15.8 5.6h3.6L8.7 20.4H5.1z" fill="currentColor" />
      <path d="M4.6 12.9 9.2 6.4h3.6l-4.6 6.5H4.6z" fill="currentColor" opacity="0.5" />
      <path d="M16.1 12.2h3.3v8.2h-3.3v-8.2z" fill="currentColor" opacity="0.5" />
    </svg>
  )
}

export function SlackIcon({ size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <path fill="#e01e5a" d="M6.31 15.17a2.53 2.53 0 0 1-2.52 2.52A2.53 2.53 0 0 1 1.27 15.17a2.53 2.53 0 0 1 2.52-2.52h2.52z" />
      <path fill="#e01e5a" d="M7.58 15.17a2.53 2.53 0 0 1 2.52-2.52 2.53 2.53 0 0 1 2.52 2.52v6.31a2.53 2.53 0 0 1-2.52 2.52 2.53 2.53 0 0 1-2.52-2.52z" />
      <path fill="#36c5f0" d="M8.83 6.31a2.53 2.53 0 0 1-2.52-2.52A2.53 2.53 0 0 1 8.83 1.27a2.53 2.53 0 0 1 2.52 2.52v2.52z" />
      <path fill="#36c5f0" d="M8.83 7.58a2.53 2.53 0 0 1 2.52 2.52 2.53 2.53 0 0 1-2.52 2.52H2.52A2.53 2.53 0 0 1 0 10.1a2.53 2.53 0 0 1 2.52-2.52z" />
      <path fill="#2eb67d" d="M17.69 8.83a2.53 2.53 0 0 1 2.52-2.52A2.53 2.53 0 0 1 22.73 8.83a2.53 2.53 0 0 1-2.52 2.52h-2.52z" />
      <path fill="#2eb67d" d="M16.42 8.83a2.53 2.53 0 0 1-2.52 2.52 2.53 2.53 0 0 1-2.52-2.52V2.52A2.53 2.53 0 0 1 13.9 0a2.53 2.53 0 0 1 2.52 2.52z" />
      <path fill="#ecb22e" d="M15.17 17.69a2.53 2.53 0 0 1 2.52 2.52 2.53 2.53 0 0 1-2.52 2.52 2.53 2.53 0 0 1-2.52-2.52v-2.52z" />
      <path fill="#ecb22e" d="M15.17 16.42a2.53 2.53 0 0 1-2.52-2.52 2.53 2.53 0 0 1 2.52-2.52h6.31A2.53 2.53 0 0 1 24 13.9a2.53 2.53 0 0 1-2.52 2.52z" />
    </svg>
  )
}

export function GitHubIcon({ size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M12 .3C5.37.3 0 5.67 0 12.3c0 5.3 3.44 9.8 8.21 11.39.6.11.82-.26.82-.58 0-.28-.01-1.04-.02-2.04-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.21.09 1.85 1.24 1.85 1.24 1.07 1.84 2.81 1.31 3.5 1 .11-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.13-.3-.54-1.52.12-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.66 1.66.25 2.88.12 3.18.77.84 1.23 1.91 1.23 3.22 0 4.61-2.81 5.62-5.48 5.92.43.37.81 1.1.81 2.22 0 1.61-.01 2.91-.01 3.3 0 .32.21.69.82.57C20.56 22.1 24 17.6 24 12.3 24 5.67 18.63.3 12 .3z" />
    </svg>
  )
}

export function HubSpotIcon({ size = 18 }) {
  // Sprocket — the commonly recognized HubSpot mark, kept small.
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="#ff7a59" aria-hidden="true">
      <path d="M12 7.15a4.85 4.85 0 1 1 0 9.7 4.85 4.85 0 0 1 0-9.7zm0 2.55a2.3 2.3 0 1 0 .01 4.6 2.3 2.3 0 0 0-.01-4.6z" />
      <circle cx="12" cy="3.15" r="1.55" />
      <circle cx="12" cy="20.85" r="1.55" />
      <circle cx="3.15" cy="12" r="1.55" />
      <circle cx="20.85" cy="12" r="1.55" />
      <circle cx="5.55" cy="5.55" r="1.45" />
      <circle cx="18.45" cy="5.55" r="1.45" />
      <circle cx="5.55" cy="18.45" r="1.45" />
      <circle cx="18.45" cy="18.45" r="1.45" />
    </svg>
  )
}

export function StripeIcon({ size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="#635bff" aria-hidden="true">
      <path d="M13.98 9.15c-2.17-.81-3.36-1.43-3.36-2.41 0-.83.68-1.31 1.9-1.31 2.23 0 4.52.86 6.09 1.63l.89-5.49C18.25.98 15.7 0 12.17 0 9.67 0 7.59.65 6.1 1.87 4.56 3.15 3.76 4.99 3.76 7.22c0 4.04 2.47 5.76 6.48 7.22 2.58.92 3.44 1.57 3.44 2.58 0 .98-.84 1.55-2.35 1.55-1.88 0-4.97-.92-6.99-2.11l-.9 5.56C5.18 22.99 8.39 24 11.71 24c2.64 0 4.84-.62 6.33-1.81 1.66-1.31 2.52-3.24 2.52-5.73 0-4.13-2.52-5.85-6.59-7.31z" />
    </svg>
  )
}

export function IntercomIcon({ size = 18 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="#1f8ded" aria-hidden="true">
      <path d="M4.4 3.4h15.2A3.4 3.4 0 0 1 23 6.8v7.8a3.4 3.4 0 0 1-3.4 3.4h-5.2l-5.8 4.2v-4.2H4.4A3.4 3.4 0 0 1 1 14.6V6.8A3.4 3.4 0 0 1 4.4 3.4z" />
      <circle cx="8" cy="10.8" r="1.35" fill="#fff" />
      <circle cx="12" cy="10.8" r="1.35" fill="#fff" />
      <circle cx="16" cy="10.8" r="1.35" fill="#fff" />
    </svg>
  )
}

export function ShopifyIcon({ size = 18 }) {
  // Shopping bag — recognizable Shopify-adjacent mark, kept simple.
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="#96bf48" aria-hidden="true">
      <path d="M16.2 6.1a4.2 4.2 0 0 0-8.4 0H5.85L4.7 21.15A1.85 1.85 0 0 0 6.54 23.1h10.92a1.85 1.85 0 0 0 1.84-1.95L18.15 6.1H16.2zm-6.5 0a2.3 2.3 0 0 1 4.6 0h-4.6zM12 10.7a1.45 1.45 0 1 1 0 2.9 1.45 1.45 0 0 1 0-2.9z" />
    </svg>
  )
}

const ICONS = {
  openclaw: OpenClawIcon,
  claude: AnthropicIcon,
  cursor: CursorIcon,
  chatgpt: OpenAIIcon,
  grok: GrokIcon,
  slack: SlackIcon,
  github: GitHubIcon,
  hubspot: HubSpotIcon,
  stripe: StripeIcon,
  intercom: IntercomIcon,
  shopify: ShopifyIcon,
}

export function BrandMark({ id, size = 16, className = '' }) {
  const meta = BRANDS[id]
  const Icon = ICONS[id]
  if (!Icon || !meta) return null
  const style = meta.color ? { color: meta.color } : undefined
  return (
    <span className={`brand-mark ${className}`.trim()} style={style} aria-hidden="true">
      <Icon size={size} />
    </span>
  )
}

/** Quiet mark next to a known platform / holder / tool. Unknown → nothing. */
export function QuietBrand({ texts = [], size = 12, className = '' }) {
  const id = resolveBrand(...texts)
  if (!id) return null
  return (
    <span className={`brand-quiet ${className}`.trim()} title={BRANDS[id].label}>
      <BrandMark id={id} size={size} />
    </span>
  )
}

function Logo({ id, size = 16, showName = false }) {
  const meta = BRANDS[id]
  if (!meta) return null
  return (
    <span
      className={`works-with-logo${meta.role === 'coming' ? ' is-coming' : ''}`}
      title={brandTooltip(id)}
    >
      <BrandMark id={id} size={size} />
      {showName && <span className="works-with-name">{meta.label}</span>}
    </span>
  )
}

/**
 * Recognition strip. Live + recipe marks under "Works with"; SaaS under
 * "Coming". Not buttons. Never claims those tools are connected.
 */
export function WorksWithStrip({ className = '', showNames = false }) {
  const works = [...LIVE_BRAND_IDS, ...RECIPE_BRAND_IDS]
  return (
    <div className={`works-with ${className}`.trim()}>
      <div className="works-with-row">
        <span className="works-with-label">Works with</span>
        {works.map((id) => (
          <Logo key={id} id={id} showName={showNames} />
        ))}
      </div>
      <div className="works-with-row is-coming">
        <span className="works-with-label">Coming</span>
        {COMING_BRAND_IDS.map((id) => (
          <Logo key={id} id={id} showName={showNames} />
        ))}
        <span className="works-with-label">+ anything OTEL</span>
      </div>
    </div>
  )
}

export {
  BRANDS,
  BRAND_ORDER,
  brandTooltip,
  resolveBrand,
}
