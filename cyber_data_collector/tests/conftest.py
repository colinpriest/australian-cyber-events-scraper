"""Shared test configuration."""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "deployment_rule: the test exercises the calibration deployment rule (it is not bypassed)")


@pytest.fixture(autouse=True)
def _deployment_rule_not_under_test(request, monkeypatch):
    """Tests of a step's own logic build toy calibrations (a cut-off and an identity, no labelled counts).

    The deployment rule (``cyber_data_collector/dedup/deployment.py``) would hold every one of them back,
    so outside tests marked ``deployment_rule`` it passes a calibration through unchanged. The rule itself,
    and each step refusing to act under it, are tested in ``test_deployment.py`` with the real gate.
    """
    if request.node.get_closest_marker("deployment_rule"):
        return
    from cyber_data_collector.dedup import deployment

    monkeypatch.setattr(deployment, "gate", lambda step_name, cal, *args, **kwargs: cal)
