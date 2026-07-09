"""The 24-month forecast against budget -- the headline planning deliverable.

Why this tab is shaped the way it is:

* **The forecast is a fan, not a line.** Finance acts on three readings: where
  we are, where we are heading, and where the likely range crosses budget. A
  single line hides the third, which is the only one that starts a conversation.

* **The budget is carried forward flat, deliberately.** Beyond the current
  fiscal year the budget frame is silent, so we hold the last known monthly
  budget level constant rather than invent an escalator. The collision between a
  growing forecast and a flat budget is precisely the thing leadership must see;
  smoothing it away would be dishonest.

* **WAPE is the headline accuracy, not MAPE.** WAPE is dollar-weighted, so a
  tiny service whose actual rounds to zero cannot dominate the score the way it
  does under MAPE. The score maps to the FinOps Foundation's forecast-variance
  maturity bands (Crawl < 20%, Walk < 15%, Run < 12%, best-in-class < 5%).

* **Two things a statistical model structurally cannot see get their own
  overlays.** A commitment (RI / SP / CUD) expiry is a scheduled step-UP in
  spend that no trend model can infer -- it is a contract event, not a trend --
  and a planned bottom-up driver (a meter-rollout wave) is known to the business
  before it reaches the bill. The overlays layer both on top of the top-line.

* **Per-cloud forecasts are small multiples, never three fans on one axis.**
  Overplotting three prediction bands on a shared axis is unreadable; three
  compact panels each answer their own question.

Everything here is amortised (`EffectiveCost`). All numbers come from the
`forecast` and `budget` engines -- this tab renders, it never computes.

Sources
-------
Forecasting capability / variance thresholds
    https://www.finops.org/framework/capabilities/forecasting/
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd
import streamlit as st

import charts
import theme
import ui
from finops_core import (
    FORECAST_VARIANCE_THRESHOLD,
    DataContext,
    Mode,
    maturity_for_variance,
)


# ==========================================================================
# Cached engine calls -- module-level, DataFrame in, so a slider drag that does
# not change the inputs never re-runs the backtest.
# ==========================================================================


@st.cache_data(show_spinner=False)
def _forecast(monthly: pd.DataFrame, horizon: int, method: str):
    import forecast as forecast_engine

    return forecast_engine.forecast_spend(monthly, horizon=horizon, method=method)


@st.cache_data(show_spinner=False)
def _variance_by_period(focus_df: pd.DataFrame, budgets: pd.DataFrame) -> pd.DataFrame:
    import budget as budget_engine

    return budget_engine.variance_table(focus_df, budgets, by=["period"])


@st.cache_data(show_spinner=False)
def _burn_down(focus_df: pd.DataFrame, budgets: pd.DataFrame) -> pd.DataFrame:
    import budget as budget_engine

    return budget_engine.burn_down(focus_df, budgets)


@st.cache_data(show_spinner=False)
def _run_rate(focus_df: pd.DataFrame) -> pd.DataFrame:
    import budget as budget_engine

    return budget_engine.run_rate_table(focus_df)


# ==========================================================================
# Panel helpers
# ==========================================================================


def _budget_line(ctx: DataContext, fc) -> Optional[pd.DataFrame]:
    """Project the monthly budget across the forecast horizon.

    Where the budget frame has no rows for a future month -- the normal case
    beyond the current fiscal year -- carry the last known monthly budget
    forward flat and let the fan chart show the collision. Same idea as the
    Executive tab: inventing an escalator would hide the thing that matters.
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


def _scale_bands(frame: pd.DataFrame, new_cost: pd.Series) -> pd.DataFrame:
    """Return a forecast frame whose point estimate is `new_cost`, with the
    prediction bands scaled by the same ratio so their width stays proportional.

    Used by the scenario and cliff overlays: `driver_overlay` /
    `commitment_expiry_overlay` move the point forecast but do not restate the
    interval, so we carry the band across at the same relative width rather than
    pretend the uncertainty vanished.
    """
    out = frame.copy()
    base = out["cost"].astype(float).replace(0.0, pd.NA)
    ratio = (new_cost.astype(float) / base).fillna(1.0)
    out["cost"] = new_cost.astype(float).to_numpy()
    for col in ("lo80", "hi80", "lo95", "hi95"):
        if col in out.columns:
            out[col] = (out[col].astype(float) * ratio).to_numpy()
    return out


# ==========================================================================
# Render
# ==========================================================================


