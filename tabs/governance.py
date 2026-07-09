"""The Governance tab -- tagging, allocation coverage, and policy.

Why this tab exists, and why it looks like this:

* **Coverage is the gate, not the goal.** A chargeback invoice that covers 82%
  of spend is disputable on the missing 18%, so allocation coverage is the first
  thing a practice has to fix before invoicing anyone. The KPI row leads with it
  and with the dollars still sitting untagged -- the number a remediation owner
  is actually measured against.

* **The worklist is the deliverable.** "Coverage is 84%" is a status; "here are
  the five services holding $40k of untagged spend" is a task. `untagged_breakdown`
  turns the KPI into work, grouped where an engineer can act on it.

* **Provider constraints bite at ingest, not here.** The tag ceilings and the
  non-retroactive activation rule are the traps that quietly cap coverage, so we
  state them precisely on the page rather than letting someone rediscover them
  the hard way.

Every number is read from `kpi`, `allocation` and `focus`; this tab computes
nothing. Costs are amortized `EffectiveCost` throughout, per the KPI engine.
"""

from __future__ import annotations

import uuid

import pandas as pd
import streamlit as st

import allocation
import charts
import focus
import kpi
import store
import ui
from finops_core import DataContext

# The chargeback-readiness line. Practitioner consensus, NOT a published FinOps
# Foundation number -- stated here so the caption and the status logic agree.
CHARGEBACK_THRESHOLD = 90.0


def render(ctx: DataContext) -> None:
    df = ctx.focus_df
    mode = ui.mode()

    if df.empty:
        ui.callout("No charge rows in the current selection. Widen the filters above.")
        return

    coverage = allocation.coverage_report(df)
    governance = allocation.tag_governance(df)

    app_cov = kpi.allocation_coverage_pct(df)
    unallocated_dollars = _application_unallocated(coverage)
    below = int((coverage["coverage_pct"] < CHARGEBACK_THRESHOLD).sum())

    # ---------------------------------------------------------------
    # KPI row
    # ---------------------------------------------------------------
    ui.section(
        "Allocation and tagging health",
        f"{ctx.config.organisation} · coverage on the application tag · {ctx.mode.label}",
    )

    cov_status = (
        "good" if (app_cov or 0) >= 95
        else "good" if (app_cov or 0) >= CHARGEBACK_THRESHOLD
        else "warning" if (app_cov or 0) >= 80
        else "critical"
    )
    ui.tile_row(
        [
            dict(
                label="Allocation coverage",
                value=ui.pct(app_cov),
                sub="tagged spend / total",
                status=cov_status,
            ),
            dict(
                label="Unallocated spend",
                value=ui.money(unallocated_dollars),
                sub="on the application tag",
                status="critical" if unallocated_dollars > 0 and (app_cov or 0) < 80 else "warning",
            ),
            dict(
                label="Chargeback readiness",
                value=kpi.chargeback_readiness(app_cov),
                sub="practitioner rule of thumb",
            ),
            dict(
                label="Tags below 90%",
                value=str(below),
                sub=f"of {len(coverage)} canonical keys",
                status="critical" if below >= 4 else "warning" if below else "good",
            ),
        ]
    )
    ui.callout(
        "The ~90% coverage line below which chargeback becomes showback is "
        "**practitioner consensus, not a published FinOps Foundation number**. "
        "Below it most enterprises invoice the tagged portion and showback the rest."
    )

    # ---------------------------------------------------------------
    # Coverage per canonical tag key
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Coverage by canonical tag key",
        "One bar per allocation key. A single series, so every bar shares one colour.",
    )

    left, right = st.columns([1.25, 1])
    with left:
        st.plotly_chart(
            charts.ranked_bar(
                coverage["tag_key"].tolist(),
                coverage["coverage_pct"].tolist(),
                mode=mode,
                height=320,
                value_prefix="",
            ),
            width="stretch",
        )
        st.caption("Values are coverage %, not dollars.")
    with right:
        for _, r in coverage.iterrows():
            st.markdown(
                f"{ui.status_pill(r['status'], r['tag_key'])} &nbsp; "
                f"**{ui.pct(r['coverage_pct'])}** covered &nbsp; "
                f"({ui.money(r['unallocated_cost'])} unallocated)",
                unsafe_allow_html=True,
            )
    ui.table_view(coverage, key="gov_coverage", label="Coverage table view")

    # ---------------------------------------------------------------
    # Where the untagged money is -- the remediation worklist
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Where the untagged money is",
        "Untagged is defined by a missing business unit -- the tag that gates "
        "chargeback. This is the remediation worklist, grouped where it can be acted on.",
    )

    wl1, wl2 = st.columns(2)
    with wl1:
        _worklist(df, "ServiceName", "Untagged by service", mode, "gov_untagged_svc")
    with wl2:
        _worklist(df, "ProviderName", "Untagged by cloud", mode, "gov_untagged_cloud")

    # ---------------------------------------------------------------
    # Tag governance -- the enforcement population
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Tag governance",
        "Per key: coverage, unallocated cost, and the row count a tagging-policy "
        "enforcement (SCP, Azure Policy, GCP org policy) would target.",
    )
    if governance.empty:
        ui.callout("No governance rows for this selection.")
    else:
        st.dataframe(
            governance.rename(
                columns={
                    "tag_key": "Tag key",
                    "coverage_pct": "Coverage %",
                    "unallocated_cost": "Unallocated cost",
                    "violations": "Rows missing the tag",
                    "status": "Status",
                }
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "Coverage %": st.column_config.NumberColumn(format="%.1f%%"),
                "Unallocated cost": st.column_config.NumberColumn(format="$%.0f"),
            },
        )

    # ---------------------------------------------------------------
    # Provider constraints
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Provider tag constraints",
        "These cap coverage in practice. Stated precisely so no one rediscovers "
        "the non-retroactive activation rule the hard way.",
    )
    _provider_constraints()

    # ---------------------------------------------------------------
    # FOCUS conformance
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "FOCUS conformance",
        "Whether the loaded frame conforms to the canonical FOCUS "
        f"{focus.FOCUS_CANONICAL_VERSION} Cost and Usage schema.",
    )
    _focus_conformance(ctx)

    # ---------------------------------------------------------------
    # Policy persistence
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Allocation policy",
        "Save the shared-cost split the CFO signed off. Policies are decisions, "
        "not data -- so they live in the durable store, not in the billing frame.",
    )
    _policy_panel()


