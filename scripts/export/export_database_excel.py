#!/usr/bin/env python3
"""Export every table of the database to one Excel workbook, one table per sheet.

    python scripts/export/export_database_excel.py                       # latest published snapshot
    python scripts/export/export_database_excel.py --snapshot-id <id>    # a retained snapshot
    python scripts/export/export_database_excel.py --db PATH             # another database
    python scripts/export/export_database_excel.py --out exports/db.xlsx

Input. By default the newest published snapshot (``instance/snapshots/``, the one
``dashboard/publication.json`` names), so the workbook holds exactly the data the
published dashboards were built from. The snapshot is decompressed to a temporary
folder, checked against its manifest hash and removed afterwards. Every database
is opened read-only (``mode=ro``). ``--db`` on a live database reads it table by
table, not as one consistent snapshot.

Output. A first sheet, ``Index``, says where the data came from and lists each
table with its sheet(s), row count, column count and notes. Then one sheet per
table: every column and every row, a bold header row, frozen panes below the
header and an autofilter. The workbook is written to a temporary file beside the
target and moved into place, so a failure leaves the previous file (or none).

Excel's limits are handled explicitly, and every case is counted on the Index and
printed; nothing is cut or changed without a note:

* sheet names: at most 31 characters, unique (case-insensitive), none of
  ``[]:*?/\\``, not starting or ending with an apostrophe, not ``History``. A table
  whose name breaks a rule gets a mapped sheet name, noted on the Index;
* rows: at most 1,048,576 a sheet, the header included. A larger table continues
  on sheets ``<name> (2)``, ``<name> (3)``, ... each with its own header;
* cells: at most 32,767 characters (UTF-16 units). A longer value is cut and
  ends with ``... [TRUNCATED: shown of total characters shown]``;
* characters XML cannot hold (control characters other than tab, newline and
  carriage return, U+FFFE, U+FFFF) are removed and counted;
* BLOBs (embedding vectors and the like) are written as ``[BLOB: n bytes not exported]``;
* integers beyond 2^53 (Excel stores numbers as doubles) and non-finite reals
  are written as text, so no digit is lost;
* text that starts with ``=`` is written as text, never as a formula;
* text that is not valid UTF-8 is decoded with U+FFFD replacement characters.

Memory. openpyxl's write-only mode streams rows to disk, but it keeps every
distinct string in the workbook's shared-strings table until the file is saved,
so peak memory grows with the amount of distinct text (a few GB for the full
database), not with the row count.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cyber_data_collector.publication import snapshot as snapshots  # noqa: E402
from cyber_data_collector.utils.config_manager import resolve_db_path  # noqa: E402

logger = logging.getLogger("export_database_excel")

EXCEL_MAX_ROWS = 1_048_576           # rows a sheet holds, header included
EXCEL_MAX_COLUMNS = 16_384
EXCEL_MAX_CELL_CHARS = 32_767        # UTF-16 code units
SHEET_NAME_MAX = 31
EXCEL_MAX_EXACT_INT = 2 ** 53        # beyond this a double loses digits

INDEX_SHEET = "Index"
RESERVED_SHEET_NAMES = {"history"}   # Excel reserves "History" (case-insensitive)
INVALID_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
# Characters XML 1.0 cannot hold (openpyxl's list plus the two non-characters lxml rejects).
ILLEGAL_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
TRUNCATION_MARKER = " ... [TRUNCATED: {shown} of {total} characters shown]"
FETCH_SIZE = 2_000

PUBLICATION_MANIFEST = Path("dashboard") / "publication.json"
DEFAULT_OUT_DIR = Path("exports")

HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
HEADER_ALIGNMENT = Alignment(vertical="top")


class ExportError(RuntimeError):
    """The export could not be completed; nothing was written to the output path."""


@dataclass
class TableReport:
    """What one table's export met: its sheets and every limit case."""

    table: str
    columns: int = 0
    rows: int = 0
    sheets: List[str] = field(default_factory=list)
    sheet_name_notes: List[str] = field(default_factory=list)
    truncated_cells: int = 0
    truncated_columns: Set[str] = field(default_factory=set)
    illegal_chars_removed: int = 0
    cells_with_illegal_chars: int = 0
    blob_cells: int = 0
    blob_columns: Set[str] = field(default_factory=set)
    big_ints_as_text: int = 0
    nonfinite_as_text: int = 0
    formula_like_text: int = 0
    invalid_utf8_values: int = 0
    internal: bool = False

    def notes(self) -> List[str]:
        out: List[str] = []
        if self.internal:
            out.append("SQLite internal table")
        out.extend(self.sheet_name_notes)
        if len(self.sheets) > 1:
            out.append(f"{self.rows:,} rows continue over {len(self.sheets)} sheets "
                       f"(Excel holds {EXCEL_MAX_ROWS:,} rows a sheet, header included)")
        if self.truncated_cells:
            out.append(f"{self.truncated_cells:,} cell(s) over {EXCEL_MAX_CELL_CHARS:,} characters truncated "
                       f"with a marker (columns: {', '.join(sorted(self.truncated_columns))})")
        if self.illegal_chars_removed:
            out.append(f"{self.illegal_chars_removed:,} character(s) illegal in XLSX removed from "
                       f"{self.cells_with_illegal_chars:,} cell(s)")
        if self.blob_cells:
            out.append(f"{self.blob_cells:,} BLOB value(s) written as a byte-length placeholder "
                       f"(columns: {', '.join(sorted(self.blob_columns))})")
        if self.big_ints_as_text:
            out.append(f"{self.big_ints_as_text:,} integer(s) beyond 2^53 written as text")
        if self.nonfinite_as_text:
            out.append(f"{self.nonfinite_as_text:,} non-finite real(s) written as text")
        if self.formula_like_text:
            out.append(f"{self.formula_like_text:,} text value(s) starting with '=' written as text, not formulas")
        if self.invalid_utf8_values:
            out.append(f"{self.invalid_utf8_values:,} text value(s) not valid UTF-8, decoded with U+FFFD")
        return out


