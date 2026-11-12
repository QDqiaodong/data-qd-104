"""Independent grouping, exact differences and explicit unresolved reasons."""
from collections import defaultdict


def reconcile(entries,bank):
    expected=defaultdict(int);counts=defaultdict(lambda:{"payment":0,"refund":0,"fee":0})
    for record in entries:
        key=(record["settlement_id"],record["currency"])
        expected[key]+=record["minor"]*(1 if record["kind"]=="payment" else -1)
        counts[key][record["kind"]]+=1
    results=[]
    for key in sorted(set(expected)|set(bank)):
        has_ledger,has_bank=key in expected,key in bank
        delta=bank[key]-expected[key] if has_ledger and has_bank else None
        status="missing_ledger" if not has_ledger else "missing_bank" if not has_bank else "balanced" if delta==0 else "amount_mismatch"
        results.append({"settlement_id":key[0],"currency":key[1],"expected_minor":expected.get(key),"bank_minor":bank.get(key),"difference_minor":delta,"status":status,"event_counts":counts[key]})
    return {"schema_version":1,"settlements":results,"summary":{"groups":len(results),"balanced":sum(r["status"]=="balanced" for r in results),"unresolved":sum(r["status"]!="balanced" for r in results)}}
