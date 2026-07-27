"""Conversion tests.

These are the highest-value tests in the package: a Julian date or implied
decimal handled wrong produces output that is confidently, plausibly incorrect,
which is far more dangerous in finance than an outright error.
"""

from datetime import date

import pytest

from jde_mcp.convert import (
    ConversionError,
    clean_string,
    date_to_julian,
    julian_to_date,
    normalize_value,
    scale_amount,
    unscale_amount,
)


class TestJulianDates:
    @pytest.mark.parametrize("julian,expected", [
        (125001, date(2025, 1, 1)),
        (125365, date(2025, 12, 31)),
        (124366, date(2024, 12, 31)),   # 2024 is a leap year
        (100001, date(2000, 1, 1)),
        (99001,  date(1999, 1, 1)),     # C=0 -> 1900s
        (95180,  date(1995, 6, 29)),
        (126208, date(2026, 7, 27)),
    ])
    def test_round_trip(self, julian, expected):
        assert julian_to_date(julian) == expected
        assert date_to_julian(expected) == julian

    def test_zero_is_no_date_not_epoch(self):
        # JDE uses 0 for "no date". Returning 1900-01-01 here would silently
        # invent a date that finance users would treat as real.
        assert julian_to_date(0) is None
        assert julian_to_date("") is None
        assert julian_to_date(None) is None

    def test_string_input_accepted(self):
        assert julian_to_date("125001") == date(2025, 1, 1)

    def test_invalid_day_of_year_rejected(self):
        with pytest.raises(ConversionError):
            julian_to_date(125400)

    def test_day_366_in_non_leap_year_rejected(self):
        with pytest.raises(ConversionError):
            julian_to_date(125366)

    def test_non_numeric_rejected(self):
        with pytest.raises(ConversionError):
            julian_to_date("not-a-date")

    def test_none_date_to_julian_is_zero(self):
        assert date_to_julian(None) == 0


class TestImpliedDecimals:
    @pytest.mark.parametrize("raw,decimals,expected", [
        (123456, 2, 1234.56),
        (100, 2, 1.00),
        (0, 2, 0.0),
        (-50000, 2, -500.00),
        (1234567, 4, 123.4567),
        (999, 0, 999.0),
    ])
    def test_scale(self, raw, decimals, expected):
        assert scale_amount(raw, decimals) == pytest.approx(expected)

    def test_round_trip(self):
        assert unscale_amount(scale_amount(123456, 2), 2) == 123456

    def test_none_and_blank(self):
        assert scale_amount(None) is None
        assert scale_amount("") is None
        assert unscale_amount(None) == 0

    def test_non_numeric_rejected(self):
        with pytest.raises(ConversionError):
            scale_amount("abc")


class TestNormalizeValue:
    def test_julian_returns_iso_string(self):
        assert normalize_value(125001, "julian_date") == "2025-01-01"

    def test_null_julian(self):
        assert normalize_value(0, "julian_date") is None

    def test_currency_scaled(self):
        assert normalize_value(123456, "currency", 2) == 1234.56

    def test_numeric_integers_stay_integers(self):
        assert normalize_value(1001.0, "numeric") == 1001
        assert isinstance(normalize_value(1001.0, "numeric"), int)

    def test_numeric_keeps_fractions(self):
        assert normalize_value(10.5, "numeric") == 10.5

    def test_string_padding_stripped(self):
        assert normalize_value("00100   ", "string") == "00100"

    def test_blank_string_becomes_none(self):
        assert normalize_value("     ", "string") is None

    def test_unknown_type_passes_through(self):
        assert normalize_value("raw", "mystery") == "raw"


def test_clean_string():
    assert clean_string("  ABC  ") == "ABC"
    assert clean_string("") is None
    assert clean_string(None) is None
