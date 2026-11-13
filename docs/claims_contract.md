# Receipt claim range analysis (independent CLI)

An independent, read-only analysis entry point next to the v1 full-export
pipeline and the monthly ingestion/report mode. Bank receipts (回单) that carry
no settlement reference may be claimable by several settlement groups. Claiming
receipts in file order can consume funds that another group could otherwise
claim exclusively, so this analysis does **not** produce an order-based claim.
It treats the claims as a global per-currency transportation problem and
reports, for every allowed relation and every demand group, the minimum and
maximum amount it can carry in **any** allocation that maximizes the total
claimed amount.

It never writes the monthly database, never changes stored reconciliation
facts, and never emits an automatic payment, adjustment or fraud decision.

## Entry point

```sh
python -m settlement_audit.claims --period YYYY-MM --db FILE \
    --receipts FILE --relations FILE --output FILE
```

Python call: `settlement_audit.claims.analyze(period, db_path, receipts_path,
relations_path, output_path)` returns the report dict. The v1
`python -m settlement_audit`, `settlement_audit.pipeline.run`, the monthly
`ingest`/`report` behavior and all existing SQLite schemas are unchanged.

## Inputs

`--db` must be an existing monthly store (schema version 1) created by
`settlement_audit.monthly`. It is opened read-only; `--period` must already be
received. Only that month's stored `reconciliation` rows are used. The whole
read happens in one read transaction, so all facts come from one snapshot, and
the store file is never modified.

Receipts CSV keeps the ordered header `receipt_id,currency,amount`:

| column | meaning |
| --- | --- |
| `receipt_id` | nonempty receipt identifier, unique within the file |
| `currency` | `USD`, `EUR` or `JPY` |
| `amount` | strictly positive decimal in that currency |

Relations CSV keeps the ordered header `receipt_id,settlement_id,currency`:

| column | meaning |
| --- | --- |
| `receipt_id` | must exist in the receipts table |
| `settlement_id` | must exist in that month's reconciliation for the currency |
| `currency` | must equal the receipt's currency |

Parsing, precision and bounds reuse `settlement_audit.money.minor`: same
supported currencies, at most that currency's decimals, absolute value at most
`10^12` minor units, positive amounts only. Both tables may be header-only
(empty). Each file is read into bytes exactly once; parsing, SHA256 digests and
the report all use that one capture.

Rejected inputs (the command exits non-zero, creates no report and leaves any
previous report untouched):

- malformed header, malformed row, empty identity field;
- unsupported currency, invalid decimal, excess precision, zero/negative
  receipt amount, amount above the minor-unit bound;
- a repeated `receipt_id` (even for an identical row) or a repeated
  `receipt_id/settlement_id/currency` relation (even for an identical row);
- a relation referencing an unknown receipt;
- a relation referencing a `settlement_id/currency` absent from this month's
  reconciliation (other months are out of scope and are not consulted);
- a relation whose currency differs from its receipt's currency
  (cross-currency relations are rejected, never converted).

## Demand, supply and exclusions

The analysis grain is `settlement_id/currency` inside the requested month and
one currency at a time; currencies are never combined or converted.

- **Demand**: each stored group whose `expected_minor` is a positive integer.
  Its demand capacity is exactly `expected_minor`.
- **Supply**: each receipt row, with capacity equal to its positive amount.
- **Allowed relation**: one `receipt -> group` arc, available only when the
  relation is present in the relations CSV and links that receipt to that
  demand group.

Groups are excluded from the claim model, with an explicit reason, and listed
under `excluded_groups`; their stored reconciliation values and status are
reproduced verbatim and are not re-judged:

| `reason` | condition |
| --- | --- |
| `missing_internal_amount` | stored `expected_minor` is null (`missing_ledger`) |
| `zero_expected_minor` | stored `expected_minor` is exactly `0` |
| `negative_expected_minor` | stored `expected_minor` is `< 0` |

A syntactically valid relation that points at an existing group with no
positive demand is listed under `excluded_relations` with reason
`group_not_positive_demand`; it adds no claim capacity.

This analysis is additional evidence only. Existing reconciliation statuses,
differences and reports are never modified or reinterpreted.

## Model and exact optimization

For each currency build the integer network

```
source --(receipt supply)--> receipt --(allowed relation, unlimited)--> group --(expected_minor)--> sink
```

