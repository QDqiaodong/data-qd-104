"""Continuous monthly ingestion and cross-month refund verification.

This module is an independent entry point next to the full-export pipeline.
It keeps every received month immutable, tracks the cumulative refunded
fraction of each payment across months, and produces historical reports whose
facts never change after later months are imported. Only the Python standard
library and SQLite are used; all amounts are exact minor-unit integers and
every rule below is synthetic offline reconciliation, never a real transfer.

CLI:
    python -m settlement_audit.monthly ingest --period YYYY-MM \
        --ledger FILE --bank FILE --db FILE
    python -m settlement_audit.monthly report --period YYYY-MM \
        --db FILE --output FILE
"""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .reconcile import reconcile

MONTHLY_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 2
_PERIOD_RE = re.compile(r"(\d{4})-(\d{2})")
_SYNC_POLL_SECONDS = 0.02
_SYNC_WAIT_SECONDS = 60.0
_DEFAULT_BUSY_TIMEOUT = 15.0


class MonthlyError(ValueError):
    """A contract or state violation in monthly mode."""


# --------------------------------------------------------------------------- #
# Period and raw-input parsing
# --------------------------------------------------------------------------- #
def parse_period(text):
    """Strictly validate an operator-declared YYYY-MM export month."""
    match = _PERIOD_RE.fullmatch(text or "")
    if not match:
        raise MonthlyError("period must be YYYY-MM")
    year, month = int(match.group(1)), int(match.group(2))
    if year < 1 or month < 1 or month > 12:
        raise MonthlyError("period must be a valid calendar month")
    return f"{year:04d}-{month:02d}"


def next_period(period):
    year, month = (int(part) for part in period.split("-"))
    month += 1
    if month == 13:
        year += 1
        month = 1
    return f"{year:04d}-{month:02d}"


def _sync_hook(stage):
    """Deterministic test synchronization hook, inert in normal operation.

    SETTLEMENT_AUDIT_SYNC_DIR names a directory containing flag files.
    ``<stage>.arrive`` is created when the process reaches *stage*, and the
    process then waits (bounded, polling) for ``<stage>.release`` before
    continuing. Timing is controlled by files, never by guessed sleeps.
    """
    directory = os.environ.get("SETTLEMENT_AUDIT_SYNC_DIR")
    if not directory:
        return
    path = Path(directory)
    (path / f"{stage}.arrive").touch(exist_ok=True)
    deadline = time.monotonic() + _SYNC_WAIT_SECONDS
    while not (path / f"{stage}.release").exists():
        if time.monotonic() > deadline:
            raise MonthlyError(f"synchronization hook {stage} timed out")
        time.sleep(_SYNC_POLL_SECONDS)


def parse_ledger(raw_bytes):
    """Parse one captured ledger byte string into canonical events.

    Returns (events_by_id, duplicate_rows). event_id is unique inside the
    month: identical canonical duplicates count once, conflicting duplicates
    fail. event_id, kind, settlement_id, currency and amount stay under the
    exact-string semantics of the full-export mode.
    """
    from .money import minor

    text = raw_bytes.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != [
        "event_id", "kind", "original_id", "settlement_id", "currency", "amount"
    ]:
        raise MonthlyError("invalid ledger columns")
    events = {}
    duplicate_rows = 0
    for row_number, row in enumerate(reader, start=1):
        if None in row or any(v is None for v in row.values()):
            raise MonthlyError("malformed ledger row")
        if any(not row[k] for k in
               ["event_id", "kind", "settlement_id", "currency", "amount"]):
            raise MonthlyError("missing ledger identity")
        kind = row["kind"]
        if kind not in {"payment", "refund", "fee"}:
            raise MonthlyError("unsupported ledger kind")
        if (kind == "refund") != bool(row["original_id"]):
            raise MonthlyError("only refunds require original payment identity")
        record = {
            "event_id": row["event_id"],
            "kind": kind,
            "original_id": row["original_id"],
            "settlement_id": row["settlement_id"],
            "currency": row["currency"],
            "minor": minor(row["amount"], row["currency"]),
            "ledger_record": row_number,
        }
        previous = events.get(record["event_id"])
        if previous is not None:
            if {k: previous[k] for k in
                ("kind", "original_id", "settlement_id", "currency", "minor")} != \
               {k: record[k] for k in
                ("kind", "original_id", "settlement_id", "currency", "minor")}:
                raise MonthlyError("conflicting ledger event identity")
            duplicate_rows += 1
        else:
            events[record["event_id"]] = record
    return events, duplicate_rows


