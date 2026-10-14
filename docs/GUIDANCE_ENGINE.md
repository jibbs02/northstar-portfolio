# Gate 3 guidance engine

Gate 3 is a local, deterministic, read-only guidance layer over the encrypted portfolio model. It
does not call a broker or market-data service, create an order, move money, forecast prices, or
persist a recommendation. All source records and settings in this gate are synthetic.

## Calculation definitions

All arithmetic uses Python `Decimal` with the fixed scales and round-half-to-even policy documented
in `PORTFOLIO_DATA_MODEL.md`. Binary floating point is rejected at input boundaries.

- **Current holding value** is quantity multiplied by the latest recorded price. If no price exists,
  the supplied position-snapshot value is used. If neither exists, the value is explicitly unknown.
- **Account and portfolio value** are sums of known holding values. The result is marked partial if
  any included holding is unknown. Allocation percentages use the known total and retain that
  completeness warning.
- **Invested assets** are total value less operational cash. Bitcoin remains its own asset class and
  is never folded into cash or a generic equity label.
- **Target drift** is current account allocation percentage minus the configured account target.
  Dollar drift is current value minus target value at the current account total.
- **Known basis** sums only supplied non-cash basis. Unrealized gain/loss is computed only for
  holdings that have both a known current value and supplied basis. Complete, partial, and
  unavailable states prevent an unknown basis from becoming zero.
- **External contributions and withdrawals** include deposits and withdrawals. Internal transfer-in
  and transfer-out records are reported separately and do not affect contribution or return flows.
- **Income** includes dividends, reinvestments, and interest. Reinvestment is income but not an
  external contribution. Withdrawals are also reported as distributions. Fees are summed from fee
  transactions or supplied fee fields; unknown fees make the result partial.
- **Realized gain/loss** is computed for a sell only when both net proceeds and supplied cost basis
  exist. Otherwise the result is partial or unavailable.
- **Freshness** compares each UTC observation timestamp with the configured price and position
  windows. A CSV position or price has date-only provenance, so Northstar conservatively uses the
  start of its source date in UTC rather than its later ingestion time. Stale data remains visible
  with a warning but is ineligible for allocation planning.
- **Cash envelopes** are virtual assignments within actual cash. They do not create transactions or
  change performance. Saving is rejected if assignments exceed actual cash.

## Performance completeness

Northstar deliberately shows `Insufficient history` instead of inventing a return. Both return
methods require at least two complete, dated, whole-portfolio valuation snapshots and known amounts
for every external deposit and withdrawal inside the measurement period. Any partial valuation,
unknown external flow, or external flow outside the valuation range blocks both results.

Money-weighted return is a deterministic Decimal XIRR over the initial valuation, dated external
flows, and ending valuation. It is annualized on a 365-day basis. Time-weighted return is cumulative,
links successive valuation periods, and additionally requires a complete valuation on every date
that has an external flow. The period flow is removed from the ending value before calculating that
period's growth. Internal transfers, income, reinvestments, fees, and trades are not external flows.

Gate 3 does not yet schedule or capture daily portfolio valuations. Until sufficient snapshots have
been supplied by a future trusted local process, return fields correctly remain unavailable.

## Inflow-only contribution allocator

The allocator accepts one destination account, an amount of new cash, and an explicit choice about
whether tracked idle cash may also be used. It considers only holdings that have a configured target,
are marked eligible for contributions, are not cash, and have a fresh positive price.

For each holding, the engine computes its target-value deficit at the projected account total. It
sorts candidates by largest deficit, then symbol and internal ID for deterministic tie-breaking. It
directs available cash toward those deficits without selling. Fractional-share mode rounds shares
down to 12 decimal places; whole-share mode rounds down to whole shares. The output states estimated
dollars, shares, resulting allocation, remainder, warnings, and a reason. Missing/stale prices and
unspendable remainders stay explicit. The output is transient planning guidance, never a trade ticket.

## Settings and atomicity

- Targets for each account must be unique and total exactly `100.000000%` after canonical rounding.
- Contribution eligibility, fractional-share support, and dividend-reinvestment preference are saved
  in the same transaction as targets.
- Bitcoin target, caution, and review thresholds must be ordered and at most 100%; the rebalancing
  review threshold must be greater than zero and at most 100%.
- Cash-envelope labels, goals, and assignments are replaced in one transaction after validating the
  cash-account relationship and total assignment.
- Audit events contain keyed actor/subject hashes and allow-listed counts or booleans, never account
  labels, symbols, financial values, or provider identifiers.

## Accessible local interface

The dashboard, settings, and allocator are server-rendered HTML and remain functional with JavaScript
disabled. Allocation and drift are conveyed by semantic tables and text rather than color alone.
Pages include a keyboard skip link, landmarks, associated form labels, table headers, visible focus,
large controls, responsive layouts, and reduced-motion rules. No third-party font, script, analytics,
or CDN request is made, and no financial state is stored in browser local/session storage or embedded
as a hidden decrypted dataset.

## Encryption and visible metadata

SQLCipher encrypts the complete v3 database, including numeric settings, valuation history, dates,
schema, indexes, and audit metadata. AES-256-GCM field encryption continues to protect account names,
provider identifiers, notes, security symbols/names, transaction descriptions/notes, cash-envelope
names/notes, and import preview/summary payloads. Exact-match blind indexes remain limited to provider
identifiers, symbols, actors/subjects, and privacy-safe import deduplication.

While the application is unlocked, dashboard values and decrypted labels necessarily exist in server
and browser memory and are visibly rendered to the authenticated user. The browser can also see
page paths, internal opaque UUID form values, record types, tax treatment, asset class, completeness,
freshness and guardrail states, import counts/status/times, and the displayed as-of timestamp. URLs,
logs, audit metadata, filenames, HTML `data-*` attributes, and browser storage do not contain financial
values, account/provider identifiers, or decrypted portfolio datasets.

## Accepted limitations

- Valuations and prices are import-sourced; no external market data or scheduled daily snapshot
  capture exists, so freshness and performance availability depend on local imported history.
- Time-weighted return uses valuation points on each external-flow date and assumes the flow occurs at
  the end of that period; intraday ordering is not represented by date-only imports.
- Realized gain/loss uses supplied sell cost basis and is not a tax engine. It does not perform lot
  selection, wash-sale analysis, tax reporting, or infer missing basis.
- The allocator is a deterministic deficit-filling heuristic. It does not model taxes, transaction
  costs, settlement restrictions, minimum purchases, or provider-specific fractional-share rules.
- Bitcoin guardrails are concentration signals only, not suitability advice or a recommendation to
  buy or sell.
