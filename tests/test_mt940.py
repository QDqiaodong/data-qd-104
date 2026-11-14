"""Tests for offline strict-MT940 collection (settlement_audit.mt940).

The fixtures are hand-computed synthetic statements. Every amount assertion is
checked against manual minor-unit arithmetic; no float is used anywhere.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from settlement_audit.input import load
from settlement_audit.mt940 import (
    AUDIT_FILENAME,
    BANK_FILENAME,
    MT940Error,
    assemble,
    build_outputs,
    collect,
    format_minor,
    load_mapping,
    parse_statements,
    unsigned_minor,
)
from settlement_audit.reconcile import reconcile

PROJECT = Path(__file__).resolve().parents[1]
DATA = PROJECT / "data" / "mt940"


def block(account, statement, page, opening, entries, closing,
          page_marker=None):
    """Render one strict MT940 page; entries are (date, mark, amount, narrative)."""
    lines = [f":25:{account}", f":28C:{statement}/{page}"]
    lines.append(opening)
    for when, mark, amount, narrative in entries:
        lines.append(f":61:{when}{mark}{amount}")
        lines.append(f":86:{narrative}")
    lines.append(closing)
    return "\n".join(lines) + "\n"


def bal(tag, dc, when, currency, amount):
    return f":{tag}:{dc}{when}{currency}{amount}"


class MT940HelperTests(unittest.TestCase):
    def test_unsigned_minor_reuses_currency_precision_and_bounds(self):
        self.assertEqual(unsigned_minor("100.00", "USD", "x", 1), 10000)
        self.assertEqual(unsigned_minor("1000", "JPY", "x", 1), 1000)
        # JPY scale is zero: any fractional digit is excess precision.
        for bad in ("1.1", "1.001", "1.0"):
            with self.assertRaises(MT940Error):
                unsigned_minor(bad, "JPY", "x", 1)
        # Sign characters are never part of MT940 amount text; precision,
        # bounds and unknown currencies are rejected like money.minor.
        for bad in ("-1.00", "1.001", "NaN"):
            with self.assertRaises(MT940Error):
                unsigned_minor(bad, "USD", "x", 1)
        with self.assertRaises(MT940Error):
            unsigned_minor("1000000000001", "USD", "x", 1)
        with self.assertRaises(MT940Error):
            unsigned_minor("1", "GBP", "x", 1)

    def test_format_minor_round_trips_per_currency(self):
        self.assertEqual(format_minor(12700, "USD"), "127.00")
        self.assertEqual(format_minor(-2000, "EUR"), "-20.00")
        self.assertEqual(format_minor(0, "JPY"), "0")
        self.assertEqual(format_minor(-5, "JPY"), "-5")


class HandComputedSingleStatementTests(unittest.TestCase):
    """Independent hand calculation of credits, debits and reversals."""

    def setUp(self):
        # Opening C 1000.00 USD.
        # entries: +100.00 (C), -20.00 (D), -3.00 (RC reversal of credit),
        #          +10.00 (RD reversal of debit) -> net +87.00
        # closing = 1000.00 + 87.00 = 1087.00
        self.text = block(
            "ACCT1", "77", 1,
            bal("60F", "C", "260105", "USD", "1000.00"),
            [("260105", "C", "100.00", "PAY"),
             ("260106", "D", "20.00", "REFUND"),
             ("260107", "RC", "3.00", "FEE"),
             ("260108", "RD", "10.00", "REVERSED DEBIT")],
            bal("62F", "C", "260110", "USD", "1087.00"))
        self.pages = parse_statements(self.text.encode("utf-8"), "unit.mt940")
        self.accounts = assemble(self.pages)
        self.mapping = {("ACCT1", "USD"): "S1"}

    def test_marks_and_hand_computed_net(self):
        page = self.accounts[0]["statements"][0]["pages"][0]
        signed = [t["signed_minor"] for t in page["transactions"]]
        self.assertEqual(signed, [10000, -2000, -300, 1000])
        self.assertEqual(sum(signed), 8700)
        bank_rows, audit = build_outputs(
            self.accounts, self.mapping,
            [{"name": "unit.mt940"}], "0" * 64)
        # bank.json carries the signed entry net, never the closing balance.
        self.assertEqual(bank_rows, [
            {"settlement_id": "S1", "currency": "USD", "amount": "87.00"}
        ])
        net = audit["settlement_nets"][0]
        self.assertEqual(net["signed_minor"], 8700)
        self.assertEqual(net["amount"], "87.00")
        self.assertEqual(
            net["basis"][0]["signed_minor"], 8700)
        # Balances stay independently recorded and signed by C/D only.
        self.assertEqual(page["opening"]["signed_minor"], 100000)
        self.assertEqual(page["closing"]["signed_minor"], 108700)
        self.assertTrue(page["closing"]["dc"], "C")

    def test_negative_debit_balance_and_negative_net(self):
        text = block(
            "ACCT2", "1", 1,
            bal("60F", "D", "260201", "EUR", "50.00"),
            [("260202", "D", "30.00", "OUT")],
            bal("62F", "D", "260203", "EUR", "80.00"))
        accounts = assemble(parse_statements(text.encode(), "d.mt940"))
        page = accounts[0]["statements"][0]["pages"][0]
        self.assertEqual(page["opening"]["signed_minor"], -5000)
        self.assertEqual(page["closing"]["signed_minor"], -8000)
        bank_rows, _ = build_outputs(
            accounts, {("ACCT2", "EUR"): "S9"}, [], "0" * 64)
        self.assertEqual(bank_rows[0]["amount"], "-30.00")

    def test_zero_net_group_is_retained(self):
        text = block(
            "ACCT3", "1", 1,
            bal("60F", "C", "260301", "JPY", "100"),
            [("260301", "C", "100", "IN"),
             ("260302", "D", "100", "OUT")],
            bal("62F", "C", "260302", "JPY", "100"))
        accounts = assemble(parse_statements(text.encode(), "z.mt940"))
        bank_rows, audit = build_outputs(
            accounts, {("ACCT3", "JPY"): "SZ"}, [], "0" * 64)
        self.assertEqual(bank_rows, [
            {"settlement_id": "SZ", "currency": "JPY", "amount": "0"}])
        self.assertEqual(audit["summary"]["zero_net_groups"], 1)


class CrossPageTests(unittest.TestCase):
    def setUp(self):
        # Statement 55, two pages. Page1 opening C 200.00, entries +50/-10
        # -> closing M 240.00. Page2 opens M 240.00, entry -40 -> F 200.00.
        self.page1 = block(
            "BIG", "55", 1,
            bal("60F", "C", "260101", "USD", "200.00"),
            [("260101", "C", "50.00", "A"), ("260102", "D", "10.00", "B")],
            bal("62M", "C", "260103", "USD", "240.00"))
        self.page2 = block(
            "BIG", "55", 2,
            bal("60M", "C", "260103", "USD", "240.00"),
            [("260104", "D", "40.00", "C")],
            bal("62F", "C", "260105", "USD", "200.00"))

    def test_continuous_pages_link_and_net_is_entry_sum(self):
        accounts = assemble(parse_statements(
            (self.page1 + self.page2).encode(), "cp.mt940"))
        statement = accounts[0]["statements"][0]
        self.assertEqual([p["page_no"] for p in statement["pages"]], [1, 2])
        audit_pages = build_outputs(
            accounts, {("BIG", "USD"): "S"}, [], "0")[1]["accounts"][0][
            "statements"][0]["pages"]
        self.assertEqual(audit_pages[1]["continuation"], {
            "previous_page": 1,
            "previous_closing_line": 8,
            "date_link_ok": True,
            "balance_link_ok": True,
        })
        bank_rows, audit = build_outputs(
            accounts, {("BIG", "USD"): "S"}, [], "0")
        # 50 - 10 - 40 = 0 net; balances return to 200.00 but must not be used.
        self.assertEqual(bank_rows[0]["amount"], "0.00")
        self.assertEqual(audit["settlement_nets"][0]["basis"][0]["pages"],
                         [1, 2])

    def test_missing_page_fails_whole_batch(self):
        pages = parse_statements(self.page2.encode(), "p2.mt940")
        with self.assertRaisesRegex(MT940Error, "continuously from 1"):
            assemble(pages)

    def test_bad_first_and_last_markers_rejected(self):
        bad_first = self.page1.replace(":60F:", ":60M:", 1)
        with self.assertRaisesRegex(MT940Error, "opening must be :60F:"):
            assemble(parse_statements(bad_first.encode(), "x.mt940"))
        bad_last = self.page2.replace(":62F:", ":62M:", 1)
        with self.assertRaisesRegex(MT940Error, "closing must be :62F:"):
            assemble(parse_statements(
                (self.page1 + bad_last).encode(), "x.mt940"))
        # Single page must carry F on both ends, M is rejected.
        single = block(
            "Q", "1", 1,
            bal("60F", "C", "260101", "USD", "1.00"),
            [("260101", "C", "1.00", "x")],
            bal("62M", "C", "260102", "USD", "2.00"))
        with self.assertRaisesRegex(MT940Error, "closing must be :62F:"):
            assemble(parse_statements(single.encode(), "s.mt940"))

    def test_broken_balance_and_date_links_rejected(self):
        # Break the opening balance only; the page stays internally balanced
        # (closing is shifted by the same unit) so the cross-page link fires.
        broken = self.page2.replace(
            ":60M:C260103USD240.00", ":60M:C260103USD241.00", 1).replace(
            ":62F:C260105USD200.00", ":62F:C260105USD201.00", 1)
        with self.assertRaisesRegex(MT940Error, "does not link"):
            assemble(parse_statements(
                (self.page1 + broken).encode(), "x.mt940"))
        stale = self.page2.replace(":60M:C260103", ":60M:C260102", 1)
        with self.assertRaisesRegex(MT940Error, "opening date"):
            assemble(parse_statements(
                (self.page1 + stale).encode(), "x.mt940"))

    def test_page_imbalance_fails(self):
        bad = self.page1.replace(":62M:C260103USD240.00",
                                 ":62M:C260103USD241.00")
        with self.assertRaisesRegex(MT940Error, "imbalance"):
            assemble(parse_statements(
                (bad + self.page2).encode(), "x.mt940"))


class ShuffleAndBatchTests(unittest.TestCase):
    def setUp(self):
        # Two accounts, two statements, pages deliberately shuffled.
        self.a1p1 = block(
            "A1", "9", 1, bal("60F", "C", "260101", "USD", "10.00"),
            [("260101", "C", "5.00", "x")],
            bal("62M", "C", "260102", "USD", "15.00"))
        self.a1p2 = block(
            "A1", "9", 2, bal("60M", "C", "260102", "USD", "15.00"),
            [("260103", "D", "2.00", "y")],
            bal("62F", "C", "260104", "USD", "13.00"))
        self.a2 = block(
            "A2", "3", 1, bal("60F", "C", "260201", "JPY", "100"),
            [("260201", "C", "7", "z")],
            bal("62F", "C", "260202", "JPY", "107"))

    def test_shuffled_multi_file_assembly(self):
        shuffled = self.a2 + self.a1p2 + self.a1p1
        # Locate the A1 page-1 payment line inside the shuffled capture.
        expected_line = shuffled.rstrip("\n").split("\n").index(
            ":61:260101C5.00") + 1
        accounts = assemble(parse_statements(
            shuffled.encode(), "shuffle.mt940"))
        self.assertEqual([a["account"] for a in accounts], ["A1", "A2"])
        s1 = accounts[0]["statements"][0]
        self.assertEqual([p["page_no"] for p in s1["pages"]], [1, 2])
        # Original source line numbers survive the reorder.
        self.assertEqual(
            s1["pages"][0]["transactions"][0]["line"], expected_line)

    def test_duplicate_page_even_equivalent_is_rejected(self):
        with self.assertRaisesRegex(MT940Error, "duplicate page"):
            assemble(parse_statements(
                (self.a1p1 + self.a1p1 + self.a1p2).encode(), "dup.mt940"))

    def test_multiple_statements_same_account_aggregate_to_one_settlement(self):
        another = block(
            "A1", "10", 1, bal("60F", "C", "260301", "USD", "0.00"),
            [("260301", "C", "4.00", "q")],
            bal("62F", "C", "260302", "USD", "4.00"))
        accounts = assemble(parse_statements(
            (self.a1p1 + self.a1p2 + another).encode(), "m.mt940"))
        bank_rows, audit = build_outputs(
            accounts, {("A1", "USD"): "S1"}, [], "0")
        # statement 9 net = 5 - 2 = 3; statement 10 net = 4 -> total 7.
        self.assertEqual(bank_rows[0]["amount"], "7.00")
        basis = audit["settlement_nets"][0]["basis"]
        self.assertEqual([b["statement_no"] for b in basis], [9, 10])

    def test_multi_currency_accounts_stay_separate(self):
        jpy_account = block(
            "A1", "11", 1, bal("60F", "C", "260401", "JPY", "100"),
            [("260401", "D", "30", "w")],
            bal("62F", "C", "260402", "JPY", "70"))
        accounts = assemble(parse_statements(
            (self.a1p1 + self.a1p2 + jpy_account).encode(), "mc.mt940"))
        mapping = {("A1", "USD"): "SU", ("A1", "JPY"): "SJ"}
        bank_rows, _ = build_outputs(accounts, mapping, [], "0")
        self.assertEqual(sorted(bank_rows, key=lambda r: r["currency"]), [
            {"settlement_id": "SJ", "currency": "JPY", "amount": "-30"},
            {"settlement_id": "SU", "currency": "USD", "amount": "3.00"},
        ])


class StrictSyntaxTests(unittest.TestCase):
    BASE = block("A", "1", 1, bal("60F", "C", "260101", "USD", "1.00"),
                 [("260101", "C", "1.00", "n")],
                 bal("62F", "C", "260102", "USD", "2.00"))

    def _reject(self, text, pattern):
        with self.assertRaisesRegex(MT940Error, pattern):
            parse_statements(text.encode(), "bad.mt940")

    def test_unknown_tag_rejected(self):
        self._reject(self.BASE.replace(":25:A", ":20:X\n:25:A"), "unknown")

    def test_bad_order_rejected(self):
        # :28C: appearing after the opening balance instead of before it.
        swapped = (
            ":25:A\n"
            ":60F:C260101USD1.00\n"
            ":28C:1/1\n"
            ":61:260101C1.00\n:86:n\n"
            ":62F:C260102USD2.00\n")
        self._reject(swapped, "bad order")
        # An entry with no page header at all.
        self._reject(":61:260101C1.00\n", "bad order")
        # A closing balance with nothing preceding it.
        self._reject(":62F:C260102USD2.00\n", "bad order")

    def test_malformed_lines_rejected(self):
        self._reject(self.BASE.replace(":25:A", ":25:lowercase"), "malformed :25:")
        self._reject(self.BASE.replace(":28C:1/1", ":28C:1/0"), "malformed :28C:")
        self._reject(self.BASE.replace(
            ":60F:C260101USD1.00", ":60F:X260101USD1.00"), "malformed balance")
        self._reject(self.BASE.replace(
            ":61:260101C1.00", ":61:260101X1.00"), "malformed :61:")
        self._reject(self.BASE.replace(":86:n", ":86:"), "must not be empty")

    def test_dates_cover_20xx_and_require_real_calendar(self):
        # YY 00..99 is interpreted strictly as 2000..2099; a leap day in a
        # non-leap year and impossible calendar days are rejected.
        leap = self.BASE.replace("260101", "230229", 1).replace(
            "260102", "230229", 1)
        self._reject(leap, "invalid calendar date")
        impossible = self.BASE.replace("260102", "260231")
        self._reject(impossible, "invalid calendar date")
        valid = self.BASE.replace("260101", "991231", 1)
        pages = parse_statements(valid.encode(), "ok.mt940")
        self.assertEqual(pages[0]["opening"]["date"], "2099-12-31")
        leap_ok = self.BASE.replace("260101", "240229", 1).replace(
            "260102", "240301", 1)
        pages = parse_statements(leap_ok.encode(), "ok.mt940")
        self.assertEqual(pages[0]["opening"]["date"], "2024-02-29")

    def test_precision_and_currency_drift_rejected(self):
        self._reject(self.BASE.replace("1.00", "1.001"), "precision")
        drift = self.BASE.replace(":62F:C260102USD2.00",
                                  ":62F:C260102EUR2.00")
        with self.assertRaisesRegex(MT940Error, "currency mismatch"):
            assemble(parse_statements(drift.encode(), "d.mt940"))

    def test_continuation_line_without_narrative_rejected(self):
        self._reject(self.BASE.replace(
            ":25:A", "ORPHAN CONTINUATION\n:25:A"), "without a preceding :86:")

    def test_entry_date_outside_page_span_rejected(self):
        bad = block("A", "1", 1, bal("60F", "C", "260105", "USD", "1.00"),
                    [("260104", "C", "1.00", "early")],
                    bal("62F", "C", "260106", "USD", "2.00"))
        with self.assertRaisesRegex(MT940Error, "outside page span"):
            assemble(parse_statements(bad.encode(), "span.mt940"))

    def test_truncated_page_without_closing_rejected(self):
        self._reject(self.BASE.split(":62F:")[0], "closing balance")
        self._reject("", "no MT940 statement pages")


class MappingTests(unittest.TestCase):
    def _mapping_bytes(self, rows):
        return json.dumps({"mappings": rows}).encode()

    def test_strict_mapping_shape(self):
        good = self._mapping_bytes([
            {"account": "A", "currency": "USD", "settlement_id": "S1"}])
        self.assertEqual(load_mapping(good, "m.json"), {("A", "USD"): "S1"})
        for payload in (
            {}, {"mappings": []}, {"mappings": [{}]},
            {"mappings": [{"account": "A", "currency": "USD",
                           "settlement_id": "S1", "extra": "x"}]},
            {"mappings": [{"account": "A", "currency": "GBP",
                           "settlement_id": "S1"}]},
            {"other": []},
        ):
            with self.assertRaises(MT940Error):
                load_mapping(json.dumps(payload).encode(), "m.json")

    def test_duplicate_mapping_rejected(self):
        payload = self._mapping_bytes([
            {"account": "A", "currency": "USD", "settlement_id": "S1"},
            {"account": "A", "currency": "USD", "settlement_id": "S2"}])
        with self.assertRaisesRegex(MT940Error, "duplicate mapping"):
            load_mapping(payload, "m.json")

    def test_missing_and_unused_mapping_fail(self):
        text = block("A", "1", 1, bal("60F", "C", "260101", "USD", "1.00"),
                     [("260101", "C", "1.00", "n")],
                     bal("62F", "C", "260102", "USD", "2.00"))
        accounts = assemble(parse_statements(text.encode(), "t.mt940"))
        with self.assertRaisesRegex(MT940Error, "missing account/currency"):
            build_outputs(accounts, {}, [], "0")
        with self.assertRaisesRegex(MT940Error, "unused mapping"):
            build_outputs(
                accounts, {("A", "USD"): "S", ("B", "USD"): "T"}, [], "0")


class CollectPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.page1 = block(
            "A1", "9", 1, bal("60F", "C", "260101", "USD", "10.00"),
            [("260101", "C", "5.00", "x"),
             ("260102", "D", "5.00", "y")],
            bal("62M", "C", "260102", "USD", "10.00"))
        self.page2 = block(
            "A1", "9", 2, bal("60M", "C", "260102", "USD", "10.00"),
            [("260103", "C", "7.00", "z")],
            bal("62F", "C", "260104", "USD", "17.00"))
        (self.root / "p1.mt940").write_text(self.page1)
        (self.root / "p2.mt940").write_text(self.page2)
        self.mapping_path = self.root / "map.json"
        self.mapping_path.write_text(json.dumps({"mappings": [
            {"account": "A1", "currency": "USD", "settlement_id": "S1"}]}))

    def test_collect_publishes_both_files_and_legacy_loads(self):
        out = self.root / "release"
        audit = collect([self.root / "p1.mt940", self.root / "p2.mt940"],
                        self.mapping_path, out)
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         sorted([AUDIT_FILENAME, BANK_FILENAME]))
        bank = json.loads((out / BANK_FILENAME).read_text())
        self.assertEqual(bank, [
            {"settlement_id": "S1", "currency": "USD", "amount": "7.00"}])
        # Audit carries raw excerpt, line numbers, narrative and basis.
        first_tx = audit["accounts"][0]["statements"][0]["pages"][0][
            "transactions"][0]
        self.assertEqual(first_tx["line"], 4)
        self.assertEqual(first_tx["narrative"], "x")
        self.assertTrue(audit["settlement_nets"][0]["basis"])
        # Legacy entry point accepts the generated bank.json unchanged.
        ledger = self.root / "ledger.csv"
        ledger.write_text(
            "event_id,kind,original_id,settlement_id,currency,amount\n"
            "p,payment,,S1,USD,7.00\n")
        entries, bank_map, _ = load(ledger, out / BANK_FILENAME)
        report = reconcile(entries, bank_map)
        self.assertEqual(report["summary"]["balanced"], 1)

    def test_failure_leaves_no_half_directory(self):
        out = self.root / "broken"
        bad = self.root / "bad.mt940"
        bad.write_text(self.page1.replace("10.00", "11.00", 1))
        with self.assertRaises(MT940Error):
            collect([bad, self.root / "p2.mt940"], self.mapping_path, out)
        self.assertFalse(out.exists())
        # No stray temp directories remain beside the intended output.
        self.assertEqual([p for p in self.root.iterdir()
                          if p.name.startswith(".broken")], [])

    def test_existing_output_is_never_overwritten(self):
        out = self.root / "release"
        collect([self.root / "p1.mt940", self.root / "p2.mt940"],
                self.mapping_path, out)
        with self.assertRaisesRegex(MT940Error, "already exists"):
            collect([self.root / "p1.mt940", self.root / "p2.mt940"],
                    self.mapping_path, out)

    def test_deterministic_output_bytes(self):
        first = self.root / "r1"
        second = self.root / "r2"
        collect([self.root / "p2.mt940", self.root / "p1.mt940"],
                self.mapping_path, first)
        collect([self.root / "p1.mt940", self.root / "p2.mt940"],
                self.mapping_path, second)
        for name in (BANK_FILENAME, AUDIT_FILENAME):
            self.assertEqual((first / name).read_bytes(),
                             (second / name).read_bytes())


class BundledSampleCLITests(unittest.TestCase):
    """The real CLI runs end to end against the bundled synthetic captures."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name) / "sample"

    def test_cli_collect_and_failure_exit_code(self):
        cmd = [sys.executable, "-m", "settlement_audit.mt940", "collect",
               "--mt940", str(DATA / "capture_part1.mt940"),
               str(DATA / "capture_part2.mt940"),
               "--mapping", str(DATA / "account_mapping.json"),
               "--output", str(self.out)]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                cwd=PROJECT)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "collected")
        bank = json.loads((self.out / BANK_FILENAME).read_text())
        self.assertEqual(bank, [
            {"settlement_id": "S1", "currency": "USD", "amount": "127.00"},
            {"settlement_id": "S2", "currency": "JPY", "amount": "0"},
            {"settlement_id": "S3", "currency": "EUR", "amount": "-20.00"},
        ])
        audit = json.loads((self.out / AUDIT_FILENAME).read_text())
        self.assertEqual(audit["summary"], {
            "accounts": 3, "statements": 3, "pages": 4,
            "transactions": 10, "settlement_groups": 3,
            "zero_net_groups": 1})

        # A malformed extra capture fails with exit 2 and writes nothing.
        bad_dir = Path(self.temp.name) / "bad"
        bad_file = Path(self.temp.name) / "bad.mt940"
        bad_file.write_text(":20:FORBIDDEN\n")
        failed = subprocess.run(
            [sys.executable, "-m", "settlement_audit.mt940", "collect",
             "--mt940", str(bad_file),
             "--mapping", str(DATA / "account_mapping.json"),
             "--output", str(bad_dir)],
            capture_output=True, text=True, cwd=PROJECT)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("unknown", failed.stderr)
        self.assertFalse(bad_dir.exists())


if __name__ == "__main__":
    unittest.main()
