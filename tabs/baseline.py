"""The Baseline tab -- an early-warning line for optimisation decay.

There is a documented pattern in cloud cost work: the savings from an
optimisation push start to erode around month three, as new resources land
outside the original scope, and by month six the bill is back where it started
-- now with more complexity to manage. Nobody notices, because no single day
looks wrong. What catches it is the drift of the *recent run-rate* away from a
*trailing baseline*, which is exactly the number this tab leads with.

Design choices that follow from that purpose:

* **Two windows, one comparison.** A trailing baseline (90 days by default,
  the FinOps review cadence) versus a short recent window (30 days). The KPI is
  their percentage gap; the annualised dollar impact makes it a budget number.
* **Never a dual axis.** The chart is one measure -- daily spend -- with the
  baseline drawn as a muted horizontal threshold and the recent window shaded.
* **Seasonality is opt-in and honest.** A utility's estate genuinely breathes
  with the seasons, so an unadjusted drift can cry wolf. The adjustment divides
  each day by a month-of-year index; it is disabled outright when there is less
  than 13 months of history to build that index from, and it says so.

Every figure comes from `kpi.py`; the chart is assembled from `charts.base_layout`.
"""

from __future__ import annotations

from typing import Dict

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import charts
import kpi
import theme
import ui
from finops_core import DataContext


@st.cache_data(show_spinner=False)
def _daily(df: pd.DataFrame) -> pd.DataFrame:
    d = (
        df.groupby(df["ChargePeriodStart"].dt.normalize(), observed=True)["EffectiveCost"]
        .sum()
        .reset_index()
    )
    d.columns = ["date", "cost"]
    return d.sort_values("date").reset_index(drop=True)


@st.cache_data(show_spinner=False)
def _seasonal_index(df: pd.DataFrame) -> Dict[int, float]:
    """Month-of-year multiplier: that calendar month's mean daily rate over the
    estate's overall mean daily rate. Uses monthly rollups so daily- and
    monthly-grain history contribute on the same footing."""
    m = df.copy()
    m["p"] = m["ChargePeriodStart"].dt.to_period("M")
    tot = m.groupby("p", observed=True)["EffectiveCost"].sum()
    rate = pd.Series({p: float(v) / p.days_in_month for p, v in tot.items()})
    overall = float(rate.mean()) if len(rate) else 0.0
    if overall <= 0:
        return {}
    by_month = rate.groupby([p.month for p in rate.index]).mean()
    return {int(mo): float(v / overall) for mo, v in by_month.items()}


@st.cache_data(show_spinner=False)
def _drift_by(df: pd.DataFrame, dimcol: str, base_days: int, recent_days: int) -> pd.DataFrame:
    rows = []
    for entity, g in df.groupby(dimcol, observed=True):
        drift = kpi.baseline_drift_pct(g, days=base_days, recent_days=recent_days)
        if drift is None:
            continue
        rows.append({"entity": str(entity), "drift_pct": drift,
                     "recent_daily": kpi.trailing_baseline(g, recent_days),
                     "baseline_daily": kpi.trailing_baseline(g, base_days)})
    out = pd.DataFrame(rows)
    return out.sort_values("drift_pct", ascending=False).reset_index(drop=True) if len(out) else out


def _severity(drift: float) -> str:
    if drift > 15:
        return "critical"
    if drift > 8:
        return "serious"
    if drift > 3:
        return "warning"
    return "good"


def _n_months(df: pd.DataFrame) -> int:
    return int(df["ChargePeriodStart"].dt.to_period("M").nunique())


