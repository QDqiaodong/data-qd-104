"""Offline collection of paged bank statements in a strict MT940 subset.

Banks deliver statements split into pages, and a page balance is not a
settlement amount. This module is an independent, fully offline entry point
next to the v1 pipeline and the monthly/claims modules. It parses a strict
synthetic subset of MT940, verifies every page and every page boundary with
exact integer minor-unit arithmetic (no float), maps account/currency pairs
explicitly to settlement IDs, and publishes two files together: a legacy
``bank.json`` readable by ``settlement_audit.input.load`` and a page/transaction
level audit JSON. Only the Python standard library is used.

Strict MT940 subset, one tag per physical line:

    :25:  account identifier, [A-Z0-9/-]{1,34}
    :28C: statement number / page number, each 1-5 digits; page defaults to 1
    :60F: or :60M: opening balance, C|D + YYMMDD + currency + unsigned amount
    :61:  one entry, YYMMDD + debit/credit mark + unsigned amount.
          Credit marks C and RD are positive; debit marks D and RC are negative.
    :86:  narrative for the immediately preceding :61:; continuation lines that
          do not start with ':' may span multiple physical lines
    :62M: or :62F: closing balance, same field shape as the opening balance

Any other tag (e.g. :20:, :64:) is rejected. Tags must appear in the order
above; a malformed tag line and a tag out of order fail the whole batch. Years
are limited to 2000-2099 (YY in 00-99). Amounts use the existing currency
precision (USD/EUR 2 decimals, JPY 0) and the 10^12 minor-unit bound; excess
precision is rejected. Balances are signed by their C/D marker, never by a sign
character in the amount text.

The captured text may be shuffled and may contain several accounts and several
statements. Pages are assembled by account/currency/statement: page numbers run
continuously from 1, first/last pages carry F and intermediate pages M,
adjacent pages link by date and signed balance, and on every page
opening + sum(signed entries) == closing. A duplicate page is rejected even
when its content is equivalent. A missing page, a broken link or an imbalance
fails the entire batch; partial output is never published.

CLI:
    python -m settlement_audit.mt940 collect \
        --mt940 FILE [FILE ...] --mapping FILE --output DIR
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path

from .money import SCALE

AUDIT_SCHEMA_VERSION = 1
BANK_FILENAME = "bank.json"
AUDIT_FILENAME = "mt940_audit.json"

_ACCOUNT_RE = re.compile(r":25:([A-Z0-9/\-]{1,34})$")
_STATEMENT_RE = re.compile(r":28C:(\d{1,5})(?:/(\d{1,5}))?$")
_BALANCE_RE = re.compile(
    r":(60[FM]|62[FM]):([CD])(\d{2})(\d{2})(\d{2})"
    r"([A-Z]{3})(\d{1,12}(?:\.\d{1,3})?)$")
_ENTRY_RE = re.compile(
    r":61:(\d{2})(\d{2})(\d{2})(C|RD|D|RC)(\d{1,12}(?:\.\d{1,3})?)$")
_NARRATIVE_RE = re.compile(r":86:(.*)$")
_TAG_PREFIX_RE = re.compile(r":([0-9A-Z]+):")
_ALLOWED_TAGS = {"25", "28C", "60F", "60M", "61", "86", "62M", "62F"}
_CREDIT_MARKS = {"C", "RD"}
_DEBIT_MARKS = {"D", "RC"}


class MT940Error(ValueError):
    """A contract violation in the strict MT940 subset or its mapping."""


# --------------------------------------------------------------------------- #
# Exact amount and date helpers (no float)
# --------------------------------------------------------------------------- #
def unsigned_minor(amount_text, currency, source, line):
    """Convert an unsigned decimal text to exact minor units.

    Mirrors settlement_audit.money.minor precision and bounds but never accepts
    a sign character: balance and entry signs come exclusively from C/D and
    debit/credit markers.
    """
    if currency not in SCALE:
        raise MT940Error(
            f"{source}:line {line}: unsupported currency {currency}")
    if not re.fullmatch(r"\d{1,12}(?:\.\d{1,3})?", amount_text):
        raise MT940Error(
            f"{source}:line {line}: invalid amount {amount_text!r}")
    whole, dot, fraction = amount_text.partition(".")
    scale = SCALE[currency]
    if len(fraction) > scale:
        raise MT940Error(
            f"{source}:line {line}: excess currency precision for {currency}: "
            f"{amount_text!r}")
    value = int(whole) * 10 ** scale + \
        int((fraction + "0" * scale)[:scale] or "0")
    if value > 10 ** 12:
        raise MT940Error(
            f"{source}:line {line}: amount outside minor-unit bounds")
    return value


def format_minor(value, currency):
    """Render a signed minor-unit integer in the currency's canonical text."""
    scale = SCALE[currency]
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if scale == 0:
        return f"{sign}{magnitude}"
    whole, fraction = divmod(magnitude, 10 ** scale)
    return f"{sign}{whole}.{fraction:0{scale}d}"


