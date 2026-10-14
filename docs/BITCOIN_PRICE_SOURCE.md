# Bitcoin spot-price source contract

Northstar values Bitcoin from exactly one explicitly documented, read-only, public market-data
endpoint. This document records public provider behavior only. No credential, identifier, account
detail, holding quantity, or financial value may ever enter this document or the repository.

## Selected source

- Source key: `coinbase_exchange_btc_usd_ticker_v1`.
- Endpoint: `GET https://api.exchange.coinbase.com/products/BTC-USD/ticker`.
- The endpoint is unauthenticated public market data. Northstar sends no `Authorization` header,
  cookie, query parameter, request body, or identifier of any kind, so contacting the source
  reveals nothing about the household beyond the request itself.
- The documented response includes a decimal `price` string and an ISO-8601 `time` member. The
  timestamp is what makes this endpoint preferable to a bare spot-price endpoint: provenance and
  freshness must come from the provider's own observation time, not from local receipt time.
- This is the only permitted source. Changing it is a reviewed code change to
  `northstar/integrations/bitcoin.py`, not configuration, and it invalidates the recorded contract
  above.

## Boundary

- The client exposes exactly one method and has no generic request escape hatch. It cannot
  represent a wallet, custody operation, transfer, deposit, withdrawal, or purchase.
- Responses stream under a 64-KiB cap with a fixed 10-second timeout, require the
  `application/json` content type, and are parsed with `Decimal`. No raw response is retained.
- A missing `price` or `time` member, a zero, negative, non-numeric, non-finite, or implausibly
  large price, a naive or unparsable timestamp, a pre-genesis timestamp, and a timestamp beyond the
  five-minute future-skew boundary all fail closed. An unusable observation is never represented,
  so a failed read cannot become a zero valuation.
- Failures translate to allowlisted privacy-safe codes only: `spot_timeout`, `spot_unavailable`,
  `spot_rate_limited`, `spot_response_too_large`, `spot_response_invalid`, and the
  `spot_validation_*` categories for response envelope, missing price, malformed price, and
  observation time. No code carries a price, timestamp, or provider message.
- An old but valid observation is returned with its true observation time. Staleness, outage
  handling, preservation of the last valid observation, and manual fallback are the consuming
  service's responsibility, not the transport's.

## Current status

- The transport is dormant. It has no route, button, operator stage, scheduler, startup hook,
  automatic refresh, Dashboard adapter, or Plan input.
- The reviewed price checkpoint service consumes it through an injected reader. The source is
  contacted only when a Bitcoin security is actually held; holding Bitcoin with no configured source
  fails closed as `spot_source_not_configured` rather than valuing it at zero.
- Manual Bitcoin price entry with explicit provenance remains available and is the documented
  fallback whenever this source is unavailable, stale, or rejected. Manual and live observations are
  never silently merged: a manual value is accepted only for a security the live source could not
  price, and it carries its own provenance into the checkpoint.
