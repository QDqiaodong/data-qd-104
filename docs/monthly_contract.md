# Monthly ingestion and cross-month refund audit (v2 report)

A second, independent entry point next to the full-export v1 pipeline. It keeps
every received export month immutable, verifies refunds against the cumulative
refundable principal of each payment across months, and renders historical
reports whose facts never change after later months are imported. Everything is
synthetic offline reconciliation over CSV/JSON/SQLite with the Python standard
library; no real payment, refund, account or external service is involved.

## Entry points

```sh
python -m settlement_audit.monthly ingest --period YYYY-MM \
    --ledger FILE --bank FILE --db FILE
python -m settlement_audit.monthly report --period YYYY-MM \
    --db FILE --output FILE
```

Python calls: `settlement_audit.monthly.ingest(period, ledger_path, bank_path,
db_path)` returns a receipt dict; `settlement_audit.monthly.report(period,
db_path, output_path)` returns the payload dict. The v1
`settlement_audit.input.load`, `settlement_audit.reconcile.reconcile`,
`settlement_audit.pipeline.run` and `python -m settlement_audit` behavior and
v1 output are unchanged.

## Inputs and exact-string semantics

The ledger CSV keeps the ordered header
`event_id,kind,original_id,settlement_id,currency,amount`; the bank input stays
a JSON list of `{settlement_id,currency,amount}`. Currency scales (USD/EUR 2
decimals, JPY 0), the 10^12 minor-unit bound, positive ledger amounts, signed
or zero bank amounts, payment-adds/refund-and-fee-subtract signs, equivalent
amount spelling (`100.0` == `100.00`) and same-`event_id` canonical
deduplication with conflict rejection are reused unchanged. A duplicate
bank `settlement_id/currency` inside one file is rejected. Each input file is
read once into raw bytes; parsing, the SHA256 digest and the stored source blob
all use that single capture.

`period` is a strictly validated operator-declared `YYYY-MM`; records carry no
event timestamp and months are never guessed from IDs. The first import may use
any valid month. Every later import must equal the calendar month immediately
after the last received month (December -> January rollover included); skipped
or back-filled months are rejected. A header-only ledger plus an empty bank
list is a valid, received empty month (monthly mode only; v1 still rejects
two-sided emptiness).

## Scope and keys

- Bank and reconciliation grain is `period / settlement_id / currency`. The
  same settlement ID and currency may appear in different months.
- `event_id` is globally unique across the whole monthly store. An event
  belongs to exactly one month; re-presenting an old event ID in a new month is
  rejected.
- A new-month refund may reference a payment received in any earlier month or
  in the same month, never a fee, another refund, an unknown ID or a future
  payment, and never across currencies. Same-month files may be unordered: the
  whole unique event set is formed first and references are checked afterwards.

## Cumulative refund principal

Each payment stores `original_minor`, `refunded_minor` and `remaining_minor`
with exact integer math, satisfying
`original_minor = refunded_minor + remaining_minor` and
`remaining_minor >= 0`. The refunded total is the sum of unique refund events
across all received months: canonical duplicate rows of one `event_id` never
add twice, while distinct refund IDs are counted separately even for equal
amounts. The whole new month is one atomic batch — if the batch would push a
payment past its principal, the entire month fails and no event, bank fact,
source or balance change survives. Fees affect the month's net settlement only
and never consume refundable principal.

Monthly reconciliation uses only that month's events through the unchanged v1
`reconcile` machinery: expected = payments - refunds - fees,
difference = bank - expected, statuses `balanced`, `amount_mismatch`,
`missing_bank`, `missing_ledger`, with missing values kept null. Refunds post
to their own export month and own `settlement_id`; they never rewrite the
original payment month or borrow the payment's settlement ID. Currencies are
never combined; negative net settlements, true zero net and one-sided
missingness stay distinct.

## Immutability, replay and transactions

Received months cannot be overwritten, appended to or deleted. Re-importing a
month compares the full canonical event set and full canonical bank set with
the stored month. Row order, equivalent amount spellings and equivalent
duplicate repetitions may differ: identical canonical facts return
`status=replayed` and touch no stored data, including the first source bytes,
digests and first-accepted duplicate-row counts. Any missing, added or changed
fact is a month conflict and the whole import fails. Replay never re-deducts
refund budget and never rejects an originally valid month because later months
exhausted its principal.

Month admission, global identity checks, refund-budget checks, facts, sources
and current balances commit together in one SQLite write transaction
(`BEGIN IMMEDIATE`). Sequence and budget decisions are re-evaluated inside the
writer lock; a competing process waits bounded by `--busy-timeout` (default 15s)
and then fails explicitly with `database lock ...`. Contract errors and
transaction exceptions roll back completely. A monthly store carries an
explicit independent schema version; a v1 `audit.sqlite` (or any other
non-monthly database) is refused before modification — no guessing, migration
or clearing. v1 databases remain exclusively owned by the v1 entry point.

## Store schema (monthly, independent schema version 1)

| Table | Grain |
| --- | --- |
| `monthly_meta` | store-level key/value, currently `schema_version=1` |
| `months` | one row per received period: sequence, both SHA256 digests, first raw ledger/bank bytes, first-accepted duplicate counts, receipt time |
| `events` | one row per globally unique `event_id` with owning period, kind/parent/settlement/currency, exact minor amount and 1-based ledger record number |
| `bank_facts` | one row per `period/settlement_id/currency` with exact minor amount and 1-based bank array index |
| `reconciliation` | one row per month-scoped settlement/currency group with expected/bank/difference, status and per-kind counts |
| `payment_balances` | one row per payment: original/refunded/remaining minor units across all received months |

## Report (schema_version 2)

`ingest` only commits and prints a receipt; it never writes a report. `report`
opens the database read-only, never creates or upgrades it, requires the
requested month to be received, and reads every fact from one snapshot using
only facts at or before the requested period. Later months cannot alter a
historical report: balances, reconciliation and sources for an old month stay
byte-identical across later imports. The JSON contains:

- `schema_version`, requested `period`, month `summary` and `settlements` in
  the v1 field semantics, plus per-kind event counts;
- `duplicate_ledger_rows` / `bank_duplicate_rows` first accepted by the month;
- `payment_balances_at_month_end`: every payment known by month end with
  currency, original/cumulative-refund/remaining minor units;
- `refunds`: each refund of the month with amount, own settlement ID and
  currency, original payment ID and payment month, and the 1-based record
  positions of both the refund row and its payment row in their first sources;
- `sources`: role (`ledger`/`bank`), period and SHA256 for every source month
  in scope, independently checkable against the raw bytes stored in `months`.

No later-month data, current time or machine-absolute path is serialized, so
the same request always produces identical bytes. Amounts are Python integers
(no float, no lossy SQL aggregation). The output is written to a same-directory
temporary file and atomically replaced; a failed write exits non-zero, removes
its temporary file and leaves the previous report intact. A report failure
never undoes a committed month, and reporting against a missing database
creates no file.

## Test synchronization hook

`SETTLEMENT_AUDIT_SYNC_DIR=<dir>` makes an ingest create `<stage>.arrive` and
wait for `<stage>.release` flag files at the `locked` and `before_commit`
stages. It is inert when unset and exists solely so concurrency tests control
commit ordering deterministically instead of sleeping.
`SETTLEMENT_AUDIT_REPORT_FAIL=rename` injects an output replacement failure for
the atomic-write test.