def _parse_date(year2, month2, day2, source, line):
    year = 2000 + int(year2)
    if not (2000 <= year <= 2099):  # defensive: two digits stay in 00-99
        raise MT940Error(f"{source}:line {line}: dates are limited to 20xx")
    try:
        return date(year, int(month2), int(day2))
    except ValueError as exc:
        raise MT940Error(
            f"{source}:line {line}: invalid calendar date {year2}{month2}{day2}"
        ) from exc


# --------------------------------------------------------------------------- #
# Line-level parsing into unvalidated page blocks
# --------------------------------------------------------------------------- #
def _fail(source, line, message):
    location = f"{source}:line {line}" if line else source
    raise MT940Error(f"{location}: {message}")


def parse_statements(raw_bytes, source):
    """Parse one captured MT940 byte string into unvalidated page blocks.

    Returns a list of block dicts in capture order. Shuffled pages are fine;
    cross-block and cross-file integrity is decided later by :func:`assemble`.
    Every retained fact carries its 1-based physical source line number.
    """
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise MT940Error(f"{source}: raw input must be bytes")
    try:
        text = bytes(raw_bytes).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MT940Error(f"{source}: input is not valid UTF-8") from exc

    blocks = []
    block = None
    stage = None  # None | 25 | 28C | open | entry | narrative | closed
    for line_number, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue  # blank separators carry no meaning
        tag_match = _TAG_PREFIX_RE.match(line)
        if tag_match:
            tag = tag_match.group(1)
            if tag not in _ALLOWED_TAGS:
                _fail(source, line_number, f"unknown or unsupported tag :{tag}:")
        if line.startswith(":25:"):
            if stage is not None and stage != "closed":
                _fail(source, line_number,
                      "bad order: :25: may only start a new statement page")
            match = _ACCOUNT_RE.fullmatch(line)
            if not match:
                _fail(source, line_number, "malformed :25: account identifier")
            block = {
                "source": source,
                "account": match.group(1),
                "raw_lines": [],
                "transactions": [],
            }
            blocks.append(block)
            stage = "25"
        elif line.startswith(":28C:"):
            if stage != "25":
                _fail(source, line_number, "bad order: :28C: must follow :25:")
            match = _STATEMENT_RE.fullmatch(line)
            if not match:
                _fail(source, line_number, "malformed :28C: statement/page")
            block["statement_text"] = match.group(1)
            block["statement_no"] = int(match.group(1))
            block["page_no"] = int(match.group(2) or "1")
            if block["statement_no"] < 1 or block["page_no"] < 1:
                _fail(source, line_number,
                      "malformed :28C: statement and page numbers must be >= 1")
            stage = "28C"
        elif line.startswith((":60F:", ":60M:")):
            if stage != "28C":
                _fail(source, line_number,
                      "bad order: opening balance must follow :28C:")
            block["opening"] = _parse_balance(line, line_number, source)
            stage = "open"
        elif line.startswith(":61:"):
            if stage not in ("open", "entry", "narrative"):
                _fail(source, line_number,
                      "bad order: :61: belongs between the balances")
            match = _ENTRY_RE.fullmatch(line)
            if not match:
                _fail(source, line_number,
                      "malformed :61: entry (strict subset: date, mark, amount "
                      "only)")
            entry_date = _parse_date(match.group(1), match.group(2),
                                     match.group(3), source, line_number)
            mark = match.group(4)
            amount_text = match.group(5)
            currency = block["opening"]["currency"]
            magnitude = unsigned_minor(amount_text, currency, source, line_number)
            block["transactions"].append({
                "line": line_number,
                "date": entry_date.isoformat(),
                "mark": mark,
                "dc": "C" if mark in _CREDIT_MARKS else "D",
                "amount_text": amount_text,
                "signed_minor": magnitude if mark in _CREDIT_MARKS else -magnitude,
                "narrative": None,
                "narrative_lines": [],
            })
            stage = "entry"
        elif line.startswith(":86:"):
            if stage != "entry":
                _fail(source, line_number,
                      "bad order: :86: must follow a :61: entry")
            match = _NARRATIVE_RE.fullmatch(line)
            body = match.group(1)
            if not body.strip():
                _fail(source, line_number, ":86: narrative must not be empty")
            entry = block["transactions"][-1]
            if entry["narrative"] is not None:
                _fail(source, line_number,
                      "bad order: only one :86: per :61: entry")
            entry["narrative_lines"].append(
                {"line": line_number, "text": line})
            entry["narrative"] = body
            stage = "narrative"
        elif line.startswith((":62M:", ":62F:")):
            if stage not in ("open", "entry", "narrative"):
                _fail(source, line_number,
                      "bad order: closing balance ends the page")
            block["closing"] = _parse_balance(line, line_number, source)
            stage = "closed"
        elif tag_match:
            # A recognized prefix with a malformed body is malformed, not unknown.
            _fail(source, line_number, f"malformed :{tag_match.group(1)}: line")
        else:
            if stage != "narrative":
                _fail(source, line_number,
                      "continuation line without a preceding :86: narrative")
            entry = block["transactions"][-1]
            entry["narrative_lines"].append(
                {"line": line_number, "text": line})
            entry["narrative"] = entry["narrative"] + "\n" + line
        if block is not None:
            block["raw_lines"].append({"line": line_number, "text": line})

    if stage is not None and stage != "closed":
        _fail(source, block["raw_lines"][-1]["line"],
              "statement page is missing its :62M:/:62F: closing balance")
    if not blocks:
        raise MT940Error(f"{source}: no MT940 statement pages found")
    return blocks