# ==========================================================================
# Panel helpers -- render only.
# ==========================================================================


def _application_unallocated(coverage: pd.DataFrame) -> float:
    """Unallocated dollars on the application tag, read from the coverage report."""
    row = coverage[coverage["tag_key"] == "application"]
    if row.empty:
        return 0.0
    return float(row["unallocated_cost"].iloc[0])


def _worklist(df: pd.DataFrame, by: str, title: str, mode: str, key: str) -> None:
    ui.section(title)
    wl = allocation.untagged_breakdown(df, by=by)
    if wl.empty:
        ui.callout("Nothing untagged on this dimension -- coverage is complete here.")
        return
    labels = [str(x) for x in wl[by].tolist()][:10]
    values = [float(v) for v in wl["cost"].tolist()][:10]
    st.plotly_chart(
        charts.ranked_bar(labels, values, mode=mode, height=300),
        width="stretch",
    )
    ui.table_view(
        wl.rename(columns={by: by, "cost": "Untagged cost", "pct_of_untagged": "% of untagged"}),
        key=key,
        label=f"{title} table view",
    )


def _provider_constraints() -> None:
    aws, azure, gcp = st.columns(3)
    with aws:
        ui.callout(
            "**AWS**<br>"
            "50 tags per resource. Up to **500 activated** cost-allocation tag "
            "keys per payer. Activation is **not retroactive** -- data flows from "
            "the activation date forward, so spend before activation stays untagged. "
            "Key &le;128 chars, value &le;256."
        )
    with azure:
        ui.callout(
            "**Azure**<br>"
            "50 name/value pairs per resource, resource group or subscription. "
            "Tag **inheritance is a Cost Management setting** applied to usage "
            "records, not to resources. Enforce with Azure Policy (Modify effect). "
            "Key &le;512 chars, value &le;256."
        )
    with gcp:
        ui.callout(
            "**GCP**<br>"
            "64 labels per resource. Key **and** value &le;63 chars, lowercase "
            "letters/numbers/underscore/dash only. Labels are **not inherited** "
            "(Resource Manager Tags are a different, inheritable construct)."
        )
    st.caption(
        "Sources: "
        "docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/activating-tags.html · "
        "learn.microsoft.com/en-us/azure/cost-management-billing/costs/enable-tag-inheritance · "
        "docs.cloud.google.com/resource-manager/docs/labels-overview"
    )


