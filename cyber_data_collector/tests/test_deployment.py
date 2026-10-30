"""The deployment rule: exact lower bounds, effective n, the policy file, steps refusing to act, held-out folds."""

from __future__ import annotations

import json
import logging
import math

import pytest

from cyber_data_collector.dedup import deployment as dp

pytestmark = pytest.mark.deployment_rule

POLICY = {"version": "test", "confidence": 0.95, "precision_floor": 0.80, "min_labelled_actions": 15,
          "require_heldout_evaluation": True, "heldout_folds": 5}


def _policy(tmp_path, **overrides):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({**POLICY, **overrides}), encoding="utf-8")
    return dp.load_policy(path)


# ----------------------------------------------------------------- the bound

def test_clopper_pearson_all_correct_has_closed_form():
    # With every labelled action right the lower bound is (1 - confidence) ** (1 / n).
    for n in (1, 10, 16, 40):
        assert dp.clopper_pearson_lower(n, n, 0.95) == pytest.approx(0.05 ** (1 / n), rel=1e-9)


def test_clopper_pearson_known_values_and_edges():
    # 10/10 is consistent with a true precision as low as 0.74 (the reviewer's example).
    assert dp.clopper_pearson_lower(10, 10, 0.95) == pytest.approx(0.7411, abs=1e-4)
    assert dp.clopper_pearson_lower(95, 100, 0.95) == pytest.approx(0.8977, abs=1e-4)
    assert dp.clopper_pearson_lower(0, 10, 0.95) == 0.0
    assert dp.clopper_pearson_lower(0, 0, 0.95) == 0.0
    # More confidence, a lower bound; more evidence at the same rate, a higher one.
    assert dp.clopper_pearson_lower(19, 20, 0.99) < dp.clopper_pearson_lower(19, 20, 0.95)
    assert dp.clopper_pearson_lower(95, 100, 0.95) > dp.clopper_pearson_lower(19, 20, 0.95)
    with pytest.raises(ValueError):
        dp.clopper_pearson_lower(11, 10, 0.95)


def test_clopper_pearson_accepts_effective_counts():
    # A non-integer effective count lies between its integer neighbours.
    lo, mid, hi = (dp.clopper_pearson_lower(k * 0.95, k, 0.95) for k in (20, 20.5, 21))
    assert lo < mid < hi


# ----------------------------------------------------------------- effective n

def test_kish_effective_n():
    assert dp.kish_effective_n([1.0] * 12) == pytest.approx(12)
    assert dp.kish_effective_n([2.5] * 12) == pytest.approx(12)        # a constant weight changes nothing
    assert dp.kish_effective_n([1, 1, 4]) == pytest.approx(36 / 18)
    assert dp.kish_effective_n([]) == 0.0
    weights = [1, 1, 1, 7.5, 7.5, 3.2]
    assert dp.kish_effective_n(weights) <= len(weights)                # weighting never inflates n


def test_more_needed():
    policy = dp.Policy(0.95, 0.80, 15, True, 5)
    assert dp.more_needed(10, 10, policy) == 5        # 14 all-correct clear 0.80; the minimum asks for 15
    assert dp.more_needed(30, 30, policy) == 0
    k = dp.more_needed(19, 20, policy)
    assert dp.clopper_pearson_lower(19 + k, 20 + k, 0.95) >= 0.80 > dp.clopper_pearson_lower(18 + k, 19 + k, 0.95)


# ----------------------------------------------------------------- the policy file

def test_policy_loads_and_the_committed_one_is_valid(tmp_path):
    p = _policy(tmp_path)
    assert (p.confidence, p.precision_floor, p.min_labelled_actions, p.heldout_folds) == (0.95, 0.80, 15, 5)
    committed = dp.load_policy()
    assert committed.require_heldout_evaluation is True and 0 < committed.precision_floor < 1


@pytest.mark.parametrize("content, message", [
    (None, "does not exist"),
    ("{not json", "unreadable"),
    ("[1, 2]", "not a JSON object"),
    (json.dumps({k: v for k, v in POLICY.items() if k != "precision_floor"}), "lacks precision_floor"),
    (json.dumps({**POLICY, "confidence": 1.5}), "confidence"),
    (json.dumps({**POLICY, "min_labelled_actions": 0}), "min_labelled_actions"),
    (json.dumps({**POLICY, "require_heldout_evaluation": "yes"}), "require_heldout_evaluation"),
    (json.dumps({**POLICY, "heldout_folds": 1}), "heldout_folds"),
])
def test_a_bad_policy_fails_loudly(tmp_path, content, message):
    path = tmp_path / "policy.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    with pytest.raises(dp.DeploymentPolicyError, match=message):
        dp.load_policy(path)


