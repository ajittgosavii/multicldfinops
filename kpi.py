"""The KPI formula engine.

Every number a VP sees is computed here, once, from a FOCUS frame. Tabs render;
they never compute. That way "what does Effective Savings Rate mean in this
tool?" has exactly one answer, and it is this file.

Two conventions that matter:

* **Amortized, always.** Executive KPIs read `EffectiveCost`. Blended and
  unblended views make commitment purchases look like lumpy one-off spikes and
  have no place on a leadership dashboard.

* **`ListCost` is the on-demand-equivalent (ODE) denominator.** FOCUS defines
  ListCost as what the same usage would have cost at published on-demand rates.
  That is precisely the ESR denominator the FinOps Foundation specifies.

Sources
-------
Effective Savings Rate    https://www.finops.org/wg/how-to-calculate-effective-savings-rate-esr/
Commitment discount waste https://www.finops.org/wg/percent-commitment-based-discount-waste-playbook/
Unit economics            https://www.finops.org/framework/capabilities/unit-economics/
Allocation                https://www.finops.org/framework/capabilities/allocation/
Forecasting               https://www.finops.org/framework/capabilities/forecasting/
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

COST = "EffectiveCost"
ODE = "ListCost"  # on-demand equivalent

# Service categories where a commitment discount (RI / SP / CUD) can apply.
# Used as the denominator of commitment coverage -- coverage against *all*
# spend, including tax and storage, would flatter the number meaninglessly.
COMMITMENT_ELIGIBLE_CATEGORIES = {
    "Compute",
    "Databases",
    "AI and Machine Learning",
    "Analytics",
}


def _safe_div(num: float, den: float) -> Optional[float]:
    if den in (0, None) or pd.isna(den):
        return None
    return num / den


# ==========================================================================
# Spend + deltas
# ==========================================================================


def total_spend(df: pd.DataFrame, cost_col: str = COST) -> float:
    return float(df[cost_col].sum()) if len(df) else 0.0


def monthly_series(df: pd.DataFrame, cost_col: str = COST) -> pd.Series:
    if not len(df):
        return pd.Series(dtype="float64")
    s = df.copy()
    s["period"] = s["ChargePeriodStart"].dt.to_period("M")
    return s.groupby("period")[cost_col].sum().sort_index()


def mom_delta_pct(df: pd.DataFrame, cost_col: str = COST) -> Optional[float]:
    """(Spend_m - Spend_{m-1}) / Spend_{m-1} * 100"""
    s = monthly_series(df, cost_col)
    if len(s) < 2:
        return None
    prior = float(s.iloc[-2])
    if prior == 0:
        return None
    return (float(s.iloc[-1]) - prior) / prior * 100.0


def yoy_delta_pct(df: pd.DataFrame, cost_col: str = COST) -> Optional[float]:
    """(Spend_m - Spend_{m-12}) / Spend_{m-12} * 100"""
    s = monthly_series(df, cost_col)
    if len(s) < 13:
        return None
    prior = float(s.iloc[-13])
    if prior == 0:
        return None
    return (float(s.iloc[-1]) - prior) / prior * 100.0


def burn_rate(df: pd.DataFrame, cost_col: str = COST) -> Optional[float]:
    """Spend per day over the observed window."""
    if not len(df):
        return None
    start = df["ChargePeriodStart"].min()
    end = df["ChargePeriodEnd"].max()
    days = max((end - start).days, 1)
    return total_spend(df, cost_col) / days


def run_rate_annualised(df: pd.DataFrame, cost_col: str = COST) -> Optional[float]:
    """Daily burn rate x 365. The number a CFO annualises in their head anyway."""
    br = burn_rate(df, cost_col)
    return None if br is None else br * 365.0


# ==========================================================================
# Commitment discounts (RI / Savings Plans / CUDs)
# ==========================================================================


def _eligible(df: pd.DataFrame) -> pd.DataFrame:
    return df[
        (df["ChargeCategory"] == "Usage")
        & (df["ServiceCategory"].isin(COMMITMENT_ELIGIBLE_CATEGORIES))
        # Spot/preemptible is priced dynamically and cannot be committed.
        & (df["PricingCategory"] != "Dynamic")
    ]


def commitment_coverage_pct(df: pd.DataFrame) -> Optional[float]:
    """Spend covered by a commitment / total commitment-eligible spend.

    Measured on the on-demand-equivalent (ListCost) basis, per the Foundation's
    Rate Optimization capability. Target is "as high as is safe" -- typically
    80-90%; 100% coverage of a shrinking estate is a trap.
    """
    e = _eligible(df)
    if not len(e):
        return None
    denom = float(e[ODE].sum())
    covered = float(e.loc[e["CommitmentDiscountId"].notna(), ODE].sum())
    r = _safe_div(covered, denom)
    return None if r is None else r * 100.0


def commitment_utilization_pct(df: pd.DataFrame) -> Optional[float]:
    """Used commitment / total commitment purchased.

    Anything below 100% is money burned. `CommitmentDiscountStatus` is the
    FOCUS column that makes this computable without vendor-specific plumbing:
    'Used' rows drew down the commitment, 'Unused' rows did not.
    """
    c = df[df["CommitmentDiscountId"].notna()]
    if not len(c):
        return None
    used = float(c.loc[c["CommitmentDiscountStatus"] == "Used", COST].sum())
    unused = float(c.loc[c["CommitmentDiscountStatus"] == "Unused", COST].sum())
    r = _safe_div(used, used + unused)
    return None if r is None else r * 100.0


def commitment_waste(df: pd.DataFrame) -> float:
    """Amortized cost of commitments that covered nothing. Pure waste, in dollars."""
    c = df[df["CommitmentDiscountId"].notna()]
    if not len(c):
        return 0.0
    return float(c.loc[c["CommitmentDiscountStatus"] == "Unused", COST].sum())


def commitment_waste_pct(df: pd.DataFrame) -> Optional[float]:
    c = df[df["CommitmentDiscountId"].notna()]
    if not len(c):
        return None
    total = float(c[COST].sum())
    r = _safe_div(commitment_waste(df), total)
    return None if r is None else r * 100.0


def on_demand_equivalent(df: pd.DataFrame) -> float:
    """What this usage would have cost at published on-demand rates."""
    usage = df[df["ChargeCategory"] == "Usage"]
    return float(usage[ODE].sum())


def effective_savings_rate_pct(df: pd.DataFrame, cost_to_achieve: float = 0.0) -> Optional[float]:
    """ESR = (savings - cost to achieve) / on-demand-equivalent spend.

    The Foundation's outcome metric for rate optimization. It collapses
    coverage, utilization, discount depth, Spot and negotiated rates into one
    net number -- which is why it beats reporting coverage alone. 100% coverage
    at 60% utilization is still a bad deal, and only ESR shows that.

    Benchmarks from the ESR working group: median ~0%, 75th percentile ~23%,
    98th percentile ~46%.
    """
    ode = on_demand_equivalent(df)
    if ode == 0:
        return None
    usage = df[df["ChargeCategory"] == "Usage"]
    effective = float(usage[COST].sum())
    savings = ode - effective - cost_to_achieve
    return savings / ode * 100.0


def esr_components(df: pd.DataFrame) -> Dict[str, Optional[float]]:
    """The three-factor decomposition: ESR ~ utilization x coverage x discount.

    Useful for a VP asking *why* the rate is what it is. The product will not
    tie exactly to `effective_savings_rate_pct` because that number also
    absorbs Spot and negotiated-rate savings on uncommitted usage.
    """
    util = commitment_utilization_pct(df)
    cov = commitment_coverage_pct(df)

    committed = df[(df["ChargeCategory"] == "Usage") & (df["CommitmentDiscountId"].notna())]
    discount = None
    if len(committed):
        ode = float(committed[ODE].sum())
        eff = float(committed[COST].sum())
        d = _safe_div(ode - eff, ode)
        discount = None if d is None else d * 100.0

    implied = None
    if None not in (util, cov, discount):
        implied = (util / 100.0) * (cov / 100.0) * (discount / 100.0) * 100.0

    return {
        "utilization_pct": util,
        "coverage_pct": cov,
        "discount_pct": discount,
        "implied_esr_pct": implied,
    }


# ==========================================================================
# Waste
# ==========================================================================


def cost_of_waste(df: pd.DataFrame, usage_waste: float = 0.0) -> float:
    """Cost of Waste = commitment waste + usage waste.

    Commitment waste is computable from FOCUS alone (unused commitment rows).
    Usage waste -- idle volumes, zombie IPs, parked-but-running non-prod --
    requires the detectors in `optimize.py`, which pass their total in here.
    """
    return commitment_waste(df) + usage_waste


def waste_pct(df: pd.DataFrame, usage_waste: float = 0.0) -> Optional[float]:
    total = total_spend(df)
    r = _safe_div(cost_of_waste(df, usage_waste), total)
    return None if r is None else r * 100.0


# ==========================================================================
# Allocation
# ==========================================================================


def allocation_coverage_pct(df: pd.DataFrame, tag: str = "application") -> Optional[float]:
    col = f"tag_{tag}"
    if col not in df.columns or not len(df):
        return None
    total = float(df[COST].sum())
    if total == 0:
        return None
    allocated = float(df.loc[df[col] != "Unallocated", COST].sum())
    return allocated / total * 100.0


def unallocated_pct(df: pd.DataFrame, tag: str = "application") -> Optional[float]:
    c = allocation_coverage_pct(df, tag)
    return None if c is None else 100.0 - c


def chargeback_readiness(coverage_pct: Optional[float]) -> str:
    """Practitioner rule of thumb, not a published Foundation threshold.

    Below ~90% allocation coverage a chargeback invoice is disputable, so most
    enterprises run chargeback on the tagged portion and showback on the rest.
    """
    if coverage_pct is None:
        return "Unknown"
    if coverage_pct >= 95:
        return "Chargeback ready"
    if coverage_pct >= 90:
        return "Chargeback ready (tagged scope)"
    if coverage_pct >= 80:
        return "Hybrid: chargeback tagged, showback remainder"
    return "Showback only"


# ==========================================================================
# Unit economics
# ==========================================================================


def unit_cost(df: pd.DataFrame, driver_value: float, cost_col: str = COST) -> Optional[float]:
    """Total cost / demand driver. The driver is a business quantity --
    customers served, kWh billed, meter reads, work orders -- never a
    technical one, or you have merely renamed the bill."""
    return _safe_div(total_spend(df, cost_col), driver_value)


def unit_cost_series(
    df: pd.DataFrame, drivers: pd.DataFrame, metric: str, cost_col: str = COST
) -> pd.DataFrame:
    """Monthly unit cost against one driver metric."""
    s = df.copy()
    s["period"] = s["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp()
    cost = s.groupby("period", as_index=False)[cost_col].sum().rename(columns={cost_col: "cost"})

    d = drivers[drivers["metric"] == metric][["period", "value"]].copy()
    d["period"] = pd.to_datetime(d["period"])

    out = cost.merge(d, on="period", how="inner")
    out["unit_cost"] = out["cost"] / out["value"].replace(0, np.nan)
    return out


# ==========================================================================
# Budget variance
# ==========================================================================


@dataclass
class Variance:
    actual: float
    budget: float

    @property
    def variance_abs(self) -> float:
        """Positive = overrun."""
        return self.actual - self.budget

    @property
    def variance_pct(self) -> Optional[float]:
        r = _safe_div(self.variance_abs, self.budget)
        return None if r is None else r * 100.0

    @property
    def status(self) -> str:
        v = self.variance_pct
        if v is None:
            return "unknown"
        if v > 10:
            return "critical"
        if v > 2:
            return "serious"
        if v < -10:
            return "warning"  # a large underrun is a planning failure too
        return "good"

    @property
    def direction(self) -> str:
        return "Overrun" if self.variance_abs > 0 else "Underrun"


def projected_year_end(
    actuals_ytd: float, forecast_remaining: float, annual_budget: float
) -> Dict[str, Optional[float]]:
    projected = actuals_ytd + forecast_remaining
    var = projected - annual_budget
    return {
        "projected_spend": projected,
        "variance_abs": var,
        "variance_pct": (_safe_div(var, annual_budget) or 0) * 100.0 if annual_budget else None,
        "direction": "Overrun" if var > 0 else "Underrun",
    }


# ==========================================================================
# Forecast accuracy
# ==========================================================================


def mape(actual: np.ndarray, forecast: np.ndarray) -> Optional[float]:
    """Mean absolute percentage error. Blows up when an actual approaches zero."""
    a, f = np.asarray(actual, float), np.asarray(forecast, float)
    mask = a != 0
    if not mask.any():
        return None
    return float(np.mean(np.abs(a[mask] - f[mask]) / np.abs(a[mask])) * 100.0)


def wape(actual: np.ndarray, forecast: np.ndarray) -> Optional[float]:
    """Weighted absolute percentage error -- dollar-weighted.

    Preferred for cloud spend: a tiny service whose actual rounds to zero
    cannot dominate the score the way it does under MAPE.
    """
    a, f = np.asarray(actual, float), np.asarray(forecast, float)
    den = np.sum(np.abs(a))
    if den == 0:
        return None
    return float(np.sum(np.abs(a - f)) / den * 100.0)


def smape(actual: np.ndarray, forecast: np.ndarray) -> Optional[float]:
    a, f = np.asarray(actual, float), np.asarray(forecast, float)
    den = (np.abs(a) + np.abs(f)) / 2.0
    mask = den != 0
    if not mask.any():
        return None
    return float(np.mean(np.abs(a[mask] - f[mask]) / den[mask]) * 100.0)


# ==========================================================================
# Baseline
# ==========================================================================


def trailing_baseline(df: pd.DataFrame, days: int = 90, cost_col: str = COST) -> Optional[float]:
    """Trailing-N-day average daily spend. The de-facto FinOps review cadence
    is 90 days, which is why that is the default."""
    if not len(df):
        return None
    end = df["ChargePeriodStart"].max()
    start = end - pd.Timedelta(days=days)
    window = df[df["ChargePeriodStart"] > start]
    if not len(window):
        return None
    return float(window[cost_col].sum()) / days


def baseline_drift_pct(df: pd.DataFrame, days: int = 90, recent_days: int = 30) -> Optional[float]:
    """Current run-rate against the trailing baseline.

    The documented decay pattern: optimization gains erode from about month 3
    as new resources land outside the original scope, and by month 6 costs are
    back where they started with extra complexity. This is the number that
    catches it.
    """
    base = trailing_baseline(df, days)
    recent = trailing_baseline(df, recent_days)
    if base in (None, 0) or recent is None:
        return None
    return (recent - base) / base * 100.0


# ==========================================================================
# One-shot executive summary
# ==========================================================================


@dataclass
class ExecutiveKPIs:
    total_spend: float
    mom_pct: Optional[float]
    yoy_pct: Optional[float]
    run_rate: Optional[float]
    esr_pct: Optional[float]
    coverage_pct: Optional[float]
    utilization_pct: Optional[float]
    commitment_waste: float
    cost_of_waste: float
    waste_pct: Optional[float]
    allocation_coverage_pct: Optional[float]
    chargeback_readiness: str
    baseline_drift_pct: Optional[float]


def executive_kpis(df: pd.DataFrame, usage_waste: float = 0.0) -> ExecutiveKPIs:
    return ExecutiveKPIs(
        total_spend=total_spend(df),
        mom_pct=mom_delta_pct(df),
        yoy_pct=yoy_delta_pct(df),
        run_rate=run_rate_annualised(df),
        esr_pct=effective_savings_rate_pct(df),
        coverage_pct=commitment_coverage_pct(df),
        utilization_pct=commitment_utilization_pct(df),
        commitment_waste=commitment_waste(df),
        cost_of_waste=cost_of_waste(df, usage_waste),
        waste_pct=waste_pct(df, usage_waste),
        allocation_coverage_pct=allocation_coverage_pct(df),
        chargeback_readiness=chargeback_readiness(allocation_coverage_pct(df)),
        baseline_drift_pct=baseline_drift_pct(df),
    )
