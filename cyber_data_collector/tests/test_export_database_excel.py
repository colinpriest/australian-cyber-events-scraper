"""Tests for scripts/export/export_database_excel.py: every table to Excel, every limit reported."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from openpyxl import load_workbook

from cyber_data_collector.publication import snapshot as snapshots
from scripts.export import export_database_excel as exporter

LONG_A = "VeryLongTableNameThatExceedsExcelsLimitAlpha"
LONG_B = "VeryLongTableNameThatExceedsExcelsLimitBravo"
BAD_CHARS = "odd[name]:with*?/\\chars"


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
        CREATE TABLE "{LONG_A}" (id INTEGER, note TEXT);
        CREATE TABLE "{LONG_B}" (id INTEGER, note TEXT);
        CREATE TABLE "{BAD_CHARS.replace('"', '""')}" (id INTEGER);
        CREATE TABLE History (id INTEGER);
        CREATE TABLE Index_ (id INTEGER);
        CREATE TABLE Cells (id INTEGER, body TEXT, vector BLOB, big INTEGER, ratio REAL);
        CREATE TABLE Many (n INTEGER, label TEXT);
        CREATE TABLE Empty (a TEXT, b TEXT);
        CREATE VIEW ManyView AS SELECT n FROM Many;
    """)
    conn.execute(f'INSERT INTO "{LONG_A}" VALUES (1, \'a\')')
    conn.execute(f'INSERT INTO "{LONG_B}" VALUES (2, \'b\')')
    conn.executemany("INSERT INTO Cells VALUES (?, ?, ?, ?, ?)", [
        (1, "x" * 40_000, b"\x00\x01" * 8, 2 ** 60, float("inf")),
        (2, "bell\x07 and vertical tab\x0b here", None, 42, 1.5),
        (3, "=1+1", None, None, None),
        (4, "normal text", None, -(2 ** 53), 0.0),
    ])
    conn.executemany("INSERT INTO Many VALUES (?, ?)", [(i, f"row {i}") for i in range(1, 8)])
    conn.commit()


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    _schema(connection)
    yield connection
    connection.close()


def _export(conn, tmp_path, **kwargs):
    out = tmp_path / "out" / "db.xlsx"
    report = exporter.export_connection(conn, out, {"Source database": "test"}, **kwargs)
    return out, report, {t.table: t for t in report.tables}


