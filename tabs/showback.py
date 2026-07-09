"""Showback and Chargeback -- where a cloud bill becomes an accountable ledger.

The tab is shaped around one distinction that practitioners routinely blur:

* **Showback moves information.** The cost stays in a central IT budget; each
  team is *told* what it consumed. Nothing lands on their P&L.
* **Chargeback moves money.** The expense is journalled onto the consuming
  team's cost centre. It hits their budget, and therefore it has to survive a
  dispute -- which is why untagged spend and shared-cost splitting are not
  cosmetic here, they are the whole game.

Neither is "more mature". It is an accounting-policy choice, and the choice is
made visible: the allocation method, the shared pool, and the untagged tax are
all controls, not hidden constants. The `~90%` allocation-coverage line below
which most enterprises fall back to showback is practitioner consensus, NOT a
published FinOps Foundation figure.

Every number is produced by `allocation.py` and `kpi.py`; this tab only renders.

Source: https://www.finops.org/framework/capabilities/invoicing-chargeback/
"""

from __future__ import annotations

from typing import Dict, Tuple

import pandas as pd
import streamlit as st

import allocation
import charts
import kpi
import theme
import ui
from finops_core import DataContext

_DIMS = {
    "Business unit": "tag_business_unit",
    "Application": "tag_application",
    "Cost center": "tag_cost_center",
}


@st.cache_data(show_spinner=False)
def _allocate(
    df: pd.DataFrame, method: str, dimcol: str, include_untagged: bool,
    fixed_items: Tuple[Tuple[str, float], ...],
) -> pd.DataFrame:
    """Cached allocation. Policy is rebuilt from hashable primitives so the
    expensive `allocate()` groupby is memoised on the frame + control state."""
    policy = allocation.SharedCostPolicy(
        method=method,
        include_untagged=include_untagged,
        fixed_percentages=dict(fixed_items) if fixed_items else None,
    )
    return allocation.allocate(df, policy, dim=dimcol)


@st.cache_data(show_spinner=False)
def _targets(df: pd.DataFrame, dimcol: str) -> list:
    d = allocation.direct_costs(df, allocation.SharedCostPolicy(), dim=dimcol)
    return [str(t) for t in d[dimcol].tolist()]


def _fold_alloc(alloc: pd.DataFrame, dimcol: str, limit: int = 8) -> pd.DataFrame:
    """Keep the top targets, fold the rest into one 'Other' row that still sums
    each cost component -- so the stacked bar and the ranked bar always agree."""
    if len(alloc) <= limit:
        return alloc
    head = alloc.head(limit - 1).copy()
    tail = alloc.iloc[limit - 1:]
    other = {dimcol: theme.OTHER_LABEL}
    for c in ["direct_cost", "shared_cost", "untagged_cost", "total_cost"]:
        other[c] = float(tail[c].sum())
    return pd.concat([head, pd.DataFrame([other])], ignore_index=True)


