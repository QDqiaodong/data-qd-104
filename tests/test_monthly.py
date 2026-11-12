"""Tests for continuous monthly ingestion and cross-month refund verification.

The end-to-end tests drive the real CLI through subprocesses, read and write
real CSV/JSON files, inspect the actual SQLite store and compare generated
historical reports byte-for-byte. All data is original synthetic offline data.
"""
import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from settlement_audit.monthly import (
    MonthlyError, ingest, next_period, parse_period, report,
)

ROOT = Path(__file__).resolve().parents[1]
MONTHLY_DATA = ROOT / "data" / "monthly"
HEADER = "event_id,kind,original_id,settlement_id,currency,amount\n"
BANK_KEYS = ["settlement_id", "currency", "amount"]


def write_ledger(path, rows):
    lines = [HEADER.rstrip("\n")]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_bank(path, rows):
    payload = [dict(zip(BANK_KEYS, row)) for row in rows]
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


class MonthlyFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.db = self.root / "monthly.sqlite"

    def ledger(self, period):
        return self.root / f"{period}_ledger.csv"

    def bank(self, period):
        return self.root / f"{period}_bank.json"

    def report(self, period):
        return self.root / f"report_{period}.json"

    def ingest_cli(self, period, ledger=None, bank=None, timeout=30,
                   env_extra=None, busy_timeout=None):
        command, env = self._ingest_command(
            period, ledger, bank, env_extra, busy_timeout)
        return subprocess.run(command, cwd=ROOT, capture_output=True,
                              text=True, timeout=timeout, env=env)

    def ingest_cli_popen(self, period, ledger=None, bank=None,
                         env_extra=None, busy_timeout=None):
        command, env = self._ingest_command(
            period, ledger, bank, env_extra, busy_timeout)
        return subprocess.Popen(command, cwd=ROOT,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)

    def _ingest_command(self, period, ledger, bank, env_extra, busy_timeout):
        ledger = ledger or self.ledger(period)
        bank = bank or self.bank(period)
        # -I (isolated mode) ignores cwd, PYTHONPATH and user site packages,
        # so the project root is injected explicitly before module import.
        launcher = (
            "import runpy,sys;"
            f"sys.path.insert(0,{str(ROOT)!r});"
            "runpy.run_module('settlement_audit.monthly',"
            "run_name='__main__',alter_sys=True)")
        command = [sys.executable, "-I", "-c", launcher,
                   "ingest", "--period", period,
                   "--ledger", str(ledger), "--bank", str(bank),
                   "--db", str(self.db)]
        if busy_timeout is not None:
            command += ["--busy-timeout", str(busy_timeout)]
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return command, env

    def report_cli(self, period, output=None, env_extra=None):
        output = output or self.report(period)
        launcher = (
            "import runpy,sys;"
            f"sys.path.insert(0,{str(ROOT)!r});"
            "runpy.run_module('settlement_audit.monthly',"
            "run_name='__main__',alter_sys=True)")
        command = [sys.executable, "-I", "-c", launcher,
                   "report", "--period", period,
                   "--db", str(self.db), "--output", str(output)]
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(command, cwd=ROOT, capture_output=True,
                              text=True, timeout=30, env=env)

    def connect(self):
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        return connection


