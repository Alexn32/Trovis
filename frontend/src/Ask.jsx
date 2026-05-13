import { useEffect, useRef, useState } from 'react'
import { api } from './api.js'
import { SendIcon, SparkleIcon } from './Icons.jsx'

// Conversational interface, wired to POST /ask. Stateless on the backend:
// the full thread ships with every request, which is how we get multi-turn
// follow-ups ("how many spans does it have?") to work without server-side
// session state.

const SUGGESTIONS = [
  'Which agents had the most errors today?',
  'What does my fleet look like overall?',
  'Are any agents misconfigured?',
  'Which agent is the slowest?',
  'How many tool calls did we make this week?',
  'Show me agents that have stopped reporting.',
]

export default function Ask() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  const threadRef = useRef(null)

  useEffect(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight
    }
  }, [messages, pending])

  async function submit(text) {
    const q = (text ?? input).trim()
    if (!q) return
    // Build the next thread (user turn appended). We submit this exact
    // thread to the backend so it has the full conversational context.
    const nextThread = [...messages, { role: 'user', content: q }]
    setMessages(nextThread)
    setInput('')
    setPending(true)
    try {
      const res = await api.ask(
        nextThread.map((m) => ({ role: m.role, content: m.content })),
      )
      setMessages((m) => [...m, { role: 'assistant', content: res.answer }])
    } catch (e) {
      setMessages((m) => [
        ...m,
        {
          role: 'assistant',
          content: `Couldn't answer: ${e.message}`,
          error: true,
        },
      ])
    } finally {
      setPending(false)
    }
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
            <div className="ask-bubble">{m.content}</div>
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
