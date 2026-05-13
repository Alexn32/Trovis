import { createContext, useContext, useEffect, useState } from 'react'

// Theme is one of: 'system' | 'light' | 'dark'.
// 'system' follows the OS preference via prefers-color-scheme.
// The actual applied theme (always 'light' or 'dark') is written to
// <html data-theme="..."> so every CSS variable swaps in lock-step.

const ThemeContext = createContext({
  theme: 'system',
  resolved: 'dark',
  setTheme: () => {},
  cycle: () => {},
})

const LS_KEY = 'oversee_theme'

function readStoredTheme() {
  try {
    const v = localStorage.getItem(LS_KEY)
    if (v === 'light' || v === 'dark' || v === 'system') return v
  } catch {
    // localStorage may be unavailable in sandbox / private mode.
  }
  return 'system'
}

function writeStoredTheme(theme) {
  try {
    localStorage.setItem(LS_KEY, theme)
  } catch {
    // ignore
  }
}

function osPrefersDark() {
  if (typeof window === 'undefined' || !window.matchMedia) return true
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

function resolve(theme) {
  if (theme === 'system') return osPrefersDark() ? 'dark' : 'light'
  return theme
}

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(() => readStoredTheme())
  const [resolved, setResolved] = useState(() => resolve(readStoredTheme()))

  // Apply the resolved theme to <html>.
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', resolved)
  }, [resolved])

  // Recompute resolved whenever theme changes.
  useEffect(() => {
    setResolved(resolve(theme))
  }, [theme])

  // When the user picks 'system', listen for OS changes so the app
  // follows along live (no reload needed).
  useEffect(() => {
    if (theme !== 'system' || typeof window === 'undefined' || !window.matchMedia) {
      return undefined
    }
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = () => setResolved(mq.matches ? 'dark' : 'light')
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [theme])

  function setTheme(next) {
    setThemeState(next)
    writeStoredTheme(next)
  }

  // Cycle order matches the icon button: system → light → dark → system
  function cycle() {
    setTheme(theme === 'system' ? 'light' : theme === 'light' ? 'dark' : 'system')
  }

  return (
    <ThemeContext.Provider value={{ theme, resolved, setTheme, cycle }}>
      {children}
    </ThemeContext.Provider>
  )
}

export function useTheme() {
  return useContext(ThemeContext)
}
