"""The Reference tab -- the FinOps Framework and FOCUS, rendered from the code's
own constants so the documentation can never drift from the implementation.

Why render from constants rather than write prose: a hand-maintained "here is our
data model" page rots the moment someone adds a column. Everything on this tab is
read live from `finops_core`, `focus` and `kpi` -- the exact objects the
dashboards and agents reason over. If the schema changes, this page changes with
it, for free. If it disagrees with the dashboards, that is a bug in one file, not
a stale doc.

This tab renders vocabulary; it touches no billing data.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import focus
import kpi
import ui
from finops_core import (
    ALLIED_PERSONAS,
    CAPABILITIES,
    CAPABILITY_2026_ADDED,
    CAPABILITY_2026_RENAMES,
    CORE_PERSONAS,
    DOMAINS,
    FORECAST_VARIANCE_THRESHOLD,
    MATURITY,
    PHASES,
    SCOPES,
    DataContext,
)

_PHASE_MEANING = {
    "Inform": "Visibility, allocation and shared accountability -- you cannot manage what you cannot see or attribute.",
    "Optimize": "Act on the visibility: rightsizing, rate commitments, waste elimination, architectural change.",
    "Operate": "Make it continuous -- governance, automation and the operating cadence that stops the gains eroding.",
}

_MATURITY_MEANING = {
    "Crawl": "Ad hoc, retrospective, partial coverage. A capability exists but is manual and inconsistently applied.",
    "Walk": "Repeatable and mostly automated, with defined ownership and reasonable coverage.",
    "Run": "Continuous, automated, near-complete coverage, tied to business KPIs and acted on in near real time.",
}

_KPI_FORMULAS = [
    ("Effective Savings Rate (ESR)",
     "(on-demand-equivalent - effective cost - cost to achieve) / on-demand-equivalent",
     "https://www.finops.org/wg/how-to-calculate-effective-savings-rate-esr/"),
    ("Commitment coverage",
     "covered eligible ListCost / total commitment-eligible ListCost",
     "https://www.finops.org/wg/how-to-calculate-effective-savings-rate-esr/"),
    ("Commitment utilisation",
     "used commitment / (used + unused commitment)",
     "https://www.finops.org/wg/percent-commitment-based-discount-waste-playbook/"),
    ("Cost of waste",
     "commitment waste (unused commitment rows) + usage waste (idle / zombie detectors)",
     "https://www.finops.org/wg/percent-commitment-based-discount-waste-playbook/"),
    ("Allocation coverage",
     "allocated cost (tag != Unallocated) / total cost",
     "https://www.finops.org/framework/capabilities/allocation/"),
    ("Budget variance",
     "actual - budget  (positive = overrun)",
     "https://www.finops.org/framework/capabilities/forecasting/"),
    ("WAPE (forecast accuracy)",
     "sum|actual - forecast| / sum|actual|",
     "https://www.finops.org/framework/capabilities/forecasting/"),
]

_FOCUS_HISTORY = [
    ("1.0-preview", "2023-11-15", "First public preview."),
    ("1.0 GA", "2024-06-20", "General availability of the Cost and Usage dataset."),
    ("1.1", "2024-11", "SkuMeter, ServiceSubcategory, capacity reservations, commitment units."),
    ("1.2", "2025-05-29", "Invoice + billing-account typing, pricing-currency columns. Canonical here."),
    ("1.3", "2025-12-11", "Adds ContractCommitment dataset + split-cost-allocation columns; deprecates ProviderName/PublisherName into ServiceProviderName/HostProviderName."),
]

_FOCUS_EMITTERS = [
    ("AWS", "Data Exports", "`FOCUS_1_0_AWS` (GA 2024-11-25), `FOCUS_1_2_AWS` (GA 2025-11-19)."),
    ("Azure", "Cost Management exports", "Dataset type `FocusCost`."),
    ("GCP", "BigQuery billing export", "`gcp_billing_export_focus_<BILLING_ACCOUNT_ID>` in immutable dataset `gcp_billing_immutable_<ID>_<Location>`."),
    ("OCI", "Cost Reports", "Oracle Cloud FOCUS cost reports."),
    ("Databricks", "System tables", "`system.billing.usage`."),
]


def render(ctx: DataContext) -> None:
    ui.section(
        "Reference -- FinOps Framework and FOCUS",
        "Rendered live from the same constants the dashboards and agents use, so it "
        "cannot drift from the implementation.",
    )

    _framework()
    st.divider()
    _phases_maturity()
    st.divider()
    _personas()
    st.divider()
    _scopes_forecast()
    st.divider()
    _focus_schema()
    st.divider()
    _focus_enums()
    st.divider()
    _focus_subcategories()
    st.divider()
    _canonical_tags()
    st.divider()
    _focus_versions()
    st.divider()
    _kpi_reference()
    st.divider()
    _links()


# ==========================================================================
# Panels
# ==========================================================================


def _framework() -> None:
    ui.section(
        "The FinOps Framework",
        "Four domains, 23 capabilities. The 2026 Framework kept the domains, "
        "renamed six capabilities and added one.",
    )
    cols = st.columns(len(DOMAINS))
    for col, domain in zip(cols, DOMAINS):
        with col:
            st.markdown(f"**{domain}**")
            chips = "".join(ui.pill(c) + " " for c in CAPABILITIES[domain])
            st.markdown(chips, unsafe_allow_html=True)

    with st.expander("2026 Framework changes (renames + the added capability)", expanded=False):
        rn = pd.DataFrame(
            [{"2025 name": k, "2026 name": v} for k, v in CAPABILITY_2026_RENAMES.items()]
        )
        st.dataframe(rn, width="stretch", hide_index=True)
        st.markdown(
            "**Added in 2026:** " + ", ".join(CAPABILITY_2026_ADDED)
            + " -- bringing the total to 23 capabilities across the same four domains."
        )


def _phases_maturity() -> None:
    left, right = st.columns(2)
    with left:
        ui.section("Phases", "The FinOps lifecycle is iterative, not linear.")
        for p in PHASES:
            st.markdown(f"**{p}** — {_PHASE_MEANING.get(p, '')}")
    with right:
        ui.section("Maturity", "A capability advances Crawl -> Walk -> Run independently.")
        for m in MATURITY:
            st.markdown(f"**{m}** — {_MATURITY_MEANING.get(m, '')}")


def _personas() -> None:
    ui.section(
        "Personas",
        "Who reads a dashboard, and what they expect off it.",
    )
    core = pd.DataFrame(
        [{"Persona": k, "What they expect": v} for k, v in CORE_PERSONAS.items()]
    )
    st.dataframe(core, width="stretch", hide_index=True)
    st.markdown(
        "**Allied personas** (they intersect FinOps rather than own it): "
        + ", ".join(ALLIED_PERSONAS) + "."
    )


def _scopes_forecast() -> None:
    left, right = st.columns([1.2, 1])
    with left:
        ui.section("Scopes", "A segment of technology spend a practice is responsible for.")
        st.markdown(
            " ".join(ui.pill(s) for s in SCOPES), unsafe_allow_html=True
        )
        st.caption("Scopes overlap; they are not mutually exclusive.")
    with right:
        ui.section("Forecast maturity bands", "Variance thresholds by maturity.")
        fc = pd.DataFrame(
            [{"Band": k, "Max variance %": v} for k, v in FORECAST_VARIANCE_THRESHOLD.items()]
        )
        st.dataframe(
            fc,
            width="stretch",
            hide_index=True,
            column_config={"Max variance %": st.column_config.NumberColumn(format="%.0f%%")},
        )


def _focus_schema() -> None:
    ui.section(
        f"FOCUS {focus.FOCUS_CANONICAL_VERSION} schema",
        "The canonical Cost and Usage columns. Filter by feature level.",
    )
    levels = [focus.MANDATORY, focus.CONDITIONAL, focus.RECOMMENDED, focus.OPTIONAL]
    sel = st.multiselect("Feature level", levels, default=[], key="ref_focus_level",
                         placeholder="All levels")

    rows = [
        {
            "Column": c.name,
            "Feature level": c.feature_level,
            "Type": c.dtype,
            "Since": c.since,
            "Note": c.note,
        }
        for c in focus.SCHEMA
        if (not sel or c.feature_level in sel)
    ]
    frame = pd.DataFrame(rows)
    st.dataframe(frame, width="stretch", hide_index=True)
    st.caption(f"{len(frame)} of {len(focus.SCHEMA)} columns shown.")
    ui.table_view(frame, key="ref_schema", label="Schema table view")


def _focus_enums() -> None:
    ui.section("Closed enumerations", "Values outside these sets fail validation.")
    order = [
        "ChargeCategory", "ChargeClass", "ChargeFrequency", "ServiceCategory",
        "PricingCategory", "CommitmentDiscountCategory", "CommitmentDiscountStatus",
        "CapacityReservationStatus",
    ]
    for name in order:
        vals = focus.ENUMS.get(name)
        if vals is None:
            continue
        chips = " ".join(ui.pill(v) for v in sorted(vals))
        st.markdown(f"**{name}** &nbsp; {chips}", unsafe_allow_html=True)
    st.caption("ChargeClass is nullable; its only non-null value is 'Correction'.")


def _focus_subcategories() -> None:
    ui.section(
        "Service subcategories",
        "The valid subcategory set under each ServiceCategory.",
    )
    for category, subs in focus.SERVICE_SUBCATEGORY.items():
        with st.expander(f"{category} ({len(subs)})", expanded=False):
            st.markdown(" ".join(ui.pill(s) for s in subs), unsafe_allow_html=True)


def _canonical_tags() -> None:
    ui.section(
        "Canonical allocation tags",
        "The keys every provider's tags/labels normalise onto, and the aliases folded in.",
    )
    rows = [
        {"Canonical key": t, "Aliases folded in": ", ".join(focus.TAG_ALIASES.get(t, [])) or "—"}
        for t in focus.CANONICAL_TAGS
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption(
        "FOCUS does not dictate which tags exist -- that is an enterprise decision. "
        "These are the keys this platform normalises onto."
    )


def _focus_versions() -> None:
    left, right = st.columns(2)
    with left:
        ui.section("FOCUS version history", "")
        hist = pd.DataFrame(_FOCUS_HISTORY, columns=["Version", "Date", "What changed"])
        st.dataframe(hist, width="stretch", hide_index=True)
        st.caption(
            f"We model {focus.FOCUS_CANONICAL_VERSION} as canonical because that is "
            "what AWS/Azure/GCP actually emit today; 1.3 columns are carried optional."
        )
    with right:
        ui.section("Native FOCUS emitters", "")
        em = pd.DataFrame(_FOCUS_EMITTERS, columns=["Cloud / platform", "Product", "Detail"])
        st.dataframe(em, width="stretch", hide_index=True)


def _kpi_reference() -> None:
    ui.section(
        "KPI formula reference",
        "Each metric exactly as the KPI engine computes it, with its finops.org source.",
    )
    frame = pd.DataFrame(
        [{"Metric": m, "Formula": f, "Source": s} for m, f, s in _KPI_FORMULAS]
    )
    st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        column_config={"Source": st.column_config.LinkColumn("Source", display_text="finops.org")},
    )
    with st.expander("kpi.py module docstring (the canonical source list, verbatim)", expanded=False):
        st.code((kpi.__doc__ or "").strip(), language="text")


def _links() -> None:
    ui.section("Further reading", "")
    st.markdown(
        "- FinOps Framework — https://www.finops.org/framework/\n"
        "- FinOps Foundation — https://www.finops.org/\n"
        "- FOCUS specification — https://focus.finops.org/\n"
        "- FOCUS changelog — https://focus.finops.org/changelog/"
    )
