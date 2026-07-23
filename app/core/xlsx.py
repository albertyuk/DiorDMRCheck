"""Shared Excel-reading utilities.

Generic cell coercion and header-fingerprint discovery used by every workbook
consumer (reconciler parsers, efficiency analysis, export writer, header
mapper, eval harness). These were historically underscore-privates of the
PLOG/DMR parser module that three other modules imported anyway — they are
public API and live here now.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .textnorm import header_key, nfkc

HEADER_SCAN_ROWS = 15  # how deep to look for the header fingerprint
HEADER_SCAN_COLS = 256  # bound sparse XFD-column header expansion
# Styled-but-empty cells make openpyxl's max_row huge; stop scanning data after
# this many blank rows in a row instead of walking phantom rows for minutes.
MAX_CONSECUTIVE_BLANK_ROWS = 200


def cell_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


_DATE_ORDERS = frozenset({"auto", "year-first", "month-first", "day-first"})
_NUMERIC_DATE_RE = re.compile(
    r"^(\d{1,4})([./-])(\d{1,2})\2(\d{1,4})$"
)


def _two_digit_year(value: int) -> int:
    """Use Python's documented %y pivot while keeping construction explicit."""
    return datetime.strptime(f"{value:02d}", "%y").year


def _build_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def to_date(v: Any, *, date_order: str = "auto") -> Optional[date]:
    """Coerce an Excel/text value to a date.

    ``date_order`` makes ambiguous numeric text an explicit caller policy.
    ``auto`` preserves the historical month-first interpretation when all
    components are ambiguous, but recognizes an impossible month in the first
    position as a two-digit year (``24/11/27`` → 2024-11-27). Separators never
    change the interpretation.
    """
    if date_order not in _DATE_ORDERS:
        raise ValueError(
            f"date_order must be one of {sorted(_DATE_ORDERS)!r}"
        )
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):
        # Excel serial date (1900 system)
        try:
            return (datetime(1899, 12, 30) + timedelta(days=float(v))).date()
        except (OverflowError, ValueError):
            return None
    s = nfkc(str(v)).strip()
    # Timestamp text is unambiguous because the year has four digits.
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    match = _NUMERIC_DATE_RE.fullmatch(s)
    if not match:
        return None
    first, middle, last = (
        int(match.group(1)),
        int(match.group(3)),
        int(match.group(4)),
    )
    first_width = len(match.group(1))
    last_width = len(match.group(4))

    if first_width == 4:
        return _build_date(first, middle, last)
    if last_width == 4:
        if date_order == "day-first":
            return _build_date(last, middle, first)
        if date_order == "month-first":
            return _build_date(last, first, middle)
        # Auto-disambiguate impossible months, then retain the historical
        # month-first reading when both leading fields are 1..12.
        if first > 12 and middle <= 12:
            return _build_date(last, middle, first)
        return _build_date(last, first, middle)
    if first_width <= 2 and last_width <= 2:
        year = _two_digit_year(last)
        if date_order == "year-first":
            return _build_date(_two_digit_year(first), middle, last)
        if date_order == "day-first":
            return _build_date(year, middle, first)
        if date_order == "month-first":
            return _build_date(year, first, middle)
        if first > 12 and middle <= 12:
            return _build_date(_two_digit_year(first), middle, last)
        return _build_date(year, first, middle)
    return None


def to_datetime(v: Any, *, date_order: str = "auto") -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    d = to_date(v, date_order=date_order)
    return datetime(d.year, d.month, d.day) if d else None


def to_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if math.isfinite(float(v)) else None
    # NFKC first so full-width digits/commas from Chinese-locale exports parse.
    s = nfkc(str(v)).strip().replace(",", "").replace("，", "")
    try:
        parsed = float(s)
        return int(parsed) if math.isfinite(parsed) else None
    except (OverflowError, ValueError):
        return None


def to_float(v: Any) -> Optional[float]:
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        parsed = float(v)
        return parsed if math.isfinite(parsed) else None
    s = nfkc(str(v)).strip().replace(",", "").replace("，", "")
    try:
        parsed = float(s)
        return parsed if math.isfinite(parsed) else None
    except ValueError:
        return None


def find_header_row(ws, required: set[str]) -> Optional[tuple[int, dict[str, int]]]:
    """Return (row_index, {header_key: column_index}) for the first row whose
    normalized cell values contain every key in *required*.

    Normal worksheets expose their already-loaded sparse cell dictionary. Use
    it instead of ``iter_rows`` so a single styled cell at XFD1 cannot cause
    openpyxl to materialize 245,760 empty header cells. Read-only worksheets
    do not retain that dictionary, so their streaming fallback is explicitly
    column-bounded.
    """
    loaded_cells = getattr(ws, "_cells", None)
    for row_index in range(1, HEADER_SCAN_ROWS + 1):
        keys: dict[str, int] = {}
        if isinstance(loaded_cells, dict):
            cells = (
                cell for (row, column), cell in loaded_cells.items()
                if row == row_index and column <= HEADER_SCAN_COLS
            )
        else:
            cells = next(ws.iter_rows(
                min_row=row_index,
                max_row=row_index,
                max_col=HEADER_SCAN_COLS,
            ), ())
        for cell in cells:
            k = header_key(cell_str(cell.value))
            if k and k not in keys:
                keys[k] = cell.column
        if required.issubset(keys.keys()):
            return row_index, keys
    return None


def extract_link_target(cell) -> str:
    """Hyperlink target of a cell, else its text if it looks like a URL.

    NOTE: this is the strict (DMR) variant — plain text that doesn't start
    with http is discarded. The PLOG/efficiency parsers deliberately accept
    any cell text as a link; that divergence predates this module.
    """
    if cell is None:
        return ""
    if cell.hyperlink and cell.hyperlink.target:
        return str(cell.hyperlink.target).strip()
    v = cell_str(cell.value)
    return v if v.lower().startswith("http") else ""
