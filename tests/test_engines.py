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


# ==========================================================================
# Tags representation
# ==========================================================================


def test_tags_are_json_strings_not_dicts(estate):
    """FOCUS calls Tags a map, rendered as String or JSON depending on the
    emitter -- both conformant. We store the string because a dict column makes
    `st.cache_data` fail `hash_pandas_object` and fall back to pickling on every
    rerun, and makes Arrow refuse the frame outright."""
    import json

    import pandas as pd
    from pandas.core.util.hashing import hash_pandas_object

    df = estate[0]
    assert not df["Tags"].map(lambda v: isinstance(v, dict)).any()
    # Round-trips back to a mapping.
    assert isinstance(json.loads(df["Tags"].iloc[0]), dict)
    # The fast cache-hash path works, i.e. no pickle fallback.
    hash_pandas_object(df.sample(500, random_state=0))


def test_explode_tags_accepts_both_dicts_and_json_strings():
    import pandas as pd

    import focus

    base = focus.empty_frame()
    rows = pd.DataFrame(
        {
            "Tags": [{"application": "CIS"}, '{"application": "OMS"}', None, "not json"],
        }
    )
    out = focus.explode_tags(pd.concat([base, rows], ignore_index=True))
    assert out["tag_application"].tolist() == ["CIS", "OMS", "Unallocated", "Unallocated"]


# ==========================================================================
# Multi-account bindings
# ==========================================================================


def test_bindings_default_to_one_per_cloud():
    """With no `[[accounts]]` block, behaviour is the old single-payer path."""
    from finops_core import AppConfig

    cfg = AppConfig()
    bindings = cfg.bindings()
    assert {b.cloud for b in bindings} == {"AWS", "Azure", "GCP"}
    assert [b.connector for b in bindings] == ["aws_native", "azure_native", "gcp_native"]


def test_multiple_payers_per_cloud_are_supported():
    """A regulated utility has more than one payer; they must not collide."""
    from finops_core import AccountBinding, AppConfig

    cfg = AppConfig(
        accounts=[
            AccountBinding("AWS", "aws_native", "Regulated payer", (("AWS_ACCESS_KEY_ID", "a"),)),
            AccountBinding("AWS", "aws_native", "Unregulated payer", (("AWS_ACCESS_KEY_ID", "b"),)),
            AccountBinding("Azure", "azure_native", "Tenant A", (), (("scope", "providers/x"),)),
        ]
    )
    bindings = cfg.bindings()
    assert len(bindings) == 3
    assert sum(b.cloud == "AWS" for b in bindings) == 2
    # Per-binding credentials differ, so two payers never share a key.
    aws = [b for b in bindings if b.cloud == "AWS"]
    assert aws[0].secret_map["AWS_ACCESS_KEY_ID"] != aws[1].secret_map["AWS_ACCESS_KEY_ID"]
    # Options reach the connector constructor (Azure needs a scope).
    assert bindings[2].option_map == {"scope": "providers/x"}


def test_bindings_are_hashable_for_the_streamlit_cache():
    """`_fetch_live` is `@st.cache_data`, so its arguments must hash."""
    from finops_core import AccountBinding

    b = AccountBinding("AWS", "aws_native", "p", (("K", "v"),), (("scope", "s"),))
    assert hash((b,))


def test_an_anomaly_must_be_both_odd_and_material(estate):
    """`is_anomaly` and `severity` must agree.

    The MAD test alone flags any wobble in a low-variance series -- the scale
    collapses toward zero, so a 5% move scores a z of 6. That produced 347
    "anomalies" on this estate, 318 of them simultaneously graded `good`. A
    flagged row graded `good` is a contradiction, and an alert nobody can act on
    trains people to ignore the channel.
    """
    df = estate[0]
    hits = anomaly.detect_by_dimension(df, dim="ServiceCategory")

    assert not hits.empty
    assert (hits["severity"] != "good").all(), "a flagged row may never be graded 'good'"
    assert (hits["deviation_pct"].abs() >= 25.0).all(), "every anomaly must be materially deviant"
    assert len(hits) < 60, f"{len(hits)} anomalies is noise, not signal"

    # The two deliberately planted spikes must still surface, as critical.
    critical = hits[hits["severity"] == "critical"]
    cats = set(critical["ServiceCategory"])
    assert {"Analytics", "Networking"} <= cats, f"planted spikes missing; got {cats}"


def test_min_deviation_pct_is_tunable(estate):
    df = estate[0]
    strict = anomaly.detect_by_dimension(df, dim="ServiceCategory", min_deviation_pct=100.0)
    loose = anomaly.detect_by_dimension(df, dim="ServiceCategory", min_deviation_pct=10.0)
    assert len(strict) < len(loose)
    assert (strict["severity"] == "critical").all()
