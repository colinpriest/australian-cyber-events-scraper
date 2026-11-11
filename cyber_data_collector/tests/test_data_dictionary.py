"""The data dictionary page (``scripts/build_data_dictionary.py``) and its place in publication."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from cyber_data_collector.publication import outputs as pub
from scripts import build_data_dictionary as dd

SCHEMA = """
CREATE TABLE RawEvents (raw_event_id TEXT PRIMARY KEY, source_type VARCHAR(50) NOT NULL, raw_title TEXT);
CREATE TABLE EnrichedEvents (enriched_event_id TEXT PRIMARY KEY,
    raw_event_id TEXT NOT NULL REFERENCES RawEvents(raw_event_id), title VARCHAR(255) NOT NULL,
    attacking_entity_id INTEGER);
CREATE TABLE DeduplicatedEvents (deduplicated_event_id TEXT PRIMARY KEY, master_enriched_event_id TEXT,
    status VARCHAR(20) DEFAULT 'Active');
CREATE TABLE EntitiesV2 (entity_id INTEGER PRIMARY KEY AUTOINCREMENT, entity_name VARCHAR(255) NOT NULL);
CREATE TABLE QualityRuns (run_id TEXT PRIMARY KEY, snapshot_id TEXT);
CREATE TABLE DedupSnapshots (snapshot_id TEXT PRIMARY KEY, deduplicated_event_id TEXT);
CREATE TABLE Old_backup (raw_event_id TEXT);
CREATE TABLE Hostile ("note<b>" TEXT);
CREATE VIEW ActiveEvents AS SELECT deduplicated_event_id FROM DeduplicatedEvents WHERE status = 'Active';
"""

MARKDOWN = """# Data Dictionary

#### RawEvents

Initial discovered events, one per article.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `raw_event_id` | TEXT | **PK** | - | UUID primary key |
| `source_type` | VARCHAR(50) | NOT NULL | - | Data source: `Perplexity`, `OAIC` |

#### QualityRuns / DedupSnapshots

Quality runs and the snapshots of merges.

| Table | Column | Type | Description |
|-------|--------|------|-------------|
| QualityRuns | `run_id`, `snapshot_id` | TEXT | Run id and the publication snapshot it measured |
| DedupSnapshots | `snapshot_id` | TEXT | Snapshot id |

* `EnrichedEvents.attacking_entity_id` - the threat actor's entity