A maximum flow is exactly an allocation that never gives a receipt more than
its supply, never gives a group more than its demand, uses only allowed
relations, and maximizes the total claimed amount. All capacities and flows are
Python integers; no float touches money. The maximum is computed with Dinic's
algorithm; the whole problem is solved per currency in polynomial time. No
allocation is enumerated per minor unit, and no enumeration of all maximum
allocations is performed.

For each arc (each relation and each group demand arc) the report gives:

- `min_claim_minor` / `max_claim_minor`: the smallest/largest flow that arc can
  carry among all allocations with maximum total claim;
- `min_witness` / `max_witness`: identifiers of complete integral allocation
  witnesses, each reaching that endpoint while still claiming the maximum total.

Ranges are computed on the residual network of the maximum flow: the maximum
of an arc is its current flow plus a residual max-flow from its head back to
its tail with both directions of that arc removed; the minimum is its current
flow minus a residual max-flow from tail to head with the arc removed. Each
witness is a full allocation, so it is independently checkable.

`range_semantics` states this explicitly: different endpoints come from
different allocations. **Minimum/maximum values of different arcs must not be
concatenated into one allocation**; individually maximal claims can be jointly
infeasible (for example two receipts sharing two groups). The `witnesses` map
contains each distinct complete allocation once, with:

- `total_claim_minor`;
- `relation_flows`: `[[receipt_id, settlement_id, minor], ...]` for strictly
  positive flows only, sorted;
- `receipt_claimed_minor` and `group_claimed_minor`: total per receipt and per
  group.

## Minimum cut evidence

Each currency section includes `minimum_cut`:

- `capacity_minor`: cut capacity, always equal to `max_total_claim_minor`;
- `source_side` / `sink_side`: node names on each side of the cut;
- `cut_edges`: `[from, to, capacity_minor]` for every edge leaving the source
  side, with `from`/`to` one of `source`, `sink`, `receipt:<id>`, `group:<id>`.

The cut is a certificate that the reported total cannot be exceeded: every
unit of claim capacity crossing the cut is already used. Unbounded relation
arcs never cross a minimum cut, so every listed capacity is a real bound.

## Report

The JSON report (`schema_version=1`, `analysis=settlement_claim_ranges`) is
deterministically serialized with sorted keys and contains:

- `period`, `range_semantics`, and `summary` (currencies, total maximum claim
  per currency, receipt/relation/demand/exclusion counts);
- `currencies`: per currency `receipt_count`, `demand_group_count`,
  `relation_count`, `supply_total_minor`, `demand_total_minor`,
  `max_total_claim_minor`, `unclaimed_demand_minor`,
  `unused_supply_minor`, `network`, `minimum_cut`, `relation_ranges`,
  `group_ranges`, `witnesses`;
- `network`: source/sink names, receipt nodes with supply and 1-based receipt
  record, group nodes with demand and status, and every edge with its capacity
  (`null` for the unlimited relation arcs) and the maximum-flow flow it carries;
- `excluded_groups` and `excluded_relations` with reasons;
- `sources`: period, monthly schema version, the month's stored ledger/bank
  SHA256 digests and duplicate-row counts, stored counts of events, bank facts
  and reconciliation rows, plus the receipts/relations SHA256 and row counts.

Both source tables and the monthly source month are named by digest, so every
included fact can be checked against the captured bytes. No machine-absolute
path, current time or later-month data is serialized, so the same inputs always
produce identical bytes.

Any failure (bad input, missing store/month, unknown reference, write failure)
exits non-zero and leaves any previous report intact. The report is written to
a same-directory temporary file, flushed, fsynced and atomically renamed into
place; a failed run removes its temporary file and never truncates the previous
report. Setting `SETTLEMENT_AUDIT_CLAIM_FAIL=rename` injects a replacement
failure for tests.

## Runtime bounds

Every amount in the bundled example and in the exhaustive tests is a concrete
integer minor-unit value; the solver itself only ever performs integer
comparisons, additions and bounded residual augmentations. The maximum-flow
search is iterative (no recursion limit on graph size). Each arc needs one
residual max-flow query, so runtime is polynomial in receipts, relations and
demand groups per currency and independent of the number of minor units.

## Complexity and scope

The number of networks, nodes and arcs is linear in receipts plus relations
plus demand groups per currency; each arc needs one residual max-flow query, so
the run time is polynomial. This is offline analysis of synthetic exports; it
does not perform payments, never contacts a bank or other external service, and
makes no final accounting decision.