def _parse_balance(line, line_number, source):
    match = _BALANCE_RE.fullmatch(line)
    if not match:
        _fail(source, line_number,
              "malformed balance line (strict subset: marker + YYMMDD + "
              "currency + unsigned amount)")
    tag, dc, y2, m2, d2, currency, amount_text = match.groups()
    balance_date = _parse_date(y2, m2, d2, source, line_number)
    magnitude = unsigned_minor(amount_text, currency, source, line_number)
    return {
        "tag": tag,
        "line": line_number,
        "text": line,
        "date": balance_date.isoformat(),
        "dc": dc,
        "currency": currency,
        "amount_text": amount_text,
        "signed_minor": magnitude if dc == "C" else -magnitude,
    }


# --------------------------------------------------------------------------- #
# Assembly: account/currency/statement integrity across the whole batch
# --------------------------------------------------------------------------- #
def assemble(pages):
    """Validate and order every captured page into complete statements.

    The whole batch is atomic: duplicate pages (even equivalent ones), missing
    pages, bad F/M markers, currency drift, date/balance discontinuities and
    page imbalances all raise :class:`MT940Error` and nothing is returned.
    """
    if not pages:
        raise MT940Error("no MT940 statement pages to assemble")

    seen = set()
    grouped = {}
    for page in pages:
        identity = (page["account"], page["statement_no"], page["page_no"])
        if identity in seen:
            raise MT940Error(
                f"duplicate page {page['page_no']} of statement "
                f"{page['statement_no']} for account {page['account']} "
                f"(from {page['source']}); equivalent duplicates are rejected")
        seen.add(identity)
        key = (page["account"], page["statement_no"])
        bucket = grouped.setdefault(key, [])
        if bucket and bucket[0]["statement_text"] != page["statement_text"]:
            raise MT940Error(
                f"account {page['account']}: conflicting statement number "
                f"spellings {bucket[0]['statement_text']!r} and "
                f"{page['statement_text']!r}")
        bucket.append(page)

    statements = []
    for (account, statement_no) in sorted(grouped):
        ordered = sorted(grouped[(account, statement_no)],
                         key=lambda p: p["page_no"])
        page_numbers = [p["page_no"] for p in ordered]
        expected_numbers = list(range(1, len(ordered) + 1))
        if page_numbers != expected_numbers:
            raise MT940Error(
                f"account {account} statement {statement_no}: pages must run "
                f"continuously from 1, got {page_numbers}")
        currency = _check_statement(account, statement_no, ordered)
        statements.append({
            "account": account,
            "statement_no": statement_no,
            "currency": currency,
            "pages": ordered,
        })

    accounts = []
    for account in sorted({s["account"] for s in statements}):
        account_statements = [s for s in statements if s["account"] == account]
        currencies = sorted({s["currency"] for s in account_statements})
        accounts.append({
            "account": account,
            "currencies": currencies,
            "statements": sorted(account_statements,
                                 key=lambda s: s["statement_no"]),
        })
    return accounts