class MonthlyEndToEndTests(unittest.TestCase):
    """Real CLI subprocesses across three consecutive months."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = MonthlyFixture(self.temp.name)
        for period in ("2026-01", "2026-02", "2026-03"):
            self.fx.ledger(period).write_bytes(
                (MONTHLY_DATA / f"{period}_ledger.csv").read_bytes())
            self.fx.bank(period).write_bytes(
                (MONTHLY_DATA / f"{period}_bank.json").read_bytes())

    def _ingest_all(self):
        for period in ("2026-01", "2026-02", "2026-03"):
            result = self.fx.ingest_cli(period)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["status"], "imported")

    def test_three_month_cli_flow_balances_and_historical_report(self):
        self._ingest_all()
        with self.fx.connect() as db:
            # Global event identity, month-scoped bank grain.
            self.assertEqual(
                db.execute("SELECT count(*) FROM events").fetchone()[0], 6)
            self.assertEqual(
                db.execute("SELECT count(*) FROM months").fetchone()[0], 3)
            self.assertEqual(
                db.execute("SELECT count(*) FROM bank_facts").fetchone()[0], 3)
            # Same settlement_id/currency legitimately appears in every month.
            rows = db.execute(
                "SELECT period,expected_minor,bank_minor,difference_minor,status "
                "FROM reconciliation ORDER BY period").fetchall()
            self.assertEqual([tuple(r) for r in rows], [
                ("2026-01", 9700, 9700, 0, "balanced"),
                ("2026-02", -5100, -5100, 0, "balanced"),
                ("2026-03", -5000, -5000, 0, "balanced"),
            ])
            balance = db.execute(
                "SELECT * FROM payment_balances").fetchone()
            self.assertEqual(tuple(balance[k] for k in (
                "original_minor", "refunded_minor", "remaining_minor")),
                (10000, 10000, 0))
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM months WHERE ledger_raw IS NOT NULL "
                    "AND bank_raw IS NOT NULL").fetchone()[0], 3)

        # February report shows only 5000/10000 refunded at February end.
        result = self.fx.report_cli("2026-02")
        self.assertEqual(result.returncode, 0, result.stderr)
        report_feb = json.loads(self.fx.report("2026-02").read_text())
        self.assertEqual(report_feb["schema_version"], 2)
        self.assertEqual(report_feb["summary"],
                         {"groups": 1, "balanced": 1, "unresolved": 0})
        balance_feb = report_feb["payment_balances_at_month_end"][0]
        self.assertEqual(balance_feb["refunded_minor"], 5000)
        self.assertEqual(balance_feb["remaining_minor"], 5000)
        refunds = report_feb["refunds"]
        self.assertEqual({r["refund_event_id"] for r in refunds}, {"r1", "r2"})
        self.assertTrue(all(
            r["original_payment_id"] == "p1"
            and r["original_payment_period"] == "2026-01"
            and r["refund_ledger_record"] in (1, 2)
            and r["payment_ledger_record"] == 1
            for r in refunds))
        # No later-month information leaks into the historical report.
        self.assertNotIn("2026-03", json.dumps(report_feb))
        self.assertNotIn("r3", json.dumps(report_feb))
        # Deterministic, machine-independent bytes.
        bytes_feb = self.fx.report("2026-02").read_bytes()
        result = self.fx.report_cli("2026-02")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(bytes_feb, self.fx.report("2026-02").read_bytes())

    def test_february_replay_keeps_balance_and_historical_bytes(self):
        self._ingest_all()
        report_before = self.fx.report("2026-02")
        result = self.fx.report_cli("2026-02")
        self.assertEqual(result.returncode, 0, result.stderr)
        bytes_before_march_report = report_before.read_bytes()

        # Replay February after March exhausted the remaining principal.
        result = self.fx.ingest_cli("2026-02")
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "replayed")
        self.assertEqual(receipt["stored_duplicate_ledger_rows"], 0)

        with self.fx.connect() as db:
            balance = db.execute(
                "SELECT * FROM payment_balances").fetchone()
            self.assertEqual(balance["refunded_minor"], 10000)
            self.assertEqual(balance["remaining_minor"], 0)
            # First captured source bytes are untouched.
            month = db.execute(
                "SELECT ledger_sha256,bank_sha256,ledger_raw FROM months "
                "WHERE period='2026-02'").fetchone()
            self.assertEqual(
                month["ledger_raw"],
                (MONTHLY_DATA / "2026-02_ledger.csv").read_bytes())
        self.fx.report_cli("2026-02")
        self.assertEqual(bytes_before_march_report,
                         self.fx.report("2026-02").read_bytes())

    def test_replay_accepts_reordered_equivalent_presentation(self):
        self._ingest_all()
        # Different row order, equivalent amount spelling, an extra equivalent
        # duplicate row: canonical facts identical => replayed, first source kept.
        reordered = self.fx.root / "feb_reordered.csv"
        reordered.write_text(
            HEADER
            + "f2,fee,,SM,USD,1\n"
            + "r2,refund,p1,SM,USD,20\n"
            + "r1,refund,p1,SM,USD,30\n"
            + "r1,refund,p1,SM,USD,30.00\n")
        result = self.fx.ingest_cli("2026-02", ledger=reordered)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "replayed")
        self.assertEqual(receipt["input_duplicate_ledger_rows"], 1)
        self.assertEqual(receipt["stored_duplicate_ledger_rows"], 0)
        with self.fx.connect() as db:
            stored = db.execute(
                "SELECT ledger_sha256 FROM months WHERE period='2026-02'"
            ).fetchone()
            self.assertEqual(
                stored["ledger_sha256"], receipt["stored_sha256"]["ledger"])
            self.assertNotEqual(
                stored["ledger_sha256"], receipt["input_sha256"]["ledger"])

    def test_replay_conflicts_do_not_mutate_the_month(self):
        self._ingest_all()
        original_bytes = self.fx.db.read_bytes()
        cases = {
            "missing_row": (
                HEADER + "r1,refund,p1,SM,USD,30.00\n"
                + "f2,fee,,SM,USD,1.00\n", None),
            "added_event": (
                (MONTHLY_DATA / "2026-02_ledger.csv").read_text()
                + "r9,refund,p1,SM,USD,1.00\n", None),
            "changed_bank": (None,
                             '[{"settlement_id":"SM","currency":"USD",'
                             '"amount":"-52.00"}]'),
        }
        for name, (ledger_text, bank_text) in cases.items():
            ledger = self.fx.root / f"{name}.csv"
            bank = self.fx.root / f"{name}.json"
            if ledger_text is not None:
                ledger.write_text(ledger_text)
            else:
                ledger.write_bytes(
                    (MONTHLY_DATA / "2026-02_ledger.csv").read_bytes())
            if bank_text is not None:
                bank.write_text(bank_text)
            else:
                bank.write_bytes(
                    (MONTHLY_DATA / "2026-02_bank.json").read_bytes())
            result = self.fx.ingest_cli("2026-02", ledger=ledger, bank=bank)
            self.assertNotEqual(result.returncode, 0, name)
            self.assertIn("immutable", result.stderr, name)
            self.assertEqual(original_bytes, self.fx.db.read_bytes(), name)

    def test_event_moved_to_new_month_is_rejected(self):
        self._ingest_all()
        write_ledger(self.fx.ledger("2026-04"),
                     [("r1", "refund", "p1", "SM", "USD", "30.00")])
        write_bank(self.fx.bank("2026-04"),
                   [("SM", "USD", "-30.00")])
        result = self.fx.ingest_cli("2026-04")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already received", result.stderr)
        with self.fx.connect() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM months WHERE period='2026-04'").fetchone())

    def test_over_budget_batch_is_fully_rejected_then_fixed(self):
        # January: p1 = 100 USD. February: two refunds totalling 50 USD,
        # leaving 50 USD. March tries 50.01 USD across two small refunds.
        write_ledger(self.fx.ledger("2026-01"),
                     [("p1", "payment", "", "SM", "USD", "100.00")])
        write_bank(self.fx.bank("2026-01"), [("SM", "USD", "100.00")])
        write_ledger(self.fx.ledger("2026-02"),
                     [("r1", "refund", "p1", "SM", "USD", "30.00"),
                      ("r2", "refund", "p1", "SM", "USD", "20.00")])
        write_bank(self.fx.bank("2026-02"), [("SM", "USD", "-50.00")])
        for period in ("2026-01", "2026-02"):
            self.assertEqual(self.fx.ingest_cli(period).returncode, 0)

        write_ledger(self.fx.ledger("2026-03"),
                     [("r3", "refund", "p1", "SM", "USD", "25.00"),
                      ("r4", "refund", "p1", "SM", "USD", "25.01"),
                      ("p2", "payment", "", "ST", "EUR", "10.00")])
        write_bank(self.fx.bank("2026-03"),
                   [("SM", "USD", "-50.01"), ("ST", "EUR", "10.00")])
        before = self.fx.db.read_bytes()
        result = self.fx.ingest_cli("2026-03")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exceed its principal", result.stderr)
        # Nothing partial survives: no month, no events, no bank facts,
        # no source rows and no principal consumed.
        self.assertEqual(before, self.fx.db.read_bytes())
        with self.fx.connect() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM months WHERE period='2026-03'").fetchone())
            self.assertEqual(db.execute(
                "SELECT count(*) FROM events WHERE period='2026-03'"
            ).fetchone()[0], 0)
            row = db.execute(
                "SELECT refunded_minor,remaining_minor FROM payment_balances "
                "WHERE payment_event_id='p1'").fetchone()
            self.assertEqual(tuple(row), (5000, 5000))

        # Correcting to exactly 50.00 succeeds on the rerun.
        write_ledger(self.fx.ledger("2026-03"),
                     [("r3", "refund", "p1", "SM", "USD", "25.00"),
                      ("r4", "refund", "p1", "SM", "USD", "25.00")])
        write_bank(self.fx.bank("2026-03"), [("SM", "USD", "-50.00")])
        result = self.fx.ingest_cli("2026-03")
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.fx.connect() as db:
            row = db.execute(
                "SELECT refunded_minor,remaining_minor FROM payment_balances "
                "WHERE payment_event_id='p1'").fetchone()
            self.assertEqual(tuple(row), (10000, 0))

        # Any further refund after zero remaining is rejected.
        write_ledger(self.fx.ledger("2026-04"),
                     [("r5", "refund", "p1", "SM", "USD", "0.01")])
        write_bank(self.fx.bank("2026-04"), [("SM", "USD", "-0.01")])
        result = self.fx.ingest_cli("2026-04")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exceed its principal", result.stderr)


class MonthlyRuleTests(unittest.TestCase):
    """Contract and state rules via the Python entry point."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = MonthlyFixture(self.temp.name)

    def _ingest(self, period, rows, bank_rows, dup_expected=None):
        write_ledger(self.fx.ledger(period), rows)
        write_bank(self.fx.bank(period), bank_rows)
        receipt = ingest(period, self.fx.ledger(period),
                         self.fx.bank(period), self.fx.db)
        return receipt

    def test_period_validation_and_sequence_including_year_rollover(self):
        for bad in ("2026", "2026-1", "2026-13", "2026-00", "abcd-01", ""):
            with self.assertRaises(MonthlyError):
                parse_period(bad)
        self.assertEqual(next_period("2026-12"), "2027-01")
        self.assertEqual(next_period("2026-01"), "2026-02")
        # First import may start at any valid month.
        self._ingest("2026-12",
                     [("p1", "payment", "", "S", "USD", "1.00")],
                     [("S", "USD", "1.00")])
        # Skipping January and landing in February is rejected.
        write_ledger(self.fx.ledger("2027-02"),
                     [("p2", "payment", "", "S", "USD", "1.00")])
        write_bank(self.fx.bank("2027-02"), [("S", "USD", "1.00")])
        with self.assertRaisesRegex(MonthlyError, "next calendar month"):
            ingest("2027-02", self.fx.ledger("2027-02"),
                   self.fx.bank("2027-02"), self.fx.db)
        # Back-filling an older month is rejected too.
        with self.assertRaisesRegex(MonthlyError, "next calendar month"):
            ingest("2026-11", self.fx.ledger("2026-12"),
                   self.fx.bank("2026-12"), self.fx.db)
        # Rollover month works.
        self._ingest("2027-01",
                     [("p2", "payment", "", "S", "USD", "1.00")],
                     [("S", "USD", "1.00")])

    def test_empty_month_occupies_a_received_month(self):
        Path(self.fx.ledger("2026-01")).write_text(HEADER)
        write_bank(self.fx.bank("2026-01"), [])
        receipt = ingest("2026-01", self.fx.ledger("2026-01"),
                         self.fx.bank("2026-01"), self.fx.db)
        self.assertEqual(receipt["status"], "imported")
        with self.fx.connect() as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM months").fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT count(*) FROM reconciliation").fetchone()[0], 0)
        # The next month must still follow immediately.
        self._ingest("2026-02",
                     [("p1", "payment", "", "S", "USD", "1.00")],
                     [("S", "USD", "1.00")])

    def test_same_month_unordered_refunds_and_equivalent_duplicates(self):
        # Refund rows appear before their same-month payment.
        rows = [
            ("r1", "refund", "p1", "S", "USD", "10.00"),
            ("r2", "refund", "p1", "S", "USD", "10.0"),
            ("p1", "payment", "", "S", "USD", "100.00"),
            ("r1", "refund", "p1", "S", "USD", "10.00"),  # equivalent dup
        ]
        receipt = self._ingest("2026-01", rows,
                               [("S", "USD", "80.00")])
        self.assertEqual(receipt["events"], 3)
        self.assertEqual(receipt["input_duplicate_ledger_rows"], 1)
        with self.fx.connect() as db:
            row = db.execute(
                "SELECT * FROM payment_balances").fetchone()
            self.assertEqual(
                (row["original_minor"], row["refunded_minor"],
                 row["remaining_minor"]), (10000, 2000, 8000))

    def test_distinct_refund_ids_count_separately(self):
        rows = [("p1", "payment", "", "S", "USD", "1.00")]
        rows += [(f"r{i}", "refund", "p1", "S", "USD", "0.25")
                 for i in range(4)]
        self._ingest("2026-01", rows, [("S", "USD", "0.00")])
        with self.fx.connect() as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM events WHERE kind='refund'"
            ).fetchone()[0], 4)
            self.assertEqual(db.execute(
                "SELECT refunded_minor FROM payment_balances"
            ).fetchone()[0], 100)

    def test_refund_rule_violations(self):
        base = [("p1", "payment", "", "S", "USD", "10.00"),
                ("p2", "payment", "", "S", "EUR", "10.00"),
                ("f1", "fee", "", "S", "USD", "1.00")]
        bad_refunds = [
            [("rx", "refund", "ghost", "S", "USD", "1.00")],
            [("rx", "refund", "f1", "S", "USD", "1.00")],
            [("rx", "refund", "p2", "S", "USD", "1.00")],
        ]
        for extra in bad_refunds:
            write_ledger(self.fx.ledger("2026-01"), base + extra)
            write_bank(self.fx.bank("2026-01"), [])
            with self.assertRaises(MonthlyError):
                ingest("2026-01", self.fx.ledger("2026-01"),
                       self.fx.bank("2026-01"), self.fx.db)
            # A failed first month leaves no monthly tables behind.
            with sqlite3.connect(self.fx.db) as db:
                tables = {r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("months", tables)

    def test_cross_month_reference_and_currency_isolation(self):
        self._ingest("2026-01",
                     [("pu", "payment", "", "SU", "USD", "10.00"),
                      ("pe", "payment", "", "SE", "EUR", "10.00")],
                     [("SU", "USD", "10.00"), ("SE", "EUR", "10.00")])
        # EUR refund cannot reference the USD payment across months.
        write_ledger(self.fx.ledger("2026-02"),
                     [("r1", "refund", "pu", "SU", "EUR", "1.00")])
        write_bank(self.fx.bank("2026-02"), [("SU", "EUR", "-1.00")])
        with self.assertRaisesRegex(MonthlyError, "currency"):
            ingest("2026-02", self.fx.ledger("2026-02"),
                   self.fx.bank("2026-02"), self.fx.db)
        write_ledger(self.fx.ledger("2026-02"),
                     [("r1", "refund", "pu", "SU", "USD", "1.00"),
                      ("r2", "refund", "pe", "SE", "EUR", "2.00")])
        write_bank(self.fx.bank("2026-02"),
                   [("SU", "USD", "-1.00"), ("SE", "EUR", "-2.00")])
        ingest("2026-02", self.fx.ledger("2026-02"),
               self.fx.bank("2026-02"), self.fx.db)
        with self.fx.connect() as db:
            rows = {r["payment_event_id"]: r for r in db.execute(
                "SELECT * FROM payment_balances")}
            self.assertEqual(rows["pu"]["remaining_minor"], 900)
            self.assertEqual(rows["pe"]["remaining_minor"], 800)

    def test_jpy_precision_negative_bank_zero_net_and_missing_sides(self):
        from settlement_audit.money import minor
        self.assertEqual(minor("1000", "JPY"), 1000)
        rows = [("p1", "payment", "", "SJ", "JPY", "1000"),
                ("r1", "refund", "p1", "SJ", "JPY", "1000")]
        # True zero net vs missing bank stays distinct.
        self._ingest("2026-01", rows, [("SJ", "JPY", "0")])
        payload = report("2026-01", self.fx.db, self.fx.report("2026-01"))
        group = payload["settlements"][0]
        self.assertEqual(group["status"], "balanced")
        self.assertEqual(group["bank_minor"], 0)
        self._ingest("2026-02",
                     [("p2", "payment", "", "SM", "JPY", "500")], [])
        payload = report("2026-02", self.fx.db, self.fx.report("2026-02"))
        self.assertEqual(payload["settlements"][0]["status"], "missing_bank")
        self.assertIsNone(payload["settlements"][0]["bank_minor"])
        write_bank(self.fx.bank("2026-03"), [("SX", "JPY", "-7")])
        Path(self.fx.ledger("2026-03")).write_text(HEADER)
        ingest("2026-03", self.fx.ledger("2026-03"),
               self.fx.bank("2026-03"), self.fx.db)
        payload = report("2026-03", self.fx.db, self.fx.report("2026-03"))
        group = payload["settlements"][0]
        self.assertEqual(group["status"], "missing_ledger")
        self.assertEqual(group["bank_minor"], -7)

    def test_bank_duplicate_key_within_file_rejected(self):
        write_ledger(self.fx.ledger("2026-01"),
                     [("p1", "payment", "", "S", "USD", "1.00")])
        write_bank(self.fx.bank("2026-01"),
                   [("S", "USD", "1.00"), ("S", "USD", "1.00")])
        with self.assertRaisesRegex(MonthlyError, "duplicate bank"):
            ingest("2026-01", self.fx.ledger("2026-01"),
                   self.fx.bank("2026-01"), self.fx.db)

    def test_fee_does_not_consume_refund_budget(self):
        self._ingest("2026-01",
                     [("p1", "payment", "", "S", "USD", "1.00"),
                      ("f1", "fee", "", "S", "USD", "1.00")],
                     [("S", "USD", "0.00")])
        with self.fx.connect() as db:
            row = db.execute("SELECT * FROM payment_balances").fetchone()
            self.assertEqual(row["remaining_minor"], 100)

    def test_report_requires_received_month_and_is_deterministic(self):
        with self.assertRaisesRegex(MonthlyError, "not found"):
            report("2026-01", self.fx.db, self.fx.report("2026-01"))
        self._ingest("2026-01",
                     [("p1", "payment", "", "S", "USD", "1.00")],
                     [("S", "USD", "1.00")])
        with self.assertRaisesRegex(MonthlyError, "has not been received"):
            report("2026-02", self.fx.db, self.fx.report("2026-02"))
        report("2026-01", self.fx.db, self.fx.report("2026-01"))
        first = self.fx.report("2026-01").read_bytes()
        # A later month cannot change the historical report bytes.
        self._ingest("2026-02",
                     [("r1", "refund", "p1", "S", "USD", "1.00")],
                     [("S", "USD", "-1.00")])
        report("2026-01", self.fx.db, self.fx.report("2026-01"))
        self.assertEqual(first, self.fx.report("2026-01").read_bytes())
        text = first.decode()
        for leak in (str(Path(self.temp.name)), "2026-02", "r1",
                     "received_at"):
            self.assertNotIn(leak, text)

    def test_v1_database_is_rejected_untouched(self):
        v1 = self.fx.root / "v1.sqlite"
        connection = sqlite3.connect(v1)
        connection.execute("CREATE TABLE source_hashes(name TEXT PRIMARY KEY,"
                           "sha256 TEXT)")
        connection.execute("INSERT INTO source_hashes VALUES('ledger','abc')")
        connection.commit()
        connection.close()
        before = v1.read_bytes()
        write_ledger(self.fx.ledger("2026-01"),
                     [("p1", "payment", "", "S", "USD", "1.00")])
        write_bank(self.fx.bank("2026-01"), [("S", "USD", "1.00")])
        with self.assertRaisesRegex(MonthlyError, "refuses"):
            ingest("2026-01", self.fx.ledger("2026-01"),
                   self.fx.bank("2026-01"), v1)
        self.assertEqual(before, v1.read_bytes())
        with sqlite3.connect(v1) as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM source_hashes").fetchone()[0], 1)
        with self.assertRaisesRegex(MonthlyError, "not a monthly store"):
            report("2026-01", v1, self.fx.report("x"))
        self.assertEqual(before, v1.read_bytes())

    def test_report_failure_keeps_previous_report_and_reruns(self):
        self._ingest("2026-01",
                     [("p1", "payment", "", "S", "USD", "1.00")],
                     [("S", "USD", "1.00")])
        output = self.fx.report("2026-01")
        report("2026-01", self.fx.db, output)
        before = output.read_bytes()
        with self.assertRaises(OSError):
            _report_with_fail("2026-01", self.fx.db, output)
        self.assertEqual(before, output.read_bytes())
        self.assertEqual(
            list(self.fx.root.glob(output.name + "*.tmp")), [])
        report("2026-01", self.fx.db, output)
        self.assertEqual(before, output.read_bytes())

    def test_duplicate_conflict_rejected_and_source_hash_verifiable(self):
        write_ledger(self.fx.ledger("2026-01"),
                     [("p1", "payment", "", "S", "USD", "1.00"),
                      ("p1", "payment", "", "S", "USD", "2.00")])
        write_bank(self.fx.bank("2026-01"), [("S", "USD", "1.00")])
        with self.assertRaisesRegex(MonthlyError, "conflicting"):
            ingest("2026-01", self.fx.ledger("2026-01"),
                   self.fx.bank("2026-01"), self.fx.db)

        write_ledger(self.fx.ledger("2026-01"),
                     [("p1", "payment", "", "S", "USD", "1.00")])
        ingest("2026-01", self.fx.ledger("2026-01"),
               self.fx.bank("2026-01"), self.fx.db)
        import hashlib
        with self.fx.connect() as db:
            month = db.execute("SELECT * FROM months").fetchone()
            self.assertEqual(
                month["ledger_sha256"],
                hashlib.sha256(
                    self.fx.ledger("2026-01").read_bytes()).hexdigest())
            self.assertEqual(
                month["ledger_raw"],
                self.fx.ledger("2026-01").read_bytes())


def _report_with_fail(period, db_path, output):
    old = os.environ.get("SETTLEMENT_AUDIT_REPORT_FAIL")
    os.environ["SETTLEMENT_AUDIT_REPORT_FAIL"] = "rename"
    try:
        return report(period, db_path, output)
    finally:
        if old is None:
            os.environ.pop("SETTLEMENT_AUDIT_REPORT_FAIL", None)
        else:
            os.environ["SETTLEMENT_AUDIT_REPORT_FAIL"] = old


class MonthlyConcurrencyTests(unittest.TestCase):
    """Two real CLI processes competing for one new month.

    Synchronization is fully deterministic: the hooked process creates
    ``<stage>.arrive`` flag files and blocks until the test creates
    ``<stage>.release``. No sleeps are used to guess the commit point.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = MonthlyFixture(self.temp.name)
        self.sync = Path(self.temp.name) / "sync"
        self.sync.mkdir()
        self.procs = []
        self.addCleanup(self._cleanup_procs)
        write_ledger(self.fx.ledger("2026-01"),
                     [("p1", "payment", "", "S", "USD", "100.00")])
        write_bank(self.fx.bank("2026-01"), [("S", "USD", "100.00")])
        # Establish the monthly store first so both racing processes only
        # contend on the new month, not on schema creation.
        self.assertEqual(self.fx.ingest_cli("2026-01").returncode, 0)
        write_ledger(self.fx.ledger("2026-02"),
                     [("r1", "refund", "p1", "S", "USD", "10.00")])
        write_bank(self.fx.bank("2026-02"), [("S", "USD", "-10.00")])

    def _cleanup_procs(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, ValueError):
                pass

    def _spawn(self, *args, **kwargs):
        proc = self.fx.ingest_cli_popen(*args, **kwargs)
        self.procs.append(proc)
        return proc

    def _wait_for(self, name, timeout=30):
        import time
        target = self.sync / name
        end = time.monotonic() + timeout
        while not target.exists():
            if time.monotonic() > end:
                raise AssertionError(f"timed out waiting for {name}")
            time.sleep(0.02)

    def _release(self, stage):
        (self.sync / f"{stage}.release").touch()

    def test_identical_concurrent_import_commits_once(self):
        env = {"SETTLEMENT_AUDIT_SYNC_DIR": str(self.sync)}
        first = self._spawn("2026-02", env_extra=env, busy_timeout=20)
        self._wait_for("locked.arrive")
        second = self._spawn("2026-02", busy_timeout=20)
        # The second process must be blocked on the writer lock, not failed.
        self.assertIsNone(second.poll())
        self._release("locked")
        # The hooked process passes to the second gate before committing.
        self._wait_for("before_commit.arrive")
        self._release("before_commit")
        stdout, stderr = first.communicate(timeout=30)
        second_out, second_err = second.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, stderr)
        self.assertEqual(second.returncode, 0, second_err)
        statuses = {json.loads(stdout)["status"],
                    json.loads(second_out)["status"]}
        self.assertEqual(statuses, {"imported", "replayed"})
        with self.fx.connect() as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM events WHERE period='2026-02'"
            ).fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT count(*) FROM months"
            ).fetchone()[0], 2)

    def test_conflicting_concurrent_import_cannot_overwrite(self):
        env = {"SETTLEMENT_AUDIT_SYNC_DIR": str(self.sync)}
        first = self._spawn("2026-02", env_extra=env, busy_timeout=20)
        self._wait_for("locked.arrive")
        conflict_ledger = self.fx.root / "conflict.csv"
        write_ledger(conflict_ledger,
                     [("r1", "refund", "p1", "S", "USD", "11.00")])
        second = self._spawn("2026-02", ledger=conflict_ledger,
                             busy_timeout=20)
        self.assertIsNone(second.poll())
        self._release("locked")
        self._wait_for("before_commit.arrive")
        self._release("before_commit")
        _, stderr = first.communicate(timeout=30)
        _, second_err = second.communicate(timeout=30)
        self.assertEqual(first.returncode, 0, stderr)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("immutable", second_err)
        with self.fx.connect() as db:
            amount = db.execute(
                "SELECT amount_minor FROM events WHERE event_id='r1'"
            ).fetchone()[0]
            self.assertEqual(amount, 1000)

    def test_lock_wait_has_bounded_failure(self):
        env = {"SETTLEMENT_AUDIT_SYNC_DIR": str(self.sync)}
        holder = self._spawn("2026-02", env_extra=env, busy_timeout=10)
        self._wait_for("locked.arrive")
        waiter = self.fx.ingest_cli("2026-02", busy_timeout=0.5)
        self.assertNotEqual(waiter.returncode, 0)
        self.assertIn("database lock", waiter.stderr)
        self._release("locked")
        self._wait_for("before_commit.arrive")
        self._release("before_commit")
        _, stderr = holder.communicate(timeout=30)
        self.assertEqual(holder.returncode, 0, stderr)

    def test_interruption_before_commit_rolls_back(self):
        env = {"SETTLEMENT_AUDIT_SYNC_DIR": str(self.sync)}
        before = self.fx.db.read_bytes()
        proc = self._spawn("2026-02", env_extra=env, busy_timeout=10)
        self._wait_for("locked.arrive")
        self._release("locked")
        self._wait_for("before_commit.arrive")
        proc.kill()
        proc.wait(timeout=10)
        self.assertNotEqual(proc.returncode, 0)
        # Database content is exactly as before the attempted import.
        self.assertEqual(before, self.fx.db.read_bytes())
        with self.fx.connect() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM months WHERE period='2026-02'").fetchone())
            self.assertEqual(db.execute(
                "SELECT refunded_minor FROM payment_balances"
            ).fetchone()[0], 0)
        # A clean rerun works without any manual cleanup.
        result = self.fx.ingest_cli("2026-02")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "imported")


if __name__ == "__main__":
    unittest.main()
