import './migrate.js' // must run before any module reads localStorage
import React from 'react'
import ReactDOM from 'react-dom/client'
import * as Sentry from '@sentry/react'
import App from './App.jsx'
import './styles.css'

// Error monitoring — fail-soft. Only initializes when VITE_SENTRY_DSN is set
// at build time; otherwise it's a no-op so local/dev builds run untouched.
if (import.meta.env.VITE_SENTRY_DSN) {
  Sentry.init({
    dsn: import.meta.env.VITE_SENTRY_DSN,
    environment: import.meta.env.VITE_SENTRY_ENVIRONMENT || 'production',
    sendDefaultPii: false,
  })
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
