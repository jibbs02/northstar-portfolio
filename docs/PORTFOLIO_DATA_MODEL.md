# Portfolio data model

The encrypted SQLCipher schema is currently version 11. Each migration runs inside one immediate
transaction and updates `app_meta.schema_version` only after all objects for that version exist.
Opening an older encrypted database follows each migration in order; reopening the current version
is idempotent.

## Entities and provenance

- `institutions` and `providers` describe custodians and data sources without exposing names or
  provider identifiers.
- `accounts` supports Roth IRA, HSA, taxable brokerage, cash, and Bitcoin accounts. The separately
  stored tax treatment is `tax_free`, `tax_advantaged`, `taxable`, or `not_applicable`; imports do
  not derive or change it.
- `securities` supports equities, ETFs, mutual funds, bonds, cash instruments, Bitcoin, and an
  explicit `other` category.
- `transactions` supports buy, sell, dividend, reinvestment, interest, deposit, withdrawal,
  transfer in, transfer out, fee, and adjustment. Trade date, optional settlement date, and UTC
  import time are distinct.
- Schwab transaction rows additionally retain a separate provider-source timestamp, a field-encrypted
  provider transaction identity, and keyed identity/fingerprint values. Immutable provider transaction
  checkpoints describe the complete mapped-account date window that was atomically accepted; their
  link rows contain only opaque relationships and keyed values.
- Immutable provider price checkpoints record what the read-only data plane currently knows about
  every held security: the source, the provider's own observation time, market-session context,
  freshness against the household price-staleness rule, and an explicit status of `observed`,
  `carried_forward`, `manual`, or `unknown`. A schema constraint makes an `unknown` observation with
  any value unrepresentable, so a missing or unusable price can never become a zero. Checkpoints are
  separate from `market_price_snapshots`; Dashboard and Plan stay disconnected from live prices until
  their own acceptance gates pass.
- `tax_lots` stores acquisition date and quantity. Cost basis is accompanied by `supplied` or
  `unknown`; the importer never calculates a missing basis.
- `position_snapshots` preserve UTC observation time and the source's calendar date. Market value
  and cost basis each have an explicit supplied/unknown state.
- `market_price_snapshots` preserve source date, UTC observation time, price, and source adapter.
- `account_targets` supports either a percentage or monetary target and enforces that exactly the
  matching value is present.
- `cash_envelopes` supports an encrypted name, optional goal/current amounts, and encrypted notes.
- `import_batches`, `import_previews`, and `import_batch_records` retain adapter provenance,
  privacy-safe counts/status, encrypted preview content, and ownership links used for reversal.
- `audit_events` stores keyed actor/subject hashes and small allow-listed metadata. It never receives
  imported values, account identifiers, descriptions, or totals.

Gate 3 adds an asset-class classification to securities, contribution eligibility to account
targets, account-level fractional-share and dividend-reinvestment preferences, portfolio-level
Bitcoin/rebalancing/freshness rules, and dated portfolio valuation snapshots. Valuation snapshots
carry an explicit complete/partial state and are the only permitted input to performance returns.
They do not silently reconstruct missing history from current positions.

Phase 4C adds four initially empty policy structures. The migration deliberately inserts no policy
defaults:

- `cash_roles` assigns each cash holding, identified by account and security, exactly one of
  emergency reserve, spending cash, or investable cash;
- `security_policy_mappings` maps a security to one strategic sleeve and records whether it is an
  eligible taxable-purchase destination;
- `household_policy_targets` stores the target percentage for each configured strategic sleeve;
- `bitcoin_contribution_bands` stores ordered household-risk percentage intervals and the portion of
  remaining surplus assigned by each interval.

Policy percentages remain canonical decimal `TEXT` inside SQLCipher. Runtime validation of complete
targets and non-overlapping Bitcoin bands belongs to the repository boundary added with the Plan
workflow; the migration itself only establishes the forward-only storage shape.

Rows created by an import reference their batch. Reversal sets `reversed_at` only on mapped records;
it does not delete securities, accounts, other batches, or unrelated records.

## Precision and rounding policy

All financial input crosses a `Decimal` boundary. Python floats and non-finite values are rejected.
SQLite columns use canonical fixed-scale decimal `TEXT`, never `REAL`:

| Value | Scale | Quantum |
| --- | ---: | --- |
| Monetary amounts and cost basis | 2 | `0.01` |
| Prices | 8 | `0.00000001` |
| Security and Bitcoin quantities | 12 | `0.000000000001` |
| Percentages | 6 | `0.000001` |

Rounding is decimal round-half-to-even. Negative quantities, prices, fees, position values, and cost
basis are rejected. Signed transaction cash flows are retained because credits and debits have
direction. A missing value is `NULL` plus an explicit `supplied`, `unknown`, or `not_applicable`
status where the distinction affects interpretation. Missing fees or cost basis never become zero.

Source dates remain ISO `YYYY-MM-DD` calendar dates. Application timestamps use timezone-aware ISO
8601 UTC. For date-only CSV positions and prices, the observation timestamp is the start of the
source date in UTC; import time remains separate and never refreshes the financial observation.
Trade date, settlement date, and import timestamp are independent fields.

## Encryption model and visible metadata

SQLCipher encrypts the entire database, including schema, indexes, decimals, dates, and metadata.
The following values also receive AES-256-GCM field encryption with a random nonce and table/row/field
associated data:

- institution and provider names;
- provider identifiers;
- account names, provider identifiers, and notes;
- security symbols and names;
- transaction descriptions and notes;
- account-target notes;
- cash-envelope names and notes;
- normalized import previews and reconciliation/error summaries.

Keyed HMAC-SHA-256 blind indexes exist only for exact account-provider-identifier and security-symbol
matching. Import fingerprints are also keyed HMAC values over the adapter namespace and a digest of
the in-memory upload.

Inside the unlocked database, intentionally visible metadata is limited to UUIDs and relationships,
enumerated record/account/instrument/status types, currency codes, key/schema versions, UTC/source
dates, canonical decimals needed for calculations, adapter version, counts, byte size, batch state,
reconciliation state, reversal state, and keyed hashes. Import history exposes only adapter, batch
state, row count, reconciliation state, UTC creation time, and an available reversal. It never shows
filenames, fingerprints, account numbers, totals, or audit hashes.
