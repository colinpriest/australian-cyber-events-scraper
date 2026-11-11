"""Write tables a dashboard shows to an Excel workbook, within Excel's limits.

Used for the workbooks published beside a dashboard page (``entity_events_detail.xlsx``,
``dedup.xlsx``): each sheet has a bold, frozen header row and an autofilter over its data, and every
cell fits Excel:

* text longer than 32,767 characters (UTF-16 units) is cut and ends with the visible marker
  ``... [TRUNCATED: shown of total characters shown]`` (the same marker as the full database export);
* characters XML cannot hold are removed;
* text starting with ``=`` is written as text, never as a formula;
* integers beyond 2^53 and non-finite reals are written as text, so no digit is lost.

Every case is counted in the returned :class:`WorkbookReport`; nothing is cut silently. The workbook is
written to a temporary file and moved into place, so a failure leaves the previous file (or none).
"""

from __future__ import annotations

import math
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from scripts.export.export_database_excel import (
    EXCEL_MAX_CELL_CHARS,
    EXCEL_MAX_EXACT_INT,
    EXCEL_MAX_ROWS,
    HEADER_ALIGNMENT,
    HEADER_FILL,
    HEADER_FONT,
    ILLEGAL_XML_CHARS,
    truncate_for_excel,
    utf16_len,
)


@dataclass
class Sheet:
    """One table: a title (at most 31 characters), its column labels and its rows (one value per column)."""

    title: str
    columns: Sequence[str]
    rows: Sequence[Sequence[Any]]
    widths: Optional[Sequence[float]] = None


@dataclass
class WorkbookReport:
    sheets: Dict[str, Dict[str, int]] = field(default_factory=dict)   # title -> {"rows": n, "columns": n}
    truncated_cells: int = 0
    truncated_columns: Dict[str, List[str]] = field(default_factory=dict)
    illegal_chars_removed: int = 0
    values_as_text: int = 0


def excel_value(value: Any, report: WorkbookReport, sheet: str, column: str) -> Any:
    """``value`` as Excel can hold it (see the module docstring); counted in ``report``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        if abs(value) > EXCEL_MAX_EXACT_INT:
            report.values_as_text += 1
            return str(value)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            report.values_as_text += 1
            return str(value)
        return value
    text = value if isinstance(value, str) else str(value)
    cleaned, removed = ILLEGAL_XML_CHARS.subn("", text)
    report.illegal_chars_removed += removed
    if utf16_len(cleaned) > EXCEL_MAX_CELL_CHARS:
        cleaned = truncate_for_excel(cleaned)
        report.truncated_cells += 1
        columns = report.truncated_columns.setdefault(sheet, [])
        if column not in columns:
            columns.append(column)
    return cleaned


def write_workbook(path: Path, sheets: Sequence[Sheet]) -> WorkbookReport:
    """Write ``sheets`` to ``path`` (replaced atomically) and return what the limits changed.

    Raises:
        ValueError: a sheet has more rows than Excel holds, a row has the wrong number of values,
            or a title is not a valid sheet name.
    """
    report = WorkbookReport()
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet in sheets:
        if len(sheet.title) > 31 or any(c in sheet.title for c in "[]:*?/\\"):
            raise ValueError(f"invalid sheet name: {sheet.title!r}")
        if len(sheet.rows) + 1 > EXCEL_MAX_ROWS:
            raise ValueError(f"{sheet.title}: {len(sheet.rows):,} rows do not fit one sheet")
        ws = workbook.create_sheet(sheet.title)
        width = len(sheet.columns)
        for col, label in enumerate(sheet.columns, start=1):
            cell = ws.cell(row=1, column=col, value=excel_value(label, report, sheet.title, label))
            cell.data_type = "s"
            cell.font, cell.fill, cell.alignment = HEADER_FONT, HEADER_FILL, HEADER_ALIGNMENT
        for r, values in enumerate(sheet.rows, start=2):
            if len(values) != width:
                raise ValueError(f"{sheet.title}: row {r - 1} has {len(values)} values for {width} columns")
            for col, value in enumerate(values, start=1):
                converted = excel_value(value, report, sheet.title, sheet.columns[col - 1])
                if converted is None:
                    continue
                cell = ws.cell(row=r, column=col, value=converted)
                if isinstance(converted, str):
                    cell.data_type = "s"            # never a formula
        ws.freeze_panes = "A2"
        if width:
            ws.auto_filter.ref = f"A1:{get_column_letter(width)}{len(sheet.rows) + 1}"
        for col in range(1, width + 1):
            given = sheet.widths[col - 1] if sheet.widths else None
            ws.column_dimensions[get_column_letter(col)].width = given or min(
                60, max(10, len(str(sheet.columns[col - 1])) + 2))
        report.sheets[sheet.title] = {"rows": len(sheet.rows), "columns": width}

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".xlsx", dir=path.parent)
    os.close(handle)
    try:
        workbook.save(temporary)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return report


def describe(report: WorkbookReport) -> str:
    """One line for the build log: sheets, and every limit case met."""
    parts = [f"{title}: {s['rows']:,} rows x {s['columns']} columns" for title, s in report.sheets.items()]
    if report.truncated_cells:
        cut = "; ".join(f"{t}: {', '.join(c)}" for t, c in report.truncated_columns.items())
        parts.append(f"{report.truncated_cells} cell(s) cut at {EXCEL_MAX_CELL_CHARS:,} characters ({cut})")
    if report.illegal_chars_removed:
        parts.append(f"{report.illegal_chars_removed} illegal character(s) removed")
    if report.values_as_text:
        parts.append(f"{report.values_as_text} number(s) written as text")
    return "; ".join(parts)
