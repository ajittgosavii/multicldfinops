"""The Executive tab -- what a VP or Director sees first.

Design constraints, applied deliberately:

* **One hero number.** Total amortised spend for the scoped period. Everything
  else supports it.
* **Six to eight KPIs, no more.** The FinOps Foundation's tiered-reporting
  guidance is explicit that the executive tier is not the operational tier.
  Effective Savings Rate and allocation coverage sit top-left, because they are
  the two most legible signals of practice health.
* **Forecast against budget, with the band.** An executive reads three things
  off a fan chart: where we are, where we are heading, and where the band
  crosses budget. Not sigma.
* **Every chart has a table twin.** Nothing is reachable only via a tooltip.

Costs are `EffectiveCost` throughout -- amortised. A commitment purchase must
never appear on a leadership dashboard as a one-off spike.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import streamlit as st

import charts
import kpi
import theme
import ui
from finops_core import DataContext, Mode, maturity_for_variance


def _safe(fn, default=None):
    """Engines are optional at import time; a missing one degrades one panel."""
    try:
        return fn()
    except Exception:
        return default


def _usage_waste_monthly(df: pd.DataFrame) -> float:
    """Monthly run-rate of detectable usage waste. `kpi.cost_of_waste` scales it
    to the observed window before adding commitment waste."""
    try:
        import optimize

        return optimize.usage_waste_total(optimize.detect_all(df))
    except Exception:
        return 0.0


@st.cache_data(show_spinner=False)
def _forecast(monthly: pd.DataFrame, horizon: int):
    import forecast

    return forecast.forecast_spend(monthly, horizon=horizon, method="auto")


def render(ctx: DataContext) -> None:
    df = ctx.focus_df
    mode = ui.mode()

    if df.empty:
        ui.callout("No charge rows in the current selection. Widen the filters above.")
        return

    usage_waste_monthly = _usage_waste_monthly(df)
    k = kpi.executive_kpis(df, usage_waste_monthly=usage_waste_monthly)

    # ---------------------------------------------------------------
    # Hero + the KPI row
    # ---------------------------------------------------------------
    ui.section(
        "Executive summary",
        f"{ctx.config.organisation} · amortised spend across "
        f"{', '.join(ctx.clouds)} · {ctx.mode.label}",
    )

    hero, spacer = st.columns([1.15, 3])
    with hero:
        ui.tile(
            "Total amortised spend",
            ui.money(k.total_spend),
            sub=f"Run-rate {ui.money(k.run_rate)}/yr" if k.run_rate else "",
            delta_pct=k.mom_pct,
            good_when="down",
            accent=True,
        )
    with spacer:
        monthly = ctx.monthly()
        if len(monthly) > 2:
            st.plotly_chart(
                charts.sparkline(monthly["cost"].tolist(), mode=mode, height=96),
                width="stretch",
                config={"displayModeBar": False},
            )
            st.caption(
                f"{len(monthly)} months of history · "
                f"YoY {ui.pct(k.yoy_pct) if k.yoy_pct is not None else 'n/a'}"
            )

    st.markdown("")

    esr_status = (
        "good" if (k.esr_pct or 0) >= 23 else "warning" if (k.esr_pct or 0) >= 10 else "critical"
    )
    util_status = (
        "good" if (k.utilization_pct or 0) >= 95 else "warning" if (k.utilization_pct or 0) >= 85 else "critical"
    )
    alloc_status = (
        "good" if (k.allocation_coverage_pct or 0) >= 90
        else "warning" if (k.allocation_coverage_pct or 0) >= 80
        else "critical"
    )
    # Industry surveys put wasted IaaS/PaaS spend at ~27-32%; mature FinOps
    # practices run 20-25%. Anything under 10% is genuinely good, so the tile
    # must be able to say so rather than defaulting to a warning forever.
    waste_status = (
        "critical" if (k.waste_pct or 0) > 20
        else "serious" if (k.waste_pct or 0) > 15
        else "warning" if (k.waste_pct or 0) > 10
        else "good"
    )

    ui.tile_row(
        [
            dict(
                label="Effective savings rate",
                value=ui.pct(k.esr_pct),
                sub="vs on-demand equivalent",
                status=esr_status,
            ),
            dict(
                label="Commitment coverage",
                value=ui.pct(k.coverage_pct),
                sub="of eligible spend",
            ),
            dict(
                label="Commitment utilisation",
                value=ui.pct(k.utilization_pct),
                sub=f"{ui.money(k.commitment_waste)} unused",
                status=util_status,
            ),
            dict(
                label="Cost of waste",
                value=ui.money(k.cost_of_waste),
                sub=f"{ui.pct(k.waste_pct)} of spend",
                status=waste_status,
            ),
            dict(
                label="Allocation coverage",
                value=ui.pct(k.allocation_coverage_pct),
                sub=k.chargeback_readiness,
                status=alloc_status,
            ),
            dict(
                label="Baseline drift",
                value=ui.pct(k.baseline_drift_pct),
                sub="30d run-rate vs 90d baseline",
                delta_pct=k.baseline_drift_pct,
                good_when="down",
            ),
        ]
    )

    ui.callout(
        "**Effective Savings Rate** is the outcome metric, not coverage. "
        "100% coverage at 60% utilisation is still a bad deal, and only ESR shows that. "
        "Foundation benchmarks: median ~0%, 75th percentile ~23%, 98th percentile ~46%."
    )

    # ---------------------------------------------------------------
    # Forecast vs budget
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Two-year forecast against budget",
        "Point forecast with 80% and 95% likely ranges. Where the band crosses "
        "budget is where the conversation starts.",
    )

    monthly = ctx.monthly()
    fc = _safe(lambda: _forecast(monthly, 24))

    if fc is None or fc.forecast.empty:
        ui.callout("Forecast engine unavailable for this selection.")
    else:
        budget_line = _budget_line(ctx, fc)
        fig = charts.forecast_fan(
            history=fc.history,
            forecast=fc.forecast,
            budget=budget_line,
            mode=mode,
            height=420,
        )
        st.plotly_chart(fig, width="stretch")

        c1, c2, c3, c4 = st.columns(4)
        wape = fc.accuracy.get("wape")
        with c1:
            ui.tile("Method", fc.method.replace("_", " ").title(), sub=f"{fc.accuracy.get('folds', 0)} backtest folds")
        with c2:
            ui.tile(
                "Forecast accuracy (WAPE)",
                ui.pct(wape) if wape is not None else "—",
                sub=f"Maturity: {fc.maturity}",
                status="good" if (wape or 99) < 12 else "warning" if (wape or 99) < 20 else "critical",
            )
        with c3:
            proj = _safe(lambda: _year_end(ctx, fc), {})
            ui.tile(
                "Projected year-end",
                ui.money(proj.get("projected_spend")) if proj else "—",
                sub=f"{proj.get('direction', '')} {ui.money(abs(proj.get('variance_abs') or 0))}" if proj else "",
                status="critical" if (proj.get("variance_pct") or 0) > 10 else "good",
            )
        with c4:
            total_24 = float(fc.forecast["cost"].sum())
            ui.tile("Next 24 months", ui.money(total_24), sub="Point forecast, cumulative")

        for note in fc.notes[:3]:
            st.caption(f"· {note}")

        table = pd.concat(
            [
                fc.history.assign(series="Actual"),
                fc.forecast.assign(series="Forecast"),
            ],
            ignore_index=True,
        )
        ui.table_view(table, key="exec_forecast", label="Forecast table view")

    # ---------------------------------------------------------------
    # Where the money is
    # ---------------------------------------------------------------
    st.divider()
    left, right = st.columns([1.35, 1])

    with left:
        ui.section("Spend by cloud over time", "Amortised, stacked. Colour follows the cloud, never its rank.")
        by_cloud = ctx.monthly_by("ProviderName")
        if len(by_cloud):
            st.plotly_chart(
                charts.stacked_area(by_cloud, "period", "ProviderName", "cost", mode=mode, height=330),
                width="stretch",
            )
            ui.table_view(by_cloud, key="exec_by_cloud", label="Cloud spend table view")

    with right:
        ui.section("Top applications", "Trailing period, amortised.")
        app_spend = (
            df.groupby("tag_application", as_index=False, observed=True)["EffectiveCost"]
            .sum()
            .sort_values("EffectiveCost", ascending=False)
        )
        folded = theme.fold_tail(list(zip(app_spend["tag_application"], app_spend["EffectiveCost"])), limit=9)
        labels = [str(l) for l, _ in folded]
        values = [float(v) for _, v in folded]
        st.plotly_chart(
            charts.ranked_bar(labels, values, mode=mode, height=330),
            width="stretch",
        )
        ui.table_view(app_spend.rename(columns={"tag_application": "Application", "EffectiveCost": "Cost"}),
                      key="exec_apps", label="Application table view")

    # ---------------------------------------------------------------
    # Budget variance by cloud
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Budget variance", "Positive is an overrun. Colour is diverging around a neutral zero.")

    var = _safe(lambda: _variance(ctx))
    if var is None or var.empty:
        ui.callout(
            "No budgets are loaded for this selection. "
            + ("Demo Mode seeds them from a plan." if ctx.mode is Mode.DEMO
               else "Live Mode reads them from each cloud's native Budgets API, where one exists.")
        )
    else:
        vc1, vc2 = st.columns([1, 1])
        with vc1:
            st.plotly_chart(
                charts.variance_bars(var["cloud"].tolist(), var["variance_abs"].tolist(), mode=mode, height=300),
                width="stretch",
            )
        with vc2:
            for _, r in var.iterrows():
                st.markdown(
                    f"{ui.status_pill(r['status'], r['cloud'])} &nbsp; "
                    f"**{ui.money(r['actual'])}** actual vs {ui.money(r['budget'])} budget &nbsp; "
                    f"({r['direction']} {ui.money(abs(r['variance_abs']))}, {r['variance_pct']:+.1f}%)",
                    unsafe_allow_html=True,
                )
        ui.table_view(var, key="exec_variance", label="Variance table view")

    # ---------------------------------------------------------------
    # What to do about it
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Largest optimization opportunities", "Detected from the billing data, not from a vendor's API.")

    opps = _safe(lambda: _opportunities(df))
    if opps is None or opps.empty:
        ui.callout("Optimization engine unavailable, or no opportunity clears the $50/month floor.")
    else:
        frame = opps.head(6)
        total_annual = float(opps["annual_savings"].sum())
        st.markdown(
            f"**{ui.money(total_annual)}** identified annualised savings across "
            f"**{opps['lever_id'].nunique()}** levers."
        )
        st.dataframe(
            frame[["lever_id", "lever_name", "cloud", "scope", "annual_savings", "effort", "risk", "confidence"]]
            .rename(
                columns={
                    "lever_id": "Lever",
                    "lever_name": "Name",
                    "cloud": "Cloud",
                    "scope": "Scope",
                    "annual_savings": "Annual savings",
                    "effort": "Effort",
                    "risk": "Risk",
                    "confidence": "Confidence",
                }
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "Annual savings": st.column_config.NumberColumn(format="$%.0f"),
                "Confidence": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.2f"),
            },
        )
        st.caption("Full catalog, evidence and delivery roadmap on the **Optimize** tab.")


# ==========================================================================
# Panel helpers
# ==========================================================================


def _budget_line(ctx: DataContext, fc) -> Optional[pd.DataFrame]:
    """Project the monthly budget across the forecast horizon.

    Where the budget has no rows for a future month -- the normal case beyond
    the current fiscal year -- we carry the last known monthly budget forward
    flat and let the fan chart show the collision. Inventing an escalator would
    hide exactly the thing the executive needs to see.
    """
    if ctx.budgets is None or ctx.budgets.empty:
        return None
    b = ctx.budgets.copy()
    b["period"] = pd.to_datetime(b["period"])
    monthly = b.groupby("period", as_index=False)["budget"].sum().rename(columns={"budget": "cost"})
    if monthly.empty:
        return None

    last = float(monthly["cost"].iloc[-1])
    future = fc.forecast[["period"]].copy()
    future = future[~future["period"].isin(monthly["period"])]
    future["cost"] = last

    return pd.concat([monthly, future], ignore_index=True).sort_values("period")


def _year_end(ctx: DataContext, fc) -> dict:
    import budget as budget_engine

    return budget_engine.year_end_projection(
        ctx.focus_df, ctx.budgets, fc.forecast, ctx.config.fiscal_year_start_month
    )


def _variance(ctx: DataContext) -> pd.DataFrame:
    import budget as budget_engine

    return budget_engine.variance_table(ctx.focus_df, ctx.budgets, by=["cloud"])


@st.cache_data(show_spinner=False)
def _opportunities(df: pd.DataFrame) -> pd.DataFrame:
    import optimize

    return optimize.opportunities_frame(optimize.detect_all(df))