def render(ctx: DataContext) -> None:
    df = ctx.focus_df
    mode = ui.mode()
    s = theme.surface(mode)

    if df.empty:
        ui.callout("No charge rows in the current selection. Widen the filters above.")
        return

    ui.section("Run-rate against a trailing baseline", "The early-warning signal for optimisation decay.")

    c1, c2, c3 = st.columns([1, 1, 1.2])
    with c1:
        base_days = st.selectbox("Baseline window (days)", [30, 60, 90, 180], index=2, key="bl_base")
    with c2:
        recent_days = st.selectbox("Recent window (days)", [7, 14, 30], index=2, key="bl_recent")
    with c3:
        enough = _n_months(df) >= 13
        adjust = st.toggle("Seasonally adjust", value=False, key="bl_adjust", disabled=not enough)
        if not enough:
            st.caption("Needs >= 13 months of history to build a month-of-year index. Disabled for this slice.")

    baseline_daily = kpi.trailing_baseline(df, days=base_days)
    recent_daily = kpi.trailing_baseline(df, days=recent_days)
    drift = kpi.baseline_drift_pct(df, days=base_days, recent_days=recent_days)
    annual_impact = ((recent_daily or 0) - (baseline_daily or 0)) * 365.0

    ui.tile_row(
        [
            dict(label=f"Trailing baseline ({base_days}d)", value=ui.money(baseline_daily) + "/day", sub="average daily spend"),
            dict(label=f"Current run-rate ({recent_days}d)", value=ui.money(recent_daily) + "/day", sub="recent average daily spend"),
            dict(
                label="Baseline drift", value=ui.pct(drift), sub="recent vs baseline",
                delta_pct=drift, good_when="down",
                status=_severity(drift or 0),
            ),
            dict(
                label="Annualised impact", value=ui.money(annual_impact),
                sub="if the drift holds for a year",
                status="critical" if annual_impact > 0 else "good",
            ),
        ]
    )

    ui.callout(
        "Optimisation gains typically erode from about **month 3** as new resources land outside the original "
        "scope, and by **month 6** costs are back to pre-optimisation levels with added complexity. Drift is the "
        "early-warning signal; the remedy is a recurring **30-90 day variance review**, not a one-off cleanup."
    )

    # ---------------------------------------------------------------
    # Daily spend with baseline rule + shaded recent window
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Daily spend, baseline and recent window", "One measure, one axis. The baseline is a muted threshold; the recent window is shaded.")

    daily = _daily(df)
    if daily.empty or baseline_daily is None:
        ui.callout("Not enough dated spend to draw a baseline for this selection.")
        return

    end = daily["date"].max()
    plot_cut = end - pd.Timedelta(days=base_days)
    plotd = daily[daily["date"] > plot_cut].copy()
    recent_cut = end - pd.Timedelta(days=recent_days)

    idx = _seasonal_index(df) if adjust else {}
    if adjust and idx:
        plotd["adj"] = plotd.apply(lambda r: r["cost"] / idx.get(r["date"].month, 1.0), axis=1)

    fig = go.Figure()
    fig.update_layout(**charts.base_layout(mode, height=360, showlegend=adjust and bool(idx)))
    fig.update_yaxes(tickprefix="$")
    fig.add_vrect(x0=recent_cut, x1=end, fillcolor=s.categorical[0], opacity=0.08, line_width=0, layer="below")
    fig.add_hline(
        y=baseline_daily, line=dict(color=s.text_muted, width=charts.LINE_WIDTH, dash="dot"),
        annotation_text=f"baseline {ui.money(baseline_daily)}/day", annotation_position="top left",
        annotation_font_color=s.text_muted,
    )
    fig.add_trace(go.Scatter(
        x=plotd["date"], y=plotd["cost"], mode="lines", name="Daily spend",
        line=dict(color=s.categorical[0], width=charts.LINE_WIDTH),
        hovertemplate="%{x|%d %b}  $%{y:,.0f}<extra></extra>",
    ))
    if adjust and idx:
        fig.add_trace(go.Scatter(
            x=plotd["date"], y=plotd["adj"], mode="lines", name="Seasonally adjusted",
            line=dict(color=s.categorical[2], width=charts.LINE_WIDTH),
            hovertemplate="%{x|%d %b}  $%{y:,.0f}<extra></extra>",
        ))
    st.plotly_chart(fig, use_container_width=True)
    ui.table_view(plotd, key="bl_daily", label="Daily spend table view")

    if adjust and idx:
        adj_base = float(plotd["adj"].tail(base_days).mean())
        adj_recent = float(plotd["adj"].tail(recent_days).mean())
        adj_drift = (adj_recent - adj_base) / adj_base * 100.0 if adj_base else None
        a1, a2 = st.columns(2)
        with a1:
            ui.tile("Raw drift", ui.pct(drift), sub="not season-corrected", status=_severity(drift or 0))
        with a2:
            ui.tile("Seasonally-adjusted drift", ui.pct(adj_drift), sub="month-of-year index removed", status=_severity(adj_drift or 0))
        st.caption("If the two differ materially, the raw signal is partly seasonal -- read the adjusted number.")
    elif not enough:
        st.caption("Seasonal adjustment unavailable: fewer than 13 months of history in this slice.")

    # ---------------------------------------------------------------
    # Per-cloud and per-application drift
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Where the drift is", "Sorted by drift, worst first. Severity ships with an icon and a label, never colour alone.")

    lft, rgt = st.columns(2)
    for col, (dimcol, title, key) in zip(
        (lft, rgt),
        [("ProviderName", "By cloud", "bl_cloud"), ("tag_application", "By application", "bl_app")],
    ):
        with col:
            ui.section(title)
            tbl = _drift_by(df, dimcol, base_days, recent_days)
            if tbl is None or tbl.empty:
                ui.callout("No entity clears a computable baseline in this slice.")
                continue
            for _, r in tbl.head(10).iterrows():
                st.markdown(
                    f"{ui.status_pill(_severity(r['drift_pct']), r['entity'])} &nbsp; "
                    f"**{r['drift_pct']:+.1f}%** &nbsp; "
                    f"({ui.money(r['recent_daily'])}/day vs {ui.money(r['baseline_daily'])}/day)",
                    unsafe_allow_html=True,
                )
            ui.table_view(tbl.rename(columns={"entity": title.replace("By ", "").title()}), key=key, label=f"{title} table view")
