// Behaviour for the public waitlist landing (markup: landing.html).
//
// Plain DOM, no framework, so the same file runs in both front doors: the
// SPA mounts it from TrovisLanding.jsx (passing the api.js calls), and the
// FastAPI-served trovisai.com page imports it as a module (using the
// same-origin fetch defaults below).
//
// The one rule the signup flow keeps: nothing says "you're on the list"
// until the server has answered that the email is saved.

export const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

const ATTRIBUTION_PARAMS = ['ref', 'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content']
const GENERIC_ERROR = 'Something went wrong and you’re not on the list yet. Please try again.'
const NETWORK_ERROR = 'We couldn’t reach Trovis. Check your connection and try again.'
const JOINED_TEXT = 'You’re on the list. We’ll reach out with early-access updates.'
const ALREADY_TEXT = 'You’re already on the list. We’ll reach out with early-access updates.'

/** Referral + UTM fields from the landing URL, plus an off-site referrer. */
export function readAttribution(loc = window.location, doc = document) {
  const out = {}
  let params
  try {
    params = new URLSearchParams(loc.search || '')
  } catch {
    params = new URLSearchParams()
  }
  for (const key of ATTRIBUTION_PARAMS) {
    const value = (params.get(key) || '').trim()
    if (value) out[key] = value.slice(0, 500)
  }
  try {
    const ref = doc.referrer ? new URL(doc.referrer) : null
    if (ref && ref.host !== loc.host) out.referrer = ref.href.slice(0, 500)
  } catch {
    // An unparseable referrer is just no referrer.
  }
  return out
}

/**
 * Analytics hook. Trovis has no analytics provider installed yet, so this
 * only forwards to one if the page has it: a GTM-style `window.dataLayer`,
 * and a `trovis:landing` DOM event anything else can listen for. Callers
 * never pass the email address or any other form text.
 */
export function track(name, props = {}) {
  const payload = { event: `trovis_landing_${name}`, ...props }
  try {
    if (Array.isArray(window.dataLayer)) window.dataLayer.push(payload)
    window.dispatchEvent(new CustomEvent('trovis:landing', { detail: payload }))
  } catch {
    // Analytics must never break the page.
  }
}

async function postJson(url, body) {
  let res
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    const err = new Error(NETWORK_ERROR)
    err.status = 0
    throw err
  }
  let data = null
  try {
    data = await res.json()
  } catch {
    // Non-JSON body: fall through to the status check.
  }
  if (!res.ok) {
    const detail = data && data.detail
    const err = new Error(typeof detail === 'string' && detail ? detail : GENERIC_ERROR)
    err.status = res.status
    throw err
  }
  return data || {}
}

export const sameOriginApi = {
  joinWaitlist: (body) => postJson('/waitlist', body),
  saveWaitlistFollowup: (body) => postJson('/waitlist/followup', body),
}

