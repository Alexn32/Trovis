import { useEffect, useState } from 'react'
import { api } from './api.js'
import { AskVisualRenderer } from './AskVisuals.jsx'
import { TrovisMark, SendIcon } from './Icons.jsx'

// Floating "Ask about your fleet" pill + ⌘K slide-up chat panel. Rendered
// once at the app-shell level so the assistant is reachable from every page.
// Powered by POST /dashboard/ask — the Trovis assistant answers fleet
// questions from live telemetry AND walks users through connecting agents.

const BASE_SUGGESTIONS = [
  'Which agent is costing me the most per task?',
  'Show me error rates across all agents',
  'How do I connect a new agent?',
  'Which agents are idle?',
]

export default function AskPill() {
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState([])
  const [pending, setPending] = useState(false)
  const [input, setInput] = useState('')
  const [suggestions, setSuggestions] = useState(BASE_SUGGESTIONS)

  // ⌘K / Ctrl+K toggles; Escape closes.
  useEffect(() => {
    function onKey(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen((o) => !o)
      } else if (e.key === 'Escape') {
        setOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // Derive a couple of suggestions from current fleet state.
  useEffect(() => {
    let alive = true
    api
      .listAgents()
      .then((agents) => {
        if (!alive || !Array.isArray(agents) || agents.length === 0) return
        const worst = [...agents]
          .map((a) => ({
            name: a.display_name || a.service_name,
            rate: a.total_spans
              ? (a.total_errors || 0) / a.total_spans
              : 0,
          }))
          .sort((x, y) => y.rate - x.rate)[0]
        const extra = []
        if (worst && worst.rate > 0.02) extra.push(`Why is ${worst.name} failing?`)
        setSuggestions([...extra, ...BASE_SUGGESTIONS].slice(0, 5))
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  async function send(text) {
    const q = (text ?? input).trim()
    if (!q || pending) return
    const next = [...messages, { role: 'user', content: q }]
    setMessages(next)
    setInput('')
    setPending(true)
    try {
      const r = await api.askDashboard(next)
      setMessages([...next, { role: 'assistant', content: r.answer, visual: r.visual || null }])
    } catch (e) {
      const msg = String(e?.message || '')
      setMessages([
        ...next,
        {
          role: 'assistant',
          content: msg.includes('503')
            ? 'AI is unavailable right now — the backend needs an ANTHROPIC_API_KEY.'
            : 'Something went wrong answering that. Please try again.',
        },
      ])
    } finally {
      setPending(false)
    }
  }

  if (!open) {
    return (
      <button type="button" className="dash-ask-pill" onClick={() => setOpen(true)}>
        <span className="dash-sq">
          <TrovisMark size={10} />
        </span>
        <span className="dash-ask-pill-text">Ask about your fleet</span>
        <kbd className="dash-kbd">⌘K</kbd>
      </button>
    )
  }

  return (
    <div className="dash-ask-overlay" onClick={() => setOpen(false)}>
      <div className="dash-ask-panel" onClick={(e) => e.stopPropagation()}>
        <div className="dash-ask-head">
          <span className="dash-ask-title">
            <span className="dash-sq">
              <TrovisMark size={10} />
            </span>
            Ask about your fleet
          </span>
          <button
            type="button"
            className="dash-ask-close"
            onClick={() => setOpen(false)}
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <div className="dash-ask-body">
          {messages.length === 0 ? (
            <div className="dash-ask-empty">
              <p className="dash-ask-help">
                Ask anything about your agents, costs, errors, or performance —
                or how to set something up.
              </p>
              <div className="dash-ask-suggest">
                {suggestions.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className="dash-suggest-pill"
                    onClick={() => send(s)}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m, i) => <Bubble key={i} m={m} />)
          )}
          {pending && (
            <div className="dash-ask-loading" aria-label="Thinking">
              <span />
              <span />
              <span />
            </div>
          )}
        </div>

        <form
          className="dash-ask-input-row"
          onSubmit={(e) => {
            e.preventDefault()
            send()
          }}
        >
          <div className="dash-ask-input-wrap">
            <input
              className="dash-ask-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Ask about your agents..."
              autoFocus
            />
            <button
              type="submit"
              className="dash-ask-send"
              disabled={!input.trim() || pending}
              aria-label="Send"
            >
              <SendIcon size={14} />
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function Bubble({ m }) {
  if (m.role === 'user') {
    return (
      <div className="dash-msg user">
        <div className="dash-bubble">{m.content}</div>
      </div>
    )
  }
  return (
    <div className="dash-msg ai">
      <div className="dash-bubble">
        <div className="dash-bubble-head">
          <span className="dash-sq sm">
            <TrovisMark size={9} />
          </span>
          TROVIS
        </div>
        {m.visual && <AskVisualRenderer visual={m.visual} />}
        <div className="dash-bubble-text">{m.content}</div>
      </div>
    </div>
  )
}
