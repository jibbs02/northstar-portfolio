# Schwab read-only integration contract

This contract was verified against the signed-in Schwab Developer Portal documentation and OpenAPI
specifications for the production **Accounts and Trading** and **Market Data** APIs on 2026-08-16.
It records public provider behavior only. App identifiers, client credentials, tokens, account
identifiers, provider payloads, and financial values must never enter this document or the repository.

## OAuth

- Authorization uses the OAuth 2 authorization-code grant.
- Authorization endpoint: `https://api.schwabapi.com/v1/oauth/authorize`.
- Token endpoint: `https://api.schwabapi.com/v1/oauth/token`.
- The authorization request documents `client_id` and the exact registered HTTPS `redirect_uri`.
- Northstar also sends a cryptographically random, one-time `state` value and rejects callbacks that
  do not return the exact initiating value. This is a fail-closed interoperability check because the
  Schwab documentation does not mention `state` explicitly. Its documented landing URL contains
  `code` and a provider `session` value; Northstar does not treat that provider session as CSRF state.
- Registered callback URLs must use HTTPS, may be comma-separated, and share a 255-character field
  limit. Northstar registers and accepts one exact production callback only.
- The token request uses HTTP Basic authentication with the Client ID and Client Secret plus an
  `application/x-www-form-urlencoded` body.
- The initial request sends `grant_type=authorization_code`, the decoded authorization code, and the
  exact redirect URI.
- Refresh sends `grant_type=refresh_token` and the current refresh token. Schwab returns a new access
  token and refresh token; replacement is atomic.
- Access tokens are documented as valid for 30 minutes. Refresh tokens are documented as valid for
  seven days, after which the complete consent flow must be repeated.
- The documented response scope is `api` and the token type is `Bearer`. The optional `id_token` is
  not needed by Northstar and is discarded.
- Schwab does not document a narrower read-only OAuth scope. The `api` grant covers the subscribed
  Accounts and Trading product, whose specification includes order endpoints. Northstar's read-only
  boundary is enforced by the complete absence of order code plus the app's zero order throttle, not
  by a provider-issued read-only token.
- The current documentation does not describe PKCE or a revocation endpoint. Northstar does not invent
  either behavior. Disconnect deletes local token material; remote revocation will be added only if
  Schwab documents support.

### Implemented callback boundary

- `POST /integrations/schwab/connect` requires the normal authenticated session and CSRF token.
- Each attempt invalidates earlier unfinished attempts and creates a 10-minute random state value.
  Only keyed hashes of the state and opaque browser-session binding are stored.
- The connect response is a no-store confirmation document with the authorization URL only in the
  **Continue to Schwab** link target. The URL, state, client ID, and query parameters are never shown
  as page text or logged. The separate link navigation prevents Schwab's internal redirects from
  remaining part of the CSP-governed form-submission chain.
- Every response retains `form-action 'self'`. The confirmation document preserves
  `Referrer-Policy: no-referrer` and provides an explicit return to Settings; starting again replaces
  any unfinished attempt.
- `GET /integrations/schwab/callback` remains behind normal Northstar identity verification. It
  rejects missing, repeated, expired, replayed, oversized, or wrong-session parameters before token
  exchange and never displays provider error text.
- A valid state is consumed atomically before the authorization code is exchanged. The callback then
  redirects immediately to a clean settings URL so the code and state leave browser history.
- Successful grants are saved only while their consumed OAuth attempt remains current, use the
  existing encrypted token bundle, and rotate the Northstar session.
- Disconnect is CSRF-protected, invalidates unfinished authorization attempts, rotates the session,
  and deletes local token material. A concurrent callback cannot reconnect after disconnect wins.
- The reviewed discovery slice calls only `GET /trader/v1/accounts/accountNumbers`. It streams at most
  64 KiB, requires `application/json`, accepts at most 50 exact `{accountNumber, hashValue}` records,
  and rejects malformed, duplicate, or unexpected records atomically. Full account numbers exist only
  during in-memory parsing; Northstar retains an optional masked suffix and the opaque hash.
- Discovery creates a five-minute, no-store, server-memory preview. Mapping choices are explicit,
  CSRF-protected, one-time, and committed atomically; cancel, expiry, conflict, or failure preserves
  all existing mappings. No balances, positions, transactions, or market data are requested.
