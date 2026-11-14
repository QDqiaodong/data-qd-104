#!/usr/bin/env python3
"""Offline mutation verification for the full-export reconciliation regression suite.

For each semantic mutation below, the settlement_audit package is copied into
a fresh temporary directory, the mutation is applied to that copy, and the
regression module (tests/test_full_reconciliation_regression.py) is executed
against the mutated source via SETTLEMENT_AUDIT_SRC.  Production sources and
existing tests are never modified; their hashes are recorded before and after
the run as evidence.

A mutation only counts as caught ("killed") when the mutated copy still
imports cleanly (syntax errors and import failures are reported as "invalid",
not as catches) and the regression module fails within the timeout (a timeout
is reported as "timeout", not as a catch).

Usage:
    python3 tools/mutation_check.py                 # baseline + all mutations
    python3 tools/mutation_check.py --baseline-only # baseline checks only
    python3 tools/mutation_check.py --json PATH     # machine-readable report

Exit code is 0 iff the baselines pass and every mutation is killed.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "settlement_audit"
REGRESSION_MODULE = ROOT / "tests" / "test_full_reconciliation_regression.py"
TEST_TIMEOUT_S = 300
PREFLIGHT = ("import settlement_audit.input,settlement_audit.reconcile,"
             "settlement_audit.pipeline,settlement_audit.money")

# Each mutation is a single, independently runnable semantic change.  "find"
# must occur exactly once in the target file.
MUTATIONS = [
    {
        "name": "sign_refund_fee_added",
        "category": "sign",
        "file": "settlement_audit/reconcile.py",
        "find": 'record["minor"]*(1 if record["kind"]=="payment" else -1)',
        "replace": 'record["minor"]*(1 if record["kind"]=="payment" else 1)',
        "rationale": "refunds and fees are added to the expected net instead of subtracted",
    },
    {
        "name": "currency_refund_parent_check_removed",
        "category": "currency_isolation",
        "file": "settlement_audit/input.py",
        "find": ' or parent["currency"]!=record["currency"]',
        "replace": "",
        "rationale": "a refund may reference an original payment of a different currency",
    },
    {
        "name": "dedup_equivalence_inverted",
        "category": "dedup",
        "file": "settlement_audit/input.py",
        "find": "if previous!=record:raise",
        "replace": "if previous==record:raise",
        "rationale": "identical duplicates are rejected as conflicts while conflicting ids pass silently",
    },
    {
        "name": "missing_one_sided_groups_dropped",
        "category": "missing",
        "file": "settlement_audit/reconcile.py",
        "find": "sorted(set(expected)|set(bank))",
        "replace": "sorted(set(expected)&set(bank))",
        "rationale": "groups present on only one side vanish instead of being reported missing_*",
    },
    {
        "name": "difference_direction_flipped",
        "category": "difference",
        "file": "settlement_audit/reconcile.py",
        "find": "delta=bank[key]-expected[key]",
        "replace": "delta=expected[key]-bank[key]",
        "rationale": "difference = expected - bank instead of bank - expected",
    },
    {
        "name": "refund_full_repayment_rejected",
        "category": "refund_boundary",
        "file": "settlement_audit/input.py",
        "find": "if refunds[key]>parent[\"minor\"]:raise",
        "replace": "if refunds[key]>=parent[\"minor\"]:raise",
        "rationale": "a refund that exactly repays the original payment is rejected as over-refund",
    },
]


def sha256_path(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fingerprint(paths):
    return {str(p.relative_to(ROOT)): sha256_path(p) for p in sorted(paths)}


def clean_env(extra=None):
    env = {k: v for k, v in os.environ.items()
           if k not in {"PYTHONPATH", "SETTLEMENT_AUDIT_SRC", "REGRESSION_FAILURE_DIR"}}
    env.update(extra or {})
    return env


def run(cmd, cwd, env, timeout):
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                              text=True, timeout=timeout)
        return {"exit_code": proc.returncode, "timed_out": False,
                "stdout": proc.stdout, "stderr": proc.stderr,
                "duration_s": round(time.monotonic() - started, 3)}
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": None, "timed_out": True,
                "stdout": exc.stdout or "", "stderr": exc.stderr or "",
                "duration_s": round(time.monotonic() - started, 3)}


def parse_unittest(stderr):
    failed = re.findall(r"^(?:FAIL|ERROR): (\S+)", stderr, re.MULTILINE)
    ran = re.search(r"^Ran (\d+) tests", stderr, re.MULTILINE)
    return failed, (int(ran.group(1)) if ran else None)


def run_regression_module(src_root, failure_dir):
    env = clean_env({"SETTLEMENT_AUDIT_SRC": str(src_root),
                     "REGRESSION_FAILURE_DIR": str(failure_dir)})
    return run([sys.executable, str(REGRESSION_MODULE)], ROOT, env, TEST_TIMEOUT_S)


def check_baseline(report):
    """Full suite (existing + new tests) and the new module alone, unmutated."""
    full = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
               ROOT, clean_env(), TEST_TIMEOUT_S)
    failed, ran = parse_unittest(full["stderr"])
    report["baseline"]["full_suite"] = {
        "command": "%s -m unittest discover -s tests" % sys.executable,
        "exit_code": full["exit_code"], "timed_out": full["timed_out"],
        "tests_run": ran, "failed_tests": failed,
        "duration_s": full["duration_s"], "status": "pass" if full["exit_code"] == 0 else "fail",
    }
    with tempfile.TemporaryDirectory(prefix="mutation_baseline_") as tmp:
        module = run_regression_module(ROOT, Path(tmp) / "failures")
    failed, ran = parse_unittest(module["stderr"])
    report["baseline"]["regression_module"] = {
        "command": "SETTLEMENT_AUDIT_SRC=%s %s %s" % (ROOT, sys.executable, REGRESSION_MODULE),
        "exit_code": module["exit_code"], "timed_out": module["timed_out"],
        "tests_run": ran, "failed_tests": failed,
        "duration_s": module["duration_s"], "status": "pass" if module["exit_code"] == 0 else "fail",
    }
    return all(b["status"] == "pass" for b in report["baseline"].values())


def check_mutation(mutation):
    record = {k: mutation[k] for k in ("name", "category", "file", "find", "replace", "rationale")}
    with tempfile.TemporaryDirectory(prefix="mutation_%s_" % mutation["name"]) as tmp:
        src = Path(tmp) / "src"
        shutil.copytree(PACKAGE, src / PACKAGE.name)
        target = src / mutation["file"]
        original = target.read_text()
        occurrences = original.count(mutation["find"])
        record["find_occurrences"] = occurrences
        if occurrences != 1:
            record["status"] = "invalid"
            record["note"] = "mutation anchor must occur exactly once"
            return record
        target.write_text(original.replace(mutation["find"], mutation["replace"]))
        if target.read_text() == original:
            record["status"] = "invalid"
            record["note"] = "mutation did not change the source"
            return record
        preflight = run([sys.executable, "-c", PREFLIGHT], src, clean_env(), 60)
        record["preflight"] = {"command": "%s -c '%s'" % (sys.executable, PREFLIGHT),
                               "exit_code": preflight["exit_code"],
                               "stderr_tail": preflight["stderr"][-500:]}
        if preflight["exit_code"] != 0:
            record["status"] = "invalid"
            record["note"] = "mutated source has syntax/import errors; not a catch"
            return record
        result = run_regression_module(src, Path(tmp) / "failures")
    failed, ran = parse_unittest(result["stderr"])
    record["test_run"] = {"exit_code": result["exit_code"], "timed_out": result["timed_out"],
                          "tests_run": ran, "failed_tests": failed,
                          "duration_s": result["duration_s"],
                          "stderr_tail": result["stderr"][-2000:]}
    if result["timed_out"]:
        record["status"] = "timeout"
        record["note"] = "regression module timed out; not a catch"
    elif result["exit_code"] == 0:
        record["status"] = "survived"
    else:
        record["status"] = "killed"
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, default=ROOT / "run" / "mutation" / "results.json",
                        help="machine-readable report path (default: run/mutation/results.json)")
    parser.add_argument("--baseline-only", action="store_true",
                        help="run only the unmutated baseline checks")
    args = parser.parse_args()

    production_files = fingerprint(PACKAGE.glob("*.py"))
    existing_tests = fingerprint(p for p in (ROOT / "tests").glob("test_*.py")
                                 if p.resolve() != REGRESSION_MODULE)
    report = {
        "tool": "mutation_check", "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0], "platform": platform.platform(),
        "source_root": str(ROOT),
        "regression_module": str(REGRESSION_MODULE.relative_to(ROOT)),
        "production_files_before": production_files,
        "existing_tests_before": existing_tests,
        "baseline": {}, "mutations": [],
    }

    baseline_ok = check_baseline(report)
    print("baseline full suite:       %s (%s tests)" % (
        report["baseline"]["full_suite"]["status"], report["baseline"]["full_suite"]["tests_run"]))
    print("baseline regression module:%s (%s tests)" % (
        report["baseline"]["regression_module"]["status"], report["baseline"]["regression_module"]["tests_run"]))

    if not args.baseline_only:
        for mutation in MUTATIONS:
            record = check_mutation(mutation)
            report["mutations"].append(record)
            failed = record.get("test_run", {}).get("failed_tests") or []
            print("mutation %-36s %-18s %s" % (record["name"], record["category"], record["status"]))
            for name in failed:
                print("    caught by: %s" % name)

    killed = sum(1 for m in report["mutations"] if m["status"] == "killed")
    survived = sum(1 for m in report["mutations"] if m["status"] == "survived")
    invalid = sum(1 for m in report["mutations"] if m["status"] == "invalid")
    timed_out = sum(1 for m in report["mutations"] if m["status"] == "timeout")
    report["production_files_after"] = fingerprint(PACKAGE.glob("*.py"))
    report["existing_tests_after"] = fingerprint(p for p in (ROOT / "tests").glob("test_*.py")
                                                 if p.resolve() != REGRESSION_MODULE)
    report["production_unchanged"] = report["production_files_before"] == report["production_files_after"]
    report["existing_tests_unchanged"] = report["existing_tests_before"] == report["existing_tests_after"]
    report["summary"] = {
        "baseline_ok": baseline_ok,
        "total": len(report["mutations"]), "killed": killed, "survived": survived,
        "invalid": invalid, "timeout": timed_out,
        "all_mutations_caught": bool(report["mutations"]) and killed == len(report["mutations"]),
        "production_unchanged": report["production_unchanged"],
        "existing_tests_unchanged": report["existing_tests_unchanged"],
    }
    ok = baseline_ok and report["summary"]["all_mutations_caught"] \
        and report["production_unchanged"] and report["existing_tests_unchanged"]
    report["verdict"] = "pass" if ok else "fail"

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("summary: %s" % json.dumps(report["summary"], sort_keys=True))
    print("report written to %s" % args.json)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
