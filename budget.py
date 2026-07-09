"""Budget variance, bridge, projection and burn-down.

Finance's view of the estate. Every number here answers one of four questions a
CFO actually asks: Are we over or under, and by how much? Why? Where will we
land at year end? And are we pacing to plan? The formulas are deliberately the
textbook ones -- a variance report that invents its own arithmetic is worse
than useless, because no one can reconcile it to the general ledger.

Formulas (fixed, do not embellish):
    Variance$      = Actual - Budget            (positive = overrun)
    Variance%      = (Actual - Budget) / Budget * 100
    Burn rate      = spend / elapsed days
    Run-rate       = daily burn * 365
    Projected YE   = Actuals_YTD + Forecast_remaining
    Attainment %   = Actual / Budget * 100

Status thresholds come from `kpi.Variance`, so a budget cell and an executive
KPI can never disagree about what "critical" means.

Source: https://www.finops.org/framework/capabilities/budgeting/
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import kpi

COST = "EffectiveCost"

# Map a FOCUS ProviderName / tag column onto the `by` vocabulary the caller uses.
_DIM_COLUMN = {
    "cloud": "ProviderName",
    "application": "tag_application",
    "business_unit": "tag_business_unit",
    "period": "period",
}
# The budget frame names the same dimensions differently (it predates the tags).
_BUDGET_COLUMN = {
    "cloud": "cloud",
    "application": "application",
    "business_unit": None,   # budgets are not struck at BU grain in the demo
    "period": "period",
}


def _actuals(focus_df: pd.DataFrame) -> pd.DataFrame:
    """Usage-only actuals at month grain, with a `period` column.

    Purchases, tax and credits are excluded: a budget is set against consumption,
    and a lumpy commitment purchase would swamp the month it lands in even though
    its value is amortized across the year.
    """
    if focus_df is None or not len(focus_df):
        return pd.DataFrame(columns=["period", "ProviderName", "tag_application", "tag_business_unit", COST])
    df = focus_df[focus_df["ChargeCategory"] == "Usage"].copy()
    df["period"] = df["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp()
    return df


def variance_table(focus_df: pd.DataFrame, budgets: pd.DataFrame, by: List[str]) -> pd.DataFrame:
    """Actual vs budget by any of ['cloud','application','business_unit','period'].

    Returns the grouping dimensions plus actual, budget, variance_abs,
    variance_pct, direction and status. `status`/`direction` reuse
    `kpi.Variance`, so thresholds are defined in exactly one place.
    """
    cols = ["actual", "budget", "variance_abs", "variance_pct", "direction", "status"]
    if not by:
        return pd.DataFrame(columns=cols)

    act = _actuals(focus_df)
    act_dims = [_DIM_COLUMN.get(b, b) for b in by]
    if act is None or not len(act):
        actual = pd.DataFrame(columns=by + ["actual"])
    else:
        actual = act.groupby(act_dims, as_index=False, observed=True)[COST].sum()
        actual = actual.rename(columns={COST: "actual", **{_DIM_COLUMN.get(b, b): b for b in by}})

    # Budget, aggregated to the same grain where the budget frame supports it.
    bud_dims = [_BUDGET_COLUMN.get(b) for b in by]
    if budgets is None or not len(budgets) or any(d is None for d in bud_dims):
        budget = pd.DataFrame(columns=by + ["budget"])
    else:
        b = budgets.copy()
        if "period" in b.columns:
            b["period"] = pd.to_datetime(b["period"]).dt.to_period("M").dt.to_timestamp()
        budget = b.groupby(bud_dims, as_index=False, observed=True)["budget"].sum()
        budget = budget.rename(columns={_BUDGET_COLUMN[b_]: b_ for b_ in by if _BUDGET_COLUMN.get(b_)})

    out = actual.merge(budget, on=by, how="outer") if len(budget) else actual.assign(budget=np.nan)
    if "actual" not in out.columns:
        out["actual"] = 0.0
    if "budget" not in out.columns:
        out["budget"] = np.nan
    out["actual"] = out["actual"].fillna(0.0)
    out["budget"] = out["budget"].fillna(0.0)

    def _row(r) -> pd.Series:
        v = kpi.Variance(actual=float(r["actual"]), budget=float(r["budget"]))
        return pd.Series(
            {"variance_abs": v.variance_abs, "variance_pct": v.variance_pct,
             "direction": v.direction, "status": v.status}
        )

    metrics = out.apply(_row, axis=1)
    out = pd.concat([out, metrics], axis=1)
    return out[by + cols].sort_values(by).reset_index(drop=True)


def bridge(budget_total: float, drivers: Dict[str, float]) -> Tuple[List[str], List[float]]:
    """Waterfall from Budget to Forecast.

    Returns (labels, deltas) ready for a waterfall chart. The first bar is the
    absolute budget; each driver is a signed delta; the final bar is the
    resulting forecast total. A waterfall is the only chart that answers "why is
    the forecast above budget?" in one read.
    """
    labels = ["Budget"] + list(drivers.keys()) + ["Forecast"]
    forecast_total = float(budget_total) + float(sum(drivers.values()))
    deltas = [float(budget_total)] + [float(v) for v in drivers.values()] + [forecast_total]
    return labels, deltas


def year_end_projection(
    focus_df: pd.DataFrame,
    budgets: pd.DataFrame,
    forecast: pd.DataFrame,
    fiscal_year_start_month: int = 1,
) -> dict:
    """Projected full-year spend against the annual budget.

    Projected YE = actuals booked so far this fiscal year + the forecast for the
    fiscal months still to come. Uses `kpi.projected_year_end` for the variance
    arithmetic so the projection and the KPI card agree.
    """
    act = _actuals(focus_df)
    if act is None or not len(act):
        return {"projected_spend": 0.0, "annual_budget": 0.0, "variance_abs": 0.0,
                "variance_pct": None, "direction": "Underrun", "months_remaining": 0}

    last_period = act["period"].max()
    fy_start = pd.Timestamp(year=last_period.year, month=fiscal_year_start_month, day=1)
    if last_period < fy_start:  # we are in the tail of the prior fiscal year
        fy_start = pd.Timestamp(year=last_period.year - 1, month=fiscal_year_start_month, day=1)
    fy_end = fy_start + pd.DateOffset(months=12)

    ytd = float(act.loc[(act["period"] >= fy_start) & (act["period"] <= last_period), COST].sum())

    fc = forecast if forecast is not None else pd.DataFrame(columns=["period", "cost"])
    remaining = 0.0
    months_remaining = 0
    if len(fc):
        f = fc.copy()
        f["period"] = pd.to_datetime(f["period"]).dt.to_period("M").dt.to_timestamp()
        in_fy = f[(f["period"] > last_period) & (f["period"] < fy_end)]
        remaining = float(in_fy["cost"].sum())
        months_remaining = int(len(in_fy))

    # Annual budget: the fiscal-year slice of the budget frame, else scale YTD.
    annual_budget = 0.0
    if budgets is not None and len(budgets):
        b = budgets.copy()
        b["period"] = pd.to_datetime(b["period"]).dt.to_period("M").dt.to_timestamp()
        annual_budget = float(b.loc[(b["period"] >= fy_start) & (b["period"] < fy_end), "budget"].sum())

    proj = kpi.projected_year_end(ytd, remaining, annual_budget)
    return {
        "projected_spend": proj["projected_spend"],
        "annual_budget": annual_budget,
        "variance_abs": proj["variance_abs"],
        "variance_pct": proj["variance_pct"],
        "direction": proj["direction"],
        "months_remaining": months_remaining,
    }


def burn_down(focus_df: pd.DataFrame, budgets: pd.DataFrame) -> pd.DataFrame:
    """Cumulative actual vs cumulative budget over the observed months.

    `pace` is cumulative_actual / cumulative_budget: > 1 means spending ahead of
    plan. A burn-down line that crosses the budget line before year end is the
    earliest visual warning finance gets.
    """
    cols = ["period", "cumulative_actual", "cumulative_budget", "pace"]
    act = _actuals(focus_df)
    if act is None or not len(act):
        return pd.DataFrame(columns=cols)

    a = act.groupby("period", as_index=False, observed=True)[COST].sum().rename(columns={COST: "actual"})
    if budgets is not None and len(budgets):
        b = budgets.copy()
        b["period"] = pd.to_datetime(b["period"]).dt.to_period("M").dt.to_timestamp()
        bud = b.groupby("period", as_index=False, observed=True)["budget"].sum()
    else:
        bud = pd.DataFrame({"period": a["period"], "budget": 0.0})

    out = a.merge(bud, on="period", how="left").sort_values("period")
    out["budget"] = out["budget"].fillna(0.0)
    out["cumulative_actual"] = out["actual"].cumsum()
    out["cumulative_budget"] = out["budget"].cumsum()
    out["pace"] = np.where(
        out["cumulative_budget"] > 0, out["cumulative_actual"] / out["cumulative_budget"], np.nan
    )
    return out[cols].reset_index(drop=True)


def run_rate_table(focus_df: pd.DataFrame) -> pd.DataFrame:
    """Per-cloud month-to-date, daily burn rate and annualised run-rate.

    MTD is the spend inside the latest observed month; the daily burn rate
    divides it by elapsed days in that month; the run-rate annualises. This is
    the "what are we on track to spend" number, split by provider.
    """
    cols = ["cloud", "mtd", "burn_rate_daily", "run_rate_annual"]
    act = _actuals(focus_df)
    if act is None or not len(act):
        return pd.DataFrame(columns=cols)

    last_period = act["period"].max()
    mtd_rows = act[act["period"] == last_period].copy()
    # Elapsed days = latest charge day within the month, floored at 1.
    day_of_month = mtd_rows["ChargePeriodStart"].dt.day
    elapsed = max(int(day_of_month.max()), 1) if len(day_of_month) else 1

    rows = []
    for cloud, g in mtd_rows.groupby("ProviderName", observed=True):
        mtd = float(g[COST].sum())
        daily = mtd / elapsed
        rows.append({"cloud": str(cloud), "mtd": mtd, "burn_rate_daily": daily,
                     "run_rate_annual": daily * 365.0})
    return pd.DataFrame(rows, columns=cols).sort_values("cloud").reset_index(drop=True)
