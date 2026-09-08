// Opening the Ask pill from elsewhere in the app.
//
// AskPill owns its own state and is mounted once at the shell level, so a
// detail pane deep in the tree has no handle on it. Rather than lift that
// state (an Ask rewrite) or thread a prop through every surface, the pill
// listens for one window event. Small, and it keeps Ask's internals its own.

export const ASK_EVENT = 'trovis:ask'

/** Open the Ask pill, optionally with a question already asked. */
export function openAsk(question = '') {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(ASK_EVENT, { detail: { question } }))
}