def _check_statement(account, statement_no, ordered):
    currency = None
    previous = None
    for expected_page, page in enumerate(ordered, start=1):
        opening = page.get("opening")
        closing = page.get("closing")
        if opening is None or closing is None:
            raise MT940Error(
                f"account {account} statement {statement_no} page "
                f"{page['page_no']}: opening and closing balances are required")
        if opening["currency"] != closing["currency"]:
            raise MT940Error(
                f"account {account} statement {statement_no} page "
                f"{page['page_no']}: opening/closing currency mismatch")
        if currency is None:
            currency = opening["currency"]
        elif opening["currency"] != currency:
            raise MT940Error(
                f"account {account} statement {statement_no}: currency drift "
                f"{currency} -> {opening['currency']}")

        last_page = len(ordered)
        required_open = "60F" if expected_page == 1 else "60M"
        required_close = "62F" if expected_page == last_page else "62M"
        if opening["tag"] != required_open:
            raise MT940Error(
                f"account {account} statement {statement_no} page "
                f"{expected_page}: opening must be :{required_open}:, got "
                f":{opening['tag']}:")
        if closing["tag"] != required_close:
            raise MT940Error(
                f"account {account} statement {statement_no} page "
                f"{expected_page}: closing must be :{required_close}:, got "
                f":{closing['tag']}:")

        for entry in page["transactions"]:
            if not (opening["date"] <= entry["date"] <= closing["date"]):
                raise MT940Error(
                    f"account {account} statement {statement_no} page "
                    f"{expected_page} line {entry['line']}: entry date "
                    f"{entry['date']} outside page span {opening['date']}.."
                    f"{closing['date']}")

        entry_sum = sum(e["signed_minor"]
                        for e in page["transactions"])
        if opening["signed_minor"] + entry_sum != closing["signed_minor"]:
            raise MT940Error(
                f"account {account} statement {statement_no} page "
                f"{expected_page}: imbalance: opening "
                f"{opening['signed_minor']} + entries {entry_sum} != closing "
                f"{closing['signed_minor']}")

        if previous is not None:
            if opening["date"] < previous["closing"]["date"]:
                raise MT940Error(
                    f"account {account} statement {statement_no} page "
                    f"{expected_page}: opening date {opening['date']} precedes "
                    f"previous page closing {previous['closing']['date']}")
            if opening["signed_minor"] != previous["closing"]["signed_minor"]:
                raise MT940Error(
                    f"account {account} statement {statement_no} page "
                    f"{expected_page}: opening balance "
                    f"{opening['signed_minor']} does not link to previous "
                    f"closing {previous['closing']['signed_minor']}")
        previous = page
    return currency