def test_gate_with_a_missing_policy_raises_rather_than_acts(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "POLICY_PATH", tmp_path / "gone.json")
    cal = _event_cal(30, 30)
    with pytest.raises(dp.DeploymentPolicyError):
        dp.gate("event-reality", cal, tmp_path / "event_reality_calibration.json")


# ----------------------------------------------------------------- the rule on toy calibrations

def _event_cal(acted, correct, weighted_precision=None, generated="2026-10-30T00:00:00", **extra):
    from cyber_data_collector.dedup import event_reality as er

    at = {"cutoff": 0.3, "rejected_labelled": acted, "correct_labelled": correct}
    if weighted_precision is not None:
        at["weighted_precision"] = weighted_precision
    return {**er.calibration_identity(), "cutoff": 0.3, "precision_target": 0.95, "min_rejections": 10,
            "at_cutoff": at, "generated": generated, **extra}


def _write_heldout(cal_path, cal, actions):
    doc = {"version": dp.HELDOUT_VERSION, "calibration": dp.calibration_fingerprint(cal), "actions": actions}
    dp.heldout_path_for(cal_path).write_text(json.dumps(doc), encoding="utf-8")


def _held(acted, correct, effective_n=None, weighted_precision=None, reproduces=True):
    return {"reproduces": reproduces, "full": {"effective_n": effective_n},
            "heldout": {"acted": acted, "correct": correct, "effective_n": effective_n,
                        "weighted_precision": weighted_precision}}


def test_enough_correct_labels_act(tmp_path):
    policy = _policy(tmp_path)
    path = tmp_path / "event_reality_calibration.json"
    cal = _event_cal(30, 30)
    _write_heldout(path, cal, {"reject": _held(30, 30)})
    assert dp.gate("event-reality", cal, path, policy) == cal


def test_ten_of_ten_is_report_only_and_says_what_would_pass(tmp_path, caplog):
    policy = _policy(tmp_path)
    path = tmp_path / "event_reality_calibration.json"
    cal = _event_cal(10, 10)
    _write_heldout(path, cal, {"reject": _held(10, 10)})
    with caplog.at_level(logging.WARNING):
        assert dp.gate("event-reality", cal, path, policy) is None
    text = caplog.text
    assert "REPORT ONLY (deployment rule)" in text and "0.741" in text and "5 more labelled rejections" in text


def test_a_weighted_calibration_is_bounded_by_its_effective_n(tmp_path):
    policy = _policy(tmp_path)
    path = tmp_path / "event_reality_calibration.json"
    cal = _event_cal(40, 39, weighted_precision=0.975)
    # 40 labelled rejections, but weighted down to an effective 12: the bound is computed from 12.
    _write_heldout(path, cal, {"reject": _held(40, 39, effective_n=12.0, weighted_precision=0.975)})
    [d] = dp.assess("event-reality", cal, policy, path)
    assert d.lower_bound_unweighted > 0.85
    assert d.lower_bound_effective == pytest.approx(dp.clopper_pearson_lower(0.975 * 12, 12, 0.95))
    assert d.lower_bound == d.lower_bound_effective and not d.deploy
    assert any("effective n 12.0" in r for r in d.reasons)


def test_weighted_without_an_effective_n_does_not_act(tmp_path):
    policy = _policy(tmp_path, require_heldout_evaluation=False)
    path = tmp_path / "event_reality_calibration.json"
    [d] = dp.assess("event-reality", _event_cal(40, 40, weighted_precision=1.0), policy, path)
    assert not d.deploy and any("effective sample size is unknown" in r for r in d.reasons)