@dataclass
class ExportReport:
    out_path: Path
    source: Dict[str, Any]
    tables: List[TableReport] = field(default_factory=list)
    views: List[str] = field(default_factory=list)
    bytes_written: int = 0
    seconds: float = 0.0

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)

    @property
    def sheet_count(self) -> int:
        return 1 + sum(len(t.sheets) for t in self.tables)


# --- sheet names --------------------------------------------------------------------------------
class SheetNamer:
    """Valid, unique (case-insensitive) Excel sheet names for table names."""

    def __init__(self, reserved: Iterable[str] = (INDEX_SHEET,)):
        self._used: Set[str] = {name.casefold() for name in reserved}

    def name(self, table: str, suffix: str = "") -> Tuple[str, List[str]]:
        notes: List[str] = []
        base = INVALID_SHEET_CHARS.sub("_", table)
        if base != table:
            notes.append("characters Excel forbids in sheet names ([]:*?/\\) replaced by '_'")
        stripped = base.strip("'")
        if stripped != base:
            notes.append("leading/trailing apostrophes removed (Excel forbids them)")
            base = stripped
        if not base:
            base = "table"
            notes.append("empty sheet name replaced by 'table'")
        room = SHEET_NAME_MAX - len(suffix)
        if len(base) > room:
            notes.append(f"table name of {len(table)} characters shortened (Excel allows {SHEET_NAME_MAX})")
            base = base[:room]
        candidate = base + suffix
        if candidate.casefold() in RESERVED_SHEET_NAMES:
            candidate = (base + "_")[:room] + suffix
            notes.append("'History' is reserved by Excel; '_' added")
        counter = 2
        while candidate.casefold() in self._used:
            tag = f"~{counter}"
            candidate = base[:room - len(tag)] + tag + suffix
            counter += 1
        if counter > 2:
            notes.append("name made unique with a '~n' tag (another sheet had the same name)")
        self._used.add(candidate.casefold())
        return candidate, notes


# --- values ------------------------------------------------------------------------------------
def utf16_len(text: str) -> int:
    """Length as Excel counts it: UTF-16 code units (a character outside the BMP counts twice)."""
    if text.isascii():
        return len(text)
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def truncate_for_excel(text: str, limit: int = EXCEL_MAX_CELL_CHARS) -> str:
    """Cut ``text`` (longer than ``limit`` UTF-16 units) and end it with a visible marker."""
    total = utf16_len(text)
    template = TRUNCATION_MARKER.replace("{total}", f"{total:,}")
    budget = limit - len(template.replace("{shown}", f"{limit:,}"))
    units = text.encode("utf-16-le", errors="surrogatepass")[:budget * 2]
    if units and 0xD800 <= int.from_bytes(units[-2:], "little") <= 0xDBFF:
        units = units[:-2]                         # never split a surrogate pair
    head = units.decode("utf-16-le", errors="surrogatepass")
    return head + template.replace("{shown}", f"{utf16_len(head):,}")


