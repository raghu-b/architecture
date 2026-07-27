"""Query builder and response normalizer tests.

Uses a canned AIS response rather than a live server so the whole suite runs
offline. The response shape here mirrors what AIS actually returns:
``fs_DATABROWSE_<TABLE>`` -> data -> gridData -> rowset, with cells keyed
``TABLE_COLUMN`` and carrying both internalValue and a formatted value.
"""

from pathlib import Path

import pytest

from jde_mcp.query import (
    build_query,
    extract_rowset,
    normalize_row,
    normalize_rows,
    row_count_info,
)
from jde_mcp.semantic import SemanticError, SemanticModel

OBJECTS = Path(__file__).resolve().parent.parent / "config" / "objects.yaml"


@pytest.fixture(scope="module")
def model():
    return SemanticModel.load(OBJECTS)


@pytest.fixture
def ap(model):
    return model.object("ap_voucher")


# --------------------------------------------------------------------------
# Query building
# --------------------------------------------------------------------------

class TestBuildQuery:
    def test_targets_the_right_table(self, ap):
        q = build_query(ap, limit=10)
        assert q["targetName"] == "F0411"
        assert q["targetType"] == "table"
        assert q["maxPageSize"] == "10"

    def test_business_names_become_jde_columns(self, ap):
        q = build_query(ap, filters={"supplier_number": {"eq": 4242}})
        cond = q["query"]["condition"][0]
        assert cond["controlId"] == "RPAN8"
        assert cond["operator"] == "EQUAL"
        assert cond["value"][0]["content"] == "4242"

    def test_currency_filter_is_unscaled(self, ap):
        # 1234.56 in business terms must go to JDE as 123456.
        q = build_query(ap, filters={"open_amount": {"gt": 1234.56}})
        assert q["query"]["condition"][0]["value"][0]["content"] == "123456"

    def test_iso_date_filter_becomes_julian(self, ap):
        q = build_query(ap, filters={"due_date": {"lt": "2025-01-01"}})
        assert q["query"]["condition"][0]["value"][0]["content"] == "125001"

    def test_between_takes_a_list(self, ap):
        q = build_query(ap, filters={"due_date": {"between": ["2025-01-01",
                                                              "2025-12-31"]}})
        cond = q["query"]["condition"][0]
        assert cond["operator"] == "BETWEEN"
        assert [v["content"] for v in cond["value"]] == ["125001", "125365"]

    def test_multiple_filters_match_all(self, ap):
        q = build_query(ap, filters={"company": {"eq": "00100"},
                                     "open_amount": {"gte": 0.01}})
        assert len(q["query"]["condition"]) == 2
        assert q["query"]["matchType"] == "MATCH_ALL"

    def test_field_projection(self, ap):
        q = build_query(ap, fields=["supplier_number", "open_amount"])
        assert q["returnControlIDs"] == "RPAN8|RPAAP"

    def test_unknown_field_rejected(self, ap):
        with pytest.raises(SemanticError):
            build_query(ap, filters={"nonexistent": {"eq": 1}})

    def test_unknown_operator_rejected(self, ap):
        with pytest.raises(SemanticError) as exc:
            build_query(ap, filters={"open_amount": {"approximately": 100}})
        assert "Valid" in str(exc.value)

    def test_malformed_filter_rejected(self, ap):
        with pytest.raises(SemanticError):
            build_query(ap, filters={"open_amount": {"gt": 1, "lt": 2}})

    def test_empty_filters_produce_no_conditions(self, ap):
        q = build_query(ap, filters={})
        assert q["query"]["condition"] == []


# --------------------------------------------------------------------------
# Response normalization
# --------------------------------------------------------------------------

AIS_RESPONSE = {
    "fs_DATABROWSE_F0411": {
        "title": "Data Browser - F0411",
        "data": {
            "gridData": {
                "id": 0,
                "rowset": [
                    {
                        "F0411_RPAN8":  {"internalValue": 4242, "value": "4242"},
                        "F0411_RPDOC":  {"internalValue": 778812, "value": "778812"},
                        "F0411_RPDCT":  {"internalValue": "PV", "value": "PV"},
                        "F0411_RPKCO":  {"internalValue": "00100 ", "value": "00100"},
                        "F0411_RPAG":   {"internalValue": 123456, "value": "1,234.56"},
                        "F0411_RPAAP":  {"internalValue": 123456, "value": "1,234.56"},
                        "F0411_RPDDJ":  {"internalValue": 125001, "value": "01/01/2025"},
                        "F0411_RPDIVJ": {"internalValue": 0, "value": ""},
                        "F0411_RPPST":  {"internalValue": "A", "value": "A"},
                    },
                    {
                        "F0411_RPAN8":  {"internalValue": 5150, "value": "5150"},
                        "F0411_RPDOC":  {"internalValue": 778813, "value": "778813"},
                        "F0411_RPAG":   {"internalValue": -50000, "value": "-500.00"},
                        "F0411_RPAAP":  {"internalValue": -50000, "value": "-500.00"},
                        "F0411_RPDDJ":  {"internalValue": 125180, "value": "06/29/2025"},
                    },
                ],
                "summary": {"records": 2, "moreRecords": False},
            }
        },
    }
}


class TestNormalization:
    def test_extract_rowset(self):
        rows = extract_rowset(AIS_RESPONSE, "F0411")
        assert len(rows) == 2

    def test_extract_falls_back_on_unexpected_key(self):
        # AIS key naming has shifted across releases; the fallback scan keeps
        # the client working after an upgrade instead of silently returning [].
        weird = {"someOtherKey": AIS_RESPONSE["fs_DATABROWSE_F0411"]}
        assert len(extract_rowset(weird, "F0411")) == 2

    def test_extract_missing_returns_empty(self):
        assert extract_rowset({}, "F0411") == []

    def test_row_count_info(self):
        info = row_count_info(AIS_RESPONSE, "F0411")
        assert info["records"] == 2
        assert info["more_records"] is False

    def test_amounts_scaled(self, ap):
        row = normalize_row(ap, extract_rowset(AIS_RESPONSE, "F0411")[0])
        assert row["gross_amount"] == 1234.56
        assert row["open_amount"] == 1234.56

    def test_negative_amounts(self, ap):
        row = normalize_row(ap, extract_rowset(AIS_RESPONSE, "F0411")[1])
        assert row["open_amount"] == -500.00

    def test_dates_iso(self, ap):
        row = normalize_row(ap, extract_rowset(AIS_RESPONSE, "F0411")[0])
        assert row["due_date"] == "2025-01-01"

    def test_zero_date_is_none_not_epoch(self, ap):
        row = normalize_row(ap, extract_rowset(AIS_RESPONSE, "F0411")[0])
        assert row["invoice_date"] is None

    def test_strings_stripped(self, ap):
        row = normalize_row(ap, extract_rowset(AIS_RESPONSE, "F0411")[0])
        assert row["company"] == "00100"

    def test_missing_columns_become_none_not_errors(self, ap):
        # Row 2 omits several columns; a partial row is more useful to the
        # model than a failed call.
        row = normalize_row(ap, extract_rowset(AIS_RESPONSE, "F0411")[1])
        assert row["pay_status"] is None
        assert row["supplier_number"] == 5150

    def test_every_declared_field_present(self, ap):
        row = normalize_row(ap, extract_rowset(AIS_RESPONSE, "F0411")[0])
        assert set(row) == set(ap.fields)

    def test_normalize_rows_batch(self, ap):
        rows = normalize_rows(ap, extract_rowset(AIS_RESPONSE, "F0411"))
        assert [r["supplier_number"] for r in rows] == [4242, 5150]
