"""Multi-Cloud FinOps Command Center.

Streamlit entry point. Owns the chrome -- mode switch, the single filter row,
and the tab registry -- and nothing else. Every tab is a module in `tabs/` with
a `render(ctx)` function that receives an already-filtered `DataContext`.

Two operating modes, one code path:

    Demo Mode   synthetic FOCUS 1.2 estate, generated in-process, no credentials
    Live Mode   real billing data via the connectors in `connectors/`

The filter row lives here, above everything it scopes, so every panel on a page
always renders against the same slice. Charts never carry their own filters.
"""

from __future__ import annotations

import uuid
from datetime import date

import streamlit as st

import data
import theme
import ui
from finops_core import AppConfig, DataContext, Mode, load_config

# --------------------------------------------------------------------------
# Tabs. Order is the reading order of a FinOps conversation: what happened,
# who owes what, where is it going, what do we do about it, how do we run the
# practice.
# --------------------------------------------------------------------------

TABS = [
    ("Executive", "tabs.executive", "The VP/Director view"),
    ("Applications", "tabs.applications", "Spend by application and business unit"),
    ("Showback & Chargeback", "tabs.showback", "Allocation, shared costs, invoices"),
    ("Baseline", "tabs.baseline", "Run-rate against a trailing baseline"),
    ("Forecast & Budget", "tabs.forecast_tab", "24-month forecast vs budget"),
    ("Optimize", "tabs.optimize_tab", "FinOps levers and opportunities"),
    ("Anomalies", "tabs.anomalies", "Spend anomaly detection"),
    ("Unit Economics", "tabs.unit_economics", "Cost per business driver"),
    ("Governance", "tabs.governance", "Tagging, coverage, policy"),
    ("AI Copilot", "tabs.copilot", "LangGraph agent team"),
    ("Integrations", "tabs.integrations", "Connectors and data sources"),
    ("Reference", "tabs.reference", "FinOps Framework and FOCUS"),
]


def _sidebar() -> tuple[AppConfig, dict]:
    with st.sidebar:
        st.markdown("### ◇ Command Center")

        # ---- Mode -------------------------------------------------------
        current = st.session_state.get("mode", load_config().mode)
        mode_label = st.radio(
            "Data source",
            options=[Mode.DEMO, Mode.LIVE],
            format_func=lambda m: m.label,
            index=0 if current is Mode.DEMO else 1,
            key="mode_radio",
            horizontal=True,
        )
        st.session_state["mode"] = mode_label
        st.caption(mode_label.blurb)

        cfg = load_config(mode=mode_label)

        if mode_label is Mode.LIVE:
            with st.expander("Connector assignment", expanded=False):
                import connectors as reg

                for cloud in ("AWS", "Azure", "GCP"):
                    options = reg.connectors_for_cloud(cloud) or ["demo"]
                    default = cfg.connector_for.get(cloud, options[0])
                    idx = options.index(default) if default in options else 0
                    cfg.connector_for[cloud] = st.selectbox(cloud, options, index=idx, key=f"conn_{cloud}")

        st.divider()

        # ---- Appearance --------------------------------------------------
        dark = st.toggle("Dark mode", value=(ui.mode() == "dark"), key="dark_toggle")
        st.session_state["colour_mode"] = "dark" if dark else "light"

        st.divider()
        st.caption(f"Organisation: **{cfg.organisation}**")
        st.caption(f"AI Copilot: {'enabled' if cfg.ai_enabled else 'set OPENAI_API_KEY to enable'}")

    return cfg, {}


def _filter_row(ctx: DataContext) -> DataContext:
    """One filter row, above everything it scopes."""
    clouds = ctx.clouds
    apps = ctx.applications
    bus = ctx.business_units
    envs = ctx.environments
    lo, hi = ctx.period_range

    c1, c2, c3, c4, c5 = st.columns([1.1, 1.6, 1.4, 1.0, 1.6])
    with c1:
        sel_clouds = st.multiselect("Cloud", clouds, default=clouds, key="f_cloud")
    with c2:
        sel_apps = st.multiselect("Application", apps, default=[], key="f_app",
                                  placeholder="All applications")
    with c3:
        sel_bus = st.multiselect("Business unit", bus, default=[], key="f_bu",
                                 placeholder="All business units")
    with c4:
        sel_envs = st.multiselect("Environment", envs, default=[], key="f_env",
                                  placeholder="All")
    with c5:
        rng = st.date_input(
            "Charge period",
            value=(lo.date(), hi.date()),
            min_value=lo.date(),
            max_value=hi.date(),
            key="f_range",
        )

    start, end = (rng if isinstance(rng, tuple) and len(rng) == 2 else (lo.date(), hi.date()))
    return ctx.filtered(
        clouds=sel_clouds or None,
        applications=sel_apps or None,
        business_units=sel_bus or None,
        environments=sel_envs or None,
        start=start,
        end=end,
    )


def _masthead_right(ctx: DataContext) -> str:
    live = ctx.mode is Mode.LIVE
    colour = theme.STATUS["good"] if live else theme.surface(ui.mode()).categorical[0]
    label = "LIVE" if live else "DEMO"
    rows = len(ctx.focus_df)
    conform = "FOCUS 1.2" if ctx.validation and ctx.validation.ok else "FOCUS (check)"
    return (
        f'{ui.pill(label, colour)} &nbsp; {ui.pill(conform)} &nbsp; '
        f'{ui.pill(f"{rows:,} charge rows")}'
    )


def main() -> None:
    ui.page_config()
    ui.inject_css()

    if not ui.require_login():
        return

    cfg, _ = _sidebar()

    if "thread_id" not in st.session_state:
        st.session_state["thread_id"] = str(uuid.uuid4())

    try:
        ctx = data.load_context(cfg)
    except Exception as exc:
        st.error(f"Could not load data: {exc}")
        st.stop()
        return

    ui.masthead(right_html=_masthead_right(ctx))

    if ctx.mode is Mode.LIVE and ctx.focus_df.empty:
        st.warning(
            "Live Mode is selected but no connector returned data. "
            "Check the **Integrations** tab for per-connector status, or switch to Demo Mode."
        )

    if not ctx.focus_df.empty:
        scoped = _filter_row(ctx)
    else:
        scoped = ctx

    st.markdown("")

    tabs = st.tabs([name for name, _, _ in TABS])
    for tab, (name, module_path, _) in zip(tabs, TABS):
        with tab:
            try:
                import importlib

                mod = importlib.import_module(module_path)
                mod.render(scoped)
            except ModuleNotFoundError:
                st.info(f"`{module_path}` is not present in this build.")
            except Exception as exc:  # one broken tab must not take down the app
                st.error(f"**{name}** failed to render: `{exc}`")
                with st.expander("Traceback"):
                    import traceback

                    st.code(traceback.format_exc())


if __name__ == "__main__":
    main()
