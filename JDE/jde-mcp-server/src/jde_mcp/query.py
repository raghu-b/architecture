"""Builds AIS data-service queries and normalizes the responses.

Filters are expressed in business terms ({"open_amount": {"gt": 0}}) and
translated here into the AIS condition structure against real JDE columns. The
model never sees or supplies a column name, which keeps the blast radius of a
hallucination to "unknown field" rather than "queried the wrong table".
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .convert import date_to_julian, normalize_value, unscale_amount
from .semantic import BusinessObject, SemanticError

# Business-friendly operator -> AIS operator
OPERATORS = {
    "eq": "EQUAL",
    "ne": "NOT_EQUAL",
    "gt": "GREATER",
    "gte": "GREATER_EQUAL",
    "lt": "LESS",
    "lte": "LESS_EQUAL",
    "between": "BETWEEN",
    "contains": "CONTAINS",
    "starts_with": "STARTS_WITH",
    "in": "LIST",
}


def _encode(obj: BusinessObject, field_name: str, value: Any) -> str:
    """Convert a business value into the on-the-wire JDE representation."""
    f = obj.field(field_name)
    if f.type == "julian_date":
        if isinstance(value, str):
            value = date.fromisoformat(value)
        if isinstance(value, date):
            return str(date_to_julian(value))
        return str(int(value))
    if f.type == "currency":
        return str(unscale_amount(float(value), f.decimals))
    return str(value)


def build_query(
    obj: BusinessObject,
    filters: dict[str, dict[str, Any]] | None = None,
    limit: int = 100,
    fields: list[str] | None = None,
    match_all: bool = True,
) -> dict[str, Any]:
    """Produce an AIS ``/jderest/v2/dataservice`` payload.

    ``filters`` maps a business field name to a single-key operator dict, e.g.
    ``{"open_amount": {"gt": 0}, "company": {"eq": "00100"}}``.
    """
    filters = filters or {}

    if fields:
        for name in fields:
            obj.field(name)  # raises SemanticError with the valid list
        columns = [obj.column_for(n) for n in fields]
    else:
        columns = obj.all_columns

    conditions: list[dict[str, Any]] = []
    for field_name, spec in filters.items():
        if spec is None:
            continue
        if not isinstance(spec, dict) or len(spec) != 1:
            raise SemanticError(
                f"filter for '{field_name}' must be a single-operator dict, "
                f"e.g. {{'gt': 0}}. Valid operators: {', '.join(sorted(OPERATORS))}"
            )
        (op, value), = spec.items()
        if op not in OPERATORS:
            raise SemanticError(
                f"unknown operator '{op}' for '{field_name}'. "
                f"Valid: {', '.join(sorted(OPERATORS))}"
            )

        if op in ("between", "in"):
            if not isinstance(value, (list, tuple)):
                raise SemanticError(f"operator '{op}' needs a list of values")
            encoded = [{"content": _encode(obj, field_name, v),
                        "specialValueId": "LITERAL"} for v in value]
        else:
            encoded = [{"content": _encode(obj, field_name, value),
                        "specialValueId": "LITERAL"}]

        conditions.append({
            "controlId": obj.column_for(field_name),
            "operator": OPERATORS[op],
            "value": encoded,
        })

    payload: dict[str, Any] = {
        "targetName": obj.table,
        "targetType": "table",
        "dataServiceType": "BROWSE",
        "maxPageSize": str(int(limit)),
        "returnControlIDs": "|".join(columns),
        "query": {
            "autoFind": True,
            "matchType": "MATCH_ALL" if match_all else "MATCH_ANY",
            "condition": conditions,
        },
    }
    return payload


# --------------------------------------------------------------------------
# Response handling
# --------------------------------------------------------------------------

def extract_rowset(response: dict[str, Any], table: str) -> list[dict[str, Any]]:
    """Pull the rowset out of an AIS data-service response.

    AIS nests results under a generated key like ``fs_DATABROWSE_F0411``, so we
    look for the expected key first and fall back to scanning for any node that
    carries a gridData/rowset — AIS key naming has shifted between releases and
    hard-coding one shape makes the client brittle across upgrades.
    """
    candidates = [f"fs_DATABROWSE_{table}", f"ds_{table}", table]
    for key in candidates:
        node = response.get(key)
        if isinstance(node, dict):
            rows = _rowset_from_node(node)
            if rows is not None:
                return rows

    for value in response.values():
        if isinstance(value, dict):
            rows = _rowset_from_node(value)
            if rows is not None:
                return rows
    return []


def _rowset_from_node(node: dict[str, Any]) -> list[dict[str, Any]] | None:
    grid = (node.get("data") or {}).get("gridData")
    if isinstance(grid, dict) and isinstance(grid.get("rowset"), list):
        return grid["rowset"]
    if isinstance(node.get("rowset"), list):
        return node["rowset"]
    return None


def row_count_info(response: dict[str, Any], table: str) -> dict[str, Any]:
    """Summary metadata (records returned, whether more exist) when present."""
    for key in (f"fs_DATABROWSE_{table}", f"ds_{table}", table):
        node = response.get(key)
        if isinstance(node, dict):
            grid = (node.get("data") or {}).get("gridData") or {}
            summary = grid.get("summary")
            if isinstance(summary, dict):
                return {"records": summary.get("records"),
                        "more_records": summary.get("moreRecords")}
    return {}


def normalize_row(obj: BusinessObject, raw_row: dict[str, Any]) -> dict[str, Any]:
    """Turn one AIS grid row into a business object dict.

    AIS returns each cell as ``{"internalValue": ..., "value": "..."}`` keyed by
    ``TABLE_COLUMN``. We prefer internalValue: it is the unformatted number,
    which is what the converters expect. The display ``value`` has already had
    locale formatting applied and would round-trip badly.
    """
    out: dict[str, Any] = {}
    for f in obj.fields.values():
        cell = raw_row.get(f"{obj.table}_{f.column}")
        if cell is None:
            cell = raw_row.get(f.column)
        if isinstance(cell, dict):
            raw = cell.get("internalValue", cell.get("value"))
        else:
            raw = cell
        try:
            out[f.name] = normalize_value(raw, f.type, f.decimals)
        except Exception:
            # A single unparseable cell should degrade that field, not fail the
            # whole query — partial data with a null is more useful to the model
            # than an opaque error.
            out[f.name] = None
    return out


def normalize_rows(obj: BusinessObject,
                   raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_row(obj, r) for r in raw_rows]