- The balances-and-positions adapter calls only the documented
  `GET /trader/v1/accounts/{accountHash}?fields=positions` endpoint for an already-mapped opaque
  hash. It has no route, button, scheduler, automatic refresh, or application invocation path. It
  streams a bounded response, stores no raw provider response, and normalizes only the values needed
  for the existing snapshot service. The response account number is validated then discarded; the
  encrypted local mapping remains authoritative for tax registration. The signed-in Schwab Account
  Access and User Preferences 1.0.0 OAS3 specification was rechecked on 2026-08-16: CASH
  `currentBalances` uses `totalCash` plus
  `longNonMarginableMarketValue`, while MARGIN uses `equity` plus `longMarginValue`. Neither schema
  documents `liquidationValue` or `cashBalance`; those names are not accepted as a production
  balance representation. A missing supported representation and a partial representation produce
  distinct privacy-safe failure categories. Negative cash, nonzero borrowing/short balances, and
  short positions are rejected. Provider `EQUITY` is compatible with local equity or ETF
  classifications. A documented `COLLECTIVE_INVESTMENT` additionally requires its subtype;
  `EXCHANGE_TRADED_FUND` is compatible only with a known local ETF security. The service validates
  every mapped account before one immutable snapshot is published and retains the prior published
  snapshot on timeout, rate limit, malformed/partial data, reconciliation failure, or unknown
  securities.
- Snapshot response diagnostics use privacy-safe validation categories for response envelope,
  account identity/mechanics, missing/malformed balance variants, position structure,
  instrument identity, optional instrument metadata, instrument type, numeric value, and unsupported
  borrowing/short position. The historical combined instrument identity/type category remains
  allowlisted for existing audit rows but is no longer emitted. Categories never contain an account,
  position index, symbol, value, field content, or raw payload.
- The snapshot parser ignores additive provider fields, accepts a bounded nonnumeric account-number
  string, and treats an omitted or null positions member as an empty position set. Position
  description and zero short quantity may be omitted or null. CUSIP, symbol, long quantity, market
  value, average price, and a supported instrument type remain required for every actual position.
  Missing, partial, or malformed balance variants, unknown mechanics/types, ambiguous values, and
  unsupported borrowing/short semantics continue to fail closed. Undocumented additive fields are
  ignored rather than treated as alternate financial values.
- The documented position `instrument.assetType` enum is `EQUITY`, `MUTUAL_FUND`, `OPTION`,
  `FUTURE`, `FOREX`, `INDEX`, `CASH_EQUIVALENT`, `FIXED_INCOME`, `PRODUCT`, `CURRENCY`, and
  `COLLECTIVE_INVESTMENT`. Northstar accepts only `EQUITY`, `MUTUAL_FUND`, `FIXED_INCOME`,
  `CASH_EQUIVALENT`, and `COLLECTIVE_INVESTMENT`; the last is accepted only with the documented
  `EXCHANGE_TRADED_FUND` subtype. Other collective-investment subtypes, options, futures, foreign
  exchange, currencies, indexes without held-security semantics, generic products, and unknown names
  remain unsupported.
  Missing, null, empty, whitespace, non-text, or otherwise malformed types use a separate
  privacy-safe category from bounded unsupported names. Collective-investment subtypes use the same
  malformed-versus-unsupported distinction; no category includes the supplied value.
- Published snapshot records contain normalized decimal values, blind-indexed provider instrument
  keys, explicit freshness/provenance/reconciliation state, and privacy-safe audit metadata. They
  are intentionally not wired into Plan generation in this slice.
- The snapshot coordinator loads the encrypted grant, refreshes an expiring access token,
  resolves rotating-refresh-token races with an atomic compare-and-swap, reads only active mapped
  account hashes through `HttpSchwabReadClient`, and delegates all-or-nothing publication to
  `SchwabSnapshotService`. It has no route, button, scheduler, or startup hook.
- Expired or revoked grants, refresh races, partial account failures, cancellation, and mapping or
  connection changes fail the sync without replacing the last published snapshot.
- A successfully rotated refresh token remains authoritative even when later response validation
  fails. Operational recovery must not restore an older database merely to roll back a failed
  snapshot attempt; pre-operation backups are retained as recovery evidence unless a separate
  credential-aware recovery decision is made.