def test_heldout_must_be_of_this_calibration_and_reproduce_it(tmp_path):
    policy = _policy(tmp_path)
    path = tmp_path / "event_reality_calibration.json"
    cal = _event_cal(30, 30)
    _write_heldout(path, {**cal, "generated": "an older calibration"}, {"reject": _held(30, 30)})
    [d] = dp.assess("event-reality", cal, policy, path)
    assert not d.deploy and any("another calibration" in r for r in d.reasons)
    _write_heldout(path, cal, {"reject": _held(30, 30, reproduces=False)})
    [d] = dp.assess("event-reality", cal, policy, path)
    assert not d.deploy and any("could not reproduce" in r for r in d.reasons)


def test_a_weak_heldout_bound_holds_the_step_back(tmp_path):
    policy = _policy(tmp_path)
    path = tmp_path / "event_reality_calibration.json"
    cal = _event_cal(30, 30)
    _write_heldout(path, cal, {"reject": _held(16, 14)})
    [d] = dp.assess("event-reality", cal, policy, path)
    assert d.lower_bound > 0.80 and not d.deploy
    assert any(r.startswith("held-out lower bound") for r in d.reasons)


def test_without_required_heldout_the_full_label_rule_alone_decides(tmp_path):
    policy = _policy(tmp_path, require_heldout_evaluation=False)
    path = tmp_path / "event_reality_calibration.json"
    assert dp.gate("event-reality", _event_cal(30, 30), path, policy) is not None
    assert dp.gate("event-reality", _event_cal(12, 12), path, policy) is None


def test_a_failing_secondary_action_alone_is_switched_off(tmp_path):
    from cyber_data_collector.dedup import victim_judgement as vj

    policy = _policy(tmp_path, require_heldout_evaluation=False)
    cal = {**vj.calibration_identity(), "cutoff": 0.18, "precision_target": 0.95, "min_actions": 10,
           "at_cutoff": {"rejected_labelled": 40, "correct_labelled": 40},
           "replacement_threshold": 0.69, "min_replacements": 5,
           "replacement_at_threshold": {"replaced_labelled": 8, "correct_labelled": 8}}
    out = dp.gate("victim-attribution", cal, tmp_path / "victim_attribution_calibration.json", policy)
    assert out["cutoff"] == 0.18 and out["replacement_threshold"] is None
    assert out["deployment_disabled"] == ["replace"]
    assert cal["replacement_threshold"] == 0.69        # the caller's copy is untouched


def test_supplier_kinds_are_independent(tmp_path):
    from cyber_data_collector.dedup import supplier_victims as sv

    policy = _policy(tmp_path, require_heldout_evaluation=False)
    cal = {**sv.calibration_identity(), "min_accepts": 5,
           "victim": {"cutoff": 0.87, "at_cutoff": {"accepted_labelled": 40, "correct_labelled": 40}},
           "same_incident": {"cutoff": 0.89, "at_cutoff": {"accepted_labelled": 30, "correct_labelled": 30},
                             "different_cutoff": 0.09,
                             "different_at_cutoff": {"rejected_labelled": 6, "correct_labelled": 6}}}
    out = dp.gate("supplier-victims", cal, tmp_path / "supplier_victim_calibration.json", policy)
    assert out["victim"]["cutoff"] == 0.87 and out["same_incident"]["cutoff"] == 0.89
    assert out["same_incident"]["different_cutoff"] is None    # those pairs stay unresolved (held)


def test_a_veto_is_reported_but_never_gated(tmp_path):
    from cyber_data_collector.dedup import duplicate_judgement as dj

    policy = _policy(tmp_path, require_heldout_evaluation=False)
    cal = {**dj.calibration_identity(), "cutoff": 0.81, "min_merges": 5,
           "at_cutoff": {"merged_labelled": 30, "correct_labelled": 30},
           "different_cutoff": 0.12, "different_at_cutoff": {"rejected_labelled": 3, "correct_labelled": 3}}
    out = dp.gate("duplicates", cal, tmp_path / "duplicate_calibration.json", policy)
    assert out["different_cutoff"] == 0.12      # holding a link back can only prevent a merge


# ----------------------------------------------------------------- steps refuse to act

