import { Component } from 'react'

/**
 * Stops one broken page from blanking the app.
 *
 * React unmounts the whole tree when a render throws and nothing catches it,
 * so a single bad row anywhere left the user staring at a white screen with
 * no back button and nothing in the UI saying what happened — which is how
 * "I click it and get nothing but a blank screen" gets reported.
 *
 * Wrap each pane and the overlay separately: a crash in Work must not take
 * Home down with it, and closing a crashed overlay must return to a working
 * page underneath.
 *
 * This is a backstop, not an excuse. A boundary that trips is still a bug —
 * it just fails legibly, and keeps the rest of the app usable.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // Keep the stack in the console — the visible copy stays plain English.
    console.error(`[${this.props.label || 'app'}] render failed`, error, info)
  }

  render() {
    const { error } = this.state
    if (!error) return this.props.children

    const { label = 'this page', onReset } = this.props
    return (
      <div className="crash-pane" role="alert">
        <h2 className="crash-title">Something went wrong on {label}.</h2>
        <p className="crash-body">
          Nothing was lost. Reload to try again, or move to another tab — the
          rest of Trovis is still working.
        </p>
        <div className="crash-actions">
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => {
              this.setState({ error: null })
              if (onReset) onReset()
            }}
          >
            Try again
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => window.location.reload()}>
            Reload Trovis
          </button>
        </div>
        <p className="crash-detail">{String(error?.message || error)}</p>
      </div>
    )
  }
}
