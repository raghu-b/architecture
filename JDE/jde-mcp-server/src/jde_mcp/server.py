"""MCP server exposing JD Edwards EnterpriseOne to Claude.

Design note on tool granularity: these are business-shaped tools
(``get_open_ap_vouchers``) rather than one generic ``run_query``. A generic SQL
passthrough gives the model more rope than it needs and gives your auditors
nothing reviewable. Narrow tools also let the docstrings carry the domain
context the model needs to choose correctly — the docstring IS the prompt.

Run with:      python -m jde_mcp.server
Or installed:  jde-mcp
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Literal

from fastmcp import FastMCP

from .ais import AISError, AISSession
from .audit import AuditLog
from .config import Settings
from .query import (
    build_query,
    extract_rowset,
    normalize_rows,
    row_count_info,
)
from .semantic import SemanticError, SemanticModel
from .writeback import (
    WritebackService,
    validate_ap_voucher,
    validate_journal_entry,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,  # stdout is the MCP transport — never log to it
)
log = logging.getLogger("jde_mcp")

SETTINGS = Settings.load()
MODEL = SemanticModel.load(SETTINGS.objects_file) if SETTINGS.objects_file.exists() else None
AUDIT = AuditLog(SETTINGS.state_db)
AIS = AISSession(SETTINGS)
WRITEBACK = WritebackService(AIS, MODEL, AUDIT, read_only=SETTINGS.read_only) if MODEL else None

mcp = FastMCP(
    "jde-finance",
    instructions=(
        "Read and act on live JD Edwards EnterpriseOne finance data. "
        "Amounts are returned in currency units and dates as ISO strings — "
        "conversions from JDE's internal encodings are already applied, do not "
        "re-scale or re-parse them. Start with list_business_objects and "
        "describe_business_object if you are unsure what data is available. "
        "Writes are staged as drafts for human approval and never post directly."
    ),
)


def _guard() -> None:
    if MODEL is None:
        raise RuntimeError(
            f"Semantic model not loaded. Expected {SETTINGS.objects_file}. "
            "Set JDE_OBJECTS_FILE or run from the package root."
        )


def _run_read(tool: str, object_name: str, filters: dict, limit: int,
              fields: list[str] | None = None) -> dict[str, Any]:
    """Shared read path: build, execute, normalize, audit."""
    _guard()
    obj = MODEL.object(object_name)
    capped = max(1, min(int(limit), SETTINGS.max_rows))

    payload = build_query(obj, filters=filters, limit=capped, fields=fields)
    response = AIS.data_service(payload)
    rows = normalize_rows(obj, extract_rowset(response, obj.table))
    info = row_count_info(response, obj.table)

    AUDIT.record(tool, "read", {"object": object_name, "filters": filters,
                                "limit": capped}, row_count=len(rows))

    result: dict[str, Any] = {"object": object_name, "row_count": len(rows),
                              "rows": rows}
    if info.get("more_records"):
        result["truncated"] = True
        result["note"] = (
            f"More rows exist beyond the {capped} returned. Narrow the filters "
            "rather than raising the limit — the server caps results at "
            f"{SETTINGS.max_rows}."
        )
    return result


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@mcp.tool()
def list_business_objects() -> dict[str, Any]:
    """List the JD Edwards business objects this server can query.

    Call this first when you do not know what data is available. Each entry
    maps a business concept (ap_voucher) to its underlying JDE table (F0411).
    """
    _guard()
    AUDIT.record("list_business_objects", "discovery")
    return {"objects": MODEL.catalog(),
            "read_only_mode": SETTINGS.read_only,
            "max_rows_per_call": SETTINGS.max_rows}


@mcp.tool()
def describe_business_object(object_name: str) -> dict[str, Any]:
    """Show the fields, types and JDE columns for one business object.

    Use this before constructing filters so you reference real field names.
    Field names here are the only ones any query tool will accept.
    """
    _guard()
    obj = MODEL.object(object_name)
    AUDIT.record("describe_business_object", "discovery", {"object": object_name})
    return obj.describe()


@mcp.tool()
def check_connection() -> dict[str, Any]:
    """Verify the AIS connection and report the server's safety settings.

    Useful as a first call to confirm credentials work before running a query
    that would otherwise fail with a confusing auth error.
    """
    try:
        info = AIS.health()
        status = "connected"
    except AISError as exc:
        info, status = {"error": str(exc)}, "unavailable"
    AUDIT.record("check_connection", "health", status=status)
    return {"status": status, **info,
            "read_only_mode": SETTINGS.read_only,
            "max_rows_per_call": SETTINGS.max_rows,
            "objects_loaded": MODEL.object_names if MODEL else []}


# ---------------------------------------------------------------------------
# Accounts Payable
# ---------------------------------------------------------------------------

@mcp.tool()
def get_open_ap_vouchers(
    supplier_number: int | None = None,
    company: str | None = None,
    business_unit: str | None = None,
    min_open_amount: float = 0.01,
    due_before: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Open (unpaid) AP vouchers from the JDE AP ledger, F0411.

    One row per voucher pay item, so a voucher with several pay items appears
    several times sharing a document_number — sum by document_number for
    voucher-level totals.

    Args:
        supplier_number: Address book number of the supplier.
        company: Document company, e.g. "00100".
        business_unit: Business unit / cost centre.
        min_open_amount: Minimum open amount; defaults to 0.01 so fully paid
            vouchers are excluded.
        due_before: ISO date (YYYY-MM-DD) — only vouchers due before this.
        limit: Maximum rows to return.
    """
    filters: dict[str, dict[str, Any]] = {"open_amount": {"gte": min_open_amount}}
    if supplier_number is not None:
        filters["supplier_number"] = {"eq": supplier_number}
    if company:
        filters["company"] = {"eq": company}
    if business_unit:
        filters["business_unit"] = {"eq": business_unit}
    if due_before:
        filters["due_date"] = {"lt": due_before}
    return _run_read("get_open_ap_vouchers", "ap_voucher", filters, limit)