def test_event_reality_step_is_report_only_under_a_failing_rule(tmp_path, monkeypatch):
    from cyber_data_collector.dedup import event_reality as er

    monkeypatch.setattr(dp, "POLICY_PATH", _policy(tmp_path).path)
    path = tmp_path / "event_reality_calibration.json"
    cal = _event_cal(10, 10)
    path.write_text(json.dumps(cal), encoding="utf-8")
    _write_heldout(path, cal, {"reject": _held(10, 10)})
    assert er.active_calibration(path) is None
    cal = _event_cal(30, 30)
    path.write_text(json.dumps(cal), encoding="utf-8")
    _write_heldout(path, cal, {"reject": _held(30, 30)})
    assert er.active_calibration(path)["cutoff"] == 0.3


def test_check_duplicates_does_not_merge_under_a_failing_rule(tmp_path, monkeypatch):
    pytest.importorskip("typesafe_sdk")
    from cyber_data_collector.dedup import duplicate_judgement as dj
    from cyber_data_collector.tests import test_duplicate_judgement as tdj
    from cyber_data_collector.utils.jev import JevJudge

    monkeypatch.setenv("TYPESAFE_MODEL", "jev-1.14.0")
    monkeypatch.setattr(dp, "POLICY_PATH", _policy(tmp_path).path)
    merges = []
    conn = tdj.conn.__wrapped__(monkeypatch, merges)
    path = tmp_path / "duplicate_calibration.json"

    def run(acted, held):
        cal = {**dj.calibration_identity(), "cutoff": 0.8, "precision_target": 0.95, "min_merges": 5,
               "generated": f"g{acted}", "at_cutoff": {"merged_labelled": acted, "correct_labelled": acted}}
        path.write_text(json.dumps(cal), encoding="utf-8")
        _write_heldout(path, cal, {"merge": _held(held, held)})
        return dj.check_duplicates(conn, timeline_search=tdj.NO_SEARCH, escalation_client=tdj.NO_ESCALATION,
                                   apply=True, judge=JevJudge(conn, client=tdj.FakeTypeSafe(0.9)),
                                   calibration_path=path, labels_path=tdj._labels(tmp_path, None))

    result = run(16, 8)            # 16/16 on the full labels, but only 8 held out: not enough
    assert not result["calibrated"] and merges == []
    result = run(40, 40)
    assert result["calibrated"] and len(merges) == 1


# ----------------------------------------------------------------- the held-out evaluation

def test_folds_are_deterministic_and_balanced():
    from scripts import evaluate_calibrations as ec

    ids = [f"event-{i}" for i in range(500)]
    folds = [ec.fold_of("event-reality", i, 5) for i in ids]
    assert folds == [ec.fold_of("event-reality", i, 5) for i in ids]
    counts = [folds.count(k) for k in range(5)]
    assert min(counts) > 70 and sum(counts) == 500


def _toy_rows(n_good, n_bad, n_kept):
    """Rejected-below rows: good rejections score low, a few wrong ones a bit higher, the rest high."""
    rows = [{"id": f"g{i}", "stratum": "A", "weight": 1.0, "p": 0.01 + 0.001 * i, "positive": True}
            for i in range(n_good)]
    rows += [{"id": f"b{i}", "stratum": "A", "weight": 1.0, "p": 0.25 + 0.001 * i, "positive": False}
             for i in range(n_bad)]
    rows += [{"id": f"k{i}", "stratum": "B", "weight": 2.0, "p": 0.6 + 0.001 * i, "positive": False}
             for i in range(n_kept)]
    return rows


def test_heldout_evaluation_on_a_toy_dataset(tmp_path):
    from scripts import evaluate_calibrations as ec

    from scripts.calibrate_event_reality import choose_cutoff

    policy = _policy(tmp_path)
    spec = dp.STEPS["event-reality"].actions[0]
    rows = _toy_rows(40, 3, 30)
    select = ec.SELECTORS[("event-reality", "reject")]
    cutoff = select(rows, 0.95, 10)
    # The step's own selection function picks the cut-off (the largest at >= 95% weighted precision).
    chosen, _ = choose_cutoff([{**r, "not_incident": r["positive"]} for r in rows], 0.95, 10)
    assert cutoff == chosen["cutoff"]
    acted = [r for r in rows if r["p"] < cutoff]
    cal = {"cutoff": cutoff, "precision_target": 0.95, "min_rejections": 10,
           "at_cutoff": {"rejected_labelled": len(acted), "correct_labelled": sum(r["positive"] for r in acted)}}
    out = ec.evaluate_action("event-reality", spec, rows, cal, policy)
    assert out["reproduces"] and out["full"]["acted"] == len(acted)
    held = out["heldout"]
    assert held["folds"] == 5 and sum(f["test"] for f in held["per_fold"]) == len(rows)
    # Every label is held out once, and judged by a cut-off chosen on the other folds only.
    pooled = []
    for fold in held["per_fold"]:
        train = [r for r in rows if ec.fold_of("event-reality", r["id"], 5) != fold["fold"]]
        test = [r for r in rows if ec.fold_of("event-reality", r["id"], 5) == fold["fold"]]
        assert fold["cutoff"] == select(train, 0.95, 10) and fold["train"] + fold["test"] == len(rows)
        pooled += [r for r in test if r["p"] < fold["cutoff"]]
    assert held["acted"] == len(pooled) and held["correct"] == sum(r["positive"] for r in pooled)
    assert held["lower_bound_unweighted"] == pytest.approx(
        dp.clopper_pearson_lower(held["correct"], held["acted"], 0.95))