def render(ctx: DataContext) -> None:
    df = ctx.focus_df
    mode = ui.mode()

    if df.empty:
        ui.callout("No charge rows in the current selection. Widen the filters above.")
        return

    monthly = ctx.monthly()
    if len(monthly) < 3:
        ui.callout("Not enough history in this selection to forecast. Widen the charge-period filter.")
        return

    # ---------------------------------------------------------------
    # Controls
    # ---------------------------------------------------------------
    ui.section(
        "Forecast controls",
        "One horizon, one method, applied to every panel below. The method 'auto' "
        "backtests the candidates on a rolling origin and keeps the lowest-WAPE one.",
    )
    import forecast as forecast_engine

    methods = forecast_engine.available_methods()
    c1, c2, c3, c4 = st.columns([1.4, 1.3, 1.1, 1.3])
    with c1:
        horizon = st.slider("Horizon (months)", min_value=6, max_value=36, value=24, step=1, key="fc_horizon")
    with c2:
        method = st.selectbox(
            "Method", methods, index=methods.index("auto") if "auto" in methods else 0, key="fc_method"
        )
    with c3:
        show_cliffs = st.toggle("Commitment cliffs", value=True, key="fc_cliffs",
                                help="Overlay the step-up in spend when RI/SP/CUD terms expire.")
    with c4:
        show_scenario = st.toggle("Scenario what-if", value=False, key="fc_scenario",
                                  help="Layer bottom-up driver adjustments on the statistical top-line.")

    fc = _forecast(monthly, horizon, method)
    if fc.forecast.empty:
        ui.callout("The forecast engine produced no rows for this selection.")
        return

    budget_line = _budget_line(ctx, fc)

    # ---------------------------------------------------------------
    # Headline KPI row
    # ---------------------------------------------------------------
    import budget as budget_engine

    ye = budget_engine.year_end_projection(df, ctx.budgets, fc.forecast, ctx.config.fiscal_year_start_month)
    wape = fc.accuracy.get("wape")
    total_h = float(fc.forecast["cost"].sum())

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        ui.tile("Method", fc.method.replace("_", " ").title(),
                sub=f"{fc.accuracy.get('folds', 0)} backtest folds", accent=True)
    with k2:
        ui.tile(
            "Forecast accuracy (WAPE)",
            ui.pct(wape) if wape is not None else "—",
            sub=f"Maturity: {fc.maturity}",
            status="good" if (wape or 99) < 12 else "warning" if (wape or 99) < 20 else "critical",
        )
    with k3:
        ui.tile(f"Next {horizon} months", ui.money(total_h), sub="Point forecast, cumulative")
    with k4:
        ui.tile(
            "Projected year-end variance",
            ui.money(ye.get("variance_abs")),
            sub=f"{ye.get('direction', '')} vs {ui.money(ye.get('annual_budget'))} budget",
            status="critical" if (ye.get("variance_pct") or 0) > 10 else "good",
        )

    # ---------------------------------------------------------------
    # The fan
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        f"{horizon}-month forecast against budget",
        "Point forecast with 80% and 95% likely ranges. The budget is carried "
        "forward flat beyond the funded year -- the collision is the point.",
    )
    fig = charts.forecast_fan(history=fc.history, forecast=fc.forecast, budget=budget_line, mode=mode, height=430)
    st.plotly_chart(fig, width="stretch")
    for note in fc.notes[:3]:
        st.caption(f"· {note}")

    fan_tbl = pd.concat(
        [fc.history.assign(series="Actual"), fc.forecast.assign(series="Forecast")], ignore_index=True
    )
    if budget_line is not None:
        fan_tbl = pd.concat([fan_tbl, budget_line.assign(series="Budget")], ignore_index=True)
    ui.table_view(fan_tbl, key="fc_fan", label="Forecast + budget table view")

    # ---------------------------------------------------------------
    # Accuracy panel
    # ---------------------------------------------------------------
    st.divider()
    ui.section("How good is this forecast?", "The backtest, and the maturity band the headline lands in.")

    acc_left, acc_right = st.columns([1, 1.15])
    with acc_left:
        acc_rows = [
            {"Metric": "WAPE (headline)", "Value": ui.pct(fc.accuracy.get("wape"))},
            {"Metric": "MAPE", "Value": ui.pct(fc.accuracy.get("mape"))},
            {"Metric": "sMAPE", "Value": ui.pct(fc.accuracy.get("smape"))},
            {"Metric": "Backtest folds", "Value": str(fc.accuracy.get("folds", 0))},
        ]
        st.dataframe(pd.DataFrame(acc_rows), width="stretch", hide_index=True)
        ui.callout(
            "**WAPE is the headline, not MAPE.** WAPE is dollar-weighted -- it divides the total "
            "absolute error by total actual spend -- so a small service whose actual rounds to zero "
            "cannot blow up the score the way it does under MAPE, which divides by each actual "
            "individually. The backtest is rolling-origin, never in-sample fit, which always flatters."
        )
    with acc_right:
        band_rows = []
        v = abs(fc.accuracy.get("wape")) if fc.accuracy.get("wape") is not None else None
        for name, thresh in FORECAST_VARIANCE_THRESHOLD.items():
            in_band = v is not None and maturity_for_variance(v) == name
            band_rows.append(
                {
                    "Maturity band": name,
                    "Variance threshold": f"< {thresh:.0f}%",
                    "This estate": "◆ here" if in_band else "",
                }
            )
        st.dataframe(pd.DataFrame(band_rows), width="stretch", hide_index=True)
        st.caption(
            "Foundation forecast-variance maturity bands. "
            "https://www.finops.org/framework/capabilities/forecasting/"
        )

    # ---------------------------------------------------------------
    # Commitment-expiry cliffs
    # ---------------------------------------------------------------
    if show_cliffs:
        st.divider()
        ui.section(
            "Commitment-expiry cliffs",
            "The single most important thing a naive forecast misses.",
        )
        cliffs = forecast_engine.commitment_expiry_overlay(df, fc.forecast)
        cliff_months = cliffs.loc[cliffs["cliff"], "period"]
        n_cliffs = int(cliffs["cliff"].sum())
        extra = float((cliffs["cost_with_cliffs"] - cliffs["cost"]).clip(lower=0).sum())

        ui.callout(
            "When an RI / Savings Plan / CUD term ends, the on-demand rate snaps back and spend steps "
            "**up** by the amortized discount the commitment was delivering. That is a scheduled "
            "contract event, not a trend -- a pure statistical model walks straight through it. This "
            "overlay reads the expiry straight out of the FOCUS purchase rows and places the step in "
            "the month it lands."
        )

        if n_cliffs == 0:
            st.caption("No commitment term expires inside this horizon on the current selection.")
        else:
            months_str = ", ".join(pd.to_datetime(cliff_months).dt.strftime("%b %Y").tolist())
            st.caption(
                f"{n_cliffs} cliff month(s) inside the horizon: **{months_str}** · "
                f"cumulative added spend if not renewed: **{ui.money(extra)}**"
            )
            cliff_frame = _scale_bands(fc.forecast, cliffs["cost_with_cliffs"])
            cl, cr = st.columns(2)
            with cl:
                st.markdown("**Trend only**")
                st.plotly_chart(
                    charts.forecast_fan(history=fc.history, forecast=fc.forecast, budget=budget_line,
                                        mode=mode, height=300),
                    width="stretch",
                )
            with cr:
                st.markdown("**With commitment-expiry cliffs**")
                st.plotly_chart(
                    charts.forecast_fan(history=fc.history, forecast=cliff_frame, budget=budget_line,
                                        mode=mode, height=300),
                    width="stretch",
                )
            st.caption("Both panels share the same budget line, so the cliff-driven divergence reads directly.")
        ui.table_view(
            cliffs[["period", "cost", "cost_with_cliffs", "cliff"]],
            key="fc_cliffs_tbl",
            label="Cliff overlay table view",
        )

    # ---------------------------------------------------------------
    # Scenario what-if
    # ---------------------------------------------------------------
    if show_scenario:
        st.divider()
        ui.section(
            "Scenario what-if",
            "Layer business-known drivers on top of the statistical top-line. Each row applies a "
            "permanent step from its month forward; adjustments compound in order.",
        )
        first_period = pd.to_datetime(fc.forecast["period"].iloc[0]).strftime("%Y-%m")
        seed = pd.DataFrame([{"period": first_period, "pct": 0.15, "label": "Meter rollout wave 2"}])
        edited = st.data_editor(
            seed,
            num_rows="dynamic",
            width="stretch",
            key="fc_scenario_editor",
            column_config={
                "period": st.column_config.TextColumn("Period (YYYY-MM)"),
                "pct": st.column_config.NumberColumn("Adjustment", format="%.2f",
                                                     help="Fraction, e.g. 0.15 = +15%"),
                "label": st.column_config.TextColumn("Driver"),
            },
        )
        adjustments: List[dict] = []
        for _, r in edited.iterrows():
            if pd.isna(r.get("period")) or pd.isna(r.get("pct")):
                continue
            adjustments.append({"period": str(r["period"]), "pct": float(r["pct"]),
                                "label": str(r.get("label", ""))})

        adj = forecast_engine.driver_overlay(fc.forecast, adjustments)
        scenario_frame = _scale_bands(fc.forecast, adj["cost_adjusted"])
        base_total = float(fc.forecast["cost"].sum())
        adj_total = float(adj["cost_adjusted"].sum())

        s1, s2, s3 = st.columns(3)
        with s1:
            ui.tile("Base forecast", ui.money(base_total), sub=f"{horizon} months, cumulative")
        with s2:
            ui.tile("With drivers", ui.money(adj_total), sub=f"{horizon} months, cumulative", accent=True)
        with s3:
            delta = adj_total - base_total
            ui.tile("Scenario delta", ui.money(delta),
                    sub="added by the drivers above",
                    status="critical" if delta > 0 else "good")

        st.plotly_chart(
            charts.forecast_fan(history=fc.history, forecast=scenario_frame, budget=budget_line,
                                mode=mode, height=360),
            width="stretch",
        )
        st.caption("Dashed line and band are the driver-adjusted forecast; the dotted line is the budget.")
        tbl = adj[["period", "cost", "cost_adjusted"]].copy()
        tbl["applied_drivers"] = adj["applied_drivers"].apply(lambda xs: ", ".join(xs) if isinstance(xs, list) else "")
        ui.table_view(tbl, key="fc_scenario_tbl", label="Scenario table view")

    # ---------------------------------------------------------------
    # Budget variance by period
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Budget variance by month", "Positive is an overrun. Colour is diverging around a neutral zero.")

    var = _variance_by_period(df, ctx.budgets)
    if var is None or var.empty:
        ui.callout(
            "No budgets are loaded for this selection. "
            + ("Demo Mode seeds them from a plan." if ctx.mode is Mode.DEMO
               else "Live Mode reads them from each cloud's native Budgets API, where one exists.")
        )
    else:
        v = var.copy()
        v["label"] = pd.to_datetime(v["period"]).dt.strftime("%b %y")
        st.plotly_chart(
            charts.variance_bars(v["label"].tolist(), v["variance_abs"].tolist(), mode=mode, height=300),
            width="stretch",
        )
        ui.table_view(var, key="fc_variance", label="Budget variance table view")

    # ---------------------------------------------------------------
    # Burn-down: cumulative actual vs cumulative budget
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Burn-down",
        "Cumulative actual against cumulative budget. Where the actual line crosses the budget line "
        "is the earliest visual warning finance gets.",
    )
    bd = _burn_down(df, ctx.budgets)
    if bd is None or bd.empty or bd["cumulative_budget"].sum() == 0:
        ui.callout("No budget to burn down against in this selection.")
    else:
        actual_line = bd.rename(columns={"cumulative_actual": "cost"})[["period", "cost"]]
        budget_cum = bd.rename(columns={"cumulative_budget": "cost"})[["period", "cost"]]
        empty_forecast = pd.DataFrame(columns=["period", "cost"])
        # forecast_fan draws two labelled lines (Actual solid, Budget dotted) on one axis with a
        # legend -- exactly the burn-down's two cumulative series, no dual axis.
        st.plotly_chart(
            charts.forecast_fan(history=actual_line, forecast=empty_forecast, budget=budget_cum,
                                mode=mode, height=320),
            width="stretch",
        )
        st.caption("Solid = cumulative actual, dotted = cumulative budget.")
        ui.table_view(bd, key="fc_burndown", label="Burn-down table view")

    # ---------------------------------------------------------------
    # Bridge: Budget -> Forecast
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Budget-to-forecast bridge",
        "Why the forecast sits where it does, decomposed into the components we can actually measure.",
    )
    bridge_labels, bridge_deltas, bridge_note = _bridge(ctx, fc, df, forecast_engine, budget_line)
    if bridge_labels is None:
        ui.callout("Not enough budget signal in this selection to build a bridge.")
    else:
        st.plotly_chart(
            charts.variance_waterfall(bridge_labels, bridge_deltas, mode=mode, height=340),
            width="stretch",
        )
        st.caption(bridge_note)
        ui.table_view(
            pd.DataFrame({"component": bridge_labels, "amount": bridge_deltas}),
            key="fc_bridge",
            label="Bridge table view",
        )

    # ---------------------------------------------------------------
    # Per-cloud small multiples
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        "Per-cloud forecasts",
        "Each cloud forecast on its own axis. Three prediction bands on one shared axis would be "
        "unreadable -- small multiples are the right answer.",
    )
    by_cloud = ctx.monthly_by("ProviderName")
    clouds = [c for c in theme.PROVIDERS if c in set(by_cloud["ProviderName"].unique())]
    clouds += [c for c in by_cloud["ProviderName"].unique() if c not in clouds]
    cols = st.columns(max(len(clouds), 1))
    for col, cloud in zip(cols, clouds):
        with col:
            st.markdown(f"**{cloud}**")
            sub = by_cloud[by_cloud["ProviderName"] == cloud][["period", "cost"]].sort_values("period")
            if len(sub) < 3:
                st.caption("Too little history to forecast.")
                continue
            fc_c = _forecast(sub, horizon, method)
            if fc_c.forecast.empty:
                st.caption("No forecast available.")
                continue
            st.plotly_chart(
                charts.forecast_fan(history=fc_c.history, forecast=fc_c.forecast, mode=mode, height=240),
                width="stretch",
                config={"displayModeBar": False},
            )
            st.caption(
                f"{fc_c.method.replace('_', ' ').title()} · WAPE "
                f"{ui.pct(fc_c.accuracy.get('wape')) if fc_c.accuracy.get('wape') is not None else '—'}"
            )

    # ---------------------------------------------------------------
    # Run-rate
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Run-rate by cloud", "Month-to-date, daily burn and the annualised run-rate finance carries in its head.")
    rr = _run_rate(df)
    if rr is None or rr.empty:
        ui.callout("No usage rows to compute a run-rate for this selection.")
    else:
        show = rr.rename(
            columns={
                "cloud": "Cloud",
                "mtd": "Month-to-date",
                "burn_rate_daily": "Daily burn",
                "run_rate_annual": "Annualised run-rate",
            }
        )
        st.dataframe(
            show,
            width="stretch",
            hide_index=True,
            column_config={
                "Month-to-date": st.column_config.NumberColumn(format="$%.0f"),
                "Daily burn": st.column_config.NumberColumn(format="$%.0f"),
                "Annualised run-rate": st.column_config.NumberColumn(format="$%.0f"),
            },
        )


