import argparse
import json
from pathlib import Path
from .pipeline import run


def main():
    parser=argparse.ArgumentParser(description="Reconcile fictional payment exports against a synthetic bank statement")
    parser.add_argument("--ledger",type=Path,default=Path("data/ledger.csv"))
    parser.add_argument("--bank",type=Path,default=Path("data/bank.json"))
    parser.add_argument("--output",type=Path,default=Path("run/audit"))
    args=parser.parse_args()
    try:report=run(args.ledger,args.bank,args.output)
    except (ValueError,OSError) as exc:parser.exit(2,f"reconciliation failed: {exc}\n")
    print(json.dumps(report["summary"],sort_keys=True))


if __name__=="__main__":main()
