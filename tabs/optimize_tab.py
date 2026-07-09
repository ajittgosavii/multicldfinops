"""FinOps optimization levers and the opportunity backlog.

Why this tab is shaped the way it is:

* **Detected from the bill, not scraped from a vendor API.** Every opportunity
  is derived from FOCUS spec columns, so the same rule fires identically on AWS,
  Azure, GCP, or any third-party cost-tool export. A native recommendations API
  is a complement, never the substitute -- it stops at the edge of the cloud
  that emitted it and is silent for a customer whose only feed is a cost tool.

* **Confidence is a first-class column, and low confidence is stated out loud.**
  The billing record proves *what was charged*, not *how hard a resource
  worked*. The two things FOCUS proves outright -- unused commitment and the
  zero-consumption idle signature -- carry high confidence; everything that
  leans on unseen telemetry (utilisation, access patterns, interruption
  tolerance) is deliberately hedged, and the evidence panel says what would
  confirm it.

* **Savings percentages are vendor "up-to" ceilings, not guarantees.** They are
  captioned as such. The detectors already apply conservative haircuts to the
  published ceilings; the catalog shows the raw ranges so a challenged number
  traces back to the provider that quoted it.

* **The backlog is sequenced into waves.** Wave 1 is the quick wins -- low
  effort and low risk -- plus the governance prerequisites that gate everything
  downstream. You cannot optimise what you cannot attribute.

This tab renders; the `optimize` and `kpi` engines compute.
"""

from __future__ import annotations

from typing import List

import pandas as pd
import streamlit as st

import charts
import kpi
import ui
from finops_core import DataContext


@st.cache_data(show_spinner=False)
def _detect(focus_df: pd.DataFrame):
    """Run every detector once. Returns the opportunity frame plus the rollups
    the panels need; the raw objects are re-derived for the evidence drill-down."""
    import optimize

    return optimize.detect_all(focus_df)


