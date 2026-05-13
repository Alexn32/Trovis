import { useEffect, useRef, useState } from 'react'
import { SendIcon, SparkleIcon } from './Icons.jsx'

// Conversational interface. The backend doesn't have an /ask endpoint
// yet, so this is wired to a local placeholder that just acknowledges the
// question. The UI is in place to swap in a real backend call later.

const SUGGESTIONS = [
  'Which agents had the most errors today?',
  'What does my fleet look like overall?',
  'Are any agents misconfigured?',
  'Which agent is the slowest?',
  'How many tool calls did we make this week?',
  'Show me agents that have stopped reporting.',
]

export default function Ask({ seedQuestion, clearSeed }) {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  const threadRef = useRef(null)

  // A question piped in from AgentDetail → autosubmit once.
  useEffect(() => {
    if (seedQuestion) {
      submit(seedQuestion)
      clearSeed?.()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedQuestion])

  useEffect(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight
    }
  }, [messages, pending])

  function submit(text) {
    const q = (text ?? input).trim()
    if (!q) return
    setMessages((m) => [...m, { role: 'user', text: q }])
    setInput('')
    setPending(true)
    // Placeholder response — wired-up answer comes when the backend
    // grows an /ask endpoint. Keep this distinguishable so demos don't
    // mistake it for real output.
    setTimeout(() => {
      setMessages((m) => [
        ...m,
        {
          role: 'assistant',
          text:
            "I can't answer questions yet — the backend's /ask endpoint isn't built. " +
            'For now, the dashboard cards and the Agent Detail view show the same ' +
            'underlying data this view will eventually reason over (descriptions, ' +
            'registrations, span counts, error rates).',
        },
      ])
      setPending(false)
    }, 300)
  }

  function onSubmit(e) {
    e.preventDefault()
    submit()
  }

  if (messages.length === 0) {
    return (
      <div className="ask-shell">
        <div className="ask-empty">
          <span className="ask-empty-icon">
            <SparkleIcon size={24} />
          </span>
          <h2>Ask anything about your agents</h2>
          <p className="ask-empty-subtitle">
            Full context across every agent — what they do, how they're
            performing, where the errors are.
          </p>
          <div className="ask-suggestions">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                className="ask-suggestion"
                onClick={() => submit(s)}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
        <AskInputRow
          value={input}
          onChange={setInput}
          onSubmit={onSubmit}
          pending={pending}
        />
      </div>
    )
  }

  return (
    <div className="ask-shell">
      <div className="ask-thread" ref={threadRef}>
        {messages.map((m, i) => (
          <div key={i} className={`ask-message ${m.role}`}>
            <div className="ask-bubble">{m.text}</div>
          </div>
        ))}
        {pending && (
          <div className="ask-message assistant">
            <div className="ask-bubble">
              <span className="spinner" /> Thinking…
            </div>
          </div>
        )}
      </div>
      <AskInputRow
        value={input}
        onChange={setInput}
        onSubmit={onSubmit}
        pending={pending}
      />
    </div>
  )
}

function AskInputRow({ value, onChange, onSubmit, pending }) {
  return (
    <form className="ask-input-row" onSubmit={onSubmit}>
      <input
        className="ask-input"
        type="text"
        placeholder="Ask anything about your agents…"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoFocus
      />
      <button
        type="submit"
        className="ask-send"
        disabled={pending || !value.trim()}
        aria-label="Send"
      >
        <SendIcon size={16} />
      </button>
    </form>
  )
}
