# Settlement reconciliation v1

Inputs: one full ledger export CSV and an independent bank statement JSON list.
Exact headers/keys match data/. IDs are nonempty. No real account, transaction,
credentials or bank API is used. Monetary values are strings, converted to exact
minor-unit integers: USD/EUR 2 decimal places, JPY 0. Ledger amounts >0, bank
amounts may be signed or zero; <=10^12 minor units absolute. No float money or FX.

Ledger event identity is event_id. Identical canonical duplicates (including
100.0 vs 100.00) count once; conflicting IDs fail. payment adds, refund/fee
subtract. Only refunds carry original_id, referencing a same-currency payment
present in the export; total distinct refunds may not exceed that payment.
Refund settlement_id may differ from the original payment. Fees have no parent.
Bank key is settlement_id/currency; any duplicate bank key fails.

Reconciliation grain is settlement_id/currency. Expected = payments-refunds-fees;
difference = bank - expected. Status: balanced, amount_mismatch, missing_bank,
missing_ledger. Missing values stay null, not zero. Currencies are never combined.
These are reconciliation facts, not automatic fraud or payment decisions.

Output audit.sqlite contains normalized ledger, bank, reconciliation, source_hashes;
JSON exposes every group and event counts, exceptions.csv exposes unresolved groups.
Unresolved groups are valid analysis output (CLI exit 0); malformed contracts fail
before writing new output. Both exports empty is rejected. Header-only ledger or
empty bank is otherwise supported. Re-running performs deterministic full-export
recomputation/replaces these tables; no incremental history or accounting journal.
DB transaction commits before report files. Single writer; files are not a
crash-atomic multi-file snapshot. CSV should be imported as text for identifiers.