- The only live invocation boundary is the host-local `run_schwab_live_snapshot.py` operator command.
  Its interactive authorization stores an owner-only, 15-minute expected mapping count and a fresh
  zero-order-throttle confirmation; execution consumes that authorization before provider work.
- Execution requires a clean reviewed checkout to match the running image's immutable commit label,
  full production preflight and schema compatibility to pass, and the newest encrypted backup to have
  been created after the portal confirmation, be no more than 30 minutes old, and pass another
  isolated restore drill. A cross-process lock prevents a concurrent run. Replaying execution
  requires a new interactive authorization.
- The command selects the sole connected Schwab connection without accepting a connection or account
  identifier, pins the exact mapping identities and connection version, and fails before token
  acquisition when the active count or pinned state differs. The pinned set is revalidated during
  atomic publication, and the outbound adapter rejects non-allowlisted identities or requests beyond
  the expected maximum before transmission. The command permits at most one token refresh, has no
  retry loop, and has no generic provider-request escape hatch.
- Authorization expiry is checked again at the loader boundary, immediately before token acquisition
  or an account request. Recovery validation cannot extend a stale portal confirmation into provider
  work. Backup freshness uses the canonical timestamp in the finalized encrypted-backup name—not
  mutable filesystem mtime—and requires a timestamp strictly after confirmation and not in the future.
- Terminal output contains only the deployed commit, execution time, expected/observed counts,
  refresh/read counts, atomic-publication and reconciliation results, and the disallowed-endpoint
  assertion. Snapshot contents remain disconnected from Dashboard and Plan.

### Production verification boundary

- Before credentials are requested, the operator must verify in the Schwab Developer Portal that the
  app's PUT/POST/DELETE order throttle is exactly zero and type `ZERO` into the setup command. The
  enabled host configuration fails closed without this external attestation. Record only pass/fail.
- Any provider-app configuration change invalidates the operational attestation. Disable Northstar
  first, then repeat the portal verification and setup command before credentials or live OAuth are
  enabled again.
- Provider credentials are entered only through hidden prompts into the external owner-only production
  secrets file. They are never accepted as command-line arguments.
- The host updater validates the same exact Schwab variables that Compose passes unmodified to the
  container. An enabled configuration fails closed on a missing credential or callback mismatch.
- The first controlled live account read remains a separate production acceptance procedure after
  this operator entrypoint is reviewed, merged, and deployed. Development, review, merge, and ordinary
  deployment use synthetic data only and must not execute the command.
- A failed state echo or token exchange blocks account discovery. The integration can be disabled and
  its provider credentials removed from the external file before another reviewed deployment.

### Dormant transaction transport

- The signed-in Schwab Account Access and User Preferences 1.0.0 OAS3 specification was rechecked on
  2026-08-16. The list endpoint requires an opaque account hash, paired ISO-8601 `startDate` and
  `endDate` values, a transaction-type filter, limits a request to one year, and returns at most 3,000
  records without a documented pagination cursor.
- Northstar requests one explicit window and all documented transaction types, applies a four-MiB
  streaming response cap and the existing fixed timeout, and rejects a 3,000-record response as
  possibly truncated. It validates only documented enums and bounded structures, discards the full
  account number and all unused response members, and returns privacy-safe normalized provider
  records with source, trade, settlement, and import timestamps kept separately.
- The signed-in transaction schemas were rechecked on 2026-08-16. Transfer items expose a documented
  instrument, amount, cost, price, 16-value fee-type enum, and `OPENING`, `CLOSING`, `AUTOMATIC`, or
  `UNKNOWN` position effect. Northstar retains only those bounded members needed for classification;
  instrument descriptions and every unused provider member are discarded.
- A synthetic-only transaction service requires complete coverage of the pinned active mapping set
  and one explicit date window before publishing an immutable checkpoint atomically. Provider
  transaction identities are field-encrypted and blind-indexed; a separate keyed fingerprint detects
  a changed record without exposing its contents. Exact window replays, changed identities, ambiguous
  semantics, unsupported records, partial history, cancellation, and mapping or connection changes
  preserve the prior checkpoint and write no partial transaction set.
