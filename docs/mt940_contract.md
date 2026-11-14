# Offline MT940 statement collection (strict subset)

A third, independent entry point next to the full-export v1 pipeline, the
continuous-month importer and the claims analysis. A bank delivers statements
physically split into pages; a page balance is a carried-forward check figure,
**not** a settlement amount. This collector reads the captured pages fully
offline, proves every page and page boundary with exact integer arithmetic,
maps each account/currency explicitly to a settlement ID, and publishes a
legacy `bank.json` (readable unchanged by `settlement_audit.input.load`)
together with a page/transaction level audit JSON. Everything is synthetic
offline processing over text/JSON files with the Python standard library only;
no real account, bank network, credential or external service is involved.

## Entry points

```sh
python -m settlement_audit.mt940 collect \
    --mt940 FILE [FILE ...] --mapping FILE --output DIR
```

Python calls: `settlement_audit.mt940.collect(mt940_paths, mapping_path,
output_dir)` returns the audit payload dict. The lower-level building blocks
`parse_statements(raw_bytes, source)`, `assemble(pages)`,
`load_mapping(raw_bytes, source)` and `build_outputs(accounts, mapping, ...)`
are also importable for tests and custom pipelines. The v1
`settlement_audit.input.load`, `reconcile`, `pipeline.run`, the monthly and
the claims entry points and all of their outputs are unchanged.

## Strict MT940 subset

One tag per physical line; tag order is mandatory. Only these tags exist in the
subset and any other tag (for example `:20:`, `:64:`) is rejected:

| Tag | Meaning | Strict form |
| --- | --- | --- |
| `:25:` | Account identifier | `[A-Z0-9/-]{1,34}` |
| `:28C:` | Statement number / page number | two groups of 1-5 digits; `statement/page`, page defaults to 1; both must be >= 1 |
| `:60F:` / `:60M:` | Opening balance, first page / intermediate page | marker `C` or `D`, then `YYMMDD`, currency, unsigned amount |
| `:61:` | One debit/credit entry | `YYMMDD` then mark `C`, `RD`, `D` or `RC`, then unsigned amount |
| `:86:` | Narrative for the immediately preceding `:61:` | non-empty body; physical continuation lines not starting with `:` may follow and may span several lines |
| `:62M:` / `:62F:` | Closing balance, intermediate page / last page | same field shape as the opening balance |

Each page is exactly one `:25:` / `:28C:` / opening / zero or more
`:61:`+optional `:86:` / closing sequence. A malformed line, a tag out of
order, an `:86:` without a preceding `:61:`, an orphan continuation line or a
page missing its closing balance fails the whole batch.

### Dates, amounts and signs

* Dates use two-digit years interpreted strictly as 2000-2099 (`YY` 00-99) and
  must be real calendar dates (including leap-year rules); entry dates must lie
  inside the opening-to-closing span of their page.
* Amounts are unsigned decimal text. Currencies and precision are reused from
  `settlement_audit.money`: USD/EUR have 2 decimal places, JPY has 0, and the
  absolute bound is 10^12 minor units. Excess precision (e.g. JPY `1.1`, USD
  `1.001`) is rejected with an explicit precision error. No amount text may
  carry a sign character.
* Entry marks: `C` and `RD` are positive credits (an `RD` is a reversal of a
  debit); `D` and `RC` are negative debits (an `RC` is a reversal of a credit).
* Balances take their sign from the `C`/`D` marker only: `C` is positive and
  `D` is negative. All arithmetic is exact integer minor-unit arithmetic;
  `float` is never used.

## Assembly across a shuffled, multi-account batch

Captured text may arrive in any order and may contain several accounts and
several statements, spread across several files. Pages are grouped by
(account, statement number) and then ordered by page number. For every
statement the batch must satisfy all of the following, or the **entire** batch
fails:

1. Page numbers run continuously from 1: no gaps, no duplicates. A duplicate
   page is rejected even when its content is byte-equivalent.
2. The first page opens with `:60F:` and closes with `:62M:` when further pages
   exist; every intermediate page opens `:60M:` and closes `:62M:`; the last
   page opens `:60M:` (or `:60F:` for a single page) and closes `:62F:`.
3. Currency is constant inside a statement; an opening/closing currency
   mismatch or cross-page currency drift is rejected.
4. Adjacent pages link: each intermediate opening date is not before the
   previous closing date and its signed balance equals the previous signed
   closing balance.
5. Every page balances: opening signed minor units + the sum of signed `:61:`
   entries equals the closing signed minor units. Across the full statement
   the last closing minus the first opening equals the summed entry net.

## Explicit account/currency mapping

The mapping file is strict JSON:

```json
{
  "mappings": [
    {"account": "12340001", "currency": "USD", "settlement_id": "S1"}
  ]
}
```

Rows have exactly the three nonempty string fields; unknown currencies,
duplicate `(account, currency)` rows, an empty list, extra fields or any other
top-level shape are rejected. Every `(account, currency)` present in the
statements must have a mapping row or the batch fails; mapping rows that match
no statement are rejected as well, so the mapping is an exact, auditable
bijection between captured account/currency pairs and settlement IDs.

## Outputs and atomic publication

Settlement amounts are the **sum of the signed `:61:` entry minor units**,
aggregated by mapped `settlement_id`/currency; the page balances are only
carried-forward checks. Groups whose net is zero are retained with an explicit
`"0"` / `"0.00"` amount. The two published files are:

* `bank.json` — the legacy list of
  `{settlement_id, currency, amount}` objects, directly consumable by
  `settlement_audit.input.load` and the v1 reconciliation pipeline.
* `mt940_audit.json` — `schema_version: 1` audit payload with per-account and
  per-statement page records: the original raw lines with physical line numbers
  and source file, opening/closing balance details (tag, date, C/D, signed
  minor units), every entry with mark, sign, line number and (possibly
  multi-line) narrative, per-page balance arithmetic and its boolean result,
  inter-page continuation evidence, the statement-level net identity, and for
  each settlement group the aggregation rule and the exact statement/page basis
  that produced it. Input file names, sizes and SHA256 digests and the mapping
  digest are recorded.

Both files are written into a fresh temporary directory in the target parent
and the directory is published with one atomic rename. An existing output
directory is never overwritten, and any validation failure leaves no output
directory and no temporary residue: there is never a half-published batch.
Output bytes are deterministic for the same set of inputs regardless of file
argument order.