def _rows(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


def test_every_table_on_its_own_sheet_with_index_first(conn, tmp_path):
    out, report, by_table = _export(conn, tmp_path)
    wb = load_workbook(out)
    assert wb.sheetnames[0] == "Index"
    assert len(report.tables) == 8
    assert report.views == ["ManyView"]
    assert report.total_rows == 1 + 1 + 0 + 0 + 0 + 4 + 7 + 0
    many = wb[by_table["Many"].sheets[0]]
    assert _rows(many)[0] == ["n", "label"]
    assert _rows(many)[1:] == [[i, f"row {i}"] for i in range(1, 8)]
    assert many.freeze_panes == "A2"
    assert many.auto_filter.ref == "A1:B8"
    empty = wb[by_table["Empty"].sheets[0]]
    assert _rows(empty) == [["a", "b"]]
    assert empty.auto_filter.ref == "A1:B1"


def test_long_and_invalid_table_names_map_to_unique_valid_sheet_names(conn, tmp_path):
    out, _report, by_table = _export(conn, tmp_path)
    wb = load_workbook(out)
    names = [by_table[t].sheets[0] for t in by_table]
    assert all(len(n) <= 31 for n in names)
    assert len({n.casefold() for n in wb.sheetnames}) == len(wb.sheetnames)
    a, b = by_table[LONG_A], by_table[LONG_B]
    assert a.sheets[0] != b.sheets[0]
    assert any("shortened" in note for note in a.notes())
    assert any("unique" in note for note in b.notes())
    bad = by_table[BAD_CHARS].sheets[0]
    assert not any(ch in bad for ch in "[]:*?/\\")
    assert any("forbids" in note for note in by_table[BAD_CHARS].notes())
    assert by_table["History"].sheets[0].casefold() != "history"
    index = _rows(wb["Index"])
    listed = {row[0]: row for row in index if row and row[0] in by_table}
    assert listed[LONG_A][1] == a.sheets[0]
    assert "shortened" in listed[LONG_A][4]


def test_over_long_cell_truncated_with_marker_and_counted(conn, tmp_path):
    out, _report, by_table = _export(conn, tmp_path)
    cells = by_table["Cells"]
    assert cells.truncated_cells == 1 and cells.truncated_columns == {"body"}
    value = _rows(load_workbook(out)[cells.sheets[0]])[1][1]
    assert len(value) <= exporter.EXCEL_MAX_CELL_CHARS
    assert value.startswith("xxx")
    assert value.endswith("of 40,000 characters shown]")
    assert "TRUNCATED" in value


def test_truncation_counts_utf16_units():
    text = "\U0001F600" * 20_000                  # 40,000 UTF-16 units, 20,000 code points
    cut = exporter.truncate_for_excel(text)
    assert exporter.utf16_len(cut) <= exporter.EXCEL_MAX_CELL_CHARS
    assert "of 40,000 characters shown" in cut


def test_control_characters_stripped_and_counted(conn, tmp_path):
    out, _report, by_table = _export(conn, tmp_path)
    cells = by_table["Cells"]
    assert cells.illegal_chars_removed == 2 and cells.cells_with_illegal_chars == 1
    assert _rows(load_workbook(out)[cells.sheets[0]])[2][1] == "bell and vertical tab here"
    assert any("illegal in XLSX" in note for note in cells.notes())


def test_blob_written_as_placeholder_and_numbers_kept_exact(conn, tmp_path):
    out, _report, by_table = _export(conn, tmp_path)
    cells = by_table["Cells"]
    rows = _rows(load_workbook(out)[cells.sheets[0]])
    assert rows[1][2] == "[BLOB: 16 bytes not exported]"
    assert cells.blob_cells == 1 and cells.blob_columns == {"vector"}
    assert rows[1][3] == str(2 ** 60) and cells.big_ints_as_text == 1
    assert rows[4][3] == -(2 ** 53)               # exactly representable: stays a number
    assert rows[1][4] == "inf" and cells.nonfinite_as_text == 1
    assert rows[3][1] == "=1+1" and cells.formula_like_text == 1
    wb = load_workbook(out)
    assert wb[cells.sheets[0]].cell(row=4, column=2).data_type == "s"


def test_large_table_continues_on_numbered_sheets(conn, tmp_path):
    out, report, by_table = _export(conn, tmp_path, max_rows_per_sheet=3)
    many = by_table["Many"]
    assert many.sheets == ["Many", "Many (2)", "Many (3)", "Many (4)"]
    wb = load_workbook(out)
    seen = []
    for name in many.sheets:
        rows = _rows(wb[name])
        assert rows[0] == ["n", "label"]
        assert len(rows) - 1 <= 2
        seen.extend(r[0] for r in rows[1:])
    assert seen == list(range(1, 8))
    assert wb["Many (4)"].auto_filter.ref == "A1:B2"
    assert any("continue over 4 sheets" in note for note in many.notes())
    index = {row[0]: row for row in _rows(wb["Index"]) if row and row[0] == "Many"}
    assert index["Many"][1] == "Many, Many (2), Many (3), Many (4)"
    assert index["Many"][2] == 7


def test_invalid_utf8_decoded_and_counted(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES (CAST(X'61FF62' AS TEXT))")
    out, _report, by_table = _export(conn, tmp_path)
    assert by_table["t"].invalid_utf8_values == 1
    assert _rows(load_workbook(out)["t"])[1][0] == "a�b"


def test_failure_leaves_previous_workbook_and_no_partial_file(conn, tmp_path, monkeypatch):
    out = tmp_path / "out" / "db.xlsx"
    out.parent.mkdir()
    out.write_bytes(b"previous")

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(exporter.Workbook, "save", boom)
    with pytest.raises(RuntimeError):
        exporter.export_connection(conn, out, {})
    assert out.read_bytes() == b"previous"
    assert [p.name for p in out.parent.iterdir()] == ["db.xlsx"]


def _file_db(tmp_path) -> Path:
    path = tmp_path / "cyber.db"
    file_conn = sqlite3.connect(path)
    _schema(file_conn)
    file_conn.close()
    return path


def test_cli_reads_database_read_only(tmp_path, capsys):
    db = _file_db(tmp_path)
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    out = tmp_path / "x.xlsx"
    assert exporter.main(["--db", str(db), "--out", str(out)]) == 0
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    printed = capsys.readouterr().out
    assert "tables: 8; rows: 13" in printed
    assert "truncated" in printed
    with pytest.raises(sqlite3.OperationalError):
        exporter.open_read_only(db).execute("CREATE TABLE nope (a)")


def test_cli_missing_database_fails_loudly(tmp_path, capsys):
    out = tmp_path / "x.xlsx"
    assert exporter.main(["--db", str(tmp_path / "missing.db"), "--out", str(out)]) == 2
    assert not out.exists()
    assert "no workbook written" in capsys.readouterr().err


def _published_store(tmp_path) -> tuple[Path, str]:
    (tmp_path / "instance").mkdir()
    db = _file_db(tmp_path / "instance")
    store = snapshots.store_for(db)
    snap = snapshots.take(db, tmp_path / "work")
    store.retain(snap, status=snapshots.PUBLISHED)
    return db, snap.snapshot_id


def test_default_input_is_latest_published_snapshot(tmp_path, monkeypatch, capsys):
    db, sid = _published_store(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "publication.json").write_text(json.dumps({"snapshot_id": sid}))
    assert exporter.main(["--db-path", str(db)]) == 0
    out = tmp_path / "exports" / f"database_{sid}.xlsx"
    assert out.is_file()
    meta = {row[0]: row[1] for row in _rows(load_workbook(out)["Index"])[:12] if row and row[0]}
    assert meta["Snapshot"] == sid
    assert "publication.json" in meta["Chosen"]


def test_publication_manifest_disagreeing_with_store_fails(tmp_path, monkeypatch, capsys):
    db, _sid = _published_store(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "publication.json").write_text(json.dumps({"snapshot_id": "snap-other"}))
    assert exporter.main(["--db-path", str(db)]) == 2
    assert "pass --snapshot-id" in capsys.readouterr().err
    assert not (tmp_path / "exports").exists()