- Classification is deliberately narrow. Explicit inbound receipts become deposits, explicit
  disbursements become withdrawals, signed provider transfers become transfer-in/out, and valid trade
  executions require one compatible known security plus a consistent position effect and signed net
  amount. Security-backed income is a dividend; instrument-free positive income is interest; an
  automatic zero-net security acquisition is a reinvestment; and a standalone fee requires explicit
  fee items. Missing fees and cost basis remain `unknown`, never zero or inferred.
- The transport and checkpoint service still have no route, operator stage, HTTP coordinator,
  scheduler, startup hook, Dashboard adapter, or automatic invocation. No provider transaction
  response or personal financial value is stored in source, fixtures, logs, exceptions, or Git
  history.

### Dormant market-data quote transport

- The reviewed quote adapter calls only `GET /marketdata/v1/quotes` with an explicit `symbols`
  batch, `fields=quote`, and `indicative=false`. It sends no account hash, mapping identifier,
  or any other parameter. It has no route, button, operator stage, scheduler, automatic refresh,
  Dashboard adapter, or application invocation path.
- The caller supplies the exact symbol set; the transport neither discovers symbols nor accepts a
  partial answer. Symbols are validated before any network activity: at most 50 unique,
  uppercase, bounded, ASCII symbols drawn from letters, digits, `.`, `-`, and `/`.
- The response is streamed under a 512-KiB cap with the existing fixed timeout, requires the
  `application/json` content type, and is parsed with `Decimal` so binary floating-point cannot
  enter a price. No raw provider response is retained.
- Every requested symbol must appear exactly once, and each record's `symbol` member must match
  its response key. Missing coverage, extra symbols, and a non-empty `errors` member all fail
  closed, so an unquoted holding can never be mistaken for a zero-priced one.
- Only the `EQUITY` and `MUTUAL_FUND` quote families are supported, matching the securities
  Northstar can hold. Options, futures, foreign exchange, indexes, and bonds are unsupported.
  `EQUITY` covers ETFs, which the existing instrument-type compatibility rules already reconcile
  against a known local ETF security.
- Each family declares its own documented price members and its own observation timestamp:
  `EQUITY` uses `lastPrice` with `closePrice` and `quoteTime`, and `MUTUAL_FUND` uses `nAV` with
  `closePrice` and `tradeTime`. Undocumented aliases are never accepted as an alternate price.
  Requiring the prior close alongside the latest price is a deliberate completeness check, not an
  inferred value.
- A zero, negative, non-finite, non-numeric, or absent price is rejected rather than normalized.
  Observation timestamps must be documented epoch milliseconds inside a plausible range and
  within the existing five-minute future-skew boundary. An old but valid observation is returned
  with its true observation time; staleness is the consuming service's decision, never a
  transport-level relabel.
- Market-session context comes from the documented `securityStatus` member, normalized to
  `normal`, `halted`, or `closed`. A missing member, a malformed value, and a bounded but
  unsupported session name produce distinct privacy-safe categories.
- Quote diagnostics use privacy-safe categories for quote structure, symbol coverage, malformed
  and unsupported asset types, quote context, missing and malformed price variants, malformed and
  unsupported sessions, and observation time. No category contains a symbol, price, timestamp,
  field content, or raw payload.
- The signed-in Market Data specification must be reconfirmed by the operator immediately before
  the milestone's live quote acceptance, exactly as the balance schemas were. Any mismatch surfaces
  as one of the categories above without recording a provider value.

### Dormant price checkpoint service

- The price service holds no HTTP client. Both the quote reader and the Bitcoin spot reader are
  injected, so it cannot originate a provider request. It has no route, button, operator stage,
  scheduler, startup hook, automatic refresh, Dashboard adapter, or Plan input.
- Quotes are requested only for securities actually held, resolved from the newest published
  provider snapshot together with the newest local position per account. Fully exited positions,
  cash, reversed imports, and archived accounts are excluded, and CSV/manual holdings are always
  included. Bitcoin never goes to the Schwab quote endpoint, and the Bitcoin source is not contacted
  at all when no Bitcoin is held.
- The quotable set is derived from a local-type-to-provider-family mapping, so it cannot drift away
  from the asset families the quote transport supports. Bonds and `other` instruments have no
  supported quote family and stay out of the outbound batch entirely: the transport rejects an
  unsupported family for the whole response, so including one unquotable symbol would discard every
  other holding's valid price. They are recorded as `unknown` and are manual-only.