# --------------------------------------------------------------------------- #
# Explicit account/currency -> settlement mapping
# --------------------------------------------------------------------------- #
def load_mapping(raw_bytes, source="mapping"):
    """Parse the strict mapping JSON into a {(account, currency): settlement}."""
    try:
        raw = json.loads(bytes(raw_bytes).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MT940Error(f"{source}: invalid JSON: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != {"mappings"} or \
            not isinstance(raw["mappings"], list):
        raise MT940Error(
            f"{source}: mapping must be an object with exactly one 'mappings' "
            "list")
    mapping = {}
    for index, row in enumerate(raw["mappings"], start=1):
        if not isinstance(row, dict) or set(row) != \
                {"account", "currency", "settlement_id"}:
            raise MT940Error(
                f"{source}: mapping row {index} must have exactly account, "
                "currency, settlement_id")
        if any(not isinstance(v, str) or not v for v in row.values()):
            raise MT940Error(
                f"{source}: mapping row {index} fields must be nonempty strings")
        if row["currency"] not in SCALE:
            raise MT940Error(
                f"{source}: mapping row {index} uses unsupported currency "
                f"{row['currency']}")
        key = (row["account"], row["currency"])
        if key in mapping:
            raise MT940Error(
                f"{source}: duplicate mapping for {key[0]}/{key[1]}")
        mapping[key] = row["settlement_id"]
    if not mapping:
        raise MT940Error(f"{source}: mapping contains no rows")
    return mapping


# --------------------------------------------------------------------------- #
# Output building: settlement nets, legacy bank.json and audit payload
# --------------------------------------------------------------------------- #
def _statement_net(statement):
    pages = statement["pages"]
    total = 0
    count = 0
    for page in pages:
        total += sum(e["signed_minor"] for e in page["transactions"])
        count += len(page["transactions"])
    first_opening = pages[0]["opening"]["signed_minor"]
    last_closing = pages[-1]["closing"]["signed_minor"]
    if last_closing - first_opening != total:
        # Defensive: per-page checks already prove the identity.
        raise MT940Error(
            f"account {statement['account']} statement "
            f"{statement['statement_no']}: statement net does not match the "
            "balance movement")
    return total, count, first_opening, last_closing


def build_outputs(accounts, mapping, input_files, mapping_sha256):
    """Aggregate signed transaction nets and build both output payloads.

    Settlement amounts are sums of signed :61: entries, never page balances.
    Every account/currency in the statements must have an explicit mapping;
    unused mapping rows are rejected as well. Zero nets are retained.
    """
    required = set()
    for account in accounts:
        for statement in account["statements"]:
            required.add((account["account"], statement["currency"]))
    missing = sorted(required - set(mapping))
    if missing:
        raise MT940Error(
            "missing account/currency -> settlement mapping for: "
            + ", ".join(f"{a}/{c}" for a, c in missing))
    unused = sorted(set(mapping) - required)
    if unused:
        raise MT940Error(
            "unused mapping rows without any matching statement: "
            + ", ".join(f"{a}/{c}" for a, c in unused))

    nets = {}
    basis = {}
    audit_accounts = []
    total_pages = total_entries = 0
    for account in accounts:
        audit_statements = []
        account_net = 0
        for statement in account["statements"]:
            net, count, first_opening, last_closing = _statement_net(statement)
            account_net += net
            total_pages += len(statement["pages"])
            total_entries += count
            key = (account["account"], statement["currency"])
            settlement_id = mapping[key]
            net_key = (settlement_id, statement["currency"])
            nets[net_key] = nets.get(net_key, 0) + net
            basis.setdefault(net_key, []).append({
                "account": account["account"],
                "statement_no": statement["statement_no"],
                "pages": [p["page_no"] for p in statement["pages"]],
                "signed_minor": net,
            })
            audit_statements.append(_audit_statement(
                statement, net, count, first_opening, last_closing))
        audit_accounts.append({
            "account": account["account"],
            "currencies": account["currencies"],
            "statements": audit_statements,
            "account_net_signed_minor": account_net,
        })

    bank_rows = []
    settlement_nets = []
    zero_groups = 0
    for (settlement_id, currency) in sorted(nets):
        signed = nets[(settlement_id, currency)]
        if signed == 0:
            zero_groups += 1
        bank_rows.append({
            "settlement_id": settlement_id,
            "currency": currency,
            "amount": format_minor(signed, currency),
        })
        settlement_nets.append({
            "settlement_id": settlement_id,
            "currency": currency,
            "signed_minor": signed,
            "amount": format_minor(signed, currency),
            "aggregation": "sum of signed :61: entry minor units; page "
                           "balances are carried-forward checks, not amounts",
            "basis": sorted(basis[(settlement_id, currency)],
                            key=lambda b: (b["account"], b["statement_no"])),
        })

    audit = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "source": "synthetic_mt940_offline_collection",
        "inputs": input_files,
        "mapping": {
            "sha256": mapping_sha256,
            "rows": [
                {"account": account, "currency": currency,
                 "settlement_id": settlement_id}
                for (account, currency), settlement_id in sorted(mapping.items())
                if (account, currency) in required
            ],
        },
        "summary": {
            "accounts": len(accounts),
            "statements": sum(len(a["statements"]) for a in accounts),
            "pages": total_pages,
            "transactions": total_entries,
            "settlement_groups": len(bank_rows),
            "zero_net_groups": zero_groups,
        },
        "accounts": audit_accounts,
        "settlement_nets": settlement_nets,
    }
    return bank_rows, audit


def _audit_statement(statement, net, count, first_opening, last_closing):
    audit_pages = []
    tx_sequence = 0
    for index, page in enumerate(statement["pages"]):
        audit_entries = []
        page_sum = 0
        for entry in page["transactions"]:
            tx_sequence += 1
            page_sum += entry["signed_minor"]
            audit_entries.append({
                "tx_id": f"T{tx_sequence:04d}",
                "line": entry["line"],
                "source": page["source"],
                "date": entry["date"],
                "mark": entry["mark"],
                "dc": entry["dc"],
                "amount": entry["amount_text"],
                "signed_minor": entry["signed_minor"],
                "narrative": entry["narrative"],
                "narrative_lines": entry["narrative_lines"],
            })
        opening = page["opening"]
        closing = page["closing"]
        continuation = None
        if index > 0:
            previous = statement["pages"][index - 1]
            continuation = {
                "previous_page": previous["page_no"],
                "previous_closing_line": previous["closing"]["line"],
                "date_link_ok": opening["date"] >= previous["closing"]["date"],
                "balance_link_ok": opening["signed_minor"]
                == previous["closing"]["signed_minor"],
            }
        audit_pages.append({
            "page": page["page_no"],
            "source": page["source"],
            "opening": _audit_balance(opening, page["source"]),
            "closing": _audit_balance(closing, page["source"]),
            "raw_lines": page["raw_lines"],
            "transactions": audit_entries,
            "page_check": {
                "opening_signed_minor": opening["signed_minor"],
                "transaction_sum_signed_minor": page_sum,
                "closing_signed_minor": closing["signed_minor"],
                "balanced": opening["signed_minor"] + page_sum
                == closing["signed_minor"],
            },
            "continuation": continuation,
        })
    return {
        "statement_no": statement["statement_no"],
        "currency": statement["currency"],
        "pages": audit_pages,
        "statement_net": {
            "signed_minor": net,
            "transaction_count": count,
            "first_opening_signed_minor": first_opening,
            "last_closing_signed_minor": last_closing,
            "arithmetic": "last_closing - first_opening == "
                          "sum(signed :61: entries)",
        },
    }


def _audit_balance(balance, source):
    return {
        "tag": balance["tag"],
        "line": balance["line"],
        "source": source,
        "text": balance["text"],
        "date": balance["date"],
        "dc": balance["dc"],
        "currency": balance["currency"],
        "amount": balance["amount_text"],
        "signed_minor": balance["signed_minor"],
    }


# --------------------------------------------------------------------------- #
# Atomic collection and publication (no half batch)
# --------------------------------------------------------------------------- #
def collect(mt940_paths, mapping_path, output_dir):
    """Read all captures, validate the batch and publish both files atomically.

    All parsing and aggregation happens in memory first; only a fully valid
    batch reaches the disk. A new directory is created via a same-parent
    temporary directory and one atomic rename, so a failure never leaves a
    partial output directory and an existing directory is never overwritten.
    """
    mt940_paths = [Path(p) for p in mt940_paths]
    if not mt940_paths:
        raise MT940Error("at least one --mt940 input is required")
    # Captures are unordered; sort so identical file sets always yield
    # identical output bytes regardless of CLI argument order.
    mt940_paths = sorted(mt940_paths, key=lambda p: str(p))
    input_files = []
    pages = []
    for path in mt940_paths:
        raw = path.read_bytes()
        input_files.append({
            "name": path.name,
            "path": str(path),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
        pages.extend(parse_statements(raw, path.name))
    accounts = assemble(pages)

    mapping_path = Path(mapping_path)
    mapping_bytes = mapping_path.read_bytes()
    mapping = load_mapping(mapping_bytes, mapping_path.name)
    bank_rows, audit = build_outputs(
        accounts, mapping, input_files,
        hashlib.sha256(mapping_bytes).hexdigest())

    bank_text = json.dumps(bank_rows, indent=2) + "\n"
    audit_text = json.dumps(audit, indent=2, sort_keys=True) + "\n"
    _publish(output_dir, bank_text.encode("utf-8"),
             audit_text.encode("utf-8"))
    return audit


def _publish(output_dir, bank_bytes, audit_bytes):
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise MT940Error(
            f"output directory already exists, refusing to overwrite: "
            f"{output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix="." + output_dir.name + ".",
                                     dir=output_dir.parent))
    try:
        for name, payload in ((BANK_FILENAME, bank_bytes),
                              (AUDIT_FILENAME, audit_bytes)):
            target = temp_dir / name
            with target.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(temp_dir, output_dir)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser():
    parser = argparse.ArgumentParser(
        prog="settlement_audit.mt940",
        description="Offline strict-MT940 statement collection into legacy "
                    "bank.json plus a page/transaction audit JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser(
        "collect", help="validate paged MT940 captures and publish both files")
    collect_parser.add_argument("--mt940", nargs="+", required=True, type=Path,
                                metavar="FILE",
                                help="one or more captured MT940 text files; "
                                     "pages may be shuffled across files")
    collect_parser.add_argument("--mapping", required=True, type=Path,
                                help="strict JSON mapping account/currency to "
                                     "settlement_id")
    collect_parser.add_argument("--output", required=True, type=Path,
                                help="new directory receiving bank.json and "
                                     "mt940_audit.json; must not exist yet")
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            audit = collect(args.mt940, args.mapping, args.output)
            receipt = {
                "status": "collected",
                "output": str(args.output),
                "files": [BANK_FILENAME, AUDIT_FILENAME],
                "summary": audit["summary"],
            }
    except (MT940Error, OSError) as exc:
        parser.exit(2, f"mt940 {args.command} failed: {exc}\n")
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