def test_heldout_catches_a_cutoff_that_does_not_generalise(tmp_path):
    from scripts import evaluate_calibrations as ec

    policy = _policy(tmp_path)
    spec = dp.STEPS["event-reality"].actions[0]
    # Good and wrong rejections interleaved: whatever cut-off a fold picks, the held-out labels pay for it.
    rows = [{"id": f"r{i}", "stratum": "A", "weight": 1.0, "p": i / 100, "positive": i % 6 != 5} for i in range(60)]
    cut = ec.SELECTORS[("event-reality", "reject")](rows, 0.80, 10)
    cal = {"cutoff": cut, "precision_target": 0.80, "min_rejections": 10, "at_cutoff": {}}
    out = ec.evaluate_action("event-reality", spec, rows, cal, policy)
    assert not out["reproduces"]           # the toy calibration records no counts to reproduce
    held = out["heldout"]
    assert held["acted"] > 0 and held["correct"] < held["acted"]
    assert held["lower_bound_unweighted"] < 0.80


def test_evaluation_documents_feed_the_rule(tmp_path, monkeypatch):
    from cyber_data_collector.dedup import event_reality as er
    from scripts import evaluate_calibrations as ec

    policy = _policy(tmp_path)
    rows = _toy_rows(40, 3, 30)
    # A precision target of 1.0 takes exactly the 40 good rejections (every fold re-chooses the same).
    cutoff = ec.SELECTORS[("event-reality", "reject")](rows, 1.0, 10)
    assert sum(r["p"] < cutoff for r in rows) == 40
    cal = {**er.calibration_identity(), "cutoff": cutoff, "precision_target": 1.0, "min_rejections": 10,
           "generated": "now", "at_cutoff": {"rejected_labelled": 40, "correct_labelled": 40,
                                             "weighted_precision": 1.0},
           "labelled_rows": {"reject": rows}}
    path = tmp_path / "event_reality_calibration.json"
    path.write_text(json.dumps(cal), encoding="utf-8")
    monkeypatch.setattr(er, "CALIBRATION_PATH", path)
    doc = ec.evaluate_step("event-reality", policy)
    assert doc["reproduces_calibration"] and doc["rows_source"] == {"reject": "calibration file"}
    assert dp.heldout_path_for(path).exists()
    [d] = dp.assess("event-reality", cal, policy, path)
    assert d.deploy and d.effective_n == pytest.approx(40)
    # A fold whose training labels lack the highest-scoring good rejection re-chooses a slightly lower cut-off.
    assert 35 <= d.heldout["acted"] <= 40 and d.heldout["correct"] == d.heldout["acted"]
    assert math.isclose(d.lower_bound, 0.05 ** (1 / 40), rel_tol=1e-9)


def test_selectors_cover_every_gated_action():
    from scripts import evaluate_calibrations as ec

    for step in dp.STEPS.values():
        for action in step.actions:
            assert (step.name, action.name) in ec.SELECTORS
            assert step.name in ec.SCRIPTS


def test_status_table_lists_every_step():
    policy = dp.load_policy()
    rows = dp.statuses(policy=policy)
    assert {r["step"] for r in rows} == set(dp.STEPS)
    table = dp.format_table(rows, policy)
    assert "duplicates" in table and "deployment_policy.json" in table