| Table | Description |
|-------|-------------|
| `Old_backup` | A V1 table kept for reference |
"""

SUPPLEMENT = {
    "groups": [
        {"name": "Core pipeline", "description": "The core.",
         "tables": ["RawEvents", "EnrichedEvents", "DeduplicatedEvents", "EntitiesV2"]},
        {"name": "Quality", "tables": ["QualityRuns", "DedupSnapshots", "ActiveEvents", "Hostile"]},
    ],
    "key_owners": {"entity_id": "EntitiesV2"},
    "not_links": ["QualityRuns.snapshot_id"],
    "tables": {
        "EnrichedEvents": {"purpose": "LLM-processed records.",
                           "columns": {"enriched_event_id": "UUID primary key", "raw_event_id": "The record",
                                       "title": "Enriched title"}},
        "DeduplicatedEvents": {"purpose": "One row per incident.",
                               "columns": {"deduplicated_event_id": "Incident id",
                                           "master_enriched_event_id": "The record that leads the incident",
                                           "status": "`Active`, `Merged` or `Rejected`"}},
        "EntitiesV2": {"purpose": "Organisations.", "columns": {"entity_id": "Id", "entity_name": "Name"}},
        "DedupSnapshots": {"purpose": "State before a merge.", "columns": {"deduplicated_event_id": "The event"}},
        "Old_backup": {"columns": {"raw_event_id": "The V1 record"}},
        "ActiveEvents": {"purpose": "Active incidents.", "columns": {"deduplicated_event_id": "Incident id"}},
        "Hostile": {"purpose": "Has <script>alert(1)</script> in it.",
                    "columns": {"note<b>": "A value like <img src=x onerror=alert(1)> & more"}},
        "RawEvents": {"columns": {"raw_title": "Title as scraped",
                                  "source_type": "Not used: the Markdown description comes first"}},
    },
}


@pytest.fixture
def sources(tmp_path):
    md = tmp_path / "dd.md"
    md.write_text(MARKDOWN, encoding="utf-8")
    sup = tmp_path / "dd.json"
    sup.write_text(json.dumps(SUPPLEMENT), encoding="utf-8")
    return md, sup


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    yield c
    c.close()


def test_every_column_of_the_schema_has_a_description(conn, sources):
    d = dd.collect(conn, *sources)
    assert d.undescribed == [] and d.no_purpose == []
    assert d.column_count == sum(len(t.columns) for t in d.tables) == 19
    html = dd.build_html(d, "now")
    for t in d.tables:
        assert f'id="{dd.anchor(t.name)}"' in html
    assert "No description yet" not in html


def test_markdown_comes_first_and_the_supplement_fills_the_gaps(conn, sources):
    d = dd.collect(conn, *sources)
    assert d.descriptions[("RawEvents", "source_type")] == "Data source: `Perplexity`, `OAIC`"
    assert d.descriptions[("RawEvents", "raw_title")] == "Title as scraped"
    assert d.descriptions[("QualityRuns", "snapshot_id")] == "Run id and the publication snapshot it measured"
    assert d.descriptions[("EnrichedEvents", "attacking_entity_id")] == "the threat actor's entity"
    by_name = {t.name: t for t in d.tables}
    assert by_name["RawEvents"].purpose == "Initial discovered events, one per article."
    assert by_name["QualityRuns"].purpose == "Quality runs and the snapshots of merges."
    assert by_name["Old_backup"].purpose == "A V1 table kept for reference"
    assert by_name["EnrichedEvents"].purpose == "LLM-processed records."


def test_undescribed_columns_are_counted_not_hidden(conn, sources, tmp_path):
    md, _ = sources
    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    d = dd.collect(conn, md, empty)
    assert ("DeduplicatedEvents", "status") in d.undescribed
    assert ("RawEvents", "source_type") not in d.undescribed
    assert "EntitiesV2" in d.no_purpose
    html = dd.build_html(d, "now")
    assert html.count("No description yet.") == len(d.undescribed) + len(d.no_purpose)
    assert f"{d.column_count - len(d.undescribed)} described" in html


def test_declared_and_inferred_links(conn, sources):
    d = dd.collect(conn, *sources)
    links = {(l.table, l.column, l.target, l.declared) for l in d.links}
    assert ("EnrichedEvents", "raw_event_id", "RawEvents", True) in links
    assert ("DeduplicatedEvents", "master_enriched_event_id", "EnrichedEvents", False) in links   # suffix of a key
    assert ("EnrichedEvents", "attacking_entity_id", "EntitiesV2", False) in links
    assert ("DedupSnapshots", "deduplicated_event_id", "DeduplicatedEvents", False) in links
    assert not any(l.table == "QualityRuns" and l.column == "snapshot_id" for l in d.links)       # not_links
    assert not any(l.table == "ActiveEvents" for l in d.links)                                     # views: none
    # nothing is inferred to or from a kept copy of an old table
    assert not any("Old_backup" in (l.table, l.target) for l in d.links)
    assert not any(l.table == l.target and l.column == l.target_column for l in d.links)


def test_diagrams_mark_declared_and_inferred_links_differently(conn, sources):
    html = dd.build_html(dd.collect(conn, *sources), "now")
    core = html[html.index("Core pipeline tables"):html.index("Every table, by area")]
    assert '<svg class="erd"' in core
    assert 'class="edge decl"' in core and 'class="edge infer"' in core
    assert 'marker-end="url(#arr)"' in core and 'marker-end="url(#arri)"' in core
    overview = html[html.index("Every table, by area"):html.index('id="contents"')]
    # two areas, plus "Other" for the table no area lists
    assert overview.count('class="panel') == 3 and 'class="panel core"' in overview
    assert {t.name: t.group for t in dd.collect(conn, *sources).tables}["Old_backup"] == dd.OTHER_GROUP
    for name in ("RawEvents", "QualityRuns", "ActiveEvents"):
        assert f'href="#{dd.anchor(name)}"' in overview
    assert "<script" not in html.lower().replace("&lt;script", "")    # no scripts, nothing external
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "") and "https://" not in html


def test_text_from_the_sources_is_escaped(conn, sources):
    html = dd.build_html(dd.collect(conn, *sources), "<b>now</b>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt; &amp; more" in html
    assert "<code>note&lt;b&gt;</code>" in html and "<b>now</b>" not in html


def test_markdown_inline_rendering():
    assert dd.md_inline("`a<b>` and **bold** [doc](x.md)") == "<code>a&lt;b&gt;</code> and <strong>bold</strong> doc"


def test_the_database_is_opened_read_only(tmp_path):
    path = tmp_path / "db with space.db"
    sqlite3.connect(path).executescript("CREATE TABLE T (a TEXT); INSERT INTO T VALUES ('x');")
    conn = dd.connect_read_only(path)
    try:
        assert conn.execute("SELECT a FROM T").fetchone() == ("x",)
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO T VALUES ('y')")
    finally:
        conn.close()


def test_the_committed_sources_describe_the_core_tables():
    md = dd.parse_markdown(dd.DEFAULT_MARKDOWN.read_text(encoding="utf-8"),
                           ["RawEvents", "EnrichedEvents", "DeduplicatedEvents", "EventDeduplicationMap"])
    assert md.columns[("RawEvents", "raw_event_id")] == "UUID primary key"
    assert ("EventDeduplicationMap", "contribution_type") in md.columns
    supplement = dd.load_supplement(dd.DEFAULT_SUPPLEMENT)
    groups = [name for name, _, _ in supplement.groups]
    assert groups[0] == "Core pipeline" and len(groups) == len(set(groups))
    placed = [t for _, _, tables in supplement.groups for t in tables]
    assert len(placed) == len(set(placed)), "a table is in two areas"


def test_publication_builds_the_page_from_the_snapshot(tmp_path, conn, monkeypatch):
    db = tmp_path / "snap.db"
    conn.backup(sqlite3.connect(db))
    out = tmp_path / "staging"
    out.mkdir()
    ctx = SimpleNamespace(db=str(db), snapshot=SimpleNamespace(snapshot_id="snap-test"), dashboard_dir=out)
    paths = pub.build_data_dictionary(ctx)
    assert paths == [out / "data_dictionary.html"]
    text = paths[0].read_text(encoding="utf-8")
    assert "from snapshot snap-test" in text and 'id="t-RawEvents"' in text
    assert "data_dictionary" in pub.DEFAULT_BUILDERS


def test_the_page_is_a_whole_document_the_stamp_can_mark(conn, sources):
    html = dd.build_html(dd.collect(conn, *sources), "now")
    assert re.search(r"<head[^>]*>", html) and html.rstrip().endswith("</html>") and "</body>" in html
    assert "prefers-color-scheme: dark" in html and 'name="viewport"' in html