def parse_bank(raw_bytes):
    """Parse one captured bank byte string into canonical bank facts."""
    from .money import minor

    raw = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(raw, list):
        raise MonthlyError("bank statement must be a list")
    bank = {}
    for index, row in enumerate(raw, start=1):
        if not isinstance(row, dict) or set(row) != \
                {"settlement_id", "currency", "amount"}:
            raise MonthlyError("invalid bank settlement fields")
        if any(not isinstance(v, str) or not v for v in row.values()):
            raise MonthlyError("bank fields must be nonempty strings")
        key = (row["settlement_id"], row["currency"])
        if key in bank:
            raise MonthlyError("duplicate bank settlement/currency")
        bank[key] = {
            "settlement_id": row["settlement_id"],
            "currency": row["currency"],
            "minor": minor(row["amount"], row["currency"], signed=True),
            "bank_record": index,
        }
    return bank


def _read_captured(path):
    """Read one file exactly once; parsing, hashing and storage share it."""
    return Path(path).read_bytes()


# --------------------------------------------------------------------------- #
# Schema helpers
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE monthly_meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE months(
    period TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    ledger_sha256 TEXT NOT NULL,
    bank_sha256 TEXT NOT NULL,
    ledger_raw BLOB NOT NULL,
    bank_raw BLOB NOT NULL,
    ledger_duplicate_rows INTEGER NOT NULL,
    bank_duplicate_rows INTEGER NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE events(
    event_id TEXT PRIMARY KEY,
    period TEXT NOT NULL REFERENCES months(period),
    kind TEXT NOT NULL,
    original_id TEXT NOT NULL,
    settlement_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    ledger_record INTEGER NOT NULL
);
CREATE INDEX events_period_idx ON events(period);
CREATE TABLE bank_facts(
    period TEXT NOT NULL REFERENCES months(period),
    settlement_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    bank_record INTEGER NOT NULL,
    PRIMARY KEY(period, settlement_id, currency)
);
CREATE TABLE reconciliation(
    period TEXT NOT NULL REFERENCES months(period),
    settlement_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    expected_minor INTEGER,
    bank_minor INTEGER,
    difference_minor INTEGER,
    status TEXT NOT NULL,
    payment_count INTEGER NOT NULL,
    refund_count INTEGER NOT NULL,
    fee_count INTEGER NOT NULL,
    PRIMARY KEY(period, settlement_id, currency)
);
CREATE TABLE payment_balances(
    payment_event_id TEXT PRIMARY KEY REFERENCES events(event_id),
    period TEXT NOT NULL REFERENCES months(period),
    currency TEXT NOT NULL,
    original_minor INTEGER NOT NULL,
    refunded_minor INTEGER NOT NULL,
    remaining_minor INTEGER NOT NULL
);
"""


def _connect(db_path, busy_timeout):
    connection = sqlite3.connect(str(db_path), timeout=busy_timeout,
                                 isolation_level=None)
    connection.row_factory = sqlite3.Row
    return connection


def _is_v1_database(connection):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if not tables:
        return False
    if "monthly_meta" in tables:
        return False
    # Any pre-existing database that is not a monthly store is foreign, most
    # importantly the v1 audit.sqlite produced by settlement_audit.pipeline.
    return True


def _check_monthly_store(connection):
    row = connection.execute(
        "SELECT value FROM monthly_meta WHERE key='schema_version'").fetchone()
    if row is None or int(row[0]) != MONTHLY_SCHEMA_VERSION:
        raise MonthlyError("unsupported monthly database schema version")


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #
def ingest(period_text, ledger_path, bank_path, db_path,
           busy_timeout=_DEFAULT_BUSY_TIMEOUT):
    """Receive one export month atomically or recognize an exact replay.

    Returns a receipt dict whose ``status`` is ``imported`` or ``replayed``.
    A contract error, month conflict, budget breach or lock failure rolls the
    whole transaction back: no partial month is stored and no refund budget
    is consumed.
    """
    period = parse_period(period_text)
    ledger_bytes = _read_captured(ledger_path)
    bank_bytes = _read_captured(bank_path)
    ledger_sha = hashlib.sha256(ledger_bytes).hexdigest()
    bank_sha = hashlib.sha256(bank_bytes).hexdigest()
    events, input_duplicates = parse_ledger(ledger_bytes)
    bank = parse_bank(bank_bytes)

    connection = _connect(db_path, busy_timeout)
    try:
        # BEGIN IMMEDIATE takes the writer lock before any decision read, so
        # the month sequence and refund budgets are re-checked inside the lock.
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MonthlyError(
                f"database lock not acquired within {busy_timeout:g}s: {exc}")
        try:
            if _is_v1_database(connection):
                raise MonthlyError(
                    "database already exists without a monthly schema; "
                    "monthly mode refuses to guess, migrate or clear it")
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='monthly_meta'").fetchone()
            if not table_exists:
                # Individual execute() calls keep the explicit BEGIN IMMEDIATE
                # transaction intact (executescript would commit it first).
                for statement in (s.strip() for s in _SCHEMA.split(";")
                                  if s.strip()):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO monthly_meta(key,value) VALUES('schema_version',?)",
                    (str(MONTHLY_SCHEMA_VERSION),))
            else:
                _check_monthly_store(connection)
            _sync_hook("locked")

            stored_month = connection.execute(
                "SELECT * FROM months WHERE period=?", (period,)).fetchone()
            last = connection.execute(
                "SELECT period, seq FROM months ORDER BY seq DESC LIMIT 1"
            ).fetchone()

            if stored_month is not None:
                receipt = _handle_replay(connection, stored_month, events, bank,
                                         input_duplicates, ledger_sha, bank_sha)
            else:
                if last is not None and next_period(last["period"]) != period:
                    raise MonthlyError(
                        f"month {period} is not the next calendar month after "
                        f"{last['period']}; skipped or back-filled months are rejected")
                _check_global_identities(connection, events)
                _check_refund_integrity(events)
                _sync_hook("before_commit")
                _insert_new_month(connection, period,
                                  0 if last is None else last["seq"] + 1,
                                  events, bank, ledger_bytes, bank_bytes,
                                  input_duplicates, ledger_sha, bank_sha)
                receipt = {
                    "status": "imported",
                    "period": period,
                    "events": len(events),
                    "bank_facts": len(bank),
                    "input_duplicate_ledger_rows": input_duplicates,
                    "stored_duplicate_ledger_rows": input_duplicates,
                    "input_sha256": {"ledger": ledger_sha, "bank": bank_sha},
                }
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
    finally:
        connection.close()
    return receipt


def _canonical_event_set(events):
    return sorted(
        (e["event_id"], e["kind"], e["original_id"], e["settlement_id"],
         e["currency"], e["minor"])
        for e in events.values())


def _canonical_bank_set(bank):
    return sorted(
        (key[0], key[1], fact["minor"]) for key, fact in bank.items())


def _handle_replay(connection, stored_month, events, bank,
                   input_duplicates, ledger_sha, bank_sha):
    stored_events = connection.execute(
        "SELECT event_id,kind,original_id,settlement_id,currency,amount_minor "
        "FROM events WHERE period=?", (stored_month["period"],)).fetchall()
    stored_event_set = sorted(tuple(row) for row in stored_events)
    stored_bank = connection.execute(
        "SELECT settlement_id,currency,amount_minor FROM bank_facts "
        "WHERE period=?", (stored_month["period"],)).fetchall()
    stored_bank_set = sorted(tuple(row) for row in stored_bank)

    # Full-set comparison over canonical facts: row order, equivalent amount
    # spellings and equivalent duplicate repetitions may all differ. Any
    # missing, added or changed fact is a month conflict; nothing is written.
    if stored_event_set != _canonical_event_set(events) or \
            stored_bank_set != _canonical_bank_set(bank):
        raise MonthlyError(
            f"month {stored_month['period']} was already received with "
            "different canonical facts; received months are immutable")
    return {
        "status": "replayed",
        "period": stored_month["period"],
        "events": len(stored_event_set),
        "bank_facts": len(stored_bank_set),
        # This run's presentation never replaces the first accepted source.
        "input_duplicate_ledger_rows": input_duplicates,
        "stored_duplicate_ledger_rows":
            stored_month["ledger_duplicate_rows"],
        "input_sha256": {"ledger": ledger_sha, "bank": bank_sha},
        "stored_sha256": {
            "ledger": stored_month["ledger_sha256"],
            "bank": stored_month["bank_sha256"],
        },
    }


def _check_global_identities(connection, events):
    if not events:
        return
    existing = {row[0] for row in connection.execute(
        "SELECT event_id FROM events WHERE event_id IN (%s)" %
        ",".join("?" for _ in events), tuple(events))}
    if existing:
        raise MonthlyError(
            "event_id already received in an earlier month: "
            + ", ".join(sorted(existing)))


def _check_refund_integrity(events):
    """Validate same-month parent references on the unordered full-month set.

    Parents received in earlier months are checked inside the write
    transaction in _insert_new_month, where stored facts are visible.
    """
    for event in events.values():
        if event["kind"] != "refund":
            continue
        parent = events.get(event["original_id"])
        if parent is None:
            continue  # earlier-month or unknown parent decided in the lock
        if parent["kind"] != "payment":
            raise MonthlyError(
                f"refund {event['event_id']} parent "
                f"{event['original_id']} is a {parent['kind']}, not a payment")
        if parent["currency"] != event["currency"]:
            raise MonthlyError(
                f"refund {event['event_id']} currency "
                f"{event['currency']} differs from payment currency "
                f"{parent['currency']}")


def _insert_new_month(connection, period, seq, events, bank,
                      ledger_bytes, bank_bytes, duplicate_rows,
                      ledger_sha, bank_sha):
    # 1. Immutable month header and first captured source bytes.
    connection.execute(
        "INSERT INTO months(period,seq,ledger_sha256,bank_sha256,ledger_raw,"
        "bank_raw,ledger_duplicate_rows,bank_duplicate_rows,received_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (period, seq, ledger_sha, bank_sha,
         sqlite3.Binary(ledger_bytes), sqlite3.Binary(bank_bytes),
         duplicate_rows, 0,
         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    connection.executemany(
        "INSERT INTO events(event_id,period,kind,original_id,settlement_id,"
        "currency,amount_minor,ledger_record) VALUES(?,?,?,?,?,?,?,?)",
        [(e["event_id"], period, e["kind"], e["original_id"],
          e["settlement_id"], e["currency"], e["minor"], e["ledger_record"])
         for e in events.values()])
    connection.executemany(
        "INSERT INTO bank_facts(period,settlement_id,currency,amount_minor,"
        "bank_record) VALUES(?,?,?,?,?)",
        [(period, fact["settlement_id"], fact["currency"], fact["minor"],
          fact["bank_record"]) for fact in bank.values()])

    # 2. Refund budget: parent may live in this month or any received month.
    payments = {}
    for row in connection.execute(
            "SELECT payment_event_id,currency,original_minor,refunded_minor "
            "FROM payment_balances"):
        payments[row["payment_event_id"]] = dict(row)
    for event in events.values():
        if event["kind"] != "payment":
            continue
        if event["event_id"] in payments:
            raise MonthlyError(
                f"duplicate payment event_id {event['event_id']}")
        payments[event["event_id"]] = {
            "event_id": event["event_id"],
            "currency": event["currency"],
            "original_minor": event["minor"],
            "refunded_minor": 0,
        }
    # Whole-month budget delta per parent: the batch is atomic, so even if
    # each refund alone fits, a batch exceeding the remaining principal fails
    # the entire month and nothing below is persisted.
    delta = {}
    for event in events.values():
        if event["kind"] == "refund":
            parent = payments.get(event["original_id"])
            if parent is None:
                known = connection.execute(
                    "SELECT kind,currency FROM events WHERE event_id=?",
                    (event["original_id"],)).fetchone()
                if known is None:
                    raise MonthlyError(
                        f"refund {event['event_id']} references unknown or "
                        f"future payment {event['original_id']}")
                raise MonthlyError(
                    f"refund {event['event_id']} parent "
                    f"{event['original_id']} is a {known['kind']}, not a payment")
            if parent["currency"] != event["currency"]:
                raise MonthlyError(
                    f"refund {event['event_id']} currency "
                    f"{event['currency']} differs from payment currency "
                    f"{parent['currency']}")
            delta[event["original_id"]] = \
                delta.get(event["original_id"], 0) + event["minor"]
    for parent_id, refund_sum in delta.items():
        parent = payments[parent_id]
        if parent["refunded_minor"] + refund_sum > parent["original_minor"]:
            raise MonthlyError(
                f"cumulative refunds for payment {parent_id} would exceed its "
                f"principal of {parent['original_minor']} minor units")
        parent["refunded_minor"] += refund_sum

    # 3. Persist current balances (insert new payments, update refunds).
    # Invariants original=refunded+remaining and remaining>=0 hold by exact
    # integer construction; fees never touch a payment's refundable principal.
    for pid, p_row in payments.items():
        remaining = p_row["original_minor"] - p_row["refunded_minor"]
        connection.execute(
            "INSERT INTO payment_balances(payment_event_id,period,currency,"
            "original_minor,refunded_minor,remaining_minor) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(payment_event_id) DO UPDATE SET "
            "refunded_minor=excluded.refunded_minor,"
            "remaining_minor=excluded.remaining_minor",
            (pid, connection.execute(
                "SELECT period FROM events WHERE event_id=?",
                (pid,)).fetchone()[0],
             p_row["currency"], p_row["original_minor"],
             p_row["refunded_minor"], remaining))

    # 4. Month-scoped reconciliation using only this month's events and the
    # same expected/difference math and statuses as the full-export mode.
    month_entries = [{
        "settlement_id": e["settlement_id"],
        "currency": e["currency"],
        "minor": e["minor"],
        "kind": e["kind"],
    } for e in events.values()]
    month_bank = {key: fact["minor"] for key, fact in bank.items()}
    report = reconcile(month_entries, month_bank)
    connection.executemany(
        "INSERT INTO reconciliation(period,settlement_id,currency,"
        "expected_minor,bank_minor,difference_minor,status,payment_count,"
        "refund_count,fee_count) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(period, group["settlement_id"], group["currency"],
          group["expected_minor"], group["bank_minor"],
          group["difference_minor"], group["status"],
          group["event_counts"]["payment"], group["event_counts"]["refund"],
          group["event_counts"]["fee"])
         for group in report["settlements"]])


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #
def report(period_text, db_path, output_path):
    """Build a deterministic schema_version=2 historical report.

    The database is opened read-only and never created or upgraded. Every fact
    comes from one read snapshot, only facts at or before the requested month
    are considered, and the output is swapped in via a same-directory temp
    file so a failed write leaves any previous report untouched.
    """
    period = parse_period(period_text)
    db_path = Path(db_path)
    if not db_path.exists():
        raise MonthlyError(f"monthly database not found: {db_path}")
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro",
                                 uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    payload = None
    try:
        if _is_v1_database(connection):
            raise MonthlyError(
                "database is not a monthly store; refusing to report")
        _check_monthly_store(connection)
        month = connection.execute(
            "SELECT * FROM months WHERE period=?", (period,)).fetchone()
        if month is None:
            raise MonthlyError(f"month {period} has not been received")

        settlements = [dict(row) for row in connection.execute(
            "SELECT settlement_id,currency,expected_minor,bank_minor,"
            "difference_minor,status,payment_count,refund_count,fee_count "
            "FROM reconciliation WHERE period=? "
            "ORDER BY settlement_id,currency", (period,))]
        for row in settlements:
            row["event_counts"] = {
                "payment": row.pop("payment_count"),
                "refund": row.pop("refund_count"),
                "fee": row.pop("fee_count"),
            }
        summary = {
            "groups": len(settlements),
            "balanced": sum(r["status"] == "balanced" for r in settlements),
            "unresolved": sum(
                r["status"] != "balanced" for r in settlements),
        }

        # Balances at month end: integer math in Python, never SQL SUM/float.
        balances = []
        for payment in connection.execute(
                "SELECT event_id,period,currency,amount_minor FROM events "
                "WHERE kind='payment' AND period<=? ORDER BY event_id",
                (period,)):
            refunded = 0
            for refund in connection.execute(
                    "SELECT amount_minor FROM events "
                    "WHERE kind='refund' AND original_id=? AND period<=?",
                    (payment["event_id"], period)):
                refunded += refund["amount_minor"]
            balances.append({
                "payment_event_id": payment["event_id"],
                "payment_period": payment["period"],
                "currency": payment["currency"],
                "original_minor": payment["amount_minor"],
                "refunded_minor": refunded,
                "remaining_minor": payment["amount_minor"] - refunded,
            })

        refund_traces = []
        for refund in connection.execute(
                "SELECT event_id,period,original_id,settlement_id,currency,"
                "amount_minor,ledger_record FROM events "
                "WHERE kind='refund' AND period=? ORDER BY event_id",
                (period,)):
            parent = connection.execute(
                "SELECT period,ledger_record FROM events WHERE event_id=?",
                (refund["original_id"],)).fetchone()
            if parent is None:  # defensive: ingest invariants forbid this
                raise MonthlyError(
                    f"refund {refund['event_id']} has no stored payment")
            refund_traces.append({
                "refund_event_id": refund["event_id"],
                "refund_period": refund["period"],
                "amount_minor": refund["amount_minor"],
                "currency": refund["currency"],
                "settlement_id": refund["settlement_id"],
                "original_payment_id": refund["original_id"],
                "original_payment_period": parent["period"],
                "refund_ledger_record": refund["ledger_record"],
                "payment_ledger_record": parent["ledger_record"],
                "sources": _source_refs(connection, period,
                                        refund["period"], parent["period"]),
            })

        sources = _all_source_refs(connection, period)
        payload = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "period": period,
            "summary": summary,
            "settlements": settlements,
            "duplicate_ledger_rows": month["ledger_duplicate_rows"],
            "bank_duplicate_rows": month["bank_duplicate_rows"],
            "payment_balances_at_month_end": balances,
            "refunds": refund_traces,
            "sources": sources,
        }
    finally:
        connection.close()

    _atomic_write_json(output_path, payload)
    return payload


def _source_refs(connection, report_period, *periods):
    refs = {}
    for period in sorted({p for p in periods if p <= report_period}):
        month = connection.execute(
            "SELECT period,ledger_sha256,bank_sha256 FROM months "
            "WHERE period=?", (period,)).fetchone()
        refs[period] = {
            "ledger": {"role": "ledger", "period": month["period"],
                       "sha256": month["ledger_sha256"]},
            "bank": {"role": "bank", "period": month["period"],
                     "sha256": month["bank_sha256"]},
        }
    return refs


def _all_source_refs(connection, period):
    refs = []
    for month in connection.execute(
            "SELECT period,ledger_sha256,bank_sha256 FROM months "
            "WHERE period<=? ORDER BY period", (period,)):
        refs.append({"role": "ledger", "period": month["period"],
                     "sha256": month["ledger_sha256"]})
        refs.append({"role": "bank", "period": month["period"],
                     "sha256": month["bank_sha256"]})
    return refs


def _atomic_write_json(output_path, payload):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n") \
        .encode("utf-8")
    temp_path = None
    try:
        # NamedTemporaryFile in the same directory guarantees same-filesystem
        # atomic replacement; the file is created with 0600 permissions.
        with __import__("tempfile").NamedTemporaryFile(
                mode="wb", dir=output_path.parent, prefix=output_path.name,
                suffix=".tmp", delete=False) as temp:
            temp_path = Path(temp.name)
            temp.write(encoded)
            temp.flush()
            os.fsync(temp.fileno())
        if os.environ.get("SETTLEMENT_AUDIT_REPORT_FAIL") == "rename":
            raise OSError("injected report replacement failure")
        os.replace(temp_path, output_path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser():
    parser = argparse.ArgumentParser(
        prog="settlement_audit.monthly",
        description="Monthly immutable ingestion and cross-month refund audit")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_parser = sub.add_parser("ingest", help="receive one export month")
    ingest_parser.add_argument("--period", required=True)
    ingest_parser.add_argument("--ledger", required=True, type=Path)
    ingest_parser.add_argument("--bank", required=True, type=Path)
    ingest_parser.add_argument("--db", required=True, type=Path)
    ingest_parser.add_argument("--busy-timeout", type=float,
                               default=_DEFAULT_BUSY_TIMEOUT)

    report_parser = sub.add_parser("report", help="render a historical month")
    report_parser.add_argument("--period", required=True)
    report_parser.add_argument("--db", required=True, type=Path)
    report_parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            receipt = ingest(args.period, args.ledger, args.bank, args.db,
                             busy_timeout=args.busy_timeout)
        else:
            report(args.period, args.db, args.output)
            receipt = {"status": "reported", "period": args.period,
                       "output": str(args.output)}
    except (MonthlyError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"monthly {args.command} failed: {exc}\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
