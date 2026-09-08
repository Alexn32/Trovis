import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { ASK_EVENT } from './askOpen.js'
import { AskVisualRenderer } from './AskVisuals.jsx'
import { TrovisMark, SendIcon } from './Icons.jsx'
import { FALLBACK_CHIPS } from './askChips.js'

// Floating Ask pill + ⌘K slide-up chat panel. Rendered once at the
// app-shell level so the assistant is reachable from every page.
// POST /dashboard/ask answers from the live work record (waiting / stuck /
// overview) and from fleet telemetry.

export default function AskPill() {
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState([])
  const [pending, setPending] = useState(false)
  const [input, setInput] = useState('')
  const [suggestions] = useState(FALLBACK_CHIPS)
  // Always points at the current `send` (defined below), so the open-from-
  // elsewhere listener can fire a question without re-subscribing each render.
  const sendRef = useRef(null)

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

  // Opened from elsewhere in the app (e.g. a job's Ask button), optionally
  // with the question already asked. See askOpen.js for why this is an event.
  useEffect(() => {
    function onAsk(e) {
      const q = String(e?.detail?.question || '').trim()
      setOpen(true)
      if (q) sendRef.current(q)
    }
    window.addEventListener(ASK_EVENT, onAsk)
    return () => window.removeEventListener(ASK_EVENT, onAsk)
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

  sendRef.current = send

  if (!open) {
    return (
      <button type="button" className="dash-ask-pill" onClick={() => setOpen(true)}>
        <span className="dash-sq">
          <TrovisMark size={10} />
        </span>
        <span className="dash-ask-pill-text">Ask</span>
        <kbd className="dash-kbd">⌘K</kbd>
      </button>
    )
  }

  return (
    <div className="dash-ask-overlay" onClick={() => setOpen(false)}>
      <div className="dash-ask-panel" onClick={(e) => e.stopPropagation()}>
        <div className="dash-ask-head">
          <div className="dash-ask-head-copy">
            <span className="dash-ask-title">
              <span className="dash-sq">
                <TrovisMark size={10} />
              </span>
              Ask
            </span>
            <span className="dash-ask-sub">Answers from your work record</span>
          </div>
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
              <div className="dash-ask-suggest">
                {suggestions.map((s) => (
                  <button
                    key={s.query}
                    type="button"
                    className="dash-suggest-pill"
                    title={s.title || undefined}
                    onClick={() => send(s.query)}
                  >
                    {s.label}
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
              placeholder="Ask what's waiting or why something's stuck…"
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
