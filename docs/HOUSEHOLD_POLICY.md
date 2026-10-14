# Phase 4C household policy

Phase 4C establishes the encrypted household-policy model, two fixed denominator views, and a
deterministic monthly surplus checklist. The implementation is provider-independent and performs no
external request.

## Fixed denominator views

Northstar exposes two named views rather than accepting a configurable denominator:

- **Investment allocation** includes mapped non-cash investments and cash explicitly assigned the
  investable-cash role. It excludes emergency reserve and spending cash.
- **Household risk exposure** includes the complete investment-allocation denominator plus emergency
  reserve. It excludes spending cash.

Bitcoin therefore has two deliberately named percentages. Sleeve weights and future purchase
guidance use the investment-allocation percentage. Bitcoin contribution-band selection uses the
household-risk percentage.

Unknown values, negative values, missing cash roles, sleeve conflicts, and unclassified holdings
mark the policy summary incomplete. Known values remain visible, but an incomplete summary is not a
valid input to the future Plan engine.

## Policy persistence

The v4 migration creates empty cash-role, sleeve-mapping, household-target, and Bitcoin-band tables.
No household values are embedded in source or inserted by migration. One atomic repository boundary
validates and replaces the four related setting groups:

- cash roles must reference current cash holdings;
- cash securities can map only to the investable-cash sleeve;
- targets must cover all six actionable sleeves and total exactly `100.000000%`;
- Bitcoin bands must cover 0 through 100 without gaps or overlaps, and both their bounds and surplus
  contribution percentages must remain within that range.

The audit event stores only the number of records in each setting group. It excludes percentages,
account or security identifiers, and all other financial values.

## Deterministic surplus engine

The pure planning boundary accepts available surplus, an optional one-time inflow, the complete
household summary, stored targets and Bitcoin bands, a configured sinking-fund constraint, and
eligible purchase destinations with recorded prices. It processes money in this fixed order:

1. assign the lesser of the configured sinking-fund contribution, its remaining gap, and total
   inflow;
2. select the Bitcoin band using the current household-risk percentage and assign that band's
   portion of remaining inflow;
3. project the final investment-allocation denominator and direct the residual toward the largest
   target-value sleeve deficits;
4. choose eligible taxable securities deterministically by sleeve, symbol, and opaque account and
   security IDs, and estimate shares by rounding down to the supported quantity scale.

Band lower bounds are inclusive and upper bounds are exclusive, except that the final band includes
exactly 100%. Actions identify whether before/after percentages use household-risk exposure or
investment allocation. Missing classifications, incomplete values, stale household data, unavailable
selected prices, incomplete targets, or invalid bands return a blocked plan with no partial actions.

The application service derives policy facts only from the latest local guidance summary and stored
policy records. Taxable destinations must be both explicitly eligible and held in a taxable brokerage
account; each taxable action carries its opaque destination account and security IDs. Bitcoin remains
a generic allocation action because Phase 4C does not choose among custody or account destinations. A configured
sinking-fund envelope and contribution live in an encrypted secure record and are converted to a
remaining-gap constraint at calculation time.

The primary server-rendered **Plan** page accepts one required available-surplus amount and an
optional one-time inflow. It shows both named denominators and either the ordered checklist or a
fail-closed explanation. POST requests require CSRF protection, invalid amounts are not echoed, and
the recommendation creates no audit event or persistence record.

The authenticated Settings page exposes the complete household policy without client-side script:
all six sleeve targets, one mapping per current security, one role per current cash holding, ordered
Bitcoin bands, taxable-destination eligibility, and an optional sinking-fund envelope/contribution.
The server reconstructs all settings from current opaque record IDs and submits them through the same
atomic repository boundary. A missing current mapping or cash role rejects the update; personal
values are never supplied by templates or source defaults. Newly imported securities render as
`unclassified` until explicitly mapped. The page starts with five Bitcoin-band rows and offers
script-free links for up to the repository limit of 20 contiguous bands.

## Release safety

Encrypted backup and isolated-restore tests cover the v4 schema and household policy records. Plan
and Settings routes use the existing authentication boundary, policy mutations require CSRF, and
invalid financial input is never echoed into HTML, URLs, or logs. The Plan page is semantic,
keyboard-operable, responsive, script-free, and identifies every percentage denominator in text.
CSV freshness uses the statement's source date at the start of that UTC day, never the later import
time, so importing an old export cannot make it actionable.

CI checks both the tracked tree and every reachable Git blob from a full-history checkout. The
scanner blocks credentials, runtime databases/backups, brokerage interchange files, spreadsheets,
and common statement/export filenames. Synthetic tests verify the complete checklist, band
boundaries, rounding, atomic rejection, stale/partial/unclassified blocking, encrypted restore, route
security, accessibility, and the absence of order or money-movement routes.

The release performs no external requests, order creation, trading, or money movement. Generated
recommendations are never persisted or written to the audit timeline.
