"""The Unit Economics tab -- cost per unit of business, not per unit of infra.

The point the FinOps Foundation keeps making about this capability, and the one
this tab is built to enforce, is a distinction:

* A **resource-efficiency** metric -- cost per vCPU, cost per GB -- is the cloud
  bill with a different label on it. It moves when you rightsize; it says
  nothing a VP can take to a rate case.
* A **business** metric -- cost per customer served, per kWh delivered, per
  meter read, per work order closed -- is defensible. It ties spend to the
  thing the organisation actually produces.

So the tab is organised around a *driver* the business supplies, and it refuses
to fake one. A cloud bill cannot contain business drivers by definition, so in
Live Mode (`ctx.drivers` empty) it explains exactly what to upload instead of
inventing a denominator.

Two hard rules earn their own panels here:

* **"Cost vs volume" is never a dual axis.** Two measures of different scale are
  both indexed to 100 at t0 and drawn on one axis -- the honest way to show
  whether cost is outrunning demand.
* **A single series carries no legend and is direct-labelled at its endpoint.**

Source: https://www.finops.org/framework/capabilities/unit-economics/
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import charts
import kpi
import theme
import ui
from finops_core import DataContext


@st.cache_data(show_spinner=False)
def _unit_series(df: pd.DataFrame, drivers: pd.DataFrame, metric: str) -> pd.DataFrame:
    return kpi.unit_cost_series(df, drivers, metric)


def _parse_upload(file) -> pd.DataFrame:
    raw = pd.read_csv(file)
    cols = {c.lower().strip(): c for c in raw.columns}
    need = ["period", "metric", "value"]
    if not all(n in cols for n in need):
        return pd.DataFrame(columns=need)
    out = raw.rename(columns={cols["period"]: "period", cols["metric"]: "metric", cols["value"]: "value"})
    out = out[["period", "metric", "value"]].copy()
    out["period"] = pd.to_datetime(out["period"], errors="coerce")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    return out.dropna(subset=["period", "value"])


def render(ctx: DataContext) -> None:
    df = ctx.focus_df
    mode = ui.mode()
    s = theme.surface(mode)

    ui.section("Unit economics", "Cost per unit of business -- the number a VP can defend, not the bill renamed.")
    ui.callout(
        "A resource-efficiency metric (cost per vCPU, cost per GB) is **the bill with a different name on it**. "
        "A business metric -- cost per customer served, per kWh delivered, per meter read, per work order closed -- "
        "is what a VP can defend in a rate case. See the FinOps Foundation's "
        "[Unit Economics capability](https://www.finops.org/framework/capabilities/unit-economics/)."
    )

    if df.empty:
        ui.callout("No charge rows in the current selection. Widen the filters above.")
        return

    # ---------------------------------------------------------------
    # Driver source -- demo carries them; live cannot, by definition
    # ---------------------------------------------------------------
    drivers = ctx.drivers
    if drivers is None or drivers.empty:
        ui.section("No business drivers in scope")
        ui.callout(
            "A cloud bill cannot contain business drivers -- so Live Mode has none until you supply them. "
            "Upload a CSV with columns **period, metric, value** (one row per metric per month), for example "
            "`2026-01-01, Customers served, 3600000`. Every metric you provide becomes selectable below."
        )
        up = st.file_uploader("Business drivers CSV (period, metric, value)", type=["csv"], key="ue_upload")
        if up is None:
            return
        drivers = _parse_upload(up)
        if drivers.empty:
            ui.callout(f"{ui.status_pill('warning', 'Upload')} &nbsp; Could not find period/metric/value rows in that file.")
            return
        st.caption(f"Loaded {len(drivers)} driver rows across {drivers['metric'].nunique()} metric(s).")

    metrics = sorted(drivers["metric"].dropna().astype(str).unique().tolist())
    if not metrics:
        ui.callout("The driver frame has no usable metric column.")
        return

    metric = st.selectbox("Business driver", metrics, key="ue_metric")
    series = _unit_series(df, drivers, metric)
    if series.empty or series["unit_cost"].dropna().empty:
        ui.callout("No overlapping months between the cost data and that driver in the current slice.")
        return

    series = series.sort_values("period").reset_index(drop=True)
    unit = series["unit_cost"]

    # ---------------------------------------------------------------
    # KPI row
    # ---------------------------------------------------------------
    current = float(unit.iloc[-1])
    mom = ((unit.iloc[-1] - unit.iloc[-2]) / unit.iloc[-2] * 100.0) if len(unit) >= 2 and unit.iloc[-2] else None
    yoy = ((unit.iloc[-1] - unit.iloc[-13]) / unit.iloc[-13] * 100.0) if len(unit) >= 13 and unit.iloc[-13] else None
    best_i = int(unit.idxmin())
    best_val = float(unit.iloc[best_i])
    best_month = pd.Timestamp(series["period"].iloc[best_i]).strftime("%b %Y")

    ui.tile_row(
        [
            dict(label="Current unit cost", value=ui.money(current, decimals=2), sub=f"per {metric.lower()}", accent=True),
            dict(label="MoM change", value=ui.pct(mom), sub="vs prior month", delta_pct=mom, good_when="down"),
            dict(label="YoY change", value=ui.pct(yoy), sub="vs same month last year", delta_pct=yoy, good_when="down"),
            dict(label="Best-ever unit cost", value=ui.money(best_val, decimals=2), sub=best_month, status="good"),
        ]
    )

    # ---------------------------------------------------------------
    # Chart 1 -- unit cost over time (single series, endpoint-labelled)
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Unit cost over time", "One series, so no legend. Only the endpoint is labelled.")

    fig1 = go.Figure()
    fig1.update_layout(**charts.base_layout(mode, height=330, showlegend=False))
    fig1.update_yaxes(tickprefix="$")
    fig1.add_trace(go.Scatter(
        x=series["period"], y=unit, mode="lines",
        line=dict(color=s.categorical[0], width=charts.LINE_WIDTH),
        hovertemplate="%{x|%b %Y}  $%{y:,.4f}<extra></extra>",
    ))
    last = series.iloc[-1]
    fig1.add_trace(go.Scatter(
        x=[last["period"]], y=[last["unit_cost"]], mode="markers+text",
        marker=dict(size=charts.MARKER_SIZE, color=s.categorical[0], line=dict(width=charts.RING_WIDTH, color=s.surface)),
        text=[f"  ${last['unit_cost']:,.2f}"], textposition="middle right",
        textfont=dict(color=s.text_primary, size=11), showlegend=False, hoverinfo="skip",
    ))
    st.plotly_chart(fig1, use_container_width=True)
    ui.table_view(series, key="ue_unit_series", label="Unit cost table view")

    # ---------------------------------------------------------------
    # Chart 2 -- cost vs volume, both indexed to 100 at t0 (one axis)
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Cost vs demand, indexed to 100", "The correct answer to 'show me cost against volume' -- one axis, never a dual scale.")

    base_cost = float(series["cost"].iloc[0]) or 1.0
    base_vol = float(series["value"].iloc[0]) or 1.0
    idx = pd.DataFrame({
        "period": series["period"],
        "Cost": series["cost"] / base_cost * 100.0,
        "Driver volume": series["value"] / base_vol * 100.0,
    })
    cmap = theme.colour_map(["Cost", "Driver volume"], mode)
    fig2 = go.Figure()
    fig2.update_layout(**charts.base_layout(mode, height=330, showlegend=True))
    for name in ["Cost", "Driver volume"]:
        fig2.add_trace(go.Scatter(
            x=idx["period"], y=idx[name], mode="lines", name=name,
            line=dict(color=cmap[name], width=charts.LINE_WIDTH),
            hovertemplate=name + "  %{y:.1f}<extra></extra>",
        ))
    st.plotly_chart(fig2, use_container_width=True)
    st.caption("Cost above volume means the unit cost is rising -- you are paying more per unit of business delivered.")
    ui.table_view(idx, key="ue_indexed", label="Indexed cost vs volume table view")

    # ---------------------------------------------------------------
    # Per-application unit cost for the chosen driver
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Unit cost by application", "Total cost allocated by application share, divided by the latest driver value.")

    latest_period = series["period"].iloc[-1]
    latest_val = float(series["value"].iloc[-1])
    dfm = df.copy()
    dfm["period"] = dfm["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp()
    latest_rows = dfm[dfm["period"] == latest_period]
    if latest_rows.empty or latest_val <= 0:
        ui.callout("No cost rows in the latest driver month for this slice.")
    else:
        by_app = (
            latest_rows.groupby("tag_application", as_index=False, observed=True)["EffectiveCost"]
            .sum().rename(columns={"EffectiveCost": "cost"})
        )
        by_app = by_app[by_app["tag_application"].astype(str) != "Unallocated"]
        by_app["unit_cost"] = by_app["cost"] / latest_val
        by_app = by_app.sort_values("cost", ascending=False).reset_index(drop=True)
        by_app = by_app.rename(columns={"tag_application": "Application", "cost": f"Cost ({latest_period:%b %Y})", "unit_cost": f"$ per {metric.lower()}"})
        st.dataframe(
            by_app, use_container_width=True, hide_index=True,
            column_config={
                f"Cost ({latest_period:%b %Y})": st.column_config.NumberColumn(format="$%.0f"),
                f"$ per {metric.lower()}": st.column_config.NumberColumn(format="$%.4f"),
            },
        )

    # ---------------------------------------------------------------
    # AI unit economics -- token/credit spend in the FOCUS schema
    # ---------------------------------------------------------------
    st.divider()
    ui.section("AI unit economics", "Cost of the AI and Machine Learning category, and an implied cost per 1k units where quantity is present.")

    ai = df[df["ServiceCategory"].astype(str) == "AI and Machine Learning"].copy()
    if ai.empty:
        ui.callout("No AI and Machine Learning spend in the current slice.")
    else:
        ai["period"] = ai["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp()
        agg_cols = {"EffectiveCost": "sum"}
        has_qty = "ConsumedQuantity" in ai.columns and ai["ConsumedQuantity"].notna().any()
        if has_qty:
            agg_cols["ConsumedQuantity"] = "sum"
        ai_m = ai.groupby("period", as_index=False, observed=True).agg(agg_cols).rename(columns={"EffectiveCost": "cost"})
        ai_m = ai_m.sort_values("period")
        if has_qty:
            ai_m["cost_per_1k"] = ai_m["cost"] / (ai_m["ConsumedQuantity"].replace(0, pd.NA) / 1000.0)

        colc, colt = st.columns([1.4, 1])
        with colc:
            fig3 = go.Figure()
            fig3.update_layout(**charts.base_layout(mode, height=280, showlegend=False))
            fig3.update_yaxes(tickprefix="$")
            fig3.add_trace(go.Scatter(
                x=ai_m["period"], y=ai_m["cost"], mode="lines",
                line=dict(color=s.categorical[3], width=charts.LINE_WIDTH),
                hovertemplate="%{x|%b %Y}  $%{y:,.0f}<extra></extra>",
            ))
            st.plotly_chart(fig3, use_container_width=True)
        with colt:
            if has_qty and ai_m["cost_per_1k"].notna().any():
                latest_cp = float(ai_m["cost_per_1k"].dropna().iloc[-1])
                # The unit lives on the raw charge rows; the aggregate never carries it.
                units = "units"
                if "ConsumedUnit" in ai.columns and ai["ConsumedUnit"].notna().any():
                    units = str(ai["ConsumedUnit"].dropna().iloc[-1])
                ui.tile(
                    "Implied cost per 1k units",
                    ui.money(latest_cp, decimals=2),
                    sub=f"latest month, per 1k {units}",
                )
            else:
                ui.tile("Implied cost per 1k units", "—", sub="no consumed quantity on AI rows")
        ui.table_view(ai_m, key="ue_ai", label="AI unit economics table view")
        st.caption(
            "FOCUS 1.2 added virtual-currency support (tokens, credits, DBUs), which is what makes token spend "
            "expressible in the standard schema rather than a vendor-specific side table."
        )
