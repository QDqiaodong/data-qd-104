"""Regression defense for the legacy full-export reconciliation (docs/contract.md).

A fixed-seed generator builds a synthetic ledger/bank scenario and an
independent expected model computes the contract answer.  The model is pure
standard library and NEVER imports settlement_audit: expected values are
derived from the generator's own structured events, not from the parser,
deduplicator or reconciler under test.

Verification goes through the real full CLI subprocess and then checks
reconciliation.json, exceptions.csv and audit.sqlite key by key.  Shuffled
row order, equivalent amount rewrites and duplicate insertion must leave the
business invariants (groups, amounts, statuses, event counts, summary)
untouched, while the source hashes and the duplicate-row count are allowed
to change in exactly the ways the contract permits.

On any failure the inputs, the seed and a replay command are preserved under
run/regression_failures/ (override with REGRESSION_FAILURE_DIR).

The source tree exercised by the CLI defaults to this repository but can be
redirected with SETTLEMENT_AUDIT_SRC (used by tools/mutation_check.py to run
this module against mutated source copies).
"""
import csv
import functools
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = Path(os.environ.get("SETTLEMENT_AUDIT_SRC") or ROOT)
FAILURE_DIR = Path(os.environ.get("REGRESSION_FAILURE_DIR") or ROOT / "run" / "regression_failures")

SEED = 20261114
LEDGER_HEADER = ["event_id", "kind", "original_id", "settlement_id", "currency", "amount"]
SCALE = {"USD": 2, "EUR": 2, "JPY": 0}
REPORT_KEYS = {"schema_version", "settlements", "summary", "duplicate_ledger_rows", "input_sha256"}
GROUP_KEYS = {"settlement_id", "currency", "expected_minor", "bank_minor", "difference_minor", "status", "event_counts"}
CSV_FIELDS = ["settlement_id", "currency", "expected_minor", "bank_minor", "difference_minor", "status"]


# --------------------------------------------------------------------------
# Fixed-seed scenario generator (stdlib only, no settlement_audit imports).
# --------------------------------------------------------------------------

def _ev(event_id, kind, original_id, settlement_id, currency, minor):
    return {"event_id": event_id, "kind": kind, "original_id": original_id,
            "settlement_id": settlement_id, "currency": currency, "minor": minor}


