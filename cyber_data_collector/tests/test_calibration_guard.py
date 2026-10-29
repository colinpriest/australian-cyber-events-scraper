"""A calibration applies only to the evidence processing it was measured on.

Cut-offs were activated on the model and question version alone, so when the
evidence readers changed (pages withheld, records filtered) they kept rejecting
and merging on a measurement of different evidence. Each reader now has an
evidence fingerprint recorded in its calibration; a mismatch makes the step
report only.
"""
from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("typesafe_sdk")

from cyber_data_collector.dedup import calibration_guard as guard  # noqa: E402
from cyber_data_collector.dedup import coherence as co  # noqa: E402
from cyber_data_collector.dedup import coherence_same_org as so  # noqa: E402
from cyber_data_collector.dedup import duplicate_judgement as dj  # noqa: E402
from cyber_data_collector.dedup import event_reality as er  # noqa: E402
from cyber_data_collector.dedup import source_gate as sg  # noqa: E402
from cyber_data_collector.dedup import supplier_victims as sv  # noqa: E402
from cyber_data_collector.dedup import victim_judgement as vj  # noqa: E402
from cyber_data_collector.processing import active_evidence  # noqa: E402
from cyber_data_collector.utils import escalation  # noqa: E402

#: module, how its active calibration is read (a path argument), and what to patch to change its evidence.
READERS = [er, dj, co, sg, vj, so]


@pytest.fixture(autouse=True)
def _models(monkeypatch):
    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")


def _write(tmp_path, module, **override):
    path = tmp_path / f"{module.__name__.rsplit('.', 1)[-1]}.json"
    path.write_text(json.dumps({"cutoff": 0.3, **module.calibration_identity(), **override}), encoding="utf-8")
    return path


def test_fingerprint_depends_on_its_parts_and_nothing_else():
    assert guard.fingerprint("r", a=1, b=[2]) == guard.fingerprint("r", b=[2], a=1)
    assert guard.fingerprint("r", a=1) != guard.fingerprint("r", a=2)
    assert guard.fingerprint("r", a=1) != guard.fingerprint("s", a=1)
    assert guard.fingerprint("r", a=1).startswith("r:")


def test_mismatch_names_what_differs():
    want = {"question_version": "q2", "model": "m", "evidence": "e:2"}
    assert guard.mismatch({"question_version": "q2", "model": "m", "evidence": "e:2"}, want) is None
    assert "question_version was 'q1'" in guard.mismatch({"question_version": "q1", "model": "m",
                                                          "evidence": "e:2"}, want)
    assert "evidence was 'e:1', now 'e:2'" in guard.mismatch({"question_version": "q2", "model": "m",
                                                              "evidence": "e:1"}, want)


def test_a_calibration_with_no_fingerprint_does_not_apply():
    want = {"question_version": "q", "model": "m", "evidence": "e:2"}
    reason = guard.mismatch({"question_version": "q", "model": "m"}, want)
    assert reason and "no evidence fingerprint" in reason


def test_an_identity_without_a_fingerprint_is_compared_on_what_it_has():
    # A reader that does not fingerprint yet (AI involvement) keeps the old rule.
    assert guard.mismatch({"question_version": "q", "model": "m"}, {"question_version": "q", "model": "m"}) is None


