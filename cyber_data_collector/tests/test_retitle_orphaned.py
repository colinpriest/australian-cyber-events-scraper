"""``retitle --orphaned-only`` re-derives only titles no current member gives."""
from __future__ import annotations

import sqlite3

from cyber_data_collector.storage.schema_init import initialize_database


def _event(c, did, title, members):
    c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, "
              "is_australian_event, is_specific_event, victim_organization_name, status) "
              "VALUES (?, ?, ?, 1, 1, 'Australian Securities and Investments Commission', 'Active')",
              (did, members[0][0], title))
    for eid, mtitle in members:
        c.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_title) VALUES (?, 'Perplexity', ?)",
                  ("r" + eid, mtitle))
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, is_australian_event, "
                  "is_specific_event) VALUES (?, ?, ?, 1, 1)", (eid, "r" + eid, mtitle))
        c.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id, "
                  "contribution_type) VALUES (?, ?, ?, ?, 'merged')", ("m" + eid, "r" + eid, eid, did))


def test_only_an_inherited_title_is_re_derived(tmp_path):
    from scripts import dedup_admin

    path = tmp_path / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    asic = "Australian Securities and Investments Commission (ASIC) Security Breach"
    _event(c, "d1", "Medical information leaked in NSW Health hack", [("e1", asic)])
    _event(c, "d2", "ASIC server hit", [("e2", "ASIC server hit"), ("e3", asic)])
    c.commit()
    c.close()
    assert dedup_admin.main(["--db", str(path), "retitle", "--orphaned-only"]) == 0
    c = sqlite3.connect(path)
    titles = dict(c.execute("SELECT deduplicated_event_id, title FROM DeduplicatedEvents"))
    assert titles == {"d1": asic, "d2": "ASIC server hit"}