@mcp.tool()
def get_ap_aging_summary(company: str | None = None,
                         as_of: str | None = None,
                         limit: int = 500) -> dict[str, Any]:
    """Aged AP payables bucketed as current / 1-30 / 31-60 / 61-90 / 90+ days.

    Aging is computed here from due_date rather than in JDE, so the buckets are
    consistent regardless of how any particular JDE report is configured. Use
    for payables position and cash planning questions.

    Args:
        company: Restrict to one document company.
        as_of: ISO date to age against; defaults to today.
    """
    from datetime import date as _date

    filters: dict[str, dict[str, Any]] = {"open_amount": {"gte": 0.01}}
    if company:
        filters["company"] = {"eq": company}

    data = _run_read("get_ap_aging_summary", "ap_voucher", filters, limit)
    ref = _date.fromisoformat(as_of) if as_of else _date.today()

    buckets = {"current": 0.0, "1_30": 0.0, "31_60": 0.0,
               "61_90": 0.0, "over_90": 0.0}
    counts = {k: 0 for k in buckets}

    for row in data["rows"]:
        amount = row.get("open_amount") or 0.0
        due = row.get("due_date")
        if not due:
            key = "current"
        else:
            days = (ref - _date.fromisoformat(due)).days
            key = ("current" if days <= 0 else
                   "1_30" if days <= 30 else
                   "31_60" if days <= 60 else
                   "61_90" if days <= 90 else "over_90")
        buckets[key] += amount
        counts[key] += 1

    return {
        "as_of": ref.isoformat(),
        "company": company,
        "buckets": {k: round(v, 2) for k, v in buckets.items()},
        "voucher_counts": counts,
        "total_open": round(sum(buckets.values()), 2),
        "rows_analyzed": data["row_count"],
        "truncated": data.get("truncated", False),
    }


# ---------------------------------------------------------------------------
# Accounts Receivable
# ---------------------------------------------------------------------------