class _Utf8Decoder:
    """``text_factory`` that counts values that are not valid UTF-8 instead of failing on them."""

    def __init__(self) -> None:
        self.invalid = 0

    def __call__(self, raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            self.invalid += 1
            return raw.decode("utf-8", errors="replace")


def _text_cell(ws, value: str) -> WriteOnlyCell:
    cell = WriteOnlyCell(ws, value=value)
    cell.data_type = "s"                           # never a formula
    return cell


def convert_value(value: Any, column: str, report: TableReport, ws,
                  cell_limit: int = EXCEL_MAX_CELL_CHARS) -> Any:
    """One SQLite value as something openpyxl writes faithfully; every change counted on ``report``."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > EXCEL_MAX_EXACT_INT:
            report.big_ints_as_text += 1
            return _text_cell(ws, str(value))
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            report.nonfinite_as_text += 1
            return _text_cell(ws, repr(value))
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        report.blob_cells += 1
        report.blob_columns.add(column)
        return f"[BLOB: {len(value):,} bytes not exported]"
    text = value if isinstance(value, str) else str(value)
    cleaned, removed = ILLEGAL_XML_CHARS.subn("", text)
    if removed:
        report.illegal_chars_removed += removed
        report.cells_with_illegal_chars += 1
    if utf16_len(cleaned) > cell_limit:
        report.truncated_cells += 1
        report.truncated_columns.add(column)
        cleaned = truncate_for_excel(cleaned, cell_limit)
    if cleaned.startswith("="):
        report.formula_like_text += 1
        return _text_cell(ws, cleaned)
    return cleaned


# --- database ------------------------------------------------------------------------------------
def open_read_only(path: Path) -> sqlite3.Connection:
    path = Path(path).resolve()
    if not path.is_file():
        raise ExportError(f"database does not exist: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def list_tables(conn: sqlite3.Connection) -> Tuple[List[str], List[str]]:
    rows = conn.execute("SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view') "
                        "ORDER BY name COLLATE NOCASE, name").fetchall()
    return [n for t, n in rows if t == "table"], [n for t, n in rows if t == "view"]


# --- workbook ------------------------------------------------------------------------------------
def _new_sheet(wb: Workbook, title: str, columns: List[str]):
    ws = wb.create_sheet(title=title)
    for index, name in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(index)].width = min(max(len(name) + 4, 10), 45)
    ws.freeze_panes = "A2"
    header = []
    for name in columns:
        cell = _text_cell(ws, ILLEGAL_XML_CHARS.sub("", name))
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
        header.append(cell)
    ws.append(header)
    return ws


def _close_sheet(ws, columns: int, rows_on_sheet: int) -> None:
    ws.auto_filter.ref = f"A1:{get_column_letter(max(columns, 1))}{rows_on_sheet + 1}"


def write_table(conn: sqlite3.Connection, wb: Workbook, table: str, namer: SheetNamer,
                decoder: _Utf8Decoder, max_rows_per_sheet: int = EXCEL_MAX_ROWS,
                cell_limit: int = EXCEL_MAX_CELL_CHARS) -> TableReport:
    """Stream one table onto one or more sheets."""
    report = TableReport(table=table, internal=table.startswith("sqlite_"))
    decoder.invalid = 0
    cursor = conn.execute(f"SELECT * FROM {_quote(table)}")
    columns = [d[0] for d in cursor.description]
    report.columns = len(columns)
    if len(columns) > EXCEL_MAX_COLUMNS:
        raise ExportError(f"table {table} has {len(columns):,} columns; an Excel sheet holds {EXCEL_MAX_COLUMNS:,}")
    data_rows_per_sheet = max_rows_per_sheet - 1
    if data_rows_per_sheet < 1:
        raise ExportError(f"max rows per sheet must be at least 2 (header and one row), got {max_rows_per_sheet}")

    title, notes = namer.name(table)
    report.sheet_name_notes = notes
    ws = _new_sheet(wb, title, columns)
    report.sheets.append(title)
    on_sheet = 0
    while True:
        batch = cursor.fetchmany(FETCH_SIZE)
        if not batch:
            break
        for row in batch:
            if on_sheet == data_rows_per_sheet:
                _close_sheet(ws, len(columns), on_sheet)
                title, _ = namer.name(table, suffix=f" ({len(report.sheets) + 1})")
                ws = _new_sheet(wb, title, columns)
                report.sheets.append(title)
                on_sheet = 0
            ws.append([convert_value(v, columns[i], report, ws, cell_limit) for i, v in enumerate(row)])
            on_sheet += 1
            report.rows += 1
    _close_sheet(ws, len(columns), on_sheet)
    report.invalid_utf8_values = decoder.invalid
    return report


def _write_index(ws, report: ExportReport) -> None:
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 42
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 10
    ws.column_dimensions["E"].width = 120
    bold = Font(bold=True)

    def label(text: str) -> WriteOnlyCell:
        cell = _text_cell(ws, text)
        cell.font = bold
        return cell

    meta: List[Tuple[str, Any]] = [("Database export", "one table per sheet, every column and row")]
    meta += [(k, v) for k, v in report.source.items()]
    meta += [("Tables", len(report.tables)), ("Rows (all tables)", report.total_rows),
             ("Views (not exported)", ", ".join(report.views) if report.views else "none"),
             ("Excel limits", f"{EXCEL_MAX_ROWS:,} rows a sheet (header included); {EXCEL_MAX_CELL_CHARS:,} "
                              f"characters a cell; sheet names of at most {SHEET_NAME_MAX} characters")]
    for key, value in meta:
        ws.append([label(key), _text_cell(ws, str(value)) if isinstance(value, str) else value])
    ws.append([])
    header_row = len(meta) + 2
    header = []
    for name in ("Table", "Sheet(s)", "Rows", "Columns", "Notes"):
        cell = _text_cell(ws, name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        header.append(cell)
    ws.append(header)
    for table in report.tables:
        notes = table.notes()
        ws.append([_text_cell(ws, table.table), _text_cell(ws, ", ".join(table.sheets)), table.rows,
                   table.columns, _text_cell(ws, "; ".join(notes)) if notes else None])
    ws.freeze_panes = f"A{header_row + 1}"
    ws.auto_filter.ref = f"A{header_row}:E{header_row + len(report.tables)}"


def _discard(wb: Workbook) -> None:
    """Close and delete the write-only sheets' temporary files after a failed export."""
    for ws in wb.worksheets:
        writer = getattr(ws, "_writer", None)
        if writer is None:
            continue
        try:
            if not ws.closed:
                ws.close()
        except Exception:                          # the export has already failed; this is cleanup only
            logger.debug("could not close sheet %s while discarding", ws.title, exc_info=True)
        try:
            os.remove(writer.out)
        except OSError:
            pass


def export_connection(conn: sqlite3.Connection, out_path: Path, source: Dict[str, Any],
                      max_rows_per_sheet: int = EXCEL_MAX_ROWS,
                      cell_limit: int = EXCEL_MAX_CELL_CHARS,
                      progress: Optional[Callable[[TableReport], None]] = None) -> ExportReport:
    """Write every table of ``conn`` to ``out_path``; the file appears only when complete."""
    started = time.monotonic()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    decoder = _Utf8Decoder()
    conn.text_factory = decoder
    tables, views = list_tables(conn)
    report = ExportReport(out_path=out_path, source=dict(source), views=views)

    wb = Workbook(write_only=True)
    temporary = out_path.with_name(f".{out_path.stem}.partial-{os.getpid()}{out_path.suffix}")
    try:
        index_ws = wb.create_sheet(title=INDEX_SHEET)   # first tab; filled in last
        namer = SheetNamer()
        for table in tables:
            table_report = write_table(conn, wb, table, namer, decoder, max_rows_per_sheet, cell_limit)
            report.tables.append(table_report)
            if progress:
                progress(table_report)
        _write_index(index_ws, report)
        wb.save(temporary)
        os.replace(temporary, out_path)
    except BaseException:
        _discard(wb)
        temporary.unlink(missing_ok=True)
        raise
    report.bytes_written = out_path.stat().st_size
    report.seconds = time.monotonic() - started
    return report


# --- input selection ------------------------------------------------------------------------------
def _published_snapshot_id(store: snapshots.SnapshotStore, manifest_path: Path) -> Tuple[str, str]:
    """The newest published snapshot, checked against ``dashboard/publication.json`` when it exists."""
    latest = store.resolve(snapshots.LATEST_PUBLISHED)
    if not manifest_path.is_file():
        return latest, f"newest published snapshot in {store.root} ({manifest_path} not found)"
    try:
        named = json.loads(manifest_path.read_text(encoding="utf-8"))["snapshot_id"]
    except (OSError, ValueError, KeyError) as exc:
        raise ExportError(f"cannot read the snapshot id from {manifest_path}: {exc}") from exc
    if named != latest:
        raise ExportError(f"{manifest_path} names snapshot {named}, but the newest published snapshot retained in "
                          f"{store.root} is {latest}; pass --snapshot-id to choose one")
    return latest, f"published snapshot named by {manifest_path}"


def _print_report(report: ExportReport) -> None:
    print(f"Wrote {report.out_path.resolve()}")
    print(f"  size: {report.bytes_written:,} bytes ({report.bytes_written / 1e6:.1f} MB) in {report.seconds:.0f} s")
    print(f"  tables: {len(report.tables)}; rows: {report.total_rows:,}; sheets: {report.sheet_count} "
          f"(Index included); views not exported: {len(report.views)}")
    cases = [(t.table, t.notes()) for t in report.tables if t.notes()]
    if not cases:
        print("  limit cases: none")
        return
    print("  limit cases:")
    for table, notes in cases:
        for note in notes:
            print(f"    {table}: {note}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--db", type=Path, help="export this SQLite database (opened read-only) instead of a snapshot")
    source.add_argument("--snapshot-id", help="export this retained snapshot (an id, or 'latest-published'); "
                                              "default: the snapshot dashboard/publication.json names")
    parser.add_argument("--db-path", default=None,
                        help="the database whose snapshot store (<its folder>/snapshots) to read; "
                             "default DATABASE_URL, else instance/cyber_events.db")
    parser.add_argument("--out", type=Path, default=None,
                        help="workbook to write (default: exports/database_<snapshot id or db name>.xlsx)")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="folder for the decompressed snapshot (default: the system temporary folder)")
    parser.add_argument("--max-rows-per-sheet", type=int, default=EXCEL_MAX_ROWS,
                        help=f"rows a sheet holds, header included (default and maximum: Excel's {EXCEL_MAX_ROWS:,})")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not 2 <= args.max_rows_per_sheet <= EXCEL_MAX_ROWS:
        parser.error(f"--max-rows-per-sheet must be between 2 and {EXCEL_MAX_ROWS:,}")

    def progress(table: TableReport) -> None:
        logger.info("%-45s %10s rows -> %s", table.table, f"{table.rows:,}", ", ".join(table.sheets))

    try:
        if args.db:
            db = Path(args.db)
            label = db.stem
            info: Dict[str, Any] = {"Source database": str(db.resolve()), "Snapshot": "none (database read directly)"}
            conn = open_read_only(db)
            try:
                out = args.out or DEFAULT_OUT_DIR / f"database_{label}.xlsx"
                info["Exported at"] = datetime.now().isoformat(timespec="seconds")
                report = export_connection(conn, out, info, args.max_rows_per_sheet, progress=progress)
            finally:
                conn.close()
        else:
            store = snapshots.store_for(Path(resolve_db_path(args.db_path)))
            if args.snapshot_id:
                sid, chosen = store.resolve(args.snapshot_id), "chosen with --snapshot-id"
            else:
                sid, chosen = _published_snapshot_id(store, PUBLICATION_MANIFEST)
            manifest = store.manifest(sid)
            logger.info("Restoring snapshot %s (%s)", sid, chosen)
            with tempfile.TemporaryDirectory(dir=args.work_dir, prefix="db-export-") as work:
                db = store.restore(sid, Path(work))
                info = {"Snapshot": sid, "Chosen": chosen, "Snapshot sha-256": manifest.get("sha256"),
                        "Snapshot taken at": manifest.get("taken_at"),
                        "Snapshot status": manifest.get("status"),
                        "Published at": manifest.get("published_at") or "",
                        "Source database": manifest.get("source_database"),
                        "Exported at": datetime.now().isoformat(timespec="seconds")}
                conn = open_read_only(db)
                try:
                    out = args.out or DEFAULT_OUT_DIR / f"database_{sid}.xlsx"
                    report = export_connection(conn, out, info, args.max_rows_per_sheet, progress=progress)
                finally:
                    conn.close()
    except (ExportError, snapshots.SnapshotError, sqlite3.Error) as exc:
        print(f"FAILED, no workbook written: {exc}", file=sys.stderr)
        return 2
    _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
