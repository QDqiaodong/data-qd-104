"""Full-export recomputation with queryable inputs and reconciliation results."""
import csv
import hashlib
import json
import sqlite3
from pathlib import Path
from .input import load
from .reconcile import reconcile


def run(ledger_path,bank_path,output):
    entries,bank,duplicates=load(ledger_path,bank_path)
    report=reconcile(entries,bank)
    report["duplicate_ledger_rows"]=duplicates
    report["input_sha256"]={name:hashlib.sha256(Path(path).read_bytes()).hexdigest() for name,path in [("ledger",ledger_path),("bank",bank_path)]}
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(output/"audit.sqlite") as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS ledger(event_id TEXT PRIMARY KEY,kind TEXT,original_id TEXT,settlement_id TEXT,currency TEXT,amount_minor INTEGER);
        CREATE TABLE IF NOT EXISTS bank(settlement_id TEXT,currency TEXT,amount_minor INTEGER,PRIMARY KEY(settlement_id,currency));
        CREATE TABLE IF NOT EXISTS reconciliation(settlement_id TEXT,currency TEXT,expected_minor INTEGER,bank_minor INTEGER,difference_minor INTEGER,status TEXT,PRIMARY KEY(settlement_id,currency));
        CREATE TABLE IF NOT EXISTS source_hashes(name TEXT PRIMARY KEY,sha256 TEXT);""")
        for table in ["ledger","bank","reconciliation","source_hashes"]:db.execute(f"DELETE FROM {table}")
        db.executemany("INSERT INTO ledger VALUES (?,?,?,?,?,?)",[tuple(r[k] for k in ["event_id","kind","original_id","settlement_id","currency","minor"]) for r in entries])
        db.executemany("INSERT INTO bank VALUES (?,?,?)",[(s,c,v) for (s,c),v in bank.items()])
        db.executemany("INSERT INTO reconciliation VALUES (?,?,?,?,?,?)",[tuple(r[k] for k in ["settlement_id","currency","expected_minor","bank_minor","difference_minor","status"]) for r in report["settlements"]])
        db.executemany("INSERT INTO source_hashes VALUES (?,?)",report["input_sha256"].items())
    (output/"reconciliation.json").write_text(json.dumps(report,indent=2,sort_keys=True)+"\n")
    with (output/"exceptions.csv").open("w",newline="",encoding="utf-8") as stream:
        writer=csv.DictWriter(stream,fieldnames=["settlement_id","currency","expected_minor","bank_minor","difference_minor","status"])
        writer.writeheader()
        writer.writerows({k:r[k] for k in writer.fieldnames} for r in report["settlements"] if r["status"]!="balanced")
    return report