@mcp.tool()
def get_open_ar_invoices(
    customer_number: int | None = None,
    company: str | None = None,
    min_open_amount: float = 0.01,
    due_before: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Open (uncollected) AR invoices from the JDE AR ledger, F03B11.

    Args:
        customer_number: Address book number of the customer.
        company: Document company.
        min_open_amount: Minimum open amount; excludes fully collected invoices.
        due_before: ISO date — only invoices due before this, for collections.
        limit: Maximum rows to return.
    """
    filters: dict[str, dict[str, Any]] = {"open_amount": {"gte": min_open_amount}}
    if customer_number is not None:
        filters["customer_number"] = {"eq": customer_number}
    if company:
        filters["company"] = {"eq": company}
    if due_before:
        filters["due_date"] = {"lt": due_before}
    return _run_read("get_open_ar_invoices", "ar_invoice", filters, limit)


# ---------------------------------------------------------------------------
# General Ledger
# ---------------------------------------------------------------------------

@mcp.tool()
def get_gl_transactions(
    business_unit: str | None = None,
    object_account: str | None = None,
    company: str | None = None,
    gl_date_from: str | None = None,
    gl_date_to: str | None = None,
    ledger_type: str = "AA",
    posted_only: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    """General ledger detail lines from F0911.

    Debits are positive and credits negative, so a balanced batch sums to zero.
    Defaults to ledger type AA (actual amounts) and posted entries only —
    including unposted rows will not tie to any published balance.

    Args:
        business_unit: Business unit / cost centre.
        object_account: Object account, e.g. "1110".
        company: Document company.
        gl_date_from: ISO start date, inclusive.
        gl_date_to: ISO end date, inclusive.
        ledger_type: "AA" actual, "BA" budget. Defaults to AA.
        posted_only: Exclude unposted entries. Keep true for reporting.
        limit: Maximum rows to return.
    """
    filters: dict[str, dict[str, Any]] = {"ledger_type": {"eq": ledger_type}}
    if business_unit:
        filters["business_unit"] = {"eq": business_unit}
    if object_account:
        filters["object_account"] = {"eq": object_account}
    if company:
        filters["company"] = {"eq": company}
    if posted_only:
        filters["posted_code"] = {"eq": "P"}
    if gl_date_from and gl_date_to:
        filters["gl_date"] = {"between": [gl_date_from, gl_date_to]}
    elif gl_date_from:
        filters["gl_date"] = {"gte": gl_date_from}
    elif gl_date_to:
        filters["gl_date"] = {"lte": gl_date_to}
    return _run_read("get_gl_transactions", "gl_transaction", filters, limit)


@mcp.tool()
def get_account_balance(
    business_unit: str,
    object_account: str,
    gl_date_from: str,
    gl_date_to: str,
    ledger_type: str = "AA",
    limit: int = 500,
) -> dict[str, Any]:
    """Net movement on one account over a date range, from posted F0911 lines.

    Returns net, total debits and total credits so the composition is visible
    rather than just a single figure. If `truncated` is true the total is
    incomplete — narrow the date range instead of trusting it.
    """
    data = get_gl_transactions(
        business_unit=business_unit, object_account=object_account,
        gl_date_from=gl_date_from, gl_date_to=gl_date_to,
        ledger_type=ledger_type, posted_only=True, limit=limit,
    )
    amounts = [r.get("amount") or 0.0 for r in data["rows"]]
    return {
        "business_unit": business_unit,
        "object_account": object_account,
        "period": {"from": gl_date_from, "to": gl_date_to},
        "ledger_type": ledger_type,
        "net_amount": round(sum(amounts), 2),
        "total_debits": round(sum(a for a in amounts if a > 0), 2),
        "total_credits": round(sum(a for a in amounts if a < 0), 2),
        "line_count": len(amounts),
        "truncated": data.get("truncated", False),
    }


# ---------------------------------------------------------------------------
# Procurement / master data
# ---------------------------------------------------------------------------

@mcp.tool()
def get_purchase_orders(
    order_number: int | None = None,
    supplier_number: int | None = None,
    business_unit: str | None = None,
    min_extended_cost: float | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Purchase order detail lines from F4311, for three-way matching.

    Compare quantity_ordered against quantity_received, and extended_cost
    against the AP voucher gross_amount, to identify match exceptions.
    """
    filters: dict[str, dict[str, Any]] = {}
    if order_number is not None:
        filters["order_number"] = {"eq": order_number}
    if supplier_number is not None:
        filters["supplier_number"] = {"eq": supplier_number}
    if business_unit:
        filters["business_unit"] = {"eq": business_unit}
    if min_extended_cost is not None:
        filters["extended_cost"] = {"gte": min_extended_cost}
    return _run_read("get_purchase_orders", "purchase_order", filters, limit)


@mcp.tool()
def lookup_address_book(
    address_number: int | None = None,
    name_contains: str | None = None,
    search_type: Literal["V", "C", "E", "any"] = "any",
    limit: int = 25,
) -> dict[str, Any]:
    """Resolve supplier/customer numbers to names via Address Book (F0101).

    Use this to turn a number into a name, or a partial name into a number,
    rather than inferring identity from a document remark.

    Args:
        address_number: Exact address book number.
        name_contains: Partial alpha name match.
        search_type: "V" supplier, "C" customer, "E" employee, "any" no filter.
        limit: Maximum rows.
    """
    filters: dict[str, dict[str, Any]] = {}
    if address_number is not None:
        filters["address_number"] = {"eq": address_number}
    if name_contains:
        filters["name"] = {"contains": name_contains}
    if search_type != "any":
        filters["search_type"] = {"eq": search_type}
    return _run_read("lookup_address_book", "address_book", filters, limit)


# ---------------------------------------------------------------------------
# Write-back (staged for human approval — never posts directly)
# ---------------------------------------------------------------------------

@mcp.tool()
def draft_journal_entry(company: str, gl_date: str, lines: list[dict],
                        explanation: str) -> dict[str, Any]:
    """Validate and stage a journal entry for human approval. Does NOT post.

    The entry must balance: debits positive, credits negative, summing to zero.
    On success you get a draft_id; a person then approves it, at which point it
    posts through a JDE business function with all normal validations applied.

    Args:
        company: Document company, e.g. "00100".
        gl_date: ISO date (YYYY-MM-DD) for the G/L date.
        lines: List of {"account": "1.1110.BEAR", "amount": 1500.00,
            "business_unit": "1", "explanation": "..."} — amount positive for
            debit, negative for credit.
        explanation: Batch-level explanation of why this entry exists.
    """
    if WRITEBACK is None:
        return {"status": "unavailable", "reason": "semantic model not loaded"}
    errors = validate_journal_entry(company, gl_date, lines)
    payload = {"company": company, "gl_date": gl_date, "lines": lines,
               "explanation": explanation}
    return WRITEBACK.stage("journal_entry", payload, explanation, errors)


@mcp.tool()
def draft_ap_voucher(supplier_number: int, company: str, gl_date: str,
                     gross_amount: float, invoice_number: str,
                     explanation: str,
                     business_unit: str | None = None,
                     due_date: str | None = None) -> dict[str, Any]:
    """Validate and stage an AP voucher for human approval. Does NOT post.

    Args:
        supplier_number: Supplier address book number.
        company: Document company.
        gl_date: ISO G/L date.
        gross_amount: Voucher gross amount in currency units.
        invoice_number: Supplier's invoice reference.
        explanation: Why this voucher is being created.
        business_unit: Optional business unit.
        due_date: Optional ISO due date; JDE derives from payment terms if omitted.
    """
    if WRITEBACK is None:
        return {"status": "unavailable", "reason": "semantic model not loaded"}
    errors = validate_ap_voucher(supplier_number, company, gl_date, gross_amount)
    payload = {"supplier_number": supplier_number, "company": company,
               "gl_date": gl_date, "gross_amount": gross_amount,
               "invoice_number": invoice_number,
               "business_unit": business_unit, "due_date": due_date}
    return WRITEBACK.stage("ap_voucher", payload, explanation, errors)


@mcp.tool()
def list_pending_drafts(limit: int = 25) -> dict[str, Any]:
    """Show write drafts awaiting human approval.

    Lets you confirm what you have staged and report status back to the user.
    You cannot approve these yourself — approval happens outside this server.
    """
    drafts = AUDIT.list_drafts(limit=limit)
    return {"pending_count": len(drafts), "drafts": drafts,
            "note": "Approve with: python -m jde_mcp.approve <draft_id> --by <name>"}


@mcp.tool()
def get_audit_trail(limit: int = 25) -> dict[str, Any]:
    """Recent tool activity from this server's independent audit log.

    Answers "what has the agent looked at", which JDE's own transaction log
    cannot, since it does not record reads.
    """
    return {"entries": AUDIT.recent(limit)}


# ---------------------------------------------------------------------------

def main() -> None:
    problems = SETTINGS.validate()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        log.error("Fix the above in your .env, then restart. See .env.example.")
        # Still start: discovery tools work, and check_connection reports the
        # problem to the user in-chat rather than failing invisibly at launch.

    log.info("jde-finance MCP server starting (read_only=%s, max_rows=%s)",
             SETTINGS.read_only, SETTINGS.max_rows)
    try:
        mcp.run()
    finally:
        AIS.close()
        AUDIT.close()


if __name__ == "__main__":
    main()