def render(ctx: DataContext) -> None:
    df = ctx.focus_df
    mode = ui.mode()

    if df.empty:
        ui.callout("No charge rows in the current selection. Widen the filters above.")
        return

    import optimize

    opps = _detect(df)
    frame = optimize.opportunities_frame(opps)
    dollar_opps = [o for o in opps if o.monthly_savings > 0]

    if not dollar_opps:
        ui.callout("No optimization opportunity clears the $50/month floor in this selection.")
        return

    # ---------------------------------------------------------------
    # Header KPI row
    # ---------------------------------------------------------------
    usage_waste_monthly = optimize.usage_waste_total(opps)
    cost_of_waste = kpi.cost_of_waste(df, usage_waste_monthly)
    waste_pct = kpi.waste_pct(df, usage_waste_monthly)
    esr = optimize.effective_savings_rate_uplift(df, opps)
    total_annual = float(frame["annual_savings"].sum())

    ui.section(
        "Optimization opportunity",
        f"{ctx.config.organisation} · detected from FOCUS billing data across {', '.join(ctx.clouds)}",
    )

    k1, k2, k3, k4, k5 = st.columns(5)
    with k1:
        ui.tile("Identified annual savings", ui.money(total_annual),
                sub=f"{len(dollar_opps)} opportunities", accent=True)
    with k2:
        ui.tile("Opportunities", str(len(dollar_opps)), sub="above the $50/mo floor")
    with k3:
        ui.tile("Cost of waste", ui.money(cost_of_waste),
                sub="commitment + usage waste",
                status="critical" if (waste_pct or 0) > 15 else "warning")
    with k4:
        ui.tile("Waste %", ui.pct(waste_pct), sub="of amortised spend")
    with k5:
        ui.tile(
            "ESR if Rate levers run",
            ui.pct(esr.get("projected_esr_pct")),
            sub=f"+{esr.get('uplift_pts') or 0:.1f} pts vs {ui.pct(esr.get('current_esr_pct'))}",
            status="good",
        )

    ui.callout(optimize.waste_definition())

    # ---------------------------------------------------------------
    # Savings by category
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Savings by category", "Rate, Usage, Architecture, AI/GPU. One series, one colour.")
    cats = optimize.savings_by_category(opps)
    cats = cats[cats["annual_savings"] > 0]
    if len(cats):
        st.plotly_chart(
            charts.ranked_bar(cats["category"].tolist(), cats["annual_savings"].tolist(), mode=mode, height=280),
            width="stretch",
        )
        ui.table_view(cats, key="opt_cats", label="Savings by category table view")

    # ---------------------------------------------------------------
    # Opportunity backlog
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Opportunity backlog", "Filter, sort, and export. Confidence is shown as a bar -- low confidence is not hidden.")

    backlog = frame[frame["annual_savings"] > 0].copy()
    all_cats = sorted(backlog["category"].unique())
    all_clouds = sorted(backlog["cloud"].unique())
    all_efforts = [e for e in ["Low", "Medium", "High"] if e in set(backlog["effort"].unique())]

    f1, f2, f3, f4 = st.columns([1.3, 1.6, 1.2, 1.3])
    with f1:
        sel_cats = st.multiselect("Category", all_cats, default=[], key="opt_f_cat", placeholder="All categories")
    with f2:
        sel_clouds = st.multiselect("Cloud", all_clouds, default=[], key="opt_f_cloud", placeholder="All clouds")
    with f3:
        min_savings = st.number_input("Min annual savings ($)", min_value=0, value=0, step=1000, key="opt_f_min")
    with f4:
        sel_efforts = st.multiselect("Effort", all_efforts, default=[], key="opt_f_effort", placeholder="Any effort")

    filtered = backlog
    if sel_cats:
        filtered = filtered[filtered["category"].isin(sel_cats)]
    if sel_clouds:
        filtered = filtered[filtered["cloud"].isin(sel_clouds)]
    if sel_efforts:
        filtered = filtered[filtered["effort"].isin(sel_efforts)]
    if min_savings:
        filtered = filtered[filtered["annual_savings"] >= min_savings]

    show = filtered.rename(
        columns={
            "lever_id": "Lever", "lever_name": "Name", "category": "Category", "cloud": "Cloud",
            "scope": "Scope", "monthly_savings": "Monthly", "annual_savings": "Annual savings",
            "effort": "Effort", "risk": "Risk", "time_to_value": "Time to value",
            "confidence": "Confidence", "resource_count": "Resources",
        }
    )
    st.dataframe(
        show,
        width="stretch",
        hide_index=True,
        column_config={
            "Monthly": st.column_config.NumberColumn(format="$%.0f"),
            "Annual savings": st.column_config.NumberColumn(format="$%.0f"),
            "Confidence": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.2f"),
        },
    )
    st.download_button(
        "Download backlog CSV",
        filtered.to_csv(index=False).encode("utf-8"),
        file_name="optimization_backlog.csv",
        mime="text/csv",
        key="opt_dl_backlog",
    )
    st.caption(
        "Savings percentages are vendor **up-to** figures -- treat them as ceilings, not guarantees. "
        "The detectors already haircut them; the catalog below shows the raw published ranges."
    )

    # ---------------------------------------------------------------
    # Evidence drill-down
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Evidence drill-down", "What the bill actually shows for one opportunity -- and where confidence is soft.")

    labels = [
        f"{o.lever_id} · {o.scope} · {ui.money(o.annual_savings)}/yr (conf {o.confidence:.2f})"
        for o in dollar_opps
    ]
    idx = st.selectbox("Opportunity", range(len(dollar_opps)), format_func=lambda i: labels[i], key="opt_evidence_sel")
    o = dollar_opps[idx]
    lv = optimize.LEVER_BY_ID[o.lever_id]

    ed_l, ed_r = st.columns([1, 1])
    with ed_l:
        st.markdown(f"**Evidence** — {o.scope} ({o.cloud})")
        ev_rows = [{"Signal": k, "Value": str(v)} for k, v in o.evidence.items()]
        st.dataframe(pd.DataFrame(ev_rows), width="stretch", hide_index=True)
        st.caption(
            f"Resources implicated: **{o.resource_count}**"
            + (f" · e.g. {', '.join(o.resource_ids)}" if o.resource_ids else "")
        )
        if o.confidence < 0.7:
            ui.callout(
                f"{ui.status_pill('warning', f'Confidence {o.confidence:.2f}')} &nbsp; This lever leans on a "
                f"signal the invoice cannot see. **Prerequisite to confirm:** {lv.prerequisites}."
            )
    with ed_r:
        st.markdown(f"**Lever {lv.id} — {lv.name}**")
        lever_rows = [
            {"Field": "Category", "Value": lv.category},
            {"Field": "Clouds", "Value": ", ".join(lv.clouds)},
            {"Field": "Savings range", "Value": f"{lv.savings_low * 100:.0f}-{lv.savings_high * 100:.0f}%"},
            {"Field": "Effort", "Value": lv.effort},
            {"Field": "Risk", "Value": lv.risk},
            {"Field": "Time to value", "Value": lv.time_to_value},
            {"Field": "Prerequisites", "Value": lv.prerequisites},
            {"Field": "Detection rule", "Value": lv.detection},
        ]
        st.dataframe(pd.DataFrame(lever_rows), width="stretch", hide_index=True)
        st.markdown(f"[Source: provider documentation]({lv.source_url})")

    # ---------------------------------------------------------------
    # Delivery roadmap
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Delivery roadmap", "Wave 1 = quick wins (low effort, low risk) plus the governance prerequisites that gate the rest.")

    road = optimize.roadmap(opps)
    if road is None or road.empty:
        ui.callout("No sequenced roadmap for this selection.")
    else:
        st.plotly_chart(
            charts.sparkline(road["cumulative_annual_savings"].tolist(), mode=mode, height=90),
            width="stretch",
            config={"displayModeBar": False},
        )
        st.caption(
            f"Cumulative identified savings across the sequenced backlog, reaching "
            f"**{ui.money(float(road['cumulative_annual_savings'].iloc[-1]))}/yr** at the end of Wave 3."
        )
        wave_summary = (
            road.groupby("wave", as_index=False)
            .agg(opportunities=("lever_id", "size"),
                 annual_savings=("annual_savings", "sum"))
            .rename(columns={"wave": "Wave", "opportunities": "Opportunities", "annual_savings": "Annual savings"})
        )
        wave_summary["Wave"] = wave_summary["Wave"].map({1: "Wave 1 · quick wins", 2: "Wave 2 · planned", 3: "Wave 3 · strategic"})
        st.dataframe(
            wave_summary,
            width="stretch",
            hide_index=True,
            column_config={"Annual savings": st.column_config.NumberColumn(format="$%.0f")},
        )
        ui.table_view(road, key="opt_roadmap", label="Full roadmap table view")

    # ---------------------------------------------------------------
    # Lever catalog
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Lever catalog", "Every lever the platform knows, with its published savings range and source.")

    cat = optimize.lever_catalog_frame()
    lc1, lc2 = st.columns([1.3, 2])
    with lc1:
        cat_options = sorted(cat["category"].unique())
        sel_lc_cat = st.multiselect("Category", cat_options, default=[], key="opt_lc_cat", placeholder="All categories")
    with lc2:
        query = st.text_input("Search name or detection rule", value="", key="opt_lc_search",
                              placeholder="e.g. spot, graviton, snapshot")

    view = cat.copy()
    if sel_lc_cat:
        view = view[view["category"].isin(sel_lc_cat)]
    if query:
        q = query.lower()
        view = view[
            view["name"].str.lower().str.contains(q) | view["detection"].str.lower().str.contains(q)
        ]
    view = view.copy()
    view["savings_range"] = view.apply(
        lambda r: f"{r['savings_low'] * 100:.0f}-{r['savings_high'] * 100:.0f}%", axis=1
    )
    display = view[
        ["id", "name", "category", "clouds", "savings_range", "effort", "risk",
         "time_to_value", "prerequisites", "detection", "source_url"]
    ].rename(
        columns={
            "id": "ID", "name": "Name", "category": "Category", "clouds": "Clouds",
            "savings_range": "Savings", "effort": "Effort", "risk": "Risk",
            "time_to_value": "Time to value", "prerequisites": "Prerequisites",
            "detection": "Detection rule", "source_url": "Source",
        }
    )
    st.dataframe(
        display,
        width="stretch",
        hide_index=True,
        column_config={"Source": st.column_config.LinkColumn("Source", display_text="docs")},
    )
    st.caption("Savings ranges are the raw vendor-published ceilings; realised savings are lower after haircuts.")