def _bridge(ctx, fc, df, forecast_engine, budget_line):
    """Construct the budget->forecast waterfall from measurable components.

    `budget.bridge` needs a drivers dict; we build it from the pieces we can
    actually measure over the next 12 forecast months -- the commitment cliffs
    (read from the FOCUS purchase rows) and the AI/ML ramp (recent AI run-rate
    annualised, minus trailing-12 AI spend) -- and let underlying growth be the
    reconciling remainder, so the waterfall always ties to the forecast total.
    """
    import budget as budget_engine

    if budget_line is None or budget_line.empty:
        return None, None, None

    fc12 = fc.forecast.head(12)
    if fc12.empty:
        return None, None, None
    forecast_12 = float(fc12["cost"].sum())

    bl = budget_line.copy()
    future_budget = bl[bl["period"].isin(fc12["period"])]
    budget_12 = float(future_budget["cost"].sum()) if len(future_budget) else float(bl["cost"].tail(12).sum())
    if budget_12 <= 0:
        return None, None, None

    # Commitment cliffs over the next 12 months.
    cliffs = forecast_engine.commitment_expiry_overlay(df, fc12)
    cliff_amt = float((cliffs["cost_with_cliffs"] - cliffs["cost"]).clip(lower=0).sum())

    # AI/ML ramp: recent monthly AI run-rate annualised minus trailing-12 AI spend.
    ai = df[(df["ServiceCategory"] == "AI and Machine Learning") & (df["ChargeCategory"] == "Usage")].copy()
    ai_ramp = 0.0
    if len(ai):
        ai["m"] = ai["ChargePeriodStart"].dt.to_period("M")
        by_month = ai.groupby("m")["EffectiveCost"].sum().sort_index()
        trailing_ai = float(by_month.tail(12).sum())
        recent_annualised = float(by_month.iloc[-1]) * 12.0
        ai_ramp = max(0.0, recent_annualised - trailing_ai)

    ai_ramp = min(ai_ramp, max(forecast_12 - budget_12, 0.0))
    growth = (forecast_12 - budget_12) - cliff_amt - ai_ramp

    drivers = {
        "Underlying growth": round(growth, 2),
        "AI/ML ramp": round(ai_ramp, 2),
        "Commitment cliffs": round(cliff_amt, 2),
    }
    labels, deltas = budget_engine.bridge(budget_12, drivers)
    note = (
        "Budget and Forecast are the annual (next-12-month) totals. Commitment cliffs are read from the "
        "FOCUS purchase rows; the AI/ML ramp is the recent AI run-rate annualised minus trailing-12 AI "
        "spend; underlying growth is the reconciling remainder, so the bars tie exactly to the forecast."
    )
    return labels, deltas, note
