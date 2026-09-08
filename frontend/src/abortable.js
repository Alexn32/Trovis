/**
 * Run `fn` with an AbortSignal. The returned cleanup aborts in-flight work
 * (Dashboard unmount / tab switch / refreshKey change).
 *
 * `isAlive()` is false after cleanup so React state updates are skipped.
 */
export function startAbortable(fn) {
  const controller = new AbortController()
  let alive = true
  fn({
    signal: controller.signal,
    isAlive: () => alive,
  })
  return () => {
    alive = false
    controller.abort()
  }
}
