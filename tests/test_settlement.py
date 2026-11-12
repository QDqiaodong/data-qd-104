import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from settlement_audit.input import load
from settlement_audit.money import minor
from settlement_audit.reconcile import reconcile
from settlement_audit.pipeline import run

DATA=Path(__file__).resolve().parents[1]/"data"


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.ledger=self.root/"ledger.csv";self.bank=self.root/"bank.json"
        self.ledger.write_bytes((DATA/"ledger.csv").read_bytes());self.bank.write_bytes((DATA/"bank.json").read_bytes())

    def test_exact_net_and_exception_reasons(self):
        rows,bank,duplicates=load(self.ledger,self.bank)
        report=reconcile(rows,bank);groups={r["settlement_id"]:r for r in report["settlements"]}
        self.assertEqual(duplicates,1)
        self.assertEqual(report["summary"],{"groups":4,"balanced":1,"unresolved":3})
        self.assertEqual(groups["S1"]["expected_minor"],12700)
        self.assertEqual(groups["S2"]["difference_minor"],-1)
        self.assertEqual(groups["S3"]["status"],"missing_bank")
        self.assertEqual(groups["S4"]["status"],"missing_ledger")
        self.assertIsNone(groups["S3"]["bank_minor"])

    def test_currency_precision_and_sign(self):
        self.assertEqual(minor("0.01","USD"),1)
        self.assertEqual(minor("-12","JPY",True),-12)
        for amount,currency in [("1.1","JPY"),("NaN","USD"),("0","EUR"),("-1","USD"),("1","GBP"),("1.001","USD")]:
            with self.assertRaises(ValueError):minor(amount,currency)

    def test_refund_parent_currency_and_overrefund(self):
        original=self.ledger.read_text()
        for value in [original.replace("refund,p1,","refund,absent,"),original.replace("S1,USD,20.00","S1,EUR,20.00"),original.replace("S1,USD,20.00","S1,USD,101.00")]:
            self.ledger.write_text(value)
            with self.assertRaises(ValueError):load(self.ledger,self.bank)

    def test_duplicate_conflict_and_bank_ambiguity(self):
        self.ledger.write_text(self.ledger.read_text().replace("p1,payment,,S1,USD,100.0\n","p1,payment,,S1,USD,99.0\n"))
        with self.assertRaisesRegex(ValueError,"conflicting"):load(self.ledger,self.bank)
        self.ledger.write_bytes((DATA/"ledger.csv").read_bytes())
        rows=json.loads(self.bank.read_text());rows.append(rows[0]);self.bank.write_text(json.dumps(rows))
        with self.assertRaisesRegex(ValueError,"duplicate bank"):load(self.ledger,self.bank)

    def test_replay_queryable_database_and_json(self):
        out=self.root/"out";run(self.ledger,self.bank,out)
        first=(out/"reconciliation.json").read_bytes();run(self.ledger,self.bank,out)
        self.assertEqual(first,(out/"reconciliation.json").read_bytes())
        with sqlite3.connect(out/"audit.sqlite") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM ledger").fetchone()[0],7)
            self.assertEqual(db.execute("SELECT count(*) FROM reconciliation WHERE status!='balanced'").fetchone()[0],3)
        self.assertEqual(len((out/"exceptions.csv").read_text().splitlines()),4)

    def test_zero_net_is_not_missing_and_currencies_stay_separate(self):
        self.ledger.write_text("event_id,kind,original_id,settlement_id,currency,amount\na,payment,,S,USD,1\nb,refund,a,S,USD,1\nc,payment,,S,EUR,1\n")
        self.bank.write_text('[{"settlement_id":"S","currency":"USD","amount":"0"}]')
        rows,bank,_=load(self.ledger,self.bank);result=reconcile(rows,bank)
        self.assertEqual(len(result["settlements"]),2)
        self.assertEqual(next(r for r in result["settlements"] if r["currency"]=="USD")["status"],"balanced")

    def test_invalid_input_leaves_previous_report(self):
        out=self.root/"out";run(self.ledger,self.bank,out);before=(out/"reconciliation.json").read_bytes()
        self.bank.write_text("not-json")
        with self.assertRaises(ValueError):run(self.ledger,self.bank,out)
        self.assertEqual(before,(out/"reconciliation.json").read_bytes())

    def test_empty_one_side_and_both_empty(self):
        self.bank.write_text("[]")
        rows,bank,_=load(self.ledger,self.bank)
        self.assertTrue(all(r["status"]=="missing_bank" for r in reconcile(rows,bank)["settlements"]))
        self.ledger.write_text("event_id,kind,original_id,settlement_id,currency,amount\n")
        with self.assertRaisesRegex(ValueError,"both inputs"):load(self.ledger,self.bank)


if __name__=="__main__":unittest.main()
