"""Tests for optimize.py -- the lever catalog and the FOCUS detectors.

These run against the deterministic demo estate, so every planted signal
(commitment waste, idle resources, gp2 volumes, ...) has an exact expectation.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import kpi
import optimize
from connectors.demo import build_demo_dataset


@pytest.fixture(scope="module")
def demo():
    df, _budgets, _drivers = build_demo_dataset()
    return df


@pytest.fixture(scope="module")
def opps(demo):
    return optimize.detect_all(demo)


# ---- catalog ---------------------------------------------------------------


def test_catalog_size():
    assert len(optimize.LEVERS) >= 45


def test_catalog_ids_unique():
    ids = [lv.id for lv in optimize.LEVERS]
    assert len(ids) == len(set(ids))
    assert optimize.LEVER_BY_ID.keys() == set(ids)


def test_catalog_savings_bounds_ordered():
    for lv in optimize.LEVERS:
        assert lv.savings_low <= lv.savings_high, f"{lv.id} savings_low > savings_high"
        assert 0.0 <= lv.savings_low <= 1.0
        assert 0.0 <= lv.savings_high <= 1.0


def test_catalog_has_source_urls():
    for lv in optimize.LEVERS:
        assert lv.source_url.startswith("http"), f"{lv.id} missing source URL"


def test_lever_catalog_frame():
    frame = optimize.lever_catalog_frame()
    assert len(frame) == len(optimize.LEVERS)
    assert {"id", "savings_low", "savings_high", "source_url"}.issubset(frame.columns)


# ---- detection -------------------------------------------------------------


def test_detect_all_distinct_levers(opps):
    lever_ids = {o.lever_id for o in opps}
    assert len(lever_ids) >= 8, f"only {len(lever_ids)} distinct levers: {lever_ids}"


def test_commitment_waste_matches_kpi(demo, opps):
    months = demo["ChargePeriodStart"].dt.to_period("M").nunique()
    expected_total = kpi.commitment_waste(demo) / months
    waste_opps = [
        o for o in opps
        if o.lever_id == "R21" and "unused commitment" in o.scope.lower()
    ]
    assert waste_opps, "no commitment-waste opportunity detected"
    got_total = sum(o.monthly_savings for o in waste_opps)
    assert got_total == pytest.approx(expected_total, rel=0.01)


def test_idle_detector_finds_all_types(demo):
    idle = optimize._detect_idle_resources(demo)
    scopes = " ".join(o.scope for o in idle)
    for rtype in ("Volume", "Gateway", "IpAddress", "Snapshot", "LoadBalancer"):
        assert rtype in scopes, f"idle detector missed {rtype}"


def test_gp2_detector_fires(demo):
    gp2 = optimize._detect_gp2_volumes(demo)
    assert gp2, "gp2 detector did not fire"
    assert gp2[0].lever_id == "U15"


def test_annual_is_twelve_times_monthly(opps):
    for o in opps:
        assert o.annual_savings == pytest.approx(o.monthly_savings * 12.0, rel=1e-9)


def test_roadmap_cumulative_monotonic(opps):
    road = optimize.roadmap(opps)
    cum = road["cumulative_annual_savings"].tolist()
    assert cum == sorted(cum), "cumulative column is not monotonically increasing"
    for a, b in zip(cum, cum[1:]):
        assert b >= a


# ---- rollups + reconciliation ---------------------------------------------


def test_usage_waste_feeds_kpi(demo, opps):
    """`usage_waste_total` is a monthly run-rate; `commitment_waste` is a
    whole-window total. `cost_of_waste` must reconcile the two rather than
    adding one month of usage waste to two years of commitment waste."""
    usage_waste_monthly = optimize.usage_waste_total(opps)
    assert usage_waste_monthly > 0

    months = kpi.months_observed(demo)
    assert months > 12, "the demo estate spans about two years"

    cow = kpi.cost_of_waste(demo, usage_waste_monthly=usage_waste_monthly)
    expected = kpi.commitment_waste(demo) + usage_waste_monthly * months
    assert cow == pytest.approx(expected, rel=1e-9)

    # The scaled figure must dominate the naive sum -- that mismatch is the bug
    # this test exists to prevent from coming back.
    naive = kpi.commitment_waste(demo) + usage_waste_monthly
    assert cow > naive * 3


def test_savings_by_category(opps):
    cat = optimize.savings_by_category(opps)
    assert set(cat["category"]).issubset({"Rate", "Usage", "Architecture", "AI/GPU"})
    assert (cat["annual_savings"] > 0).any()


def test_esr_uplift(demo, opps):
    up = optimize.effective_savings_rate_uplift(demo, opps)
    assert up["projected_esr_pct"] >= (up["current_esr_pct"] or 0)
    assert up["rate_savings_annual"] > 0


def test_untagged_is_zero_dollar_prereq(demo):
    untagged = optimize._detect_untagged(demo)
    assert untagged, "untagged governance flag not raised"
    assert untagged[0].monthly_savings == 0.0
    assert untagged[0].evidence["unallocated_amortized_total"] > 0
