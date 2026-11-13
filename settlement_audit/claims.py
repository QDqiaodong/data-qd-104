"""Independent receipt-claim range analysis over one received monthly store.

Background
----------
A bank receipt (回单) without a settlement reference may be claimable by
several settlement groups.  Claiming receipts in file order can consume funds
that another group could otherwise claim exclusively, so order-based claiming
is not a sound allocation.  This module instead treats the claims as a global
per-currency transportation problem and answers, for every allowed relation
and every demand group, the minimum and maximum amount it can carry in *any*
allocation that maximizes the total claimed amount.

It is deliberately separate from the v1 full-export pipeline and from the
monthly ingestion/report code: it reads one already-received month read-only,
never writes the monthly database and never changes reconciliation facts.

Model (per currency)
--------------------
- demand: settlement group ``(settlement_id, currency)`` whose stored
  ``expected_minor`` is a positive integer;
- supply: each receipt row with a positive minor amount;
- allowed relation: ``receipt_id -> (settlement_id, currency)`` from the
  relation CSV; only relations whose currency matches the receipt currency
  and whose group has positive demand participate;
- network: ``source -> receipt -> group -> sink`` with receipt supply and
  group demand capacities.  A maximum flow is exactly a maximum total claim
  respecting every receipt supply and every group demand.

Ranges come from residual-network queries (see :mod:`settlement_audit.flow`),
never from enumerating minor units or all allocations.  Each reported
endpoint carries a complete integral allocation witness; independently
maximized endpoints are *not* jointly attainable and no combined vector is
produced.

CLI::

    python -m settlement_audit.claims --period YYYY-MM --db FILE \
        --receipts FILE --relations FILE --output FILE
"""
import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .flow import FlowNetwork, edge_flow_range
from .money import minor

CLAIM_SCHEMA_VERSION = 1
SOURCE_SCHEMA_VERSION = 1  # monthly store schema this analysis understands
RECEIPT_HEADER = ["receipt_id", "currency", "amount"]
RELATION_HEADER = ["receipt_id", "settlement_id", "currency"]
_RANGE_SEMANTICS = (
    "Each minimum/maximum is the endpoint of that arc or group total across "
    "all allocations that maximize the total claimed amount. Endpoints are "
    "witnessed by separate complete allocations and are not jointly "
    "attainable; do not concatenate maxima into one allocation.")


class ClaimError(ValueError):
    """A contract, store or input violation in claim analysis."""


# --------------------------------------------------------------------------- #
# Input parsing (one read, exact strings, integer minor units)
# --------------------------------------------------------------------------- #
def _decode(raw_bytes, label):
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ClaimError(f"{label} is not valid UTF-8: {exc}") from exc


def _rows(text, expected_header, label):
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != expected_header:
        raise ClaimError(f"invalid {label} columns")
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ClaimError(f"malformed {label} row")
        if any(not row[column] for column in expected_header):
            raise ClaimError(f"missing {label} identity")
        yield row


def parse_receipts(raw_bytes):
    """Parse receipt CSV bytes into ``{receipt_id: record}``.

    Header ``receipt_id,currency,amount``.  Amount precision, positivity and
    the 10^12 minor-unit bound reuse :func:`settlement_audit.money.minor`.
    Any repeated ``receipt_id`` is rejected, even for identical rows.
    """
    receipts = {}
    for row_number, row in enumerate(
            _rows(_decode(raw_bytes, "receipts"), RECEIPT_HEADER, "receipt"),
            start=1):
        receipt_id = row["receipt_id"]
        currency = row["currency"]
        try:
            supply = minor(row["amount"], currency)
        except ValueError as exc:
            raise ClaimError(
                f"receipt {receipt_id}: {exc}") from exc
        if receipt_id in receipts:
            raise ClaimError(
                f"duplicate receipt_id {receipt_id!r} in receipts")
        receipts[receipt_id] = {
            "receipt_id": receipt_id,
            "currency": currency,
            "supply_minor": supply,
            "receipt_record": row_number,
        }
    return receipts


