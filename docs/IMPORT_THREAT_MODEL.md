# Production manual CSV import threat model

Manual CSV imports are authenticated, CSRF-protected workflows. In production, Cloudflare Access and
application-side JWT validation protect them; they do not call Schwab, a market-data service, or any
other network service.

## Security boundary

The browser sends one CSV through `upload -> parse -> validate -> preview -> commit`. Multipart
requests require a bounded `Content-Length` and remain below an in-memory spool threshold. The file
is capped at 2 MiB, closed immediately, and never copied to a repository, runtime directory,
database BLOB, log, filename field, URL, cookie, or audit event. Only normalized preview records are
persisted under AES-GCM with a 15-minute expiry. Commit deletes that preview in the same transaction
that writes portfolio records; startup and subsequent import operations purge expired previews.

The parser limits imports to 5,000 nonblank rows and 500 characters per field. It accepts UTF-8 or
UTF-8 with a byte-order mark. NUL bytes, malformed encoding/CSV, incorrect or duplicate headers,
inconsistent columns, ambiguous accounts, unsupported types, invalid dates/decimals, non-finite
numbers, unsafe signs, inconsistent totals, and spreadsheet-formula prefixes in text are rejected.

Errors contain only a row number, fixed header name, and allow-listed code. They never interpolate
source values. Application code does not log upload bodies or parsed rows; production disables the
HTTP access log.

The selected local account is matched to the source identifier with a keyed blind index. The
identifier is field-encrypted on the account and in the encrypted preview, and absent from rendered
HTML. A keyed, adapter-namespaced fingerprint makes an exact repeated file idempotent.

## Approved manual CSV formats

These adapters intentionally accept only Northstar's reviewed manual v1 layouts. They are for
entering a current, personally verified snapshot without credentials or provider APIs; they do not
claim compatibility with any brokerage export. Copy only the required current values into this
layout, using a minimal matching identifier rather than a full account number.

### Positions: `manual_positions_csv_v1`

The header must match exactly:

```text
Row Type,Account Number,Symbol,Description,Security Type,Quantity,Price,Market Value,Cost Basis,As Of Date
```

`Row Type` is `POSITION` or one optional `TOTAL`. Security types are `EQUITY`, `ETF`, `MUTUAL FUND`,
`BOND`, `CASH`, `BITCOIN`, and `OTHER`. Dates are ISO `YYYY-MM-DD`. A `TOTAL` expected market value
must equal the decimal sum of position values. Each supplied value must also be within one cent of
quantity × price after monetary rounding. A positions file contains one current source date and is
authoritative for that account through that date: committing it retires older positions omitted from
the snapshot. Blank position cost basis remains unknown. Future source dates are rejected.

### Transactions: `manual_transactions_csv_v1`

The header must match exactly:

```text
Account Number,Trade Date,Settlement Date,Action,Symbol,Description,Quantity,Price,Fees,Amount,Cost Basis
```

Actions are `BUY`, `SELL`, `DIVIDEND`, `REINVESTMENT`, `INTEREST`, `DEPOSIT`, `WITHDRAWAL`, `TRANSFER
IN`, `TRANSFER OUT`, `FEE`, and `ADJUSTMENT`. One optional `TOTAL` action supplies the expected sum of
`Amount`. Trades require symbol, quantity, and price; other actions require an amount. Settlement
date may be blank; trade date may not. Blank fees and cost basis remain explicitly unknown when
relevant.
Future trade dates are rejected; future settlement dates remain accepted because valid trades can
settle after their trade date.

## Explicitly unsupported

- Live Schwab OAuth/API payloads, tokens, account discovery, and market data.
- OFX, QFX, Excel, PDF statements, archives, and arbitrary CSV layouts.
- Locale dates, thousands separators, currency symbols, parenthesized negatives, or automatic
  delimiter/header/type guessing.
- Silently mapping unknown actions, inventing cost basis or fees, or repairing inconsistent totals.
- Persisting an uploaded file for later reprocessing.

Use invented identifiers and values in development and tests. The existing personal-data exercise is
a rehearsal, not the production baseline. Do not continue onboarding it. Personal data is permitted
in the fresh production database only during the integrated Phase 6 gate after Phase 5 passes; it
must never enter a repository checkout, Git history, logs, GitHub, fixtures, or documentation.
