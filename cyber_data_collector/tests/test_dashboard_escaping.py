"""Hostile database text cannot inject markup or script into any dashboard.

A scraped title containing ``</script>`` used to end the entity dashboard's
data script, so whatever followed ran as page code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cyber_data_collector.utils.html_safe import safe_url, script_json

HOSTILE = '</script><script>alert("x")</script><img src=x onerror=alert(1)> &amp;'
HOSTILE_URL = "javascript:alert(1)"


def _scripts_balanced(page: str) -> None:
    """Every <script> the template opens is closed exactly once, and the hostile text never appears raw."""
    assert page.count("</script>") == len(re.findall(r"<script\b", page))
    assert '<script>alert("x")' not in page
    assert "<img src=x onerror" not in page


def test_script_json_round_trips_and_never_closes_a_script():
    value = {"title": HOSTILE, "n": [1, 2]}
    text = script_json(value, ensure_ascii=False)
    assert "</" not in text and "<" not in text and " " not in text
    assert json.loads(text) == value


@pytest.mark.parametrize("url, expected", [
    ("https://example.com/a", "https://example.com/a"),
    ("HTTP://example.com", "HTTP://example.com"),
    (HOSTILE_URL, None), ("JaVaScRiPt:alert(1)", None), ("data:text/html,x", None),
    ("", None), (None, None),
])
def test_safe_url_allows_only_http(url, expected):
    assert safe_url(url) == expected


def test_entity_dashboard():
    from scripts.build_entity_dashboard import build_html, summarise
    rows = [{"event_id": "d1", "entity": HOSTILE, "title": HOSTILE, "description": HOSTILE,
             "date": "2024-01-02", "size": "UNKNOWN", "estimated": False, "members": 1,
             "records": None, "vendor": HOSTILE, "size_basis": HOSTILE, "size_confidence": None}]
    page = build_html(rows, summarise(rows))
    _scripts_balanced(page)
    embedded = json.loads(re.search(r"const ROWS = (\[.*?\]);\n", page, re.S).group(1))
    assert embedded[0]["title"] == HOSTILE        # the data survives intact


def test_dedup_dashboard():
    from scripts.build_dedup_dashboard import build_html
    page = build_html({"events": [{"title": HOSTILE, "victim": HOSTILE,
                                   "members": [{"url": HOSTILE_URL, "title": HOSTILE}]}],
                       "stats": {"generated_at": HOSTILE}})
    _scripts_balanced(page)
    assert "/^https?:/i.test(m.url" in page        # links are drawn only for http(s)


def test_timeline_dashboard():
    from scripts.build_timeline_dashboard import build_html
    org = {"name": HOSTILE, "size": HOSTILE, "stale": False, "event_count": 1, "reconciled": False, "gaps": set(),
           "timeline": {"researched_at": "2026-10-27T00:00", "model": HOSTILE, "timeline": HOSTILE,
                        "citations": [HOSTILE_URL, "https://ok.example/x"]},
           "incidents": [{"number": "1", "date": "2024", "what": HOSTILE, "public": HOSTILE,
                          "source": HOSTILE_URL}],
           "events": [{"date": "2024-01-02", "title": HOSTILE, "records": 1}]}
    page = build_html([org])
    _scripts_balanced(page)
    assert 'href="javascript' not in page.lower()
    assert 'href="https://ok.example/x"' in page


def test_recurrence_report(tmp_path: Path):
    from scripts.build_recurrence_dashboard import render_report
    run = tmp_path / "recurrent_timing"
    run.mkdir()
    (run / "recurrent_timing_report.md").write_text(f"# Report\n\nRemaining repeat: {HOSTILE}\n", encoding="utf-8")
    body = render_report(run)
    assert "Remaining repeat" in body                 # the report was rendered
    assert "<script" not in body and "<img" not in body


def test_static_dashboard_embeds_only_script_safe_json():
    """The main dashboard embeds ~30 data blocks; none may use plain json.dumps."""
    import inspect
    from scripts.build_static_dashboard import build_html
    source = inspect.getsource(build_html)
    assert "json.dumps(" not in source and "script_json(" in source