def render(ctx: DataContext) -> None:
    df = ctx.focus_df
    mode = ui.mode()

    if df.empty:
        ui.callout("No charge rows in the current selection. Widen the filters above.")
        return

    ui.section("Showback and Chargeback", "Allocation, shared-cost splitting, and an invoice that reconciles to the penny.")
    ui.callout(
        "**Showback moves information** -- the cost stays in a central budget and the team is merely told what it "
        "used. **Chargeback moves money** -- the expense lands on the consuming team's P&L. Neither is more mature; "
        "it is an accounting-policy choice. See the FinOps Foundation's "
        "[Invoicing and Chargeback capability](https://www.finops.org/framework/capabilities/invoicing-chargeback/)."
    )

    # ---------------------------------------------------------------
    # Controls -- one row, above everything they scope
    # ---------------------------------------------------------------
    c1, c2, c3 = st.columns([1.3, 1.2, 1.0])
    with c1:
        method = st.selectbox("Allocation method", allocation.ALLOCATION_METHODS, index=allocation.ALLOCATION_METHODS.index("proportional"), key="sb_method")
    with c2:
        dim_label = st.selectbox("Allocate onto", list(_DIMS.keys()), key="sb_dim")
    with c3:
        include_untagged = st.toggle("Spread untagged", value=True, key="sb_untagged")

    dimcol = _DIMS[dim_label]
    short = dimcol.replace("tag_", "")
    targets = _targets(df, dimcol)

    fixed_items: Tuple[Tuple[str, float], ...] = ()
    if method == "fixed_percentage":
        st.caption("Edit each target's share. The policy is only usable when the shares sum to 100.")
        n = max(len(targets), 1)
        seed = pd.DataFrame({dim_label: targets, "Percentage": [round(100.0 / n, 4)] * len(targets)})
        edited = st.data_editor(
            seed, key="sb_fixed_editor", hide_index=True, width="stretch",
            column_config={"Percentage": st.column_config.NumberColumn(min_value=0.0, max_value=100.0, step=0.5, format="%.2f")},
            disabled=[dim_label],
        )
        fixed_map = {str(r[dim_label]): float(r["Percentage"]) for _, r in edited.iterrows()}
        fixed_items = tuple(sorted(fixed_map.items()))
        errs = allocation.SharedCostPolicy(method="fixed_percentage", fixed_percentages=fixed_map).validate()
        for e in errs:
            ui.callout(f"{ui.status_pill('warning', 'Policy')} &nbsp; {e}")
        st.caption(f"Sum of shares: **{sum(fixed_map.values()):.2f}** (must equal 100).")

    alloc = _allocate(df, method, dimcol, include_untagged, fixed_items)
    if alloc.empty:
        ui.callout("Nothing directly attributable to allocate onto for this dimension in the current slice.")
        return

    # ---------------------------------------------------------------
    # KPI row
    # ---------------------------------------------------------------
    total = kpi.total_spend(df)
    direct = float(alloc["direct_cost"].sum())
    policy = allocation.SharedCostPolicy(method=method, include_untagged=include_untagged,
                                         fixed_percentages=dict(fixed_items) if fixed_items else None)
    pool_shared = allocation.shared_pool(df, policy)
    coverage = kpi.allocation_coverage_pct(df, tag=short)
    untagged_dollars = total * (100.0 - (coverage or 0.0)) / 100.0

    ui.tile_row(
        [
            dict(label="Total amortised", value=ui.money(total), sub="reconciles to the estate", accent=True),
            dict(label="Directly attributable", value=ui.money(direct), sub=f"{ui.pct((direct/total*100) if total else None)} of total"),
            dict(label="Shared pool", value=ui.money(pool_shared), sub="platform + security + governance"),
            dict(label="Shared as % of total", value=ui.pct((pool_shared/total*100) if total else None), sub="the shared-cost tax"),
            dict(
                label=f"Untagged ({short})", value=ui.money(untagged_dollars),
                sub=f"{ui.pct((100.0-(coverage or 0.0)))} unallocated",
                status="critical" if (coverage or 0) < 80 else "warning" if (coverage or 0) < 90 else "good",
            ),
        ]
    )

    # ---------------------------------------------------------------
    # Composition + ranking
    # ---------------------------------------------------------------
    st.divider()
    folded = _fold_alloc(alloc, dimcol, limit=8)
    left, right = st.columns([1.3, 1])

    with left:
        ui.section("Cost composition per target", "Direct, then the shared and untagged allocations layered on top.")
        long = folded.melt(
            id_vars=[dimcol], value_vars=["direct_cost", "shared_cost", "untagged_cost"],
            var_name="component", value_name="amount",
        )
        long["component"] = long["component"].map(
            {"direct_cost": "Direct", "shared_cost": "Shared allocation", "untagged_cost": "Untagged allocation"}
        )
        st.plotly_chart(
            charts.stacked_bar(long, dimcol, "component", "amount", mode=mode, height=360),
            width="stretch",
        )
        ui.table_view(long, key="sb_composition", label="Composition table view")

    with right:
        ui.section("Fully-loaded cost", "Direct + allocations, ranked. One series, one colour.")
        st.plotly_chart(
            charts.ranked_bar(folded[dimcol].astype(str).tolist(), folded["total_cost"].tolist(), mode=mode, height=360),
            width="stretch",
        )
        ui.table_view(
            alloc.rename(columns={dimcol: dim_label}), key="sb_ranked", label="Allocation table view",
        )

    # ---------------------------------------------------------------
    # Chargeback invoice
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Chargeback invoice", "One month, one line per component per target. The lines sum to the allocated total.")

    months = sorted(df["ChargePeriodStart"].dt.to_period("M").astype(str).unique())
    period = st.selectbox("Billing month", months, index=len(months) - 1, key="sb_period")
    invoice = allocation.chargeback(df, policy, dim=dimcol, period=period)

    if invoice.empty:
        ui.callout("No charges for that month in the current slice.")
    else:
        wide = invoice.pivot_table(index=dimcol, columns="line_item", values="amount", aggfunc="sum", observed=True).fillna(0.0)
        for c in ["Direct", "Shared allocation", "Untagged allocation"]:
            if c not in wide.columns:
                wide[c] = 0.0
        wide = wide[["Direct", "Shared allocation", "Untagged allocation"]]
        wide["Total"] = wide.sum(axis=1)
        wide = wide.sort_values("Total", ascending=False).reset_index().rename(columns={dimcol: dim_label})
        money_cols = ["Direct", "Shared allocation", "Untagged allocation", "Total"]
        st.dataframe(
            wide, width="stretch", hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="$%.0f") for c in money_cols},
        )
        st.download_button(
            "Download invoice CSV", invoice.to_csv(index=False).encode("utf-8"),
            file_name=f"chargeback_{short}_{period}.csv", mime="text/csv", key="sb_invoice_dl",
        )

    # ---------------------------------------------------------------
    # Method comparison -- the conversation the CFO actually needs
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "How the shared-cost tax changes with the method",
        "Same pool, three splitting rules. The gap between them is what a target is really arguing about.",
    )
    n = max(len(targets), 1)
    even_fixed = tuple(sorted({t: 100.0 / n for t in targets}.items()))
    fixed_for_cmp = fixed_items if fixed_items else even_fixed

    frames = {}
    for label, mth, fx in [
        ("Even split", "even_split", ()),
        ("Proportional", "proportional", ()),
        ("Fixed %", "fixed_percentage", fixed_for_cmp),
    ]:
        a = _allocate(df, mth, dimcol, include_untagged, fx)
        frames[label] = a.set_index(dimcol)["shared_cost"] if len(a) else pd.Series(dtype=float)

    cmp = pd.DataFrame(frames).fillna(0.0)
    if cmp.empty:
        ui.callout("No targets to compare under the current slice.")
    else:
        cmp = cmp.sort_values("Proportional", ascending=False).reset_index().rename(columns={dimcol: dim_label})
        st.dataframe(
            cmp, width="stretch", hide_index=True,
            column_config={c: st.column_config.NumberColumn(format="$%.0f") for c in ["Even split", "Proportional", "Fixed %"]},
        )
        st.caption("Each column is the shared pool this target absorbs under that rule -- direct spend is unchanged.")

    # ---------------------------------------------------------------
    # Readiness banner
    # ---------------------------------------------------------------
    st.divider()
    readiness = kpi.chargeback_readiness(coverage)
    status = "good" if (coverage or 0) >= 90 else "warning" if (coverage or 0) >= 80 else "critical"
    st.markdown(
        f"{ui.status_pill(status, readiness)} &nbsp; allocation coverage on **{short}** is "
        f"**{ui.pct(coverage)}**.",
        unsafe_allow_html=True,
    )
    st.caption(
        "The ~90% coverage line below which a chargeback invoice is treated as disputable is practitioner "
        "consensus, not a published FinOps Foundation number."
    )
