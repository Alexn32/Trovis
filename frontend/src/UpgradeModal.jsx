import { useState } from 'react'
import { api } from './api.js'

// Plan-picker modal → Stripe Checkout. Picking a paid tier calls
// PUT /account/plan {plan, cycle}; the backend returns a Stripe checkout_url
// and we redirect there. The plan only actually flips once Stripe's webhook
// confirms payment — so on return the dashboard reflects the new plan.
//
// Pricing shown here is indicative and mirrors the marketing page; Stripe
// charges the exact amounts configured on the prices. Annual = 20% off,
// displayed per-month.
const TIERS = [
  { id: 'starter', name: 'Starter', monthly: 49, agents: 15 },
  { id: 'pro', name: 'Pro', monthly: 199, agents: 50, highlight: true },
]

const RANK = { free: 0, starter: 1, pro: 2, enterprise: 3 }

export default function UpgradeModal({ open, me, onClose, onApplied }) {
  const [cycle, setCycle] = useState('monthly')
  const [busy, setBusy] = useState(null) // tier id currently processing
  const [error, setError] = useState(null)

  if (!open) return null

  const currentPlan = me?.org?.plan || 'free'

  async function choose(tier) {
    setError(null)
    setBusy(tier.id)
    try {
      const res = await api.setPlan(tier.id, cycle)
      if (res?.checkout_url) {
        // Hand off to Stripe's hosted checkout.
        window.location.href = res.checkout_url
        return
      }
      if (res?.status === 'applied') {
        onApplied?.()
        onClose?.()
        return
      }
      setError('Could not start checkout. Please try again.')
    } catch (e) {
      const msg = String(e?.message || '')
      if (msg.includes('503') || /not configured|unavailable/i.test(msg)) {
        setError('Billing isn’t available just yet — check back soon.')
      } else {
        setError('Something went wrong starting checkout. Please try again.')
      }
    } finally {
      setBusy(null)
    }
  }

  const priceLabel = (t) => {
    const m = cycle === 'annual' ? Math.round(t.monthly * 0.8) : t.monthly
    return { big: `$${m}`, small: cycle === 'annual' ? '/mo · billed annually' : '/month' }
  }

  return (
    <div className="upgrade-backdrop" onClick={onClose}>
      <div className="upgrade-card" onClick={(e) => e.stopPropagation()}>
        <button className="upgrade-close" onClick={onClose} aria-label="Close">×</button>
        <h2 className="upgrade-title">Upgrade your plan</h2>
        <p className="upgrade-sub">
          Pay for agents, nothing else — every feature is included at every tier.
        </p>

        <div className="upgrade-toggle" role="group" aria-label="Billing cycle">
          <button
            className={cycle === 'monthly' ? 'is-active' : ''}
            onClick={() => setCycle('monthly')}
          >Monthly</button>
          <button
            className={cycle === 'annual' ? 'is-active' : ''}
            onClick={() => setCycle('annual')}
          >Annual − 20%</button>
        </div>

        <div className="upgrade-tiers">
          {TIERS.map((t) => {
            const p = priceLabel(t)
            const isCurrent = currentPlan === t.id
            const isDowngrade = RANK[t.id] <= RANK[currentPlan]
            return (
              <div key={t.id} className={`upgrade-tier${t.highlight ? ' is-highlight' : ''}`}>
                <div className="upgrade-tier-name">{t.name}</div>
                <div className="upgrade-tier-price">
                  <span className="upgrade-tier-big">{p.big}</span>
                  <span className="upgrade-tier-small"> {p.small}</span>
                </div>
                <div className="upgrade-tier-agents">Up to {t.agents} agents · all features</div>
                <button
                  className={`btn ${t.highlight ? 'btn-primary' : 'btn-secondary'} btn-block`}
                  disabled={busy !== null || isCurrent || isDowngrade}
                  onClick={() => choose(t)}
                >
                  {busy === t.id
                    ? 'Starting checkout…'
                    : isCurrent
                      ? 'Current plan'
                      : isDowngrade
                        ? 'Included'
                        : `Choose ${t.name}`}
                </button>
              </div>
            )
          })}
        </div>

        {error && <div className="upgrade-error">{error}</div>}

        <div className="upgrade-foot">
          Need more than 50 agents?{' '}
          <a href="mailto:hello@trovisai.com">Talk to us about Enterprise</a>.
        </div>
      </div>
    </div>
  )
}