/** A message a visitor can act on — never a raw "HTTP 500" or fetch error. */
function friendlyError(err) {
  if (!err) return GENERIC_ERROR
  if (err.status === 0 || err.code === 'network' || err.code === 'timeout') return NETWORK_ERROR
  const msg = String(err.message || '').trim()
  if (!msg || /^HTTP\b|failed to fetch|networkerror|load failed|abort|\[object/i.test(msg)) return GENERIC_ERROR
  if (err.status >= 500) return GENERIC_ERROR
  return msg
}

function prefersReducedMotion() {
  try {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  } catch {
    return false
  }
}

/**
 * Wire up a tablist: click + arrow keys / Home / End, roving tabindex, and the
 * matching panel shown. Returns select(index, { focus }).
 */
function setupTabs(tabs, on, { onSelect } = {}) {
  const panels = tabs.map((t) => document.getElementById(t.getAttribute('aria-controls')))
  function select(index, { focus = false } = {}) {
    tabs.forEach((tab, i) => {
      const active = i === index
      tab.setAttribute('aria-selected', active ? 'true' : 'false')
      tab.tabIndex = active ? 0 : -1
      const panel = panels[i]
      if (!panel) return
      if (active && panel.hidden) {
        panel.hidden = false
        panel.classList.remove('is-entering')
        void panel.offsetWidth
        panel.classList.add('is-entering')
      } else if (!active) {
        panel.hidden = true
      }
    })
    if (focus) tabs[index].focus()
    if (onSelect) onSelect(index)
  }
  tabs.forEach((tab, i) => {
    on(tab, 'click', () => select(i))
    on(tab, 'keydown', (e) => {
      const last = tabs.length - 1
      let next = null
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown') next = i === last ? 0 : i + 1
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') next = i === 0 ? last : i - 1
      else if (e.key === 'Home') next = 0
      else if (e.key === 'End') next = last
      if (next === null) return
      e.preventDefault()
      select(next, { focus: true })
    })
  })
  return select
}

/**
 * Mount the landing behaviour on `root` (an element containing landing.html).
 * Returns a cleanup function.
 */
export function mountLanding(root, options = {}) {
  const api = {
    joinWaitlist: options.joinWaitlist || sameOriginApi.joinWaitlist,
    saveWaitlistFollowup: options.saveWaitlistFollowup || sameOriginApi.saveWaitlistFollowup,
  }
  const page = root.matches?.('.tl') ? root : root.querySelector('.tl')
  if (!page) return () => {}

  const cleanups = []
  const on = (el, type, fn, opts) => {
    if (!el) return
    el.addEventListener(type, fn, opts)
    cleanups.push(() => el.removeEventListener(type, fn, opts))
  }
  const $ = (sel) => page.querySelector(sel)
  const $$ = (sel) => Array.from(page.querySelectorAll(sel))
  const reduced = prefersReducedMotion()
  const hasIO = typeof window.IntersectionObserver === 'function'

  page.classList.add('tl-js')
  cleanups.push(() => page.classList.remove('tl-js'))

  // Header gains a hairline once the page scrolls under it.
  const header = $('.tl-header')
  const onScroll = () => header && header.classList.toggle('is-stuck', window.scrollY > 8)
  on(window, 'scroll', onScroll, { passive: true })
  onScroll()

  // Sign in: the SPA swaps to its login view; the static page follows the link.
  if (typeof options.onSignIn === 'function') {
    $$('[data-signin]').forEach((a) =>
      on(a, 'click', (e) => {
        e.preventDefault()
        options.onSignIn()
      }),
    )
  }

  // Every "Join the waitlist" CTA lands on the email field.
  const joinSection = $('#join')
  const emailInput = $('#tl-email')
  const successBox = $('[data-success]')
  $$('[data-cta]').forEach((a) =>
    on(a, 'click', (e) => {
      track('cta_click', { location: a.getAttribute('data-cta') })
      if (!joinSection) return
      e.preventDefault()
      joinSection.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'center' })
      const target = successBox && !successBox.hidden ? successBox : emailInput
      if (target) target.focus({ preventScroll: true })
    }),
  )

  // --- Product preview: steps as tabs, evidence chips jump to a step. ---
  const stepTabs = $$('.tl-step')
  const selectStep = stepTabs.length ? setupTabs(stepTabs, on) : null
  $$('[data-goto]').forEach((chip) =>
    on(chip, 'click', () => {
      const index = stepTabs.findIndex((t) => t.dataset.step === chip.dataset.goto)
      if (index >= 0 && selectStep) selectStep(index, { focus: true })
    }),
  )

  // --- Ask examples ---
  const qTabs = $$('.tl-q')
  if (qTabs.length) setupTabs(qTabs, on)

  // --- Motion: reveal the preview as a sequence, sections as they arrive. ---
  const preview = $('#tl-preview')
  const reveals = $$('.tl-reveal')
  const timers = []
  const goLive = () => {
    if (!preview || preview.classList.contains('is-live')) return
    preview.classList.add('is-live')
    timers.push(setTimeout(() => preview.classList.add('is-ready'), reduced ? 0 : 2200))
  }
  if (reduced || !hasIO) {
    if (preview) preview.classList.add('is-live', 'is-ready')
    reveals.forEach((el) => el.classList.add('is-in'))
  } else {
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return
          if (entry.target === preview) goLive()
          else entry.target.classList.add('is-in')
          io.unobserve(entry.target)
        })
      },
      { threshold: 0.15, rootMargin: '0px 0px -40px 0px' },
    )
    if (preview) io.observe(preview)
    reveals.forEach((el) => io.observe(el))
    cleanups.push(() => io.disconnect())
  }
  cleanups.push(() => timers.forEach(clearTimeout))

  // --- Signup ---
  const attribution = readAttribution()
  const joinForm = $('[data-form="join"]')
  const followForm = $('[data-form="followup"]')
  const successText = $('[data-success-text]')
  const moreDone = $('[data-more-done]')
  let followupToken = null
  let joinBusy = false
  let followBusy = false
  let started = false

  const markStarted = () => {
    if (started) return
    started = true
    track('form_start')
  }
  on(emailInput, 'focus', markStarted)
  on(emailInput, 'input', () => {
    markStarted()
    emailInput.removeAttribute('aria-invalid')
  })

  function showError(form, message) {
    const el = form.querySelector('[data-error]')
    if (!el) return
    el.textContent = message || ''
    el.hidden = !message
  }
  function setBusy(form, busy, idleLabel, busyLabel) {
    const btn = form.querySelector('[data-submit]')
    if (!btn) return
    btn.disabled = busy
    btn.textContent = busy ? busyLabel : idleLabel
    form.setAttribute('aria-busy', busy ? 'true' : 'false')
  }

  on(joinForm, 'submit', async (e) => {
    e.preventDefault()
    if (joinBusy) return
    showError(joinForm, '')
    const email = (emailInput.value || '').trim()
    if (!EMAIL_RE.test(email)) {
      emailInput.setAttribute('aria-invalid', 'true')
      showError(joinForm, email ? 'Please enter a valid work email.' : 'Please enter your work email.')
      emailInput.focus()
      return
    }
    const website = (joinForm.querySelector('[name="website"]')?.value || '').trim()
    joinBusy = true
    setBusy(joinForm, true, 'Join the waitlist', 'Joining…')
    try {
      const res = await api.joinWaitlist({
        email,
        website: website || null,
        source: 'landing-waitlist',
        ...attribution,
      })
      const status = res && res.status
      if (status !== 'joined' && status !== 'already_joined') throw new Error(GENERIC_ERROR)
      // Saved: only now does the page say so.
      followupToken = (res && res.followup_token) || null
      joinForm.hidden = true
      successText.textContent = status === 'joined' ? JOINED_TEXT : ALREADY_TEXT
      successBox.hidden = false
      if (followForm) followForm.hidden = !followupToken
      successBox.focus({ preventScroll: true })
      track(status === 'joined' ? 'signup_success' : 'signup_already_joined')
    } catch (err) {
      showError(joinForm, friendlyError(err))
      track('signup_error', { status: err && typeof err.status === 'number' ? err.status : null })
    } finally {
      joinBusy = false
      setBusy(joinForm, false, 'Join the waitlist', 'Joining…')
    }
  })

  const finishFollowup = () => {
    followForm.hidden = true
    followupToken = null
  }
  on($('[data-skip]'), 'click', () => {
    finishFollowup()
    track('followup_skip')
    successBox.focus({ preventScroll: true })
  })
  on(followForm, 'submit', async (e) => {
    e.preventDefault()
    if (followBusy || !followupToken) return
    showError(followForm, '')
    const company = (followForm.querySelector('[name="company"]')?.value || '').trim()
    const aiUse = (followForm.querySelector('[name="ai_use"]')?.value || '').trim()
    if (!company && !aiUse) {
      finishFollowup()
      track('followup_skip')
      successBox.focus({ preventScroll: true })
      return
    }
    followBusy = true
    setBusy(followForm, true, 'Send answers', 'Sending…')
    try {
      await api.saveWaitlistFollowup({ token: followupToken, company: company || null, ai_use: aiUse || null })
      finishFollowup()
      moreDone.hidden = false
      successBox.focus({ preventScroll: true })
      track('followup_saved')
    } catch (err) {
      showError(followForm, friendlyError(err))
    } finally {
      followBusy = false
      setBusy(followForm, false, 'Send answers', 'Sending…')
    }
  })

  return () => cleanups.splice(0).forEach((fn) => fn())
}
