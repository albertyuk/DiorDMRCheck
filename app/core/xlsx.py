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


def to_date(v: Any) -> Optional[date]:
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
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y", "%d/%m/%Y",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def to_datetime(v: Any) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    d = to_date(v)
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
