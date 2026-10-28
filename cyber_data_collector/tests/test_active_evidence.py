"""One rule for which records and pages may drive event decisions."""

from __future__ import annotations

import json
import sqlite3

import pytest

from cyber_data_collector.processing import active_evidence as ae
from cyber_data_collector.processing import breach_source as bs
from cyber_data_collector.storage.schema_init import initialize_database

OPENAI_PAGE = ("Vulnerability exploit. Contained. OpenAI agent bypasses controls to access Australia's Medicare "
               "statistics portal. Australia disclosed that an OpenAI research agent autonomously bypassed access "
               "controls to reach non-public files on a Services Australia portal.")


@pytest.mark.parametrize("title, names, page, belongs", [
    # 2026-10-28: the Mackay Sugar record held a breach-list page about the OpenAI agent.
    ("Mackay Sugar ransomware attack", ["Mackay Sugar"], OPENAI_PAGE, False),
    ("NAB data breach involving 13,000 customers", ["NAB"], "NAB said 13,000 customers were affected.", True),
    ("Uber cyber incident", ["Uber Technologies, Inc."], "Uber concealed a breach affecting riders.", True),
    ("Services Australia data breach", ["Services Australia"], "A Services Australia staff member ...", True),
    ("Statement on MediSecure data breach", ["MediSecure"],
     "Beware the dangers of data breach fatigue. The public are being warned ...", False),
    ("Australian sugar producer attacked", [None], OPENAI_PAGE, False),     # title words only
    ("Data breach", [None], "anything at all", None),                         # nothing to check against
    ("Acme breach", ["Acme"], "", None),                                       # no page
])
def test_page_identity(title, names, page, belongs):
    assert ae.page_identity(title, names, page)[0] is belongs


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "instance" / "t.db"
    initialize_database(str(path))
    c = sqlite3.connect(path)
    c.execute("INSERT INTO DeduplicatedEvents (deduplicated_event_id, master_enriched_event_id, title, status, "
              "victim_organization_name, is_australian_event, is_specific_event) "
              "VALUES ('d', 'e1', 'Mackay Sugar ransomware attack', 'Active', 'Mackay Sugar', 1, 1)")
    for raw, status, page in (("r1", "Active", OPENAI_PAGE),
                              ("r2", "Active", "Mackay Sugar confirmed a ransomware attack on its mill systems."),
                              ("r3", "Inactive", "Mackay Sugar junk page that was rejected.")):
        c.execute("INSERT INTO RawEvents (raw_event_id, source_type, raw_content) VALUES (?, 'GoogleSearch', ?)",
                  (raw, page))
        c.execute("INSERT INTO EnrichedEvents (enriched_event_id, raw_event_id, title, status, is_australian_event, "
                  "is_specific_event, perplexity_enrichment_data) VALUES (?, ?, 'Mackay Sugar ransomware attack', ?, "
                  "1, 1, ?)", (f"e{raw[1]}", raw, status, json.dumps({"formal_entity_name": "Mackay Sugar Limited"})))
        c.execute("INSERT INTO EventDeduplicationMap (map_id, raw_event_id, enriched_event_id, deduplicated_event_id) "
                  "VALUES (?, ?, ?, 'd')", (f"m{raw}", raw, f"e{raw[1]}"))
    c.commit()
    yield c
    c.close()


def test_readers_use_active_members_and_withhold_another_articles_page(conn):
    from cyber_data_collector.dedup.event_reality import build_state

    for state in (bs.event_state(conn, "d"), build_state(conn, "d")):
        pages = [r["page_text"] for r in state["records"]]
        assert state["event"]["record_count"] == 2                              # the inactive record is out
        assert not any("junk page" in p for p in pages)
        assert any(p.startswith("Mackay Sugar confirmed") for p in pages)
        withheld = [r for r in state["records"] if "page_withheld" in r]
        assert len(withheld) == 1 and "OpenAI" not in withheld[0]["page_text"]