def parse_relations(raw_bytes):
    """Parse relation CSV bytes into an ordered list of unique relations.

    Header ``receipt_id,settlement_id,currency``.  A repeated
    ``(receipt_id, settlement_id, currency)`` key is rejected.  Both tables
    may be header-only (empty).
    """
    relations = []
    seen = set()
    for row_number, row in enumerate(
            _rows(_decode(raw_bytes, "relations"), RELATION_HEADER,
                  "relation"),
            start=1):
        key = (row["receipt_id"], row["settlement_id"], row["currency"])
        if key in seen:
            raise ClaimError(
                "duplicate relation "
                f"{key[0]}/{key[1]}/{key[2]} in relations")
        seen.add(key)
        relations.append({
            "receipt_id": key[0],
            "settlement_id": key[1],
            "currency": key[2],
            "relation_record": row_number,
        })
    return relations


# --------------------------------------------------------------------------- #
# Read-only single-snapshot store access
# --------------------------------------------------------------------------- #
def _open_snapshot(db_path):
    db_path = Path(db_path)
    if not db_path.exists():
        raise ClaimError(f"monthly database not found: {db_path}")
    connection = sqlite3.connect(
        f"file:{db_path.resolve()}?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "monthly_meta" not in tables:
        connection.close()
        raise ClaimError(
            "database is not a monthly store; claim analysis refuses to guess")
    version = connection.execute(
        "SELECT value FROM monthly_meta WHERE key='schema_version'").fetchone()
    if version is None or int(version[0]) != SOURCE_SCHEMA_VERSION:
        connection.close()
        raise ClaimError("unsupported monthly database schema version")
    required = {"months", "reconciliation", "events", "bank_facts"}
    if not required <= tables:
        connection.close()
        raise ClaimError("monthly database is missing required tables")
    # One explicit read transaction gives every SELECT below the same
    # snapshot; nothing is ever written in read-only mode.
    connection.execute("BEGIN")
    return connection


def _read_snapshot(connection, period):
    month = connection.execute(
        "SELECT period,ledger_sha256,bank_sha256,ledger_duplicate_rows,"
        "bank_duplicate_rows FROM months WHERE period=?", (period,)).fetchone()
    if month is None:
        raise ClaimError(f"month {period} has not been received")
    groups = [dict(row) for row in connection.execute(
        "SELECT settlement_id,currency,expected_minor,bank_minor,"
        "difference_minor,status,payment_count,refund_count,fee_count "
        "FROM reconciliation WHERE period=? "
        "ORDER BY settlement_id,currency", (period,))]
    counts = {
        "events": connection.execute(
            "SELECT count(*) FROM events WHERE period=?", (period,)).fetchone()[0],
        "bank_facts": connection.execute(
            "SELECT count(*) FROM bank_facts WHERE period=?",
            (period,)).fetchone()[0],
        "reconciliation": len(groups),
    }
    connection.execute("COMMIT")
    return dict(month), groups, counts


# --------------------------------------------------------------------------- #
# Model construction
# --------------------------------------------------------------------------- #
def _classify_groups(groups):
    demand, excluded = [], []
    for group in groups:
        expected = group["expected_minor"]
        if expected is None:
            reason = "missing_internal_amount"
        elif expected == 0:
            reason = "zero_expected_minor"
        elif expected < 0:
            reason = "negative_expected_minor"
        else:
            demand.append(group)
            continue
        excluded.append({
            "settlement_id": group["settlement_id"],
            "currency": group["currency"],
            "expected_minor": expected,
            "status": group["status"],
            "reason": reason,
        })
    return demand, excluded


def _validate_relations(relations, receipts, all_groups, demand_keys):
    """Resolve references; hard errors for unknown and cross-currency refs."""
    retained, excluded = [], []
    for relation in relations:
        receipt = receipts.get(relation["receipt_id"])
        if receipt is None:
            raise ClaimError(
                "relation references unknown receipt "
                f"{relation['receipt_id']!r}")
        if receipt["currency"] != relation["currency"]:
            raise ClaimError(
                f"relation for receipt {relation['receipt_id']!r} uses "
                f"currency {relation['currency']!r} but the receipt is "
                f"{receipt['currency']!r}; cross-currency relations are "
                "rejected")
        key = (relation["settlement_id"], relation["currency"])
        if key not in all_groups:
            raise ClaimError(
                "relation references unknown settlement/currency group "
                f"{relation['settlement_id']}/{relation['currency']}")
        if key not in demand_keys:
            excluded.append({
                **relation,
                "reason": "group_not_positive_demand",
            })
            continue
        retained.append(relation)
    return retained, excluded


def _witness_id(flows):
    encoded = json.dumps(flows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


class _CurrencyCase:
    """One currency's transportation network and its maximum flow."""

    def __init__(self, currency, receipts, groups, relations):
        self.currency = currency
        self.receipts = receipts  # list sorted by receipt_id
        self.groups = groups  # list sorted by settlement_id
        self.relations = relations  # list sorted (receipt_id, settlement_id)
        count = 2 + len(receipts) + len(groups)
        self.source = 0
        self.sink = count - 1
        self.network = FlowNetwork(count)
        self.receipt_node = {
            r["receipt_id"]: 1 + i for i, r in enumerate(receipts)}
        self.group_node = {
            g["settlement_id"]: 1 + len(receipts) + i
            for i, g in enumerate(groups)}
        self.supply_total = sum(r["supply_minor"] for r in receipts)
        self.demand_total = sum(g["expected_minor"] for g in groups)
        unbounded = self.supply_total + self.demand_total
        self.supply_edges = {}
        for receipt in receipts:
            self.supply_edges[receipt["receipt_id"]] = self.network.add_edge(
                self.source, self.receipt_node[receipt["receipt_id"]],
                receipt["supply_minor"])
        self.relation_edges = []
        for relation in relations:
            edge = self.network.add_edge(
                self.receipt_node[relation["receipt_id"]],
                self.group_node[relation["settlement_id"]],
                unbounded)
            self.relation_edges.append((relation, edge))
        self.group_edges = {}
        for group in groups:
            self.group_edges[group["settlement_id"]] = self.network.add_edge(
                self.group_node[group["settlement_id"]], self.sink,
                group["expected_minor"])
        self.max_total = self.network.max_flow(self.source, self.sink)
        self.base_flows = [self.network.flow(edge_id)
                           for edge_id in range(len(self.network.edges))]
        self._ensure_accounting()
        self._witnesses = {}
        self._witness_order = []

    # -- evidence helpers -------------------------------------------------- #
    def _ensure_accounting(self):
        for relation, edge in self.relation_edges:
            supply = next(r["supply_minor"] for r in self.receipts
                          if r["receipt_id"] == relation["receipt_id"])
            if not 0 <= self.base_flows[edge] <= supply:
                raise ClaimError("internal claim accounting violation")
        for group in self.groups:
            edge = self.group_edges[group["settlement_id"]]
            if not 0 <= self.base_flows[edge] <= group["expected_minor"]:
                raise ClaimError("internal claim accounting violation")

    def _relation_vector(self, flows):
        return [[relation["receipt_id"], relation["settlement_id"],
                 flows[edge]]
                for relation, edge in self.relation_edges
                if flows[edge] > 0]

    def witness(self, flows):
        vector = self._relation_vector(flows)
        identifier = "w" + _witness_id(vector)
        if identifier not in self._witnesses:
            receipt_totals = {}
            group_totals = {}
            for receipt_id, settlement_id, amount in vector:
                receipt_totals[receipt_id] = \
                    receipt_totals.get(receipt_id, 0) + amount
                group_totals[settlement_id] = \
                    group_totals.get(settlement_id, 0) + amount
            self._witnesses[identifier] = {
                "total_claim_minor": sum(
                    amount for _r, _s, amount in vector),
                "relation_flows": vector,
                "receipt_claimed_minor": dict(sorted(receipt_totals.items())),
                "group_claimed_minor": dict(sorted(group_totals.items())),
            }
            self._witness_order.append(identifier)
        return identifier

    def min_cut(self):
        reachable = self.network.reachable(self.source)
        cut_edges = []
        capacity = 0
        for u, v, edge_capacity, _forward in self.network.edges:
            if u in reachable and v not in reachable:
                cut_edges.append([self._node_name(u), self._node_name(v),
                                  edge_capacity])
                capacity += edge_capacity
        if capacity != self.max_total:
            raise ClaimError("internal minimum-cut evidence mismatch")
        return {
            "capacity_minor": capacity,
            "source_side": sorted(self._node_name(n) for n in reachable),
            "sink_side": sorted(self._node_name(n) for n in range(
                self.network.n) if n not in reachable),
            "cut_edges": cut_edges,
        }

    def _node_name(self, node):
        if node == self.source:
            return "source"
        if node == self.sink:
            return "sink"
        for receipt in self.receipts:
            if self.receipt_node[receipt["receipt_id"]] == node:
                return "receipt:" + receipt["receipt_id"]
        for group in self.groups:
            if self.group_node[group["settlement_id"]] == node:
                return "group:" + group["settlement_id"]
        raise ClaimError("internal unknown node")

    def network_json(self):
        edges = []
        for receipt in self.receipts:
            edge = self.supply_edges[receipt["receipt_id"]]
            edges.append({
                "from": "source",
                "to": "receipt:" + receipt["receipt_id"],
                "kind": "receipt_supply",
                "capacity_minor": receipt["supply_minor"],
                "flow_minor": self.base_flows[edge],
            })
        for relation, edge in self.relation_edges:
            edges.append({
                "from": "receipt:" + relation["receipt_id"],
                "to": "group:" + relation["settlement_id"],
                "kind": "allowed_relation",
                "capacity_minor": None,
                "flow_minor": self.base_flows[edge],
                "relation_record": relation["relation_record"],
            })
        for group in self.groups:
            edge = self.group_edges[group["settlement_id"]]
            edges.append({
                "from": "group:" + group["settlement_id"],
                "to": "sink",
                "kind": "group_demand",
                "capacity_minor": group["expected_minor"],
                "flow_minor": self.base_flows[edge],
            })
        return {
            "source": "source",
            "sink": "sink",
            "receipt_nodes": [
                {"node": "receipt:" + r["receipt_id"],
                 "supply_minor": r["supply_minor"],
                 "receipt_record": r["receipt_record"]}
                for r in self.receipts],
            "group_nodes": [
                {"node": "group:" + g["settlement_id"],
                 "settlement_id": g["settlement_id"],
                 "currency": g["currency"],
                 "expected_minor": g["expected_minor"]}
                for g in self.groups],
            "edges": edges,
        }

    # -- ranges ------------------------------------------------------------ #
    def relation_ranges(self):
        entries = []
        for relation, edge in self.relation_edges:
            low, high, low_flows, high_flows = edge_flow_range(
                self.network, self.base_flows, edge, self.source, self.sink)
            entries.append({
                "receipt_id": relation["receipt_id"],
                "settlement_id": relation["settlement_id"],
                "currency": self.currency,
                "relation_record": relation["relation_record"],
                "min_claim_minor": low,
                "max_claim_minor": high,
                "min_witness": self.witness(low_flows),
                "max_witness": self.witness(high_flows),
            })
        return entries

    def group_ranges(self):
        entries = []
        for group in self.groups:
            edge = self.group_edges[group["settlement_id"]]
            low, high, low_flows, high_flows = edge_flow_range(
                self.network, self.base_flows, edge, self.source, self.sink)
            entries.append({
                "settlement_id": group["settlement_id"],
                "currency": self.currency,
                "expected_minor": group["expected_minor"],
                "status": group["status"],
                "min_claim_minor": low,
                "max_claim_minor": high,
                "min_witness": self.witness(low_flows),
                "max_witness": self.witness(high_flows),
            })
        return entries

    def witnesses_json(self):
        return {identifier: self._witnesses[identifier]
                for identifier in self._witness_order}


def analyze(period, db_path, receipts_path, relations_path, output_path):
    """Analyze one received month read-only and atomically replace ``output``.

    Every input file is read exactly once; parsing, hashing and reporting use
    that capture.  Any failure before the final rename leaves any previous
    report untouched.
    """
    receipt_bytes = Path(receipts_path).read_bytes()
    relation_bytes = Path(relations_path).read_bytes()
    receipts = parse_receipts(receipt_bytes)
    relations = parse_relations(relation_bytes)

    connection = _open_snapshot(db_path)
    try:
        month, groups, counts = _read_snapshot(connection, period)
    finally:
        connection.close()

    demand_groups, excluded_groups = _classify_groups(groups)
    all_group_keys = {(g["settlement_id"], g["currency"]) for g in groups}
    demand_keys = {(g["settlement_id"], g["currency"])
                   for g in demand_groups}
    retained, excluded_relations = _validate_relations(
        relations, receipts, all_group_keys, demand_keys)

    demand_by_currency = {}
    for group in demand_groups:
        demand_by_currency.setdefault(group["currency"], []).append(group)
    receipts_by_currency = {}
    for receipt in receipts.values():
        receipts_by_currency.setdefault(receipt["currency"], []).append(
            receipt)
    relations_by_currency = {}
    for relation in retained:
        relations_by_currency.setdefault(relation["currency"], []).append(
            relation)

    currencies = sorted(set(demand_by_currency) | set(receipts_by_currency)
                        | set(relations_by_currency))
    sections = {}
    totals = {}
    for currency in currencies:
        receipt_rows = sorted(
            receipts_by_currency.get(currency, []),
            key=lambda row: row["receipt_id"])
        group_rows = sorted(
            demand_by_currency.get(currency, []),
            key=lambda row: row["settlement_id"])
        relation_rows = sorted(
            relations_by_currency.get(currency, []),
            key=lambda row: (row["receipt_id"], row["settlement_id"]))
        case = _CurrencyCase(currency, receipt_rows, group_rows,
                             relation_rows)
        cut = case.min_cut()
        sections[currency] = {
            "receipt_count": len(receipt_rows),
            "demand_group_count": len(group_rows),
            "relation_count": len(relation_rows),
            "supply_total_minor": case.supply_total,
            "demand_total_minor": case.demand_total,
            "max_total_claim_minor": case.max_total,
            "unclaimed_demand_minor": case.demand_total - case.max_total,
            "unused_supply_minor": case.supply_total - case.max_total,
            "network": case.network_json(),
            "minimum_cut": cut,
            "relation_ranges": case.relation_ranges(),
            "group_ranges": case.group_ranges(),
            "witnesses": case.witnesses_json(),
        }
        totals[currency] = case.max_total

    payload = {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "analysis": "settlement_claim_ranges",
        "period": period,
        "range_semantics": _RANGE_SEMANTICS,
        "summary": {
            "currencies": currencies,
            "total_max_claim_minor_by_currency": totals,
            "receipt_count": len(receipts),
            "relation_count": len(relations),
            "retained_relation_count": len(retained),
            "demand_group_count": len(demand_groups),
            "excluded_group_count": len(excluded_groups),
            "excluded_relation_count": len(excluded_relations),
        },
        "currencies": sections,
        "excluded_groups": excluded_groups,
        "excluded_relations": excluded_relations,
        "sources": {
            "period": period,
            "monthly_schema_version": SOURCE_SCHEMA_VERSION,
            "month": {
                "ledger_sha256": month["ledger_sha256"],
                "bank_sha256": month["bank_sha256"],
                "ledger_duplicate_rows": month["ledger_duplicate_rows"],
                "bank_duplicate_rows": month["bank_duplicate_rows"],
            },
            "stored_counts": counts,
            "receipts": {
                "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
                "rows": len(receipts),
            },
            "relations": {
                "sha256": hashlib.sha256(relation_bytes).hexdigest(),
                "rows": len(relations),
            },
        },
    }
    _atomic_write_json(output_path, payload)
    return payload


def _atomic_write_json(output_path, payload):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
        "utf-8")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="wb", dir=output_path.parent, prefix=output_path.name,
                suffix=".tmp", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if os.environ.get("SETTLEMENT_AUDIT_CLAIM_FAIL") == "rename":
            raise OSError("injected claim report replacement failure")
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
        prog="settlement_audit.claims",
        description="Receipt claim min/max range analysis over one received "
                    "monthly settlement month (read-only)")
    parser.add_argument("--period", required=True, help="received YYYY-MM")
    parser.add_argument("--db", required=True, type=Path,
                        help="monthly SQLite store (opened read-only)")
    parser.add_argument("--receipts", required=True, type=Path,
                        help="receipts CSV: receipt_id,currency,amount")
    parser.add_argument("--relations", required=True, type=Path,
                        help="relations CSV: receipt_id,settlement_id,currency")
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        payload = analyze(args.period, args.db, args.receipts,
                          args.relations, args.output)
    except (ClaimError, ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"claim analysis failed: {exc}\n")
    print(json.dumps({
        "status": "analyzed",
        "period": payload["period"],
        "output": str(args.output),
        "summary": payload["summary"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
