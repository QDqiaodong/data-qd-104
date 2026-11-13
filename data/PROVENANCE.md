# Source and license

All bundled data is original synthetic demonstration data for fictional entities.
It is not real customer information, official measurements, live service history,
or copied third-party content. It is distributed under this repository's MIT
license. No third-party source code is vendored; installed dependencies retain
their own licenses. No paid service, real account or external dataset is required.

The `monthly/` files (`YYYY-MM_ledger.csv`, `YYYY-MM_bank.json`) are original
synthetic inputs for the continuous-month, cross-month refund scenario: a
January USD payment and fee, February and March refunds of that payment, and
matching synthetic bank settlement rows. They describe no real entity or
account and contain no credentials.

The `claims/` files are original synthetic inputs for the read-only receipt
claim-range example. `2026-04_ledger.csv` / `2026-04_bank.json` form one
accepted month with positive, zero, negative and missing internal groups in
USD, JPY and EUR. `receipts.csv` and `relations.csv` are synthetic bank
receipts and allowed receipt-to-group relations, including a greedy-claim
counterexample and a relation that is legal but unused in every optimum. They
are not real bank receipts, customers or settlement data.
