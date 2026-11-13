"""Tests for the independent receipt-claim range analysis.

Every test drives real files and the real CLI / Python entry point. Data is
prepared through the genuine monthly CLI from original synthetic exports, the
report is checked against an independent exhaustive enumeration on small
graphs, and the monthly store is verified to be byte-identical afterwards.

Exhaustive checks use tiny decimal amounts (for example ``0.10`` USD = 10 minor
units) so an honest enumeration over minor units stays tractable while still
exercising the real currency precision rules.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from settlement_audit.claims import (
    ClaimError, analyze, parse_receipts, parse_relations,
)

ROOT = Path(__file__).resolve().parents[1]
LEDGER_HEADER = \
    "event_id,kind,original_id,settlement_id,currency,amount\n"
RECEIPT_HEADER = "receipt_id,currency,amount\n"
RELATION_HEADER = "receipt_id,settlement_id,currency\n"


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #
def launcher(module):
    return ("import runpy,sys;"
            f"sys.path.insert(0,{str(ROOT)!r});"
            f"runpy.run_module({module!r},run_name='__main__',alter_sys=True)")


def write_ledger(path, rows):
    lines = [LEDGER_HEADER.rstrip("\n")]
    lines += [",".join(str(v) for v in row) for row in rows]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_bank(path, rows):
    payload = [{"settlement_id": s, "currency": c, "amount": a}
               for s, c, a in rows]
    Path(path).write_text(json.dumps(payload), encoding="utf-8")


def write_receipts(path, rows):
    lines = [RECEIPT_HEADER.rstrip("\n")]
    lines += [",".join(str(v) for v in row) for row in rows]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_relations(path, rows):
    lines = [RELATION_HEADER.rstrip("\n")]
    lines += [",".join(str(v) for v in row) for row in rows]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class Fixture:
    """One scratch monthly store prepared by the real monthly CLI."""

    def __init__(self, root, period="2026-04"):
        self.root = Path(root)
        self.period = period
        self.db = self.root / "monthly.sqlite"
        self.output = self.root / "claims.json"

    def ingest(self, rows, bank_rows):
        ledger = self.root / f"{self.period}_ledger.csv"
        bank = self.root / f"{self.period}_bank.json"
        write_ledger(ledger, rows)
        write_bank(bank, bank_rows)
        result = subprocess.run(
            [sys.executable, "-I", "-c", launcher("settlement_audit.monthly"),
             "ingest", "--period", self.period,
             "--ledger", str(ledger), "--bank", str(bank),
             "--db", str(self.db)],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return json.loads(result.stdout)

    def receipts(self, rows):
        path = self.root / "receipts.csv"
        write_receipts(path, rows)
        return path

    def relations(self, rows):
        path = self.root / "relations.csv"
        write_relations(path, rows)
        return path

    def run_cli(self, receipts=None, relations=None, output=None,
                period=None, env_extra=None):
        receipts = receipts or self.root / "receipts.csv"
        relations = relations or self.root / "relations.csv"
        output = output or self.output
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [sys.executable, "-I", "-c", launcher("settlement_audit.claims"),
             "--period", period or self.period, "--db", str(self.db),
             "--receipts", str(receipts), "--relations", str(relations),
             "--output", str(output)],
            cwd=ROOT, capture_output=True, text=True, timeout=60, env=env)


def prepare(fixture, groups):
    """Ingest a month whose stored expected_minor equals each group amount."""
    rows = [(f"p{index}", "payment", "", settlement_id, currency, amount)
            for index, (settlement_id, currency, amount)
            in enumerate(groups, start=1)]
    fixture.ingest(rows, [(s, c, a) for s, c, a in groups])


def cents(value):
    """A USD amount whose minor-unit value is exactly ``value``."""
    return f"0.{value:02d}"


# --------------------------------------------------------------------------- #
# Independent exhaustive verification
# --------------------------------------------------------------------------- #
def enumerate_optimum(supply, demand, allowed):
    """All allocations at maximum total claim, by exhaustive enumeration.

    ``supply`` is ``{receipt_id: int}``, ``demand`` is
    ``{group: int}``, ``allowed`` is a sorted list of ``(receipt, group)``.
    Returns ``(best_total, [allocation dict])``.
    """
    order = sorted(allowed)
    best = -1
    allocations = []

    def walk(index, receipt_left, group_left, current):
        nonlocal best
        if index == len(order):
            total = sum(current.values())
            if total > best:
                best = total
                allocations.clear()
                allocations.append(dict(current))
            elif total == best:
                allocations.append(dict(current))
            return
        receipt, group = order[index]
        cap = min(receipt_left.get(receipt, 0), group_left.get(group, 0))
        for amount in range(cap + 1):
            current[(receipt, group)] = amount
            if amount:
                receipt_left[receipt] = receipt_left.get(receipt, 0) - amount
                group_left[group] = group_left.get(group, 0) - amount
            walk(index + 1, receipt_left, group_left, current)
            if amount:
                receipt_left[receipt] += amount
                group_left[group] += amount
                if not receipt_left[receipt]:
                    del receipt_left[receipt]
                if not group_left[group]:
                    del group_left[group]
        current.pop((receipt, group), None)

    walk(0, dict(supply), dict(demand), {})
    return best, allocations


def verify_report_against_enumeration(testcase, report, currency, supply,
                                      demand, allowed):
    """Check every max total, relation range and group range exhaustively."""
    best, allocations = enumerate_optimum(supply, demand, allowed)
    section = report["currencies"][currency]
    testcase.assertEqual(section["max_total_claim_minor"], best)
    testcase.assertEqual(section["minimum_cut"]["capacity_minor"], best)

    relation_ranges = {(row["receipt_id"], row["settlement_id"]): row
                       for row in section["relation_ranges"]}
    testcase.assertEqual(set(relation_ranges), set(allowed))
    for (receipt, group), row in relation_ranges.items():
        values = [allocation.get((receipt, group), 0)
                  for allocation in allocations]
        testcase.assertEqual(row["min_claim_minor"], min(values))
        testcase.assertEqual(row["max_claim_minor"], max(values))

    group_ranges = {row["settlement_id"]: row
                    for row in section["group_ranges"]}
    testcase.assertEqual(set(group_ranges), set(demand))
    for group, row in group_ranges.items():
        values = [sum(allocation.get((receipt, other), 0)
                      for (receipt, other) in allowed if other == group)
                  for allocation in allocations]
        testcase.assertEqual(row["min_claim_minor"], min(values))
        testcase.assertEqual(row["max_claim_minor"], max(values))

    # Every witness is a feasible maximum allocation with the stated total.
    witnesses = section["witnesses"]
    for row in section["relation_ranges"] + section["group_ranges"]:
        for key in ("min_witness", "max_witness"):
            witness = witnesses[row[key]]
            testcase.assertEqual(witness["total_claim_minor"], best)
            used = {}
            for receipt, group, amount in witness["relation_flows"]:
                testcase.assertGreater(amount, 0)
                testcase.assertIn((receipt, group), allowed)
                used[(receipt, group)] = amount
            for receipt in supply:
                testcase.assertLessEqual(
                    sum(v for (r, _g), v in used.items() if r == receipt),
                    supply[receipt])
            for group in demand:
                testcase.assertLessEqual(
                    sum(v for (_r, g), v in used.items() if g == group),
                    demand[group])
            if "receipt_id" in row and "settlement_id" in row:
                testcase.assertEqual(
                    used.get((row["receipt_id"], row["settlement_id"]), 0),
                    row[key.replace("_witness", "_claim_minor")])
            else:
                testcase.assertEqual(
                    sum(v for (_r, g), v in used.items()
                        if g == row["settlement_id"]),
                    row[key.replace("_witness", "_claim_minor")])


# --------------------------------------------------------------------------- #
# End-to-end CLI tests on the bundled shape
# --------------------------------------------------------------------------- #
class ClaimCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = Fixture(self.temp.name)

    def _standard(self):
        prepare(self.fx, [
            ("SA", "USD", "100.00"), ("SB", "USD", "60.00"),
            ("SD", "USD", "30.00"), ("SE", "USD", "30.00"),
            ("SG", "USD", "1.00"), ("SH", "USD", "1.00"),
            ("SI", "USD", "1.00"), ("SK", "USD", "1.00"),
            ("SJ", "JPY", "500"),
        ])
        self.fx.receipts([
            ("R1", "USD", "80.00"), ("R2", "USD", "70.00"),
            ("R3", "USD", "30.00"), ("R5", "JPY", "300"),
            ("R7", "USD", "40.00"), ("R8", "USD", "1.00"),
            ("R9", "USD", "1.00"), ("R10", "USD", "1.00"),
            ("R11", "USD", "1.00"),
        ])
        self.fx.relations([
            ("R1", "SA", "USD"), ("R1", "SB", "USD"),
            ("R2", "SA", "USD"), ("R2", "SB", "USD"),
            ("R3", "SA", "USD"), ("R7", "SD", "USD"),
            ("R7", "SE", "USD"), ("R8", "SG", "USD"),
            ("R8", "SH", "USD"), ("R9", "SG", "USD"),
            ("R10", "SI", "USD"), ("R11", "SI", "USD"),
            ("R11", "SK", "USD"), ("R5", "SJ", "JPY"),
        ])

    def test_cli_report_shape_and_read_only_store(self):
        self._standard()
        before = self.fx.db.read_bytes()
        result = self.fx.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        stdout = json.loads(result.stdout)
        self.assertEqual(stdout["status"], "analyzed")
        self.assertEqual(before, self.fx.db.read_bytes())
        report = json.loads(self.fx.output.read_text())

        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["analysis"], "settlement_claim_ranges")
        self.assertEqual(report["period"], "2026-04")
        self.assertIn("not jointly", report["range_semantics"])
        self.assertEqual(report["summary"]["currencies"], ["JPY", "USD"])
        self.assertEqual(
            report["summary"]["total_max_claim_minor_by_currency"],
            {"JPY": 300, "USD": 20400})

        usd = report["currencies"]["USD"]
        self.assertEqual(usd["supply_total_minor"], 22400)
        self.assertEqual(usd["demand_total_minor"], 22400)
        self.assertEqual(usd["max_total_claim_minor"], 20400)
        self.assertEqual(usd["unclaimed_demand_minor"], 2000)
        self.assertEqual(usd["unused_supply_minor"], 2000)
        self.assertEqual(usd["minimum_cut"]["capacity_minor"], 20400)
        self.assertIn("source", usd["minimum_cut"]["source_side"])
        self.assertIn("sink", usd["minimum_cut"]["sink_side"])
        for edge in usd["minimum_cut"]["cut_edges"]:
            self.assertEqual(len(edge), 3)
        # The maximum-flow summary of the network is internally consistent.
        flow_out = sum(edge["flow_minor"] for edge in usd["network"]["edges"]
                       if edge["from"] == "source")
        self.assertEqual(flow_out, 20400)
        self.assertEqual(
            sum(edge["flow_minor"] for edge in usd["network"]["edges"]
                if edge["to"] == "sink"), 20400)

        # Source evidence is content-addressed and self-consistent.
        sources = report["sources"]
        self.assertEqual(
            sources["receipts"]["sha256"],
            __import__("hashlib").sha256(
                (self.fx.root / "receipts.csv").read_bytes()).hexdigest())
        self.assertEqual(
            sources["relations"]["sha256"],
            __import__("hashlib").sha256(
                (self.fx.root / "relations.csv").read_bytes()).hexdigest())
        self.assertEqual(sources["period"], "2026-04")
        self.assertEqual(sources["monthly_schema_version"], 1)
        with sqlite3.connect(self.fx.db) as db:
            stored = db.execute(
                "SELECT ledger_sha256,bank_sha256 FROM months WHERE period=?",
                ("2026-04",)).fetchone()
        self.assertEqual(sources["month"]["ledger_sha256"], stored[0])
        self.assertEqual(sources["month"]["bank_sha256"], stored[1])

        # Deterministic replacement.
        first = self.fx.output.read_bytes()
        self.assertEqual(self.fx.run_cli().returncode, 0)
        self.assertEqual(first, self.fx.output.read_bytes())

    def test_exhaustive_range_check_on_small_graph(self):
        # One shared bottleneck pair of receipts plus an independent leg.
        prepare(self.fx, [("SA", "USD", cents(10)), ("SB", "USD", cents(7)),
                          ("SC", "USD", cents(5))])
        self.fx.receipts([("R1", "USD", cents(10)), ("R2", "USD", cents(8)),
                          ("R3", "USD", cents(4))])
        allowed = [("R1", "SA"), ("R1", "SB"), ("R2", "SA"), ("R2", "SB"),
                   ("R3", "SC")]
        self.fx.relations([(r, g, "USD") for r, g in allowed])
        result = self.fx.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.fx.output.read_text())
        verify_report_against_enumeration(
            self, report, "USD",
            {"R1": 10, "R2": 8, "R3": 4},
            {"SA": 10, "SB": 7, "SC": 5}, allowed)

    def test_greedy_failure_and_legally_unused_edge(self):
        # R0 (3) only reaches G2 (demand 3). R2 (3) only reaches G0 (demand 2).
        # R1 (1) may reach G0 or G1 (each demand 2). G0 is fully served by R2,
        # which has no alternative, so R1 must serve G1: relation R1->G0 is
        # allowed yet carries 0 in every optimum. A relation-order greedy claim
        # (R1->G0 first) fills G0 with R1, strands R1 and claims only 5 of the
        # optimal 6.
        prepare(self.fx, [("SG0", "USD", cents(2)), ("SG1", "USD", cents(2)),
                          ("SG2", "USD", cents(3))])
        self.fx.receipts([("R0", "USD", cents(3)), ("R1", "USD", cents(1)),
                          ("R2", "USD", cents(3))])
        allowed = [("R0", "SG2"), ("R1", "SG0"), ("R1", "SG1"),
                   ("R2", "SG0")]
        self.fx.relations([(r, g, "USD") for r, g in allowed])
        result = self.fx.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.fx.output.read_text())
        verify_report_against_enumeration(
            self, report, "USD", {"R0": 3, "R1": 1, "R2": 3},
            {"SG0": 2, "SG1": 2, "SG2": 3}, allowed)
        section = report["currencies"]["USD"]
        relation = {(row["receipt_id"], row["settlement_id"]): row
                    for row in section["relation_ranges"]}
        # The allowed edge forced out of every optimum has both endpoints 0.
        self.assertEqual(relation[("R1", "SG0")]["min_claim_minor"], 0)
        self.assertEqual(relation[("R1", "SG0")]["max_claim_minor"], 0)
        self.assertEqual(section["max_total_claim_minor"], 6)
        # A relation-order greedy claim under-claims on the same graph.
        supply_left = {"R0": 3, "R1": 1, "R2": 3}
        demand_left = {"SG0": 2, "SG1": 2, "SG2": 3}
        greed = 0
        for receipt, group in allowed:
            amount = min(supply_left[receipt], demand_left[group])
            supply_left[receipt] -= amount
            demand_left[group] -= amount
            greed += amount
        self.assertEqual(greed, 5)
        self.assertLess(greed, section["max_total_claim_minor"])
        # R1 cannot give SG0 anything while still maximizing the total.
        self.assertEqual(relation[("R1", "SG1")]["min_claim_minor"], 1)
        self.assertEqual(relation[("R1", "SG1")]["max_claim_minor"], 1)

    def test_maxima_are_not_jointly_attainable(self):
        # Two receipts of 1 and two groups of 1, fully cross-connected. Every
        # relation has maximum 1 and every group has maximum 1, yet the total
        # maximum is only 2: concatenating the individual maxima would claim 4.
        prepare(self.fx, [("SA", "USD", cents(1)), ("SB", "USD", cents(1))])
        self.fx.receipts([("R1", "USD", cents(1)), ("R2", "USD", cents(1))])
        allowed = [("R1", "SA"), ("R1", "SB"), ("R2", "SA"), ("R2", "SB")]
        self.fx.relations([(r, g, "USD") for r, g in allowed])
        result = self.fx.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.fx.output.read_text())
        verify_report_against_enumeration(
            self, report, "USD", {"R1": 1, "R2": 1},
            {"SA": 1, "SB": 1}, allowed)
        section = report["currencies"]["USD"]
        self.assertEqual(section["max_total_claim_minor"], 2)
        relation_max = sum(row["max_claim_minor"]
                           for row in section["relation_ranges"])
        group_max = sum(row["max_claim_minor"]
                        for row in section["group_ranges"])
        self.assertEqual((relation_max, group_max), (4, 2))
        self.assertGreater(relation_max, section["max_total_claim_minor"])
        # Each relation endpoint is witnessed by a complete maximum allocation,
        # never by a stitched vector.
        for row in section["relation_ranges"]:
            for key in ("min_witness", "max_witness"):
                witness = section["witnesses"][row[key]]
                self.assertEqual(witness["total_claim_minor"], 2)
                claimed = sum(amount for _r, _g, amount
                              in witness["relation_flows"])
                self.assertEqual(claimed, 2)

    def test_bottleneck_cut_and_endpoint_witnesses(self):
        prepare(self.fx, [("SB", "USD", "100.00")])
        self.fx.receipts([("R1", "USD", "40.00"), ("R2", "USD", "60.00")])
        self.fx.relations([("R1", "SB", "USD"), ("R2", "SB", "USD")])
        result = self.fx.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        section = json.loads(self.fx.output.read_text())["currencies"]["USD"]
        self.assertEqual(section["max_total_claim_minor"], 10000)
        cut = section["minimum_cut"]
        self.assertEqual(cut["capacity_minor"], 10000)
        self.assertEqual(
            {(e[0], e[1], e[2]) for e in cut["cut_edges"]},
            {("source", "receipt:R1", 4000),
             ("source", "receipt:R2", 6000)})
        group = section["group_ranges"][0]
        self.assertEqual((group["min_claim_minor"], group["max_claim_minor"]),
                         (10000, 10000))
        for row in section["relation_ranges"]:
            witness = section["witnesses"][row["max_witness"]]
            self.assertEqual(witness["total_claim_minor"], 10000)

    def test_multi_currency_isolation(self):
        prepare(self.fx, [("SA", "USD", "10.00"), ("SJ", "JPY", "500"),
                          ("SE", "EUR", "5.00")])
        self.fx.receipts([("R1", "USD", "10.00"), ("R2", "JPY", "300"),
                          ("R3", "EUR", "5.00")])
        self.fx.relations([("R1", "SA", "USD"), ("R2", "SJ", "JPY"),
                           ("R3", "SE", "EUR")])
        result = self.fx.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.fx.output.read_text())
        self.assertEqual(report["summary"]["currencies"],
                         ["EUR", "JPY", "USD"])
        self.assertEqual(
            report["summary"]["total_max_claim_minor_by_currency"],
            {"EUR": 500, "JPY": 300, "USD": 1000})
        # JPY is zero-decimal; USD/EUR group capacities stay in minor units.
        self.assertEqual(
            report["currencies"]["JPY"]["max_total_claim_minor"], 300)
        self.assertEqual(report["currencies"]["EUR"]["supply_total_minor"], 500)
        # Cross-currency summation is impossible: each currency is separate.
        self.assertEqual(
            set(report["summary"]["total_max_claim_minor_by_currency"]),
            {"EUR", "JPY", "USD"})

    def test_empty_inputs_produce_empty_report(self):
        prepare(self.fx, [("SA", "USD", "10.00")])
        self.fx.receipts([])
        self.fx.relations([])
        result = self.fx.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.fx.output.read_text())
        self.assertEqual(report["currencies"]["USD"]["max_total_claim_minor"], 0)
        self.assertEqual(report["currencies"]["USD"]["relation_ranges"], [])
        # With no receipts the group still exists with both endpoints zero.
        self.assertEqual(len(report["currencies"]["USD"]["group_ranges"]), 1)
        self.assertEqual(
            report["currencies"]["USD"]["group_ranges"][0]["max_claim_minor"],
            0)
        self.assertEqual(report["excluded_relations"], [])

        # No positive demand and no receipts -> no currency section at all.
        other_root = Path(self.temp.name) / "empty_other"
        other_root.mkdir()
        second = Fixture(other_root, period="2026-05")
        second.ingest([
            ("p1", "payment", "", "SZ", "USD", "10.00"),
            ("r1", "refund", "p1", "SZ", "USD", "10.00"),
        ], [("SZ", "USD", "0.00")])
        second.receipts([])
        second.relations([])
        result = second.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        empty = json.loads(second.output.read_text())
        self.assertEqual(empty["currencies"], {})
        self.assertEqual(empty["excluded_groups"][0]["reason"],
                         "zero_expected_minor")

    def test_missing_and_negative_groups_are_excluded_not_reconciled(self):
        # SN nets negative (payment 10, fee 12); SZ nets zero; SM has no
        # internal rows. All three appear only as exclusions with reasons and
        # their stored reconciliation facts are reproduced unchanged.
        self.fx.ingest([
            ("p1", "payment", "", "SA", "USD", "10.00"),
            ("p2", "payment", "", "SN", "USD", "10.00"),
            ("f1", "fee", "", "SN", "USD", "12.00"),
            ("p3", "payment", "", "SZ", "USD", "10.00"),
            ("r1", "refund", "p3", "SZ", "USD", "10.00"),
        ], [("SA", "USD", "10.00"), ("SN", "USD", "-2.00"),
            ("SZ", "USD", "0.00"), ("SM", "USD", "5.00")])
        self.fx.receipts([("R1", "USD", "10.00")])
        self.fx.relations([("R1", "SA", "USD"), ("R1", "SM", "USD"),
                           ("R1", "SZ", "USD"), ("R1", "SN", "USD")])
        result = self.fx.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.fx.output.read_text())
        reasons = {row["settlement_id"]: row
                   for row in report["excluded_groups"]}
        self.assertEqual(reasons["SM"]["reason"], "missing_internal_amount")
        self.assertIsNone(reasons["SM"]["expected_minor"])
        self.assertEqual(reasons["SM"]["status"], "missing_ledger")
        self.assertEqual(reasons["SZ"]["reason"], "zero_expected_minor")
        self.assertEqual(reasons["SN"]["reason"], "negative_expected_minor")
        self.assertEqual(reasons["SN"]["expected_minor"], -200)
        self.assertEqual(reasons["SN"]["status"], "balanced")
        self.assertEqual(
            {row["settlement_id"] for row in report["excluded_relations"]},
            {"SM", "SZ", "SN"})
        self.assertTrue(all(
            row["reason"] == "group_not_positive_demand"
            for row in report["excluded_relations"]))
        # The reconciliation table in the store is unchanged by the analysis.
        with sqlite3.connect(self.fx.db) as db:
            stored = {row[0]: (row[1], row[2], row[3]) for row in db.execute(
                "SELECT settlement_id,expected_minor,difference_minor,status "
                "FROM reconciliation WHERE period=? ORDER BY settlement_id",
                ("2026-04",))}
        self.assertEqual(stored, {
            "SA": (1000, 0, "balanced"),
            "SM": (None, None, "missing_ledger"),
            "SN": (-200, 0, "balanced"),
            "SZ": (0, 0, "balanced"),
        })


# --------------------------------------------------------------------------- #
# Contract rejection and range-semantics tests
# --------------------------------------------------------------------------- #
class ClaimContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = Fixture(self.temp.name)
        prepare(self.fx, [("SA", "USD", "10.00"), ("SB", "USD", "5.00"),
                          ("SJ", "JPY", "100")])
        self.output = self.fx.output
        self.good_receipts = self.fx.receipts([("R1", "USD", "10.00")])
        self.good_relations = self.fx.relations([("R1", "SA", "USD")])

    def _reject(self, receipts=None, relations=None, message=None):
        receipts = receipts or self.good_receipts
        relations = relations or self.good_relations
        with self.assertRaises(ClaimError) as caught:
            analyze(self.fx.period, self.fx.db, receipts, relations,
                    self.output)
        if message:
            self.assertIn(message, str(caught.exception))
        self.assertFalse(self.output.exists())

    def _receipt_file(self, rows):
        path = self.fx.root / "bad_receipts.csv"
        write_receipts(path, rows)
        return path

    def _relation_file(self, rows):
        path = self.fx.root / "bad_relations.csv"
        write_relations(path, rows)
        return path

    def test_duplicate_and_invalid_receipts(self):
        for rows in [
            [("R1", "USD", "1.00"), ("R1", "USD", "1.00")],
            [("R1", "USD", "1.00"), ("R1", "USD", "2.00")],
            [("R1", "GBP", "1.00")],
            [("R1", "USD", "0.00")],
            [("R1", "USD", "-1.00")],
            [("R1", "USD", "1.001")],
            [("R1", "JPY", "1.5")],
            [("R1", "USD", "10000000000000.00")],
        ]:
            with self.subTest(rows=rows):
                self._reject(receipts=self._receipt_file(rows))

    def test_duplicate_unknown_and_cross_currency_relations(self):
        cases = [
            ([("R1", "SA", "USD"), ("R1", "SA", "USD")], "duplicate"),
            ([("R9", "SA", "USD")], "unknown receipt"),
            ([("R1", "SX", "USD")], "unknown settlement"),
            ([("R1", "SA", "JPY")], "cross-currency"),
        ]
        for relations, message in cases:
            with self.subTest(relations=relations):
                self._reject(relations=self._relation_file(relations),
                             message=message)

    def test_header_and_missing_month_errors(self):
        bad = self.fx.root / "bad_header.csv"
        bad.write_text("receipt,currency,amount\nR1,USD,1.00\n")
        self._reject(receipts=bad, message="columns")
        with self.assertRaisesRegex(ClaimError, "has not been received"):
            analyze("2026-09", self.fx.db, self.good_receipts,
                    self.good_relations, self.output)
        missing_db = self.fx.root / "absent.sqlite"
        with self.assertRaisesRegex(ClaimError, "not found"):
            analyze(self.fx.period, missing_db, self.good_receipts,
                    self.good_relations, self.output)

    def test_stale_report_is_kept_on_failure_and_replaced_on_success(self):
        analyze(self.fx.period, self.fx.db, self.good_receipts,
                self.good_relations, self.output)
        before = self.output.read_bytes()
        bad = self._receipt_file([("R1", "USD", "1.00"),
                                  ("R1", "USD", "1.00")])
        with self.assertRaises(ClaimError):
            analyze(self.fx.period, self.fx.db, bad, self.good_relations,
                    self.output)
        self.assertEqual(before, self.output.read_bytes())
        old = os.environ.get("SETTLEMENT_AUDIT_CLAIM_FAIL")
        os.environ["SETTLEMENT_AUDIT_CLAIM_FAIL"] = "rename"
        try:
            with self.assertRaises(OSError):
                analyze(self.fx.period, self.fx.db, self.good_receipts,
                        self.good_relations, self.output)
        finally:
            if old is None:
                os.environ.pop("SETTLEMENT_AUDIT_CLAIM_FAIL", None)
            else:
                os.environ["SETTLEMENT_AUDIT_CLAIM_FAIL"] = old
        self.assertEqual(before, self.output.read_bytes())
        self.assertEqual(
            list(self.fx.root.glob(self.output.name + "*.tmp")), [])
        new_receipts = self.fx.receipts([("R1", "USD", "10.00"),
                                         ("R2", "USD", "5.00")])
        new_relations = self.fx.relations([("R1", "SA", "USD"),
                                           ("R2", "SB", "USD")])
        analyze(self.fx.period, self.fx.db, new_receipts, new_relations,
                self.output)
        self.assertNotEqual(before, self.output.read_bytes())

    def test_parse_helpers_are_strict_and_tables_may_be_empty(self):
        self.assertEqual(parse_receipts(b"receipt_id,currency,amount\n"), {})
        self.assertEqual(parse_relations(
            b"receipt_id,settlement_id,currency\n"), [])
        with self.assertRaisesRegex(ClaimError, "malformed"):
            parse_receipts(b"receipt_id,currency,amount\nR1,USD\n")
        with self.assertRaisesRegex(ClaimError, "UTF-8"):
            parse_receipts(b"\xff\xfe")


# --------------------------------------------------------------------------- #
# Exhaustive small-graph verification through the Python entry point
# --------------------------------------------------------------------------- #
class ClaimEnumerationTests(unittest.TestCase):
    """Independent exhaustive enumeration over many small synthetic graphs."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_enumerated_ranges_across_generated_graphs(self):
        import random
        random.seed(104)
        for trial in range(10):
            root = Path(self.temp.name) / f"case{trial}"
            root.mkdir()
            fx = Fixture(root, period="2026-04")
            receipt_count = random.randint(1, 3)
            group_count = random.randint(1, 2)
            supplies = [random.randint(1, 3) for _ in range(receipt_count)]
            demands = [random.randint(1, 3) for _ in range(group_count)]
            allowed = [(i, j) for i in range(receipt_count)
                       for j in range(group_count) if random.random() < 0.6]
            if not allowed:
                allowed = [(0, 0)]
            prepare(fx, [(f"G{j}", "USD", cents(d))
                         for j, d in enumerate(demands)])
            fx.receipts([(f"R{i}", "USD", cents(s))
                         for i, s in enumerate(supplies)])
            fx.relations([(f"R{i}", f"G{j}", "USD") for i, j in allowed])
            payload = analyze(fx.period, fx.db, root / "receipts.csv",
                              root / "relations.csv", fx.output)
            supply = {f"R{i}": s for i, s in enumerate(supplies)}
            demand = {f"G{j}": d for j, d in enumerate(demands)}
            pairs = [(f"R{i}", f"G{j}") for i, j in allowed]
            with self.subTest(trial=trial, supplies=supplies,
                              demands=demands, allowed=allowed):
                verify_report_against_enumeration(
                    self, payload, "USD", supply, demand, pairs)


class ClaimCliFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = Fixture(self.temp.name)
        prepare(self.fx, [("SA", "USD", "10.00")])
        self.fx.receipts([("R1", "USD", "10.00")])
        self.fx.relations([("R1", "SA", "USD")])

    def test_cli_exit_codes_and_stale_report(self):
        self.assertEqual(self.fx.run_cli().returncode, 0)
        before = self.fx.output.read_bytes()
        bad_relations = self.fx.root / "bad.csv"
        write_relations(bad_relations, [("R1", "SX", "USD")])
        result = self.fx.run_cli(relations=bad_relations)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown settlement", result.stderr)
        self.assertEqual(before, self.fx.output.read_bytes())

        # A v1 (non-monthly) database is refused untouched.
        v1 = self.fx.root / "v1.sqlite"
        connection = sqlite3.connect(v1)
        connection.execute(
            "CREATE TABLE source_hashes(name TEXT PRIMARY KEY, sha256 TEXT)")
        connection.commit()
        connection.close()
        v1_bytes = v1.read_bytes()
        other = self.fx.root / "other.json"
        result = subprocess.run(
            [sys.executable, "-I", "-c", launcher("settlement_audit.claims"),
             "--period", "2026-04", "--db", str(v1),
             "--receipts", str(self.fx.root / "receipts.csv"),
             "--relations", str(self.fx.root / "relations.csv"),
             "--output", str(other)],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a monthly store", result.stderr)
        self.assertEqual(v1_bytes, v1.read_bytes())
        self.assertFalse(other.exists())


if __name__ == "__main__":
    unittest.main()