- Observation times are normalized to UTC before storage. Carry-forward selects the newest prior
  observation by ordering that stored text, so a caller-supplied offset would let an older value win.
- Publication is atomic under a per-connection lock, with the connection version and mapping set
  revalidated inside the publishing transaction. The prior checkpoint is preserved without partial
  writes on connection or mapping change, missing held securities, a symbol set that does not match
  the request, a non-quote response, an invalid or conflicting manual observation set, load timeout,
  cancellation, or any unexpected failure.
- A source outage is different from a broken run: it degrades the checkpoint rather than discarding
  it. Each held security records an explicit status. A successful read is `observed`. When a source
  fails, the newest valid observation for that security is carried forward as `carried_forward` with
  its original price, source, and observation time — searched across every published checkpoint, so
  even a prolonged outage preserves the last valid value.
- A gap with nothing to carry is recorded as `unknown` with no price and no observation time. The
  schema enforces this rather than trusting the service, so a missing or stale price can never be
  substituted with zero.
- Manual fallback is explicit. A manual observation carries its own bounded `manual*` provenance
  label and is accepted only for a held security the live sources could not price. A manual value
  that contradicts a successful live read, targets an unheld symbol, or duplicates another manual
  value fails the run rather than silently overriding anything.
- Freshness is computed against the household `price_stale_after_hours` rule and recomputed on every
  read, so an aging observation cannot keep presenting itself as fresh. A checkpoint is `complete`
  only when every held security has a live observation and no source failed. `usable_for_valuation`
  additionally requires every observation to be fresh, giving Plan and any valuation consumer one
  fail-closed signal.
- Only a complete checkpoint advances the connection's last-successful-sync time. Sync-run rows and
  audit events record allowlisted categories and permitted counts only: no symbol, price,
  observation time, or provider payload.

## Read-only API surface

Accounts and transactions use `https://api.schwabapi.com/trader/v1`:

- `GET /accounts/accountNumbers` discovers plain/encrypted pairs. Northstar must immediately discard
  the plain account number and retain only the encrypted `hashValue` plus an encrypted optional suffix.
- `GET /accounts` reads linked account balances and optionally positions.
- `GET /accounts/{accountNumber}` reads one account using the encrypted account hash.
- `GET /accounts/{accountNumber}/transactions` reads transaction history.
- `GET /accounts/{accountNumber}/transactions/{transactionId}` reads one transaction when needed.

Market data uses `https://api.schwabapi.com/marketdata/v1`:

- `GET /quotes` reads quotes for a bounded symbol list.
- `GET /{symbol_id}/quotes` reads one quote.
- `GET /pricehistory` reads bounded historical prices when needed.
- `GET /markets` and `GET /markets/{market_id}` expose market hours.
- `GET /instruments` and `GET /instruments/{cusip_id}` expose instrument metadata.

Northstar intentionally has no client method, schema, route, or service for the documented POST, PUT,
or DELETE order endpoints, order preview, or money movement. The Schwab App order throttle is zero.

## Eligibility and rate limits

- The OAuth consent flow lets the user select which accounts to link. Current provider documentation
  does not publish a more specific account-type eligibility matrix, so Northstar will discover only
  accounts actually returned after explicit consent and will not assume every Schwab account is
  eligible.
- Schwab documents an app-configured throttle of 0–120 PUT/POST/DELETE order requests per minute per
  account and says GET order requests are unthrottled. Northstar's configured order throttle is zero;
  live enablement requires a fresh operator attestation that the provider portal still shows zero.
- No numeric account-read or market-data limit is published in the current signed-in documentation.
  Northstar must still use bounded requests, backoff on throttling, and preserve the last valid snapshot
  on provider failure.

## Provider documentation

- [Accounts and Trading specification](https://developer.schwab.com/products/trader-api--individual/details/specifications/Retail%20Trader%20API%20Production)
- [OAuth documentation](https://developer.schwab.com/products/trader-api--individual/details/documentation/Retail%20Trader%20API%20Production?docId=retail-trader-api-production--trader-api--individual--documentation)
- [Market Data specification](https://developer.schwab.com/products/trader-api--individual/details/specifications/Market%20Data%20Production)
