"""JDE encoding conversions.

Two JDE storage conventions will silently corrupt your results if you skip
them, so they are handled here once and applied to every row that leaves the
server:

1. Julian dates. JDE stores dates as CYYDDD, where C is a century offset
   (0 = 1900s, 1 = 2000s), YY is the year within the century, DDD is the day
   of year. 125001 is 2025-01-01, not "125001" and not 1970-05-05.

2. Implied decimals. Numeric amount fields are stored as integers scaled by
   the currency's decimal places. A voucher of $1,234.56 is stored as 123456.
   Handing the raw integer to a model produces answers that are wrong by two
   orders of magnitude and look entirely plausible.
"""

from __future__ import annotations

from datetime import date, timedelta


class ConversionError(ValueError):
    """Raised when a value cannot be interpreted in the declared JDE format."""


# --------------------------------------------------------------------------
# Julian dates
# --------------------------------------------------------------------------

def julian_to_date(value: int | str | None) -> date | None:
    """Convert a JDE Julian date (CYYDDD) to a ``datetime.date``.

    Returns None for the JDE 'no date' sentinel (0 / blank), which is common
    in optional date columns and must not be reported as 1900-01-01.
    """
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ConversionError(f"not a Julian date: {value!r}") from exc

    if n == 0:
        return None
    if n < 0 or n > 999999:
        raise ConversionError(f"Julian date out of range: {value!r}")

    century_offset, remainder = divmod(n, 100000)
    year_in_century, day_of_year = divmod(remainder, 1000)

    if day_of_year < 1 or day_of_year > 366:
        raise ConversionError(f"invalid day-of-year in Julian date: {value!r}")

    year = 1900 + century_offset * 100 + year_in_century

    # Guard the year boundary explicitly. Adding a timedelta would happily roll
    # day 366 of a non-leap year into 1 January of the next year — a silently
    # wrong date, which is the failure mode this whole module exists to prevent.
    days_in_year = 366 if _is_leap(year) else 365
    if day_of_year > days_in_year:
        raise ConversionError(
            f"day {day_of_year} does not exist in {year}: {value!r}"
        )

    return date(year, 1, 1) + timedelta(days=day_of_year - 1)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def date_to_julian(value: date | None) -> int:
    """Convert a ``datetime.date`` back to JDE Julian (CYYDDD).

    Needed whenever we push a date into a query condition or a write payload —
    JDE will not accept an ISO string in a date column.
    """
    if value is None:
        return 0
    century_offset = (value.year - 1900) // 100
    year_in_century = (value.year - 1900) % 100
    day_of_year = value.timetuple().tm_yday
    return century_offset * 100000 + year_in_century * 1000 + day_of_year


# --------------------------------------------------------------------------
# Implied decimals
# --------------------------------------------------------------------------

def scale_amount(raw: int | float | str | None, decimals: int = 2) -> float | None:
    """Apply implied decimal places to a raw JDE numeric value."""
    if raw is None or raw == "":
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConversionError(f"not numeric: {raw!r}") from exc
    return round(n / (10 ** decimals), decimals)


def unscale_amount(value: float | None, decimals: int = 2) -> int:
    """Inverse of :func:`scale_amount`, for building write payloads."""
    if value is None:
        return 0
    return int(round(value * (10 ** decimals)))


# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------

def clean_string(value) -> str | None:
    """JDE pads fixed-width character columns with spaces; strip them."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def normalize_value(raw, field_type: str, decimals: int = 2):
    """Dispatch a raw column value through the right converter.

    ``field_type`` comes from the semantic model (config/objects.yaml), which
    is the single place that knows what each JDE column actually means.
    """
    if field_type == "julian_date":
        d = julian_to_date(raw)
        return d.isoformat() if d else None
    if field_type == "currency":
        return scale_amount(raw, decimals)
    if field_type == "numeric":
        if raw is None or raw == "":
            return None
        try:
            f = float(raw)
        except (TypeError, ValueError):
            return None
        return int(f) if f.is_integer() else f
    if field_type == "string":
        return clean_string(raw)
    return raw
