// Test-only stand-in for "openclaw/plugin-sdk/plugin-entry" (aliased in via
// esbuild --alias for the test bundle). The real definePluginEntry just
// registers metadata with the gateway; for tests the identity function is
// all we need — the suite calls plugin.register() with a fake api itself.
export function definePluginEntry<T>(def: T): T {
  return def
}
