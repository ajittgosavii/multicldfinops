"""Unit tests for the four analytics engines.

The demo dataset is built once (module-scoped) because generating a 24-month,
~55k-row FOCUS estate is not free, and every engine reads the same frame the
app does. These are behaviour tests, not smoke tests: each one asserts a
property the research says the engine must have (interval ordering, WAPE
finiteness, cliff detection, both planted anomalies, penny-accurate allocation).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import allocation
import anomaly
import budget
import finops_core
import focus
import forecast
from connectors.demo import build_demo_dataset


@pytest.fixture(scope="module")
def estate():
    df, budgets, drivers = build_demo_dataset()
    return df, budgets, drivers


@pytest.fixture(scope="module")
def monthly(estate):
    df = estate[0].copy()
    df["period"] = df["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp()
    return df.groupby("period", as_index=False)["EffectiveCost"].sum().rename(columns={"EffectiveCost": "cost"})


# ==========================================================================
# forecast
# ==========================================================================


def test_forecast_returns_24_rows_ordered_and_nonnegative(monthly):
    res = forecast.forecast_spend(monthly, horizon=24, method="auto")
    fc = res.forecast
    assert len(fc) == 24
    # lo95 <= lo80 <= cost <= hi80 <= hi95, and everything >= 0.
    assert (fc["lo95"] <= fc["lo80"] + 1e-6).all()
    assert (fc["lo80"] <= fc["cost"] + 1e-6).all()
    assert (fc["cost"] <= fc["hi80"] + 1e-6).all()
    assert (fc["hi80"] <= fc["hi95"] + 1e-6).all()
    for col in ["cost", "lo80", "hi80", "lo95", "hi95"]:
        assert (fc[col] >= -1e-6).all()


def test_forecast_maturity_and_method(monthly):
    res = forecast.forecast_spend(monthly, horizon=24, method="auto")
    assert res.method in forecast.available_methods()
    assert res.maturity in list(finops_core.FORECAST_VARIANCE_THRESHOLD.keys()) + ["Below Crawl", "Unknown"]


def test_backtest_wape_is_finite_float(monthly):
    bt = forecast.backtest(monthly, method="holt_winters")
    assert bt["folds"] >= 1
    assert isinstance(bt["wape"], float)
    assert np.isfinite(bt["wape"])


def test_every_method_runs(monthly):
    for m in ["naive", "seasonal_naive", "linear", "holt_winters", "sarima", "ensemble"]:
        res = forecast.forecast_spend(monthly, horizon=12, method=m)
        assert len(res.forecast) == 12


def test_commitment_expiry_overlay_adds_a_cliff(estate, monthly):
    df = estate[0]
    res = forecast.forecast_spend(monthly, horizon=24, method="auto")
    overlay = forecast.commitment_expiry_overlay(df, res.forecast)
    assert "cost_with_cliffs" in overlay.columns
    assert "cliff" in overlay.columns
    assert overlay["cliff"].any(), "expected at least one commitment-expiry cliff in a 24-month horizon"
    # The cliff must raise projected spend, not lower it.
    assert overlay["cost_with_cliffs"].sum() > overlay["cost"].sum()


def test_driver_overlay_steps_up_from_the_named_month(monthly):
    res = forecast.forecast_spend(monthly, horizon=24, method="auto")
    target = res.forecast["period"].iloc[6].strftime("%Y-%m")
    adj = [{"period": target, "pct": 0.15, "label": "Meter rollout wave 2"}]
    out = forecast.driver_overlay(res.forecast, adj)
    # Before the wave, unchanged; from the wave on, +15%.
    assert np.allclose(out["cost_adjusted"].iloc[:6], out["cost"].iloc[:6])
    assert out["cost_adjusted"].iloc[6] > out["cost"].iloc[6]


# ==========================================================================
# budget
# ==========================================================================


def test_variance_table_by_cloud(estate):
    df, budgets, _ = estate
    vt = budget.variance_table(df, budgets, by=["cloud"])
    assert len(vt) == 3  # AWS, Azure, GCP
    # Variance$ = Actual - Budget, exactly.
    assert np.allclose(vt["variance_abs"], vt["actual"] - vt["budget"])
    assert set(vt["cloud"]) == {"AWS", "Azure", "GCP"}


def test_year_end_projection(estate, monthly):
    df, budgets, _ = estate
    res = forecast.forecast_spend(monthly, horizon=24, method="auto")
    proj = budget.year_end_projection(df, budgets, res.forecast)
    assert proj["projected_spend"] >= 0
    assert "months_remaining" in proj


def test_run_rate_table(estate):
    df = estate[0]
    rr = budget.run_rate_table(df)
    assert set(rr["cloud"]) == {"AWS", "Azure", "GCP"}
    assert (rr["run_rate_annual"] >= rr["mtd"]).all()


# ==========================================================================
# anomaly
# ==========================================================================


def test_detect_finds_both_planted_spikes(estate):
    df = estate[0]
    flagged = anomaly.detect_by_dimension(df, dim="ServiceCategory")
    hit = set(flagged["ServiceCategory"].astype(str).unique())
    assert "Analytics" in hit, "did not catch the planted Analytics spike"
    assert "Networking" in hit, "did not catch the planted Networking spike"


def test_summarise_returns_typed_points(estate):
    df = estate[0]
    flagged = anomaly.detect_by_dimension(df, dim="ServiceCategory")
    points = anomaly.summarise(flagged)
    assert points
    assert all(p.severity in ("good", "warning", "serious", "critical") for p in points)
    assert all(p.value > p.expected for p in points if p.deviation_pct > 0)


# ==========================================================================
# allocation
# ==========================================================================


def test_allocate_reconciles_to_estate_total(estate):
    df = estate[0]
    policy = allocation.SharedCostPolicy()  # include_untagged=True by default
    alloc = allocation.allocate(df, policy, dim="tag_business_unit")
    estate_total = float(df["EffectiveCost"].sum())
    allocated_total = float(alloc["total_cost"].sum())
    assert abs(allocated_total - estate_total) / estate_total < 0.0001


def test_coverage_report_row_per_canonical_tag(estate):
    df = estate[0]
    cov = allocation.coverage_report(df)
    assert len(cov) == len(focus.CANONICAL_TAGS)
    assert set(cov["tag_key"]) == set(focus.CANONICAL_TAGS)
    assert (cov["status"].isin(["good", "warning", "critical"])).all()


def test_fixed_percentage_policy_validates(estate):
    bad = allocation.SharedCostPolicy(method="fixed_percentage", fixed_percentages={"A": 60, "B": 30})
    assert bad.validate()  # sums to 90, must flag
    good = allocation.SharedCostPolicy(method="fixed_percentage", fixed_percentages={"A": 60, "B": 40})
    assert not good.validate()