def generate_scenario(seed=SEED):
    """Deterministic scenario covering every contract feature under test.

    Returns (events, duplicates, bank): events are unique ledger events with
    integer minor-unit amounts; duplicates are extra ledger rows that repeat
    an existing event with an equivalent amount spelling; bank maps
    (settlement_id, currency) to signed integer minor units.
    """
    rng = random.Random(seed)
    events = []

    def payment(eid, sid, ccy, minor):
        events.append(_ev(eid, "payment", "", sid, ccy, minor))

    def fee(eid, sid, ccy, minor):
        events.append(_ev(eid, "fee", "", sid, ccy, minor))

    def refund(eid, original, sid, ccy, minor):
        events.append(_ev(eid, "refund", original, sid, ccy, minor))

    bank = {}

    # S1/USD: two payments, same-settlement partial refund, fee; balanced.
    p1 = rng.randrange(50, 201) * 100          # whole dollars -> "100.0"-style duplicates
    p2 = rng.randrange(1000, 20001)
    r1 = rng.randrange(1, p1 // 2)
    f1 = rng.randrange(100, 901)
    payment("p-s1-a", "S1", "USD", p1)
    payment("p-s1-b", "S1", "USD", p2)
    refund("r-s1-a", "p-s1-a", "S1", "USD", r1)
    fee("f-s1-a", "S1", "USD", f1)
    bank[("S1", "USD")] = p1 + p2 - r1 - f1

    # S2/JPY: payment and fee; bank one yen short (negative difference).
    p3 = rng.randrange(5000, 20001)
    f2 = rng.randrange(10, 501)
    payment("p-s2-a", "S2", "JPY", p3)
    fee("f-s2-a", "S2", "JPY", f2)
    bank[("S2", "JPY")] = p3 - f2 - 1

    # S3/EUR: ledger only -> missing_bank.
    payment("p-s3-a", "S3", "EUR", rng.randrange(100, 5001))

    # S4/USD: bank only, negative amount -> missing_ledger.
    bank[("S4", "USD")] = -rng.randrange(1, 2001)

    # S5/USD + S6/USD: cross-settlement refund.  The refund settles in S6 but
    # references the S5 payment (same currency, different settlement_id).
    p5 = rng.randrange(5000, 10001)
    r5 = rng.randrange(100, p5 // 2)
    p6 = rng.randrange(1000, 5001)
    f6 = rng.randrange(10, 201)
    payment("p-s5-a", "S5", "USD", p5)
    payment("p-s6-a", "S6", "USD", p6)
    refund("r-s6-a", "p-s5-a", "S6", "USD", r5)
    fee("f-s6-a", "S6", "USD", f6)
    bank[("S5", "USD")] = p5
    bank[("S6", "USD")] = p6 - r5 - f6

    # S7/EUR: payment fully refunded -> zero net, bank exactly "0"; balanced.
    p7 = rng.randrange(1000, 9001)
    payment("p-s7-a", "S7", "EUR", p7)
    refund("r-s7-a", "p-s7-a", "S7", "EUR", p7)
    bank[("S7", "EUR")] = 0

    # S8/JPY: payment exactly offset by a fee -> zero net, no bank row.
    # The group must still appear with expected_minor 0 and null bank fields.
    p8 = rng.randrange(100, 2001)
    payment("p-s8-a", "S8", "JPY", p8)
    fee("f-s8-a", "S8", "JPY", p8)

    # S9 shared settlement id in two currencies; both balanced.  Currency
    # isolation: the USD and EUR groups must never be combined.
    p9u = rng.randrange(1000, 5001)
    f9u = rng.randrange(10, 101)
    p9e = rng.randrange(1000, 5001)
    payment("p-s9-usd", "S9", "USD", p9u)
    fee("f-s9-usd", "S9", "USD", f9u)
    payment("p-s9-eur", "S9", "EUR", p9e)
    bank[("S9", "USD")] = p9u - f9u
    bank[("S9", "EUR")] = p9e

    # S10/EUR: two partial refunds against one payment (total <= payment);
    # bank above expectation (positive difference).
    p10 = rng.randrange(5000, 9001)
    r10a = rng.randrange(100, 1001)
    r10b = rng.randrange(100, 1001)
    payment("p-s10-a", "S10", "EUR", p10)
    refund("r-s10-a", "p-s10-a", "S10", "EUR", r10a)
    refund("r-s10-b", "p-s10-a", "S10", "EUR", r10b)
    bank[("S10", "EUR")] = p10 - r10a - r10b + rng.randrange(1, 51)

    # Equivalent duplicates: identical events, different amount spellings.
    duplicates = [("p-s1-a", "variant"), ("p-s2-a", "variant")]
    return events, duplicates, bank


# --------------------------------------------------------------------------
# Independent expected model.  Everything below derives the contract answer
# from the generator's structured events with plain integer arithmetic.
# --------------------------------------------------------------------------

def independent_model(events, bank):
    expected = {}
    counts = {}
    for e in events:
        key = (e["settlement_id"], e["currency"])
        expected[key] = expected.get(key, 0) + (e["minor"] if e["kind"] == "payment" else -e["minor"])
        counts.setdefault(key, {"payment": 0, "refund": 0, "fee": 0})[e["kind"]] += 1
    groups = {}
    for key in sorted(set(expected) | set(bank)):
        has_ledger, has_bank = key in expected, key in bank
        exp, bnk = expected.get(key), bank.get(key)
        delta = bnk - exp if has_ledger and has_bank else None
        if not has_ledger:
            status = "missing_ledger"
        elif not has_bank:
            status = "missing_bank"
        elif delta == 0:
            status = "balanced"
        else:
            status = "amount_mismatch"
        groups[key] = {"settlement_id": key[0], "currency": key[1],
                       "expected_minor": exp, "bank_minor": bnk,
                       "difference_minor": delta, "status": status,
                       "event_counts": counts.get(key, {"payment": 0, "refund": 0, "fee": 0})}
    summary = {"groups": len(groups),
               "balanced": sum(1 for g in groups.values() if g["status"] == "balanced"),
               "unresolved": sum(1 for g in groups.values() if g["status"] != "balanced")}
    return {"groups": groups, "summary": summary}


# --------------------------------------------------------------------------
# Rendering.  Amounts are formatted from integer minor units; equivalent
# spellings (100.0 vs 100.00, leading zeros) parse to the same minor value.
# --------------------------------------------------------------------------

def equivalent_formats(minor, currency):
    negative = minor < 0
    whole, frac = divmod(abs(minor), 100) if SCALE[currency] else (abs(minor), None)
    base = str(whole) if frac is None else "%d.%02d" % (whole, frac)
    forms = {base, "0" + base, "00" + base}
    if frac == 0:
        forms |= {str(whole), "%d.0" % whole, "0" + str(whole)}
    elif frac is not None and frac % 10 == 0:
        forms.add("%d.%d" % (whole, frac // 10))
    return sorted(("-" if negative else "") + f for f in forms)


def canonical_format(minor, currency):
    if SCALE[currency]:
        whole, frac = divmod(abs(minor), 100)
        text = "%d.%02d" % (whole, frac)
    else:
        text = str(abs(minor))
    return ("-" if minor < 0 else "") + text


def render_ledger(events, duplicates=(), variant_events=(), rng=None):
    """Render ledger CSV text.  duplicates lists (event_id, style) extra rows;
    variant_events re-spells base-row amounts with an equivalent format."""
    variants = {e["event_id"]: _pick_variant(e, rng) for e in events if e["event_id"] in set(variant_events)}
    by_id = {e["event_id"]: e for e in events}
    rows = []
    for e in events:
        rows.append(_ledger_row(e, variants.get(e["event_id"], canonical_format(e["minor"], e["currency"]))))
    for event_id, style in duplicates:
        e = by_id[event_id]
        amount = _pick_variant(e, rng) if style == "variant" else canonical_format(e["minor"], e["currency"])
        rows.append(_ledger_row(e, amount))
    return ",".join(LEDGER_HEADER) + "\n" + "".join(rows)


def _ledger_row(e, amount_text):
    return ",".join([e["event_id"], e["kind"], e["original_id"], e["settlement_id"], e["currency"], amount_text]) + "\n"


def _pick_variant(event, rng):
    forms = equivalent_formats(event["minor"], event["currency"])
    canonical = canonical_format(event["minor"], event["currency"])
    noncanonical = [f for f in forms if f != canonical]
    if rng and noncanonical:
        return rng.choice(noncanonical)
    return (noncanonical or [canonical])[0]


def render_bank(bank, order=None, rng=None, force_variant=()):
    keys = list(order) if order is not None else sorted(bank)
    rows = []
    for i, key in enumerate(keys):
        minor = bank[key]
        if rng and key in force_variant:
            amount = rng.choice([f for f in equivalent_formats(minor, key[1]) if f != canonical_format(minor, key[1])])
        else:
            amount = canonical_format(minor, key[1])
        rows.append({"settlement_id": key[0], "currency": key[1], "amount": amount})
    return json.dumps(rows, indent=1) + "\n"


def shuffle_texts(ledger_text, bank_text, seed):
    rng = random.Random(seed)
    lines = ledger_text.splitlines(keepends=True)
    header, rows = lines[0], lines[1:]
    rng.shuffle(rows)
    bank = json.loads(bank_text)
    rng.shuffle(bank)
    return header + "".join(rows), json.dumps(bank, indent=1) + "\n"


# --------------------------------------------------------------------------
# CLI execution and artifact comparison.
# --------------------------------------------------------------------------

def run_cli(ledger_path, bank_path, out_dir):
    env = {k: v for k, v in os.environ.items()
           if k not in {"PYTHONPATH", "SETTLEMENT_AUDIT_SRC", "REGRESSION_FAILURE_DIR"}}
    env["PYTHONPATH"] = str(SRC_ROOT)
    cmd = [sys.executable, "-m", "settlement_audit",
           "--ledger", str(ledger_path), "--bank", str(bank_path), "--output", str(out_dir)]
    return subprocess.run(cmd, cwd=str(SRC_ROOT), env=env, capture_output=True, text=True, timeout=120)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


class FullReconciliationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.ledger_path = self.work / "ledger.csv"
        self.bank_path = self.work / "bank.json"
        self.out_dir = self.work / "out"
        self.seed = SEED
        self.stage = "setup"
        self.last_result = None

    # -- failure evidence: inputs, seed and replay command -----------------
    def _dump_failure_bundle(self, exc):
        try:
            bundle = FAILURE_DIR / self.id()
            bundle.mkdir(parents=True, exist_ok=True)
            for name, path in [("ledger.csv", self.ledger_path), ("bank.json", self.bank_path)]:
                if path.exists():
                    (bundle / name).write_bytes(path.read_bytes())
            (bundle / "seed.txt").write_text("seed=%d\nstage=%s\n" % (self.seed, self.stage))
            (bundle / "replay.txt").write_text(
                "# reproduce the failing test:\ncd %s && %s %s %s.%s\n"
                "# rerun the CLI directly (source tree: %s):\ncd %s && %s -m settlement_audit "
                "--ledger %s --bank %s --output %s\n"
                % (ROOT, sys.executable, ROOT / "tests" / "test_full_reconciliation_regression.py",
                   type(self).__name__, self._testMethodName,
                   SRC_ROOT, SRC_ROOT, sys.executable,
                   bundle / "ledger.csv", bundle / "bank.json", bundle / "out"))
            if self.last_result is not None:
                (bundle / "cli_stdout.txt").write_text(self.last_result.stdout)
                (bundle / "cli_stderr.txt").write_text(self.last_result.stderr)
            (bundle / "failure.txt").write_text("%r\n" % (exc,))
            print("\n[failure bundle saved to %s]" % bundle, file=sys.stderr)
        except Exception as dump_exc:  # never mask the original failure
            print("\n[failure bundle could not be written: %r]" % (dump_exc,), file=sys.stderr)

    @staticmethod
    def preserve_evidence(fn):
        @functools.wraps(fn)
        def wrapper(self):
            try:
                return fn(self)
            except Exception as exc:
                self._dump_failure_bundle(exc)
                raise
        return wrapper

    # -- artifact comparison ------------------------------------------------
    def check_outputs(self, model, events, bank, duplicate_rows, ledger_bytes, bank_bytes):
        self.assertEqual(self.last_result.returncode, 0,
                         "CLI failed: %s" % self.last_result.stderr.strip())
        hashes = {"ledger": sha256_bytes(ledger_bytes), "bank": sha256_bytes(bank_bytes)}

        report = json.loads((self.out_dir / "reconciliation.json").read_text())
        self.assertEqual(set(report), REPORT_KEYS)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["summary"], model["summary"])
        self.assertEqual(report["duplicate_ledger_rows"], duplicate_rows)
        self.assertEqual(report["input_sha256"], hashes)
        actual = {(r["settlement_id"], r["currency"]): r for r in report["settlements"]}
        self.assertEqual(set(actual), set(model["groups"]))
        for key, want in model["groups"].items():
            got = actual[key]
            self.assertEqual(set(got), GROUP_KEYS)
            for field in ("expected_minor", "bank_minor", "difference_minor", "status", "event_counts"):
                self.assertEqual(got[field], want[field], "%s %s" % (key, field))

        with (self.out_dir / "exceptions.csv").open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, CSV_FIELDS)
            rows = list(reader)
        unresolved = [model["groups"][k] for k in sorted(model["groups"]) if model["groups"][k]["status"] != "balanced"]
        self.assertEqual(len(rows), len(unresolved))
        for row, want in zip(rows, unresolved):
            self.assertEqual(row["settlement_id"], want["settlement_id"])
            self.assertEqual(row["currency"], want["currency"])
            self.assertEqual(row["status"], want["status"])
            for field in ("expected_minor", "bank_minor", "difference_minor"):
                self.assertEqual(row[field], "" if want[field] is None else str(want[field]), "%s %s" % (row, field))

        with sqlite3.connect(self.out_dir / "audit.sqlite") as db:
            ledger_rows = set(db.execute(
                "SELECT event_id,kind,original_id,settlement_id,currency,amount_minor FROM ledger"))
            self.assertEqual(ledger_rows, {(e["event_id"], e["kind"], e["original_id"],
                                            e["settlement_id"], e["currency"], e["minor"]) for e in events})
            bank_rows = {(s, c): v for s, c, v in db.execute("SELECT settlement_id,currency,amount_minor FROM bank")}
            self.assertEqual(bank_rows, dict(bank))
            recon = {(s, c): (e, b, d, st) for s, c, e, b, d, st in db.execute(
                "SELECT settlement_id,currency,expected_minor,bank_minor,difference_minor,status FROM reconciliation")}
            self.assertEqual(set(recon), set(model["groups"]))
            for key, want in model["groups"].items():
                self.assertEqual(recon[key], (want["expected_minor"], want["bank_minor"],
                                              want["difference_minor"], want["status"]), key)
            stored_hashes = dict(db.execute("SELECT name,sha256 FROM source_hashes"))
            self.assertEqual(stored_hashes, hashes)
        return report

    def run_valid(self, ledger_text, bank_text, model, events, bank, duplicate_rows):
        self.ledger_path.write_text(ledger_text)
        self.bank_path.write_text(bank_text)
        self.last_result = run_cli(self.ledger_path, self.bank_path, self.out_dir)
        return self.check_outputs(model, events, bank, duplicate_rows,
                                  ledger_text.encode(), bank_text.encode())

    # -- tests --------------------------------------------------------------
    @preserve_evidence
    def test_generated_scenario_matches_independent_model(self):
        self.stage = "baseline scenario"
        events, duplicates, bank = generate_scenario(self.seed)
        model = independent_model(events, bank)
        ledger_text = render_ledger(events, duplicates)
        bank_text = render_bank(bank)
        self.run_valid(ledger_text, bank_text, model, events, bank, len(duplicates))

    @preserve_evidence
    def test_scenario_covers_required_contract_features(self):
        events, duplicates, bank = generate_scenario(self.seed)
        model = independent_model(events, bank)
        groups = model["groups"]
        currencies = {c for _, c in groups}
        self.assertEqual(currencies, {"USD", "EUR", "JPY"})
        statuses = {g["status"] for g in groups.values()}
        self.assertEqual(statuses, {"balanced", "amount_mismatch", "missing_bank", "missing_ledger"})
        # zero net: balanced with expected 0, and one-sided with expected 0.
        self.assertTrue(any(g["status"] == "balanced" and g["expected_minor"] == 0 and g["bank_minor"] == 0
                            for g in groups.values()))
        self.assertTrue(any(g["status"] == "missing_bank" and g["expected_minor"] == 0
                            and g["bank_minor"] is None and g["difference_minor"] is None
                            for g in groups.values()))
        # one-sided missing keeps null, not zero.
        self.assertTrue(any(g["status"] == "missing_ledger" and g["expected_minor"] is None
                            and g["event_counts"] == {"payment": 0, "refund": 0, "fee": 0}
                            for g in groups.values()))
        # both difference directions are exercised.
        diffs = {g["difference_minor"] for g in groups.values() if g["status"] == "amount_mismatch"}
        self.assertTrue(any(d < 0 for d in diffs) and any(d > 0 for d in diffs))
        # cross-settlement refund: refund settled in S6 references an S5 payment.
        by_id = {e["event_id"]: e for e in events}
        self.assertTrue(any(e["kind"] == "refund"
                            and e["settlement_id"] != by_id[e["original_id"]]["settlement_id"]
                            and e["currency"] == by_id[e["original_id"]]["currency"] for e in events))
        # multiple refunds against one payment, a full refund, shared
        # settlement id across currencies, and signed/zero bank amounts.
        parents = [e["original_id"] for e in events if e["kind"] == "refund"]
        self.assertTrue(any(parents.count(p) > 1 for p in parents))
        self.assertTrue(any(e["kind"] == "refund" and e["minor"] == by_id[e["original_id"]]["minor"] for e in events))
        self.assertEqual({c for s, c in groups if s == "S9"}, {"USD", "EUR"})
        self.assertTrue(any(v < 0 for v in bank.values()) and any(v == 0 for v in bank.values()))
        # equivalent duplicates re-spell amounts (100.0 vs 100.00 style).
        self.assertEqual(len(duplicates), 2)
        self.assertTrue(any(canonical_format(by_id[eid]["minor"], by_id[eid]["currency"])
                            not in equivalent_formats(by_id[eid]["minor"], by_id[eid]["currency"])[:1]
                            for eid, _ in duplicates))

    @preserve_evidence
    def test_shuffle_preserves_business_invariants(self):
        events, duplicates, bank = generate_scenario(self.seed)
        model = independent_model(events, bank)
        base_ledger = render_ledger(events, duplicates)
        base_bank = render_bank(bank)
        self.stage = "shuffle baseline"
        base_report = self.run_valid(base_ledger, base_bank, model, events, bank, len(duplicates))

        self.stage = "shuffled inputs"
        shuffled_ledger, shuffled_bank = shuffle_texts(base_ledger, base_bank, self.seed + 1)
        self.assertNotEqual(shuffled_ledger, base_ledger)
        self.assertNotEqual(shuffled_bank, base_bank)
        report = self.run_valid(shuffled_ledger, shuffled_bank, model, events, bank, len(duplicates))
        # business invariants identical; only the source hashes may change.
        self.assertEqual(report["settlements"], base_report["settlements"])
        self.assertEqual(report["summary"], base_report["summary"])
        self.assertEqual(report["duplicate_ledger_rows"], base_report["duplicate_ledger_rows"])
        self.assertNotEqual(report["input_sha256"], base_report["input_sha256"])

    @preserve_evidence
    def test_equivalent_amount_rewrite_preserves_business_invariants(self):
        events, duplicates, bank = generate_scenario(self.seed)
        model = independent_model(events, bank)
        base_ledger = render_ledger(events, duplicates)
        base_bank = render_bank(bank)
        self.stage = "rewrite baseline"
        base_report = self.run_valid(base_ledger, base_bank, model, events, bank, len(duplicates))

        self.stage = "equivalent amount rewrite"
        rng = random.Random(self.seed + 2)
        rewritten_ledger = render_ledger(events, duplicates, variant_events={e["event_id"] for e in events}, rng=rng)
        rewritten_bank = render_bank(bank, rng=rng, force_variant=set(bank))
        self.assertNotEqual(rewritten_ledger, base_ledger)
        self.assertNotEqual(rewritten_bank, base_bank)
        report = self.run_valid(rewritten_ledger, rewritten_bank, model, events, bank, len(duplicates))
        self.assertEqual(report["settlements"], base_report["settlements"])
        self.assertEqual(report["summary"], base_report["summary"])
        self.assertEqual(report["duplicate_ledger_rows"], base_report["duplicate_ledger_rows"])
        self.assertNotEqual(report["input_sha256"], base_report["input_sha256"])

    @preserve_evidence
    def test_duplicate_insertion_changes_only_duplicate_count(self):
        events, duplicates, bank = generate_scenario(self.seed)
        model = independent_model(events, bank)
        base_ledger = render_ledger(events, duplicates)
        base_bank = render_bank(bank)
        self.stage = "duplicate baseline"
        base_report = self.run_valid(base_ledger, base_bank, model, events, bank, len(duplicates))

        self.stage = "duplicate insertion"
        extra = [("p-s1-b", "variant"), ("p-s3-a", "exact"), ("p-s2-a", "variant")]
        more_ledger = render_ledger(events, duplicates + extra)
        report = self.run_valid(more_ledger, base_bank, model, events, bank, len(duplicates) + len(extra))
        # duplicate count and source hash change; every business fact stays.
        self.assertEqual(report["duplicate_ledger_rows"], base_report["duplicate_ledger_rows"] + len(extra))
        self.assertEqual(report["settlements"], base_report["settlements"])
        self.assertEqual(report["summary"], base_report["summary"])
        self.assertNotEqual(report["input_sha256"]["ledger"], base_report["input_sha256"]["ledger"])
        self.assertEqual(report["input_sha256"]["bank"], base_report["input_sha256"]["bank"])

    @preserve_evidence
    def test_contract_failures_preserve_previous_artifacts(self):
        events, duplicates, bank = generate_scenario(self.seed)
        ledger_text = render_ledger(events, duplicates)
        bank_text = render_bank(bank)
        self.stage = "pre-failure baseline"
        self.run_valid(ledger_text, bank_text, independent_model(events, bank), events, bank, len(duplicates))
        artifacts = {name: (self.out_dir / name).read_bytes()
                     for name in ("reconciliation.json", "exceptions.csv", "audit.sqlite")}
        listing = sorted(p.name for p in self.out_dir.iterdir())

        def corrupt_ledger(mutate):
            broken = [dict(e) for e in events]
            by_id = {e["event_id"]: e for e in broken}
            mutate(by_id)
            return render_ledger(list(by_id.values()), duplicates)

        def drop_event(by_id, event_id):
            by_id.pop(event_id)

        cases = []
        # illegal refund: original payment absent from the export.
        cases.append(("refund_absent_parent", corrupt_ledger(
            lambda by_id: by_id["r-s6-a"].__setitem__("original_id", "no-such-event")), bank_text))
        # illegal refund: original payment in a different currency.  The
        # amount is reduced to 1 minor unit so that no other validation
        # (e.g. the over-refund budget) can reject it first.
        def cross_currency_parent(by_id):
            by_id["r-s1-a"]["original_id"] = "p-s3-a"
            by_id["r-s1-a"]["minor"] = 1
        cases.append(("refund_cross_currency_parent", corrupt_ledger(cross_currency_parent), bank_text))
        # illegal refund: total refunds exceed the original payment.
        def over_refund(by_id):
            drop_event(by_id, "r-s7-a")
            by_id["r-s7-b"] = _ev("r-s7-b", "refund", "p-s7-a", "S7", "EUR",
                                  by_id["p-s7-a"]["minor"] + 1)
        cases.append(("refund_exceeds_payment", corrupt_ledger(over_refund), bank_text))
        # duplicate bank settlement/currency key.
        dup_bank = json.loads(bank_text)
        dup_bank.append(dict(dup_bank[0]))
        cases.append(("duplicate_bank_key", ledger_text, json.dumps(dup_bank, indent=1) + "\n"))

        for name, bad_ledger, bad_bank in cases:
            with self.subTest(case=name):
                self.stage = "contract failure: " + name
                self.ledger_path.write_text(bad_ledger)
                self.bank_path.write_text(bad_bank)
                self.last_result = run_cli(self.ledger_path, self.bank_path, self.out_dir)
                self.assertEqual(self.last_result.returncode, 2,
                                 "%s exited %s: %s" % (name, self.last_result.returncode, self.last_result.stderr.strip()))
                self.assertIn("reconciliation failed", self.last_result.stderr)
                for artifact, before in artifacts.items():
                    self.assertEqual((self.out_dir / artifact).read_bytes(), before,
                                     "%s modified %s" % (name, artifact))
                self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()), listing)


if __name__ == "__main__":
    unittest.main()