@pytest.mark.parametrize("module", READERS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_identity_records_the_evidence_fingerprint(module):
    identity = module.calibration_identity()
    assert identity["evidence"] == module.evidence_fingerprint()
    assert identity["evidence"].split(":")[0]


@pytest.mark.parametrize("module", READERS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_matching_calibration_is_active_and_a_stale_one_reports_only(module, tmp_path, caplog):
    assert module.active_calibration(_write(tmp_path, module))["cutoff"] == 0.3
    with caplog.at_level(logging.WARNING):
        assert module.active_calibration(_write(tmp_path, module, evidence="old:0000")) is None
    assert "REPORT ONLY" in caplog.text and "evidence was 'old:0000'" in caplog.text
    assert "dedup_admin.py recalibrate" in caplog.text


@pytest.mark.parametrize("module", READERS, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_a_calibration_that_predates_fingerprinting_is_ignored(module, tmp_path, caplog):
    path = _write(tmp_path, module)
    data = json.loads(path.read_text())
    del data["evidence"]
    path.write_text(json.dumps(data))
    with caplog.at_level(logging.WARNING):
        assert module.active_calibration(path) is None
    assert "no evidence fingerprint" in caplog.text


def test_supplier_calibration_with_other_evidence_refuses_to_apply(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(sv.calibration_identity()))
    assert sv.active_calibration(good)
    stale = tmp_path / "stale.json"
    stale.write_text(json.dumps({**sv.calibration_identity(), "evidence": "supplier-victims:0000"}))
    with pytest.raises(sv.CalibrationError, match="evidence processing"):
        sv.active_calibration(stale)


# --- every reader's fingerprint moves when what it shows moves ---------------------------------

def _fingerprints():
    return {m.__name__.rsplit(".", 1)[-1]: m.evidence_fingerprint() for m in READERS + [sv]
            if hasattr(m, "evidence_fingerprint")} | {"sv": sv.calibration_identity()["evidence"]}


def test_changing_event_evidence_moves_every_judgement_built_on_it(monkeypatch):
    before = _fingerprints()
    monkeypatch.setattr(er, "PAGE_CHARS", er.PAGE_CHARS * 10)
    after = _fingerprints()
    changed = {k for k in before if before[k] != after[k]}
    # event reality, duplicates, victim attribution and supplier victims show events through build_state
    assert {"event_reality", "duplicate_judgement", "victim_judgement", "sv"} <= changed
    assert "source_gate" not in changed


def test_changing_the_active_evidence_rules_moves_the_fingerprints(monkeypatch):
    before = _fingerprints()
    monkeypatch.setattr(active_evidence, "_GENERIC", active_evidence._GENERIC | {"newword"})
    after = _fingerprints()
    assert before["event_reality"] != after["event_reality"] and before["coherence"] != after["coherence"]
    assert before["source_gate"] == after["source_gate"]


def test_bumping_a_readers_version_moves_its_fingerprint(monkeypatch):
    for module, attribute in ((er, "EVIDENCE_VERSION"), (dj, "EVIDENCE_VERSION"), (co, "EVIDENCE_VERSION"),
                              (sg, "EVIDENCE_VERSION"), (vj, "EVIDENCE_VERSION"), (so, "EVIDENCE_VERSION")):
        before = module.evidence_fingerprint()
        monkeypatch.setattr(module, attribute, getattr(module, attribute) + "-next")
        assert module.evidence_fingerprint() != before, module.__name__


def test_the_escalation_effort_is_part_of_the_evidence(monkeypatch):
    before = er.evidence_fingerprint()
    monkeypatch.setattr(escalation, "REASONING_EFFORT", "low")
    assert er.evidence_fingerprint() != before and co.evidence_fingerprint() != before


def test_date_provenance_changes_move_the_duplicate_fingerprint_only(monkeypatch):
    from cyber_data_collector.dedup import date_provenance

    before = _fingerprints()
    monkeypatch.setattr(date_provenance, "EVIDENCE_VERSION", "date-provenance-next")
    after = _fingerprints()
    assert before["duplicate_judgement"] != after["duplicate_judgement"]
    assert before["event_reality"] == after["event_reality"]


def test_the_committed_source_gate_calibration_is_attested_for_the_current_reader():
    """Its reader has not changed since it was measured, so it was stamped rather than re-measured;
    changing the reader (or the version) makes this fail until it is recalibrated."""
    cal = json.loads((sg.CALIBRATION_PATH).read_text(encoding="utf-8"))
    assert cal["evidence"] == sg.evidence_fingerprint()
    assert "Attested, not re-measured" in cal["evidence_attestation"]
    assert sg.active_calibration() is not None
