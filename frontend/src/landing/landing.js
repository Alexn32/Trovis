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
const JOINED_TITLE = 'You’re on the early-access list.'
const ALREADY_TITLE = 'You’re already on the early-access list.'
const FOLLOWUP_BODY = 'We’ll reach out as access opens. Tell us what you’d connect so we can better understand your team’s needs.'
const PLAIN_BODY = 'We’ll reach out as access opens.'

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

  // --- Product preview: workflow → its latest run → one recorded step. ---
  const wfButtons = $$('.tl-wf')
  const runs = $$('[data-run]')
  const expls = $$('[data-expl]')
  const evButtons = $$('.tl-ev-btn')
  // Each run opens on the step its markup marks as selected.
  const defaultStep = {}
  runs.forEach((run) => {
    const pressed = run.querySelector('.tl-ev-btn[aria-pressed="true"]')
    if (pressed) defaultStep[run.dataset.run] = pressed.dataset.ev
  })

  function selectStep(evId, { focus = false } = {}) {
    const btn = evButtons.find((b) => b.dataset.ev === evId)
    if (!btn) return
    const run = btn.closest('[data-run]')
    run.querySelectorAll('.tl-ev-btn').forEach((b) => b.setAttribute('aria-pressed', b === btn ? 'true' : 'false'))
    run.querySelectorAll('[data-detail]').forEach((d) => {
      d.hidden = d.dataset.detail !== evId
    })
    if (focus) btn.focus()
  }
  function selectWorkflow(id) {
    wfButtons.forEach((b) => b.setAttribute('aria-pressed', b.dataset.wf === id ? 'true' : 'false'))
    runs.forEach((r) => {
      r.hidden = r.dataset.run !== id
    })
    expls.forEach((x) => {
      x.hidden = x.dataset.expl !== id
    })
    if (defaultStep[id]) selectStep(defaultStep[id])
  }

  wfButtons.forEach((b) =>
    on(b, 'click', () => {
      if (b.getAttribute('aria-pressed') === 'true') return
      selectWorkflow(b.dataset.wf)
      track('preview_workflow', { workflow: b.dataset.wf })
    }),
  )
  evButtons.forEach((b) => {
    on(b, 'click', () => {
      selectStep(b.dataset.ev)
      track('preview_step', { step: b.dataset.ev })
    })
    // Up/Down walk the recorded steps of the open run.
    on(b, 'keydown', (e) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return
      const list = Array.from(b.closest('[data-run]').querySelectorAll('.tl-ev-btn'))
      const i = list.indexOf(b)
      const next = list[e.key === 'ArrowDown' ? Math.min(i + 1, list.length - 1) : Math.max(i - 1, 0)]
      e.preventDefault()
      selectStep(next.dataset.ev, { focus: true })
    })
  })
  // Evidence in an explanation opens the step it cites.
  $$('[data-goto]').forEach((chip) =>
    on(chip, 'click', () => {
      selectStep(chip.dataset.goto, { focus: true })
      track('preview_evidence', { step: chip.dataset.goto })
    }),
  )

  // --- Motion: sections fade in as they arrive. ---
  const reveals = $$('.tl-reveal')
  if (reduced || !hasIO) {
    reveals.forEach((el) => el.classList.add('is-in'))
  } else {
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return
          entry.target.classList.add('is-in')
          io.unobserve(entry.target)
        })
      },
      { threshold: 0.15, rootMargin: '0px 0px -40px 0px' },
    )
    reveals.forEach((el) => io.observe(el))
    cleanups.push(() => io.disconnect())
  }

  // --- Signup: the same form in the hero and the closing section. Joining
  // in either marks both as joined; the optional questions are asked once,
  // where the visitor signed up. ---
  const attribution = readAttribution()
  const blocks = $$('[data-signup]').map((el) => ({
    el,
    where: el.dataset.signup,
    joinForm: el.querySelector('[data-form="join"]'),
    email: el.querySelector('input[name="email"]'),
    success: el.querySelector('[data-success]'),
    successTitle: el.querySelector('[data-success-title]'),
    successBody: el.querySelector('[data-success-body]'),
    followForm: el.querySelector('[data-form="followup"]'),
    moreDone: el.querySelector('[data-more-done]'),
  }))
  let followupToken = null
  let joinBusy = false
  let followBusy = false
  let started = false

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

  // Every "Request early access" CTA lands on an email field (or the success
  // message, once joined).
  $$('[data-cta]').forEach((a) =>
    on(a, 'click', (e) => {
      track('cta_click', { location: a.getAttribute('data-cta') })
      const block = blocks.find((b) => b.where === a.getAttribute('data-cta-target')) || blocks[0]
      if (!block) return
      e.preventDefault()
      block.el.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'center' })
      const target = block.success && !block.success.hidden ? block.success : block.email
      if (target) target.focus({ preventScroll: true })
    }),
  )

  blocks.forEach((block) => {
    const { joinForm, email, followForm } = block
    const markStarted = () => {
      if (started) return
      started = true
      track('form_start', { location: block.where })
    }
    on(email, 'focus', markStarted)
    on(email, 'input', () => {
      markStarted()
      email.removeAttribute('aria-invalid')
    })

    on(joinForm, 'submit', async (e) => {
      e.preventDefault()
      if (joinBusy) return
      showError(joinForm, '')
      const value = (email.value || '').trim()
      if (!EMAIL_RE.test(value)) {
        email.setAttribute('aria-invalid', 'true')
        showError(joinForm, value ? 'Please enter a valid work email.' : 'Please enter your work email.')
        email.focus()
        return
      }
      const website = (joinForm.querySelector('[name="website"]')?.value || '').trim()
      joinBusy = true
      setBusy(joinForm, true, 'Request early access', 'Requesting…')
      try {
        const res = await api.joinWaitlist({
          email: value,
          website: website || null,
          source: 'landing-waitlist',
          ...attribution,
        })
        const status = res && res.status
        if (status !== 'joined' && status !== 'already_joined') throw new Error(GENERIC_ERROR)
        // Saved: only now does the page say so — in every signup block.
        followupToken = (res && res.followup_token) || null
        blocks.forEach((b) => {
          const askHere = Boolean(followupToken) && b === block
          b.joinForm.hidden = true
          b.successTitle.textContent = status === 'joined' ? JOINED_TITLE : ALREADY_TITLE
          b.successBody.textContent = askHere ? FOLLOWUP_BODY : PLAIN_BODY
          b.success.hidden = false
          if (b.followForm) b.followForm.hidden = !askHere
        })
        block.success.focus({ preventScroll: true })
        track(status === 'joined' ? 'signup_success' : 'signup_already_joined', { location: block.where })
      } catch (err) {
        showError(joinForm, friendlyError(err))
        track('signup_error', { location: block.where, status: err && typeof err.status === 'number' ? err.status : null })
      } finally {
        joinBusy = false
        setBusy(joinForm, false, 'Request early access', 'Requesting…')
      }
    })

    if (!followForm) return
    const finishFollowup = () => {
      followForm.hidden = true
      followupToken = null
      block.successBody.textContent = PLAIN_BODY
    }
    on(block.el.querySelector('[data-skip]'), 'click', () => {
      finishFollowup()
      track('followup_skip')
      block.success.focus({ preventScroll: true })
    })
    on(followForm, 'submit', async (e) => {
      e.preventDefault()
      if (followBusy || !followupToken) return
      showError(followForm, '')
      const company = (followForm.querySelector('[name="company"]')?.value || '').trim()
      const tools = (followForm.querySelector('[name="tools"]')?.value || '').trim()
      if (!company && !tools) {
        finishFollowup()
        track('followup_skip')
        block.success.focus({ preventScroll: true })
        return
      }
      followBusy = true
      setBusy(followForm, true, 'Share with the team', 'Saving…')
      try {
        await api.saveWaitlistFollowup({ token: followupToken, company: company || null, tools: tools || null })
        finishFollowup()
        // The thank-you line says it all; drop the now-repeated body.
        block.successBody.hidden = true
        block.moreDone.hidden = false
        block.success.focus({ preventScroll: true })
        track('followup_saved')
      } catch (err) {
        showError(followForm, friendlyError(err))
      } finally {
        followBusy = false
        setBusy(followForm, false, 'Share with the team', 'Saving…')
      }
    })
  })

  return () => cleanups.splice(0).forEach((fn) => fn())
}
