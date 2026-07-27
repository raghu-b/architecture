"""Semantic model tests.

The model is what stops a field-name hallucination from becoming a query
against the wrong column, so the error paths matter as much as the happy path:
an unknown field must fail loudly, with the valid options listed.
"""

from pathlib import Path

import pytest

from jde_mcp.semantic import SemanticError, SemanticModel

OBJECTS = Path(__file__).resolve().parent.parent / "config" / "objects.yaml"


@pytest.fixture(scope="module")
def model() -> SemanticModel:
    return SemanticModel.load(OBJECTS)


def test_loads_expected_objects(model):
    for name in ["ap_voucher", "ar_invoice", "gl_transaction",
                 "purchase_order", "address_book"]:
        assert name in model.object_names


def test_table_mapping(model):
    assert model.object("ap_voucher").table == "F0411"
    assert model.object("ar_invoice").table == "F03B11"
    assert model.object("gl_transaction").table == "F0911"


def test_field_to_column(model):
    ap = model.object("ap_voucher")
    assert ap.column_for("open_amount") == "RPAAP"
    assert ap.column_for("due_date") == "RPDDJ"
    assert ap.column_for("supplier_number") == "RPAN8"


def test_field_types_declared(model):
    ap = model.object("ap_voucher")
    assert ap.field("due_date").type == "julian_date"
    assert ap.field("gross_amount").type == "currency"
    assert ap.field("company").type == "string"


def test_unknown_object_lists_alternatives(model):
    with pytest.raises(SemanticError) as exc:
        model.object("not_a_thing")
    assert "ap_voucher" in str(exc.value)


def test_unknown_field_lists_alternatives(model):
    with pytest.raises(SemanticError) as exc:
        model.object("ap_voucher").field("totally_made_up")
    assert "open_amount" in str(exc.value)


def test_custom_decimals_respected(model):
    # Unit cost carries 4 implied decimals, not the default 2.
    assert model.object("purchase_order").field("unit_cost").decimals == 4


def test_writeback_routes_configured(model):
    route = model.write_target("journal_entry")
    assert route.orchestration
    assert "company" in route.required_fields


def test_unknown_writeback_target_refuses(model):
    with pytest.raises(SemanticError):
        model.write_target("delete_everything")


def test_catalog_is_compact(model):
    catalog = model.catalog()
    assert len(catalog) == len(model.object_names)
    assert all({"object", "jde_table", "field_count"} <= set(e) for e in catalog)


def test_describe_includes_columns(model):
    described = model.object("gl_transaction").describe()
    assert described["jde_table"] == "F0911"
    names = {f["name"] for f in described["fields"]}
    assert {"amount", "gl_date", "posted_code"} <= names