def _focus_conformance(ctx: DataContext) -> None:
    v = ctx.validation
    df = ctx.focus_df

    if v is None:
        ui.callout("No validation result for this selection (typically an empty Live frame).")
    else:
        status = "good" if v.ok and not v.warnings else "warning" if v.ok else "critical"
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            ui.tile("Conformance", "Pass" if v.ok else "Fail", sub=v.summary()[:40], status=status)
        with c2:
            ui.tile("Errors", str(len(v.errors)), status="critical" if v.errors else "good")
        with c3:
            ui.tile("Warnings", str(len(v.warnings)), status="warning" if v.warnings else "good")
        with c4:
            ui.tile("Rows", f"{v.row_count:,}")

        if v.errors:
            with st.expander(f"{len(v.errors)} error(s)", expanded=False):
                for e in v.errors:
                    st.markdown(f"- {e}")
        if v.warnings:
            with st.expander(f"{len(v.warnings)} warning(s)", expanded=False):
                for w in v.warnings:
                    st.markdown(f"- {w}")

    rows = [
        {"Mandatory column": name, "Present": ("Yes" if name in df.columns else "No")}
        for name in focus.MANDATORY_COLUMNS
    ]
    mand = pd.DataFrame(rows)
    absent = int((mand["Present"] == "No").sum())
    st.caption(
        f"{len(mand) - absent} of {len(mand)} mandatory FOCUS "
        f"{focus.FOCUS_CANONICAL_VERSION} columns present."
    )
    ui.table_view(mand, key="gov_mandatory", label="Mandatory columns present/absent")


def _policy_panel() -> None:
    backend = store.backend()
    if not backend.durable:
        ui.callout(
            f"Store backend: **{backend.kind}** (not durable). {backend.detail}"
        )
    else:
        st.caption(f"Store backend: **{backend.kind}** -- durable.")

    try:
        store.init()
    except Exception as exc:
        ui.callout(f"Policy store unavailable: `{exc}`")
        return

    with st.form("gov_policy_form", clear_on_submit=False):
        name = st.text_input("Policy name", placeholder="e.g. FY26 shared-cost split")
        method = st.selectbox(
            "Shared-cost method",
            allocation.ALLOCATION_METHODS,
            help="How shared and untagged cost is spread across targets.",
        )
        include_untagged = st.checkbox(
            "Spread untagged spend as its own pool", value=True,
            help="Keeps the allocation exhaustive so an invoice reconciles to the penny.",
        )
        submitted = st.form_submit_button("Save policy", type="primary")

    if submitted:
        if not name.strip():
            st.warning("Give the policy a name before saving.")
        else:
            try:
                store.save_policy(
                    str(uuid.uuid4()),
                    name.strip(),
                    {"method": method, "include_untagged": bool(include_untagged)},
                    active=False,
                )
                st.success(f"Saved policy '{name.strip()}'.")
            except Exception as exc:
                st.error(f"Could not save policy: `{exc}`")

    try:
        policies = store.list_policies()
    except Exception as exc:
        ui.callout(f"Could not list policies: `{exc}`")
        return

    if not policies:
        st.caption("No saved policies yet.")
        return

    frame = pd.DataFrame(
        [
            {
                "Name": p["name"],
                "Method": p["payload"].get("method", ""),
                "Spread untagged": "Yes" if p["payload"].get("include_untagged") else "No",
                "Active": "Yes" if p.get("active") else "No",
            }
            for p in policies
        ]
    )
    st.dataframe(frame, width="stretch", hide_index=True)
