"""The Applications tab -- spend seen through the lens an app owner recognises.

Executives read the estate by cloud; an engineering director reads it by the
thing they are on the hook for -- an application, its business unit, its
environments. This tab is that pivot, and it is deliberately built so the same
number never appears twice with two different definitions:

* **Identity, not rank, drives colour.** A treemap coloured by cloud lets a
  reader keep AWS/Azure/GCP fixed while they drill applications underneath --
  the survivor of a filter never repaints.
* **Concentration is a first-class KPI.** "How much of the bill is three apps?"
  is the question that decides where a FinOps practice spends its week, so
  top-1 and top-3 share sit in the KPI row, not buried in a chart.
* **Untagged spend is shown, never hidden.** It is the number that gates
  chargeback, so it leads rather than lurks.
* **The per-application drawer runs the same KPI engine on a sub-frame.** An
  app's commitment coverage and ESR are computed by exactly the functions the
  Executive tab uses -- there is one definition of ESR in this product.

Every figure is `EffectiveCost` (amortised); every chart carries a table twin.
"""

from __future__ import annotations

from typing import Dict

import pandas as pd
import streamlit as st

import charts
import kpi
import theme
import ui
from finops_core import DataContext


@st.cache_data(show_spinner=False)
def _aggregates(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """One pass over the (large) frame, cached on its hash.

    Streamlit reruns the whole script on every widget touch and pandas copies
    on nearly every groupby, so the estate-wide rollups are computed once here
    rather than per-panel.
    """
    app_total = (
        df.groupby("tag_application", as_index=False, observed=True)["EffectiveCost"]
        .sum()
        .sort_values("EffectiveCost", ascending=False)
        .reset_index(drop=True)
    )
    bu_total = (
        df.groupby("tag_business_unit", as_index=False, observed=True)["EffectiveCost"]
        .sum()
        .sort_values("EffectiveCost", ascending=False)
        .reset_index(drop=True)
    )
    m = df.copy()
    m["period"] = m["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp()
    by_app_month = (
        m.groupby(["period", "tag_application"], as_index=False, observed=True)["EffectiveCost"]
        .sum()
        .rename(columns={"EffectiveCost": "cost"})
    )
    m["month"] = m["ChargePeriodStart"].dt.to_period("M").astype(str)
    by_app_monthstr = (
        m.groupby(["month", "tag_application"], as_index=False, observed=True)["EffectiveCost"].sum()
    )
    treemap = df[["ProviderName", "tag_application", "EffectiveCost"]].copy()
    treemap["ProviderName"] = treemap["ProviderName"].astype(str)
    treemap["tag_application"] = treemap["tag_application"].astype(str)
    return {
        "app_total": app_total,
        "bu_total": bu_total,
        "by_app_month": by_app_month,
        "by_app_monthstr": by_app_monthstr,
        "treemap": treemap,
    }


def _fold_keep(totals: pd.Series, limit: int = 8) -> list:
    pairs = list(zip(totals.index.astype(str), totals.values.astype(float)))
    kept = theme.fold_tail(pairs, limit=limit)
    return [str(l) for l, _ in kept if l != theme.OTHER_LABEL]


def render(ctx: DataContext) -> None:
    df = ctx.focus_df
    mode = ui.mode()

    if df.empty:
        ui.callout("No charge rows in the current selection. Widen the filters above.")
        return

    agg = _aggregates(df)
    app_total = agg["app_total"]
    total = kpi.total_spend(df)

    # ---------------------------------------------------------------
    # KPI row
    # ---------------------------------------------------------------
    ui.section(
        "Applications and business units",
        f"{ctx.config.organisation} · amortised spend · colour follows the cloud, never its rank",
    )

    real_apps = app_total[app_total["tag_application"].astype(str) != "Unallocated"]
    n_apps = int(real_apps["tag_application"].nunique())
    top1 = float(real_apps["EffectiveCost"].iloc[0]) if len(real_apps) else 0.0
    top3 = float(real_apps["EffectiveCost"].head(3).sum()) if len(real_apps) else 0.0
    untagged_pct = kpi.unallocated_pct(df) or 0.0
    untagged_dollars = total * untagged_pct / 100.0

    ui.tile_row(
        [
            dict(label="Applications", value=f"{n_apps}", sub="distinct, tagged"),
            dict(
                label="Top application share",
                value=ui.pct((top1 / total * 100.0) if total else None),
                sub=str(real_apps["tag_application"].iloc[0]) if len(real_apps) else "",
            ),
            dict(
                label="Concentration (top 3)",
                value=ui.pct((top3 / total * 100.0) if total else None),
                sub="of total spend",
                status="warning" if total and top3 / total > 0.6 else "good",
            ),
            dict(
                label="Untagged spend",
                value=ui.money(untagged_dollars),
                sub=f"{ui.pct(untagged_pct)} of spend",
                status="critical" if untagged_pct > 20 else "warning" if untagged_pct > 10 else "good",
            ),
        ]
    )

    # ---------------------------------------------------------------
    # Treemap -- drillable, cloud-coloured
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Where the money sits", "Cloud, then application. Click a tile to drill; the pathbar walks back up.")
    st.plotly_chart(
        charts.allocation_treemap(
            agg["treemap"], path_cols=["ProviderName", "tag_application"],
            value_col="EffectiveCost", colour_by="ProviderName", mode=mode, height=460,
        ),
        width="stretch",
    )
    tm_table = (
        agg["treemap"].groupby(["ProviderName", "tag_application"], as_index=False, observed=True)["EffectiveCost"]
        .sum().sort_values("EffectiveCost", ascending=False)
    )
    ui.table_view(tm_table, key="apps_treemap", label="Cloud x application table view")

    # ---------------------------------------------------------------
    # Monthly stacked area by application (folded to 8)
    # ---------------------------------------------------------------
    st.divider()
    left, right = st.columns([1.4, 1])

    with left:
        ui.section("Spend by application over time", "Amortised, stacked. The long tail folds into 'Other'.")
        bam = agg["by_app_month"]
        keep = _fold_keep(bam.groupby("tag_application", observed=True)["cost"].sum(), limit=8)
        folded = bam.copy()
        folded["tag_application"] = folded["tag_application"].astype(str).where(
            folded["tag_application"].astype(str).isin(keep), theme.OTHER_LABEL
        )
        folded = folded.groupby(["period", "tag_application"], as_index=False, observed=True)["cost"].sum()
        st.plotly_chart(
            charts.stacked_area(folded, "period", "tag_application", "cost", mode=mode, height=360),
            width="stretch",
        )
        ui.table_view(folded, key="apps_by_month", label="Application-by-month table view")

    with right:
        ui.section("Business units", "Ranked, amortised. One series -> one colour.")
        bu = agg["bu_total"]
        keep_bu = _fold_keep(bu.set_index("tag_business_unit")["EffectiveCost"], limit=8)
        folded_bu = bu.copy()
        folded_bu["tag_business_unit"] = folded_bu["tag_business_unit"].astype(str).where(
            folded_bu["tag_business_unit"].astype(str).isin(keep_bu), theme.OTHER_LABEL
        )
        folded_bu = folded_bu.groupby("tag_business_unit", as_index=False, observed=True)["EffectiveCost"].sum()
        folded_bu = folded_bu.sort_values("EffectiveCost", ascending=False)
        st.plotly_chart(
            charts.ranked_bar(
                folded_bu["tag_business_unit"].astype(str).tolist(),
                folded_bu["EffectiveCost"].tolist(),
                mode=mode, height=360,
            ),
            width="stretch",
        )
        ui.table_view(
            folded_bu.rename(columns={"tag_business_unit": "Business unit", "EffectiveCost": "Cost"}),
            key="apps_bu", label="Business-unit table view",
        )

    # ---------------------------------------------------------------
    # Heatmap -- application x month magnitude
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Application x month intensity", "Sequential blue: one hue, light to dark, encodes magnitude only.")
    bams = agg["by_app_monthstr"]
    keep_h = _fold_keep(bams.groupby("tag_application", observed=True)["EffectiveCost"].sum(), limit=8)
    hm = bams.copy()
    hm["tag_application"] = hm["tag_application"].astype(str).where(
        hm["tag_application"].astype(str).isin(keep_h), theme.OTHER_LABEL
    )
    hm = hm.groupby(["tag_application", "month"], as_index=False, observed=True)["EffectiveCost"].sum()
    matrix = hm.pivot(index="tag_application", columns="month", values="EffectiveCost").fillna(0.0)
    matrix = matrix.reindex(sorted(matrix.columns), axis=1)
    matrix = matrix.loc[matrix.sum(axis=1).sort_values(ascending=False).index]
    if matrix.empty:
        ui.callout("Not enough application-month coverage to draw a heatmap for this selection.")
    else:
        st.plotly_chart(charts.heatmap(matrix, mode=mode, height=420, colourbar_title="EffectiveCost"),
                        width="stretch")
        ui.table_view(matrix.reset_index(), key="apps_heatmap", label="Heatmap table view")

    # ---------------------------------------------------------------
    # Per-application detail -- same KPI engine, on a sub-frame
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Application detail", "Everything below is computed by the shared KPI engine on the chosen app's rows.")

    choices = [a for a in real_apps["tag_application"].astype(str).tolist()]
    if not choices:
        ui.callout("No tagged application in this selection to inspect.")
        return

    app = st.selectbox("Application", choices, key="apps_detail_pick")
    sub = df[df["tag_application"].astype(str) == app]
    if sub.empty:
        ui.callout("No rows for that application in the current slice.")
        return

    sub_total = kpi.total_spend(sub)
    coverage = kpi.commitment_coverage_pct(sub)
    esr = kpi.effective_savings_rate_pct(sub)
    ui.tile_row(
        [
            dict(label="App amortised spend", value=ui.money(sub_total), sub=f"{ui.pct((sub_total/total*100) if total else None)} of estate"),
            dict(label="Commitment coverage", value=ui.pct(coverage), sub="of eligible spend"),
            dict(
                label="Effective savings rate",
                value=ui.pct(esr),
                sub="vs on-demand equivalent",
                status="good" if (esr or 0) >= 23 else "warning" if (esr or 0) >= 10 else "critical",
            ),
        ]
    )

    c1, c2, c3 = st.columns(3)
    for col, (dim, title, key) in zip(
        (c1, c2, c3),
        [("ProviderName", "Cloud split", "apps_d_cloud"),
         ("ServiceCategory", "Service category", "apps_d_cat"),
         ("tag_environment", "Environment", "apps_d_env")],
    ):
        with col:
            ui.section(title)
            g = sub.groupby(dim, as_index=False, observed=True)["EffectiveCost"].sum().sort_values("EffectiveCost", ascending=False)
            keep_g = _fold_keep(g.set_index(dim)["EffectiveCost"], limit=8)
            g[dim] = g[dim].astype(str).where(g[dim].astype(str).isin(keep_g), theme.OTHER_LABEL)
            g = g.groupby(dim, as_index=False, observed=True)["EffectiveCost"].sum().sort_values("EffectiveCost", ascending=False)
            st.plotly_chart(
                charts.ranked_bar(g[dim].astype(str).tolist(), g["EffectiveCost"].tolist(), mode=mode, height=260),
                width="stretch",
            )
            ui.table_view(g.rename(columns={dim: title, "EffectiveCost": "Cost"}), key=key, label=f"{title} table view")

    ui.section("Month-on-month trend", "A single filled series -- so no legend; the title names it.")
    subm = sub.copy()
    subm["period"] = subm["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp()
    trend = subm.groupby(["period"], as_index=False, observed=True)["EffectiveCost"].sum().rename(columns={"EffectiveCost": "cost"})
    trend["application"] = app
    st.plotly_chart(
        charts.stacked_area(trend, "period", "application", "cost", mode=mode, height=280),
        width="stretch",
    )
    ui.table_view(trend[["period", "cost"]], key="apps_d_trend", label="Trend table view")
