"""Payment/refund export and independent synthetic bank settlement statement."""
import csv
import json
from .money import minor


def load(ledger_path,bank_path):
    entries={};duplicate_rows=0
    with open(ledger_path,encoding="utf-8",newline="") as stream:
        reader=csv.DictReader(stream)
        if reader.fieldnames!=["event_id","kind","original_id","settlement_id","currency","amount"]:raise ValueError("invalid ledger columns")
        for row in reader:
            if None in row or any(v is None for v in row.values()):raise ValueError("malformed ledger row")
            if any(not row[k] for k in ["event_id","kind","settlement_id","currency","amount"]):raise ValueError("missing ledger identity")
            kind=row["kind"]
            if kind not in {"payment","refund","fee"}:raise ValueError("unsupported ledger kind")
            if (kind=="refund") != bool(row["original_id"]):raise ValueError("only refunds require original payment identity")
            record={**{k:row[k] for k in ["event_id","kind","original_id","settlement_id","currency"]},"minor":minor(row["amount"],row["currency"])}
            previous=entries.get(row["event_id"])
            if previous:
                if previous!=record:raise ValueError("conflicting ledger event identity")
                duplicate_rows+=1
            else:entries[row["event_id"]]=record
    refunds={}
    for record in entries.values():
        if record["kind"]=="refund":
            parent=entries.get(record["original_id"])
            if not parent or parent["kind"]!="payment" or parent["currency"]!=record["currency"]:raise ValueError("refund requires same-currency original payment in export")
            key=parent["event_id"];refunds[key]=refunds.get(key,0)+record["minor"]
            if refunds[key]>parent["minor"]:raise ValueError("total refunds exceed payment")
    with open(bank_path,encoding="utf-8") as stream:raw=json.load(stream)
    if not isinstance(raw,list):raise ValueError("bank statement must be a list")
    bank={}
    for row in raw:
        if not isinstance(row,dict) or set(row)!={"settlement_id","currency","amount"}:raise ValueError("invalid bank settlement fields")
        if any(not isinstance(v,str) or not v for v in row.values()):raise ValueError("bank fields must be nonempty strings")
        key=(row["settlement_id"],row["currency"])
        if key in bank:raise ValueError("duplicate bank settlement/currency")
        bank[key]=minor(row["amount"],row["currency"],signed=True)
    if not entries and not bank:raise ValueError("both inputs are empty")
    return list(entries.values()),bank,duplicate_rows
