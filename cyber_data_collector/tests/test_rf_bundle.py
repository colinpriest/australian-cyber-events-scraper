"""Versioned Random Forest filter bundles: integrity, reproducibility checks,
acceptance, and report-only behaviour for bundles that did not pass."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from cyber_data_collector.filtering import rf_bundle as rb
from cyber_data_collector.filtering.rf_event_filter import RfEventFilter

SMALL = {"tfidf": {"max_features": 50, "ngram_range": [1, 1], "min_df": 1, "max_df": 1.0},
         "forest": {"n_estimators": 10, "max_depth": 3, "random_state": 0, "n_jobs": 1}}


def _frame(n: int = 40) -> pd.DataFrame:
    rows = []
    for i in range(n):
        real = i % 2 == 0
        rows.append({"raw_event_id": f"r{i}", "source_type": "GoogleSearch",
                     "raw_title": ("ransomware attack breach data stolen" if real else "weather sport recipe garden"),
                     "raw_description": "", "raw_content": "", "source_url": f"https://site{i}.example/a",
                     "label": int(real), "origin": "test", "group": f"org:{i // 2}"})
    return pd.DataFrame(rows)


def _bundle(root: Path, accepted: bool, bundle_id: str = "rf-test") -> Path:
    frame = _frame()
    objects = rb.fit(frame, SMALL)
    return rb.write_bundle(bundle_id, objects, {"accepted": accepted, "params": SMALL}, frame.head(10), root=root)


def _model_dir(tmp_path: Path, accepted: bool) -> Path:
    _bundle(tmp_path / "bundles", accepted)
    (tmp_path / "ACTIVE_BUNDLE").write_text("rf-test\n", encoding="utf-8")
    return tmp_path


def test_bundle_round_trips(tmp_path):
    _bundle(tmp_path, accepted=True)
    loaded = rb.load_bundle("rf-test", tmp_path)
    assert loaded.enforcing and loaded.manifest["feature_version"] == rb.FEATURE_VERSION


def test_bundles_are_immutable(tmp_path):
    _bundle(tmp_path, accepted=True)
    with pytest.raises(FileExistsError):
        _bundle(tmp_path, accepted=True)


def test_tampered_file_is_refused(tmp_path):
    directory = _bundle(tmp_path, accepted=True)
    (directory / "model.pkl").write_bytes((directory / "model.pkl").read_bytes() + b"x")
    with pytest.raises(rb.BundleError, match="hash"):
        rb.load_bundle("rf-test", tmp_path)


def test_model_that_no_longer_reproduces_its_predictions_is_refused(tmp_path):
    """What a library upgrade that changed predictions looks like - refused, not re-pickled."""
    directory = _bundle(tmp_path, accepted=True)
    reference = json.loads((directory / "reference.json").read_text())
    reference["keep_probability"] = [1 - p for p in reference["keep_probability"]]
    (directory / "reference.json").write_text(json.dumps(reference))
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["reference_sha256"] = rb._sha256(directory / "reference.json")
    (directory / "manifest.json").write_text(json.dumps(manifest))
    before = (directory / "model.pkl").read_bytes()
    with pytest.raises(rb.BundleError, match="predictions differ"):
        rb.load_bundle("rf-test", tmp_path)
    assert (directory / "model.pkl").read_bytes() == before      # never rewritten


def test_unaccepted_bundle_only_reports(tmp_path):
    rf = RfEventFilter(str(_model_dir(tmp_path, accepted=False)))
    result = rf.should_keep_event("GoogleSearch", "weather sport recipe garden", url="https://x.example")
    assert result.is_cyber_relevant                                 # kept
    assert "report-only" in result.reasoning[0]
    assert rf.stats["would_filter"] == 1 and rf.stats["events_filtered"] == 0


def test_accepted_bundle_filters(tmp_path):
    rf = RfEventFilter(str(_model_dir(tmp_path, accepted=True)))
    assert not rf.should_keep_event("GoogleSearch", "weather sport recipe garden",
                                    url="https://x.example").is_cyber_relevant
    assert rf.should_keep_event("GoogleSearch", "ransomware attack breach data stolen",
                                url="https://x.example").is_cyber_relevant


def test_metrics_and_acceptance():
    m = rb.metrics([1, 1, 0, 0, 0], [True, True, False, False, True])
    assert m["drop_precision"] == 1.0 and m["real_loss"] == 0.0 and m["junk_caught"] == pytest.approx(2 / 3)
    assert rb.accepted(m)
    assert not rb.accepted(rb.metrics([1, 0], [False, False]))      # loses a real incident
    assert not rb.accepted(rb.metrics([1, 0], [True, True]))        # drops nothing: no use


def test_grouped_cv_never_scores_an_organisation_it_trained_on(monkeypatch):
    seen = []
    real_fit = rb.fit

    def spy(frame, params):
        seen.append(set(frame["group"]))
        return real_fit(frame, params)

    monkeypatch.setattr(rb, "fit", spy)
    frame = _frame()
    result = rb.grouped_cv(frame, SMALL, folds=4)
    assert result["folds"] == 4 and len(seen) == 4
    all_groups = set(frame["group"])
    held_out = [all_groups - trained for trained in seen]
    assert all(h and not (h & t) for h, t in zip(held_out, seen))


def test_the_committed_active_bundle_loads_and_is_report_only():
    """The 2025-09 model failed acceptance (drop precision 0.53, real loss 0.06)."""
    rf = RfEventFilter()
    assert rf.bundle_id == "rf-legacy-2025-09" and rf.is_loaded and not rf.enforcing


def test_evaluation_refuses_when_training_membership_is_unknown(tmp_path):
    """Without training_records.csv nothing can be shown to be held out (reviewer finding 38)."""
    from scripts.train_rf_filter import TrainingMembershipUnknown, held_out_records

    frame = pd.DataFrame({"raw_event_id": ["a", "b", "c"], "group": ["acme", "acme", "borealis"],
                          "origin": ["training_csv", "labels", "labels"]})
    with pytest.raises(TrainingMembershipUnknown):
        held_out_records("rf-new", {"kind": "trained"}, frame, bundles_dir=tmp_path)
    # The adopted legacy model's training records are the 2025-09 file's; their organisations are excluded too.
    legacy = held_out_records("rf-legacy", {"kind": "legacy"}, frame, bundles_dir=tmp_path)
    assert list(legacy["raw_event_id"]) == ["c"]
    (tmp_path / "rf-new").mkdir()
    pd.DataFrame({"raw_event_id": ["c"], "group": ["borealis"]}).to_csv(tmp_path / "rf-new" / "training_records.csv")
    trained = held_out_records("rf-new", {"kind": "trained"}, frame, bundles_dir=tmp_path)
    assert list(trained["raw_event_id"]) == ["a", "b"]
