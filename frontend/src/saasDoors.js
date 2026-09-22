// The work-system doors: which API calls each OAuth connector uses, and the
// copy the product may say about it. Pure data + thunks, no React.
//
// One module, two readers: the Connections page rows and the guided Connect
// flow's inline card (ConnectGuide.jsx) — so "Connect Shopify" behaves the
// same whether it is clicked on the page or offered in the chat. Thunks, not
// bound references, so a test can stub `api.*` after this module loads.
//
// Copy rule (pinned by connectionsMount.test.mjs): the fineprint states the
// truth boundary in product words and never carries implementation details
// such as the link key; that is taught where the agent is set up.

import { api } from './api.js'
import { getConnector } from './connectors.js'

// One row per work system: which API calls its door uses. Thunks, not bound
// references, so a test can stub `api.*` after this module loads.
export const SAAS_DOORS = {
  stripe: {
    start: () => api.startStripeConnect(),
    disconnect: () => api.disconnectStripe(),
    configured: (d) => !!d?.stripe_oauth_configured,
    notConfigured: 'Stripe Connect isn’t configured on this deploy yet.',
    startError: 'Could not start Stripe Connect.',
    disconnectError: 'Could not disconnect Stripe.',
    fineprint:
      'Stripe helps Trovis understand payment and refund outcomes related to work. Trovis only '
      + 'associates Stripe activity with work it can reliably link. This is not Trovis billing.',
  },
  hubspot: {
    start: () => api.startHubSpotConnect(),
    disconnect: () => api.disconnectHubSpot(),
    configured: (d) => !!d?.hubspot_oauth_configured,
    notConfigured: 'HubSpot Connect isn’t configured on this deploy yet.',
    startError: 'Could not start HubSpot Connect.',
    disconnectError: 'Could not disconnect HubSpot.',
    fineprint:
      'HubSpot helps Trovis understand deal and ticket changes related to work. Trovis only '
      + 'associates HubSpot activity with work it can reliably link. This is not CRM or contact sync.',
  },
  shopify: {
    start: (shop) => api.startShopifyConnect(shop),
    disconnect: () => api.disconnectShopify(),
    configured: (d) => !!d?.shopify_oauth_configured,
    notConfigured: 'Shopify Connect isn’t configured on this deploy yet.',
    startError: 'Could not start Shopify Connect.',
    disconnectError: 'Could not disconnect Shopify.',
    needsShop: true,
    shopPlaceholder: 'your-store.myshopify.com',
    fineprint:
      'Shopify helps Trovis understand order, payment, refund, and fulfillment outcomes related '
      + 'to work. Trovis only associates Shopify activity with work it can reliably link. This is '
      + 'not catalog, product, or customer sync.',
  },
}

/** The door for a registry id, or null when the connector has no OAuth door. */
export function doorFor(connectorId) {
  const c = getConnector(connectorId)
  if (!c || c.setup_type !== 'oauth') return null
  return SAAS_DOORS[c.id] || null
}
