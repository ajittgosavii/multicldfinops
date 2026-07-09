"""Spend anomaly detection.

Why this tab is shaped the way it is:

* **It detects unusual-after-seasonality, not merely unusual.** A utility's
  cloud bill has weekday/weekend structure and a monthly cycle. A detector that
  ignores them pages someone every Monday. The default `stl_mad` decomposes
  trend + seasonal + residual and runs a median-absolute-deviation test on the
  RESIDUAL, so the natural shape of spend is modelled out rather than alarmed on.

* **Detection runs per dimension, not just on the estate total.** A spike in one
  small service is invisible in the pooled total but unmistakable against its own
  baseline. That is the whole reason a small anomaly is findable at all.

* **Severity is never carried by colour alone.** Every flagged point ships an
  icon + label status pill AND a distinct diamond marker symbol -- two redundant
  channels -- so the signal survives colour-vision deficiency and greyscale
  print.

* **AWS Cost Anomaly Detection semantics, deliberately.** A >= 10-day warm-up
  before anything is flagged, and dynamic thresholds relative to the series' own
  dispersion, never a static dollar amount.

This tab renders; the `anomaly` engine computes.

Source: https://docs.aws.amazon.com/cost-management/latest/userguide/manage-ad.html
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import charts
import ui
from finops_core import DataContext

_DIMENSIONS = ["ServiceCategory", "ProviderName", "tag_application", "ServiceName"]
_METHODS = ["stl_mad", "mad", "zscore"]


def _scope_lookback(focus_df: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    """Keep only charge rows within `lookback_days` of the latest charge date.

    Anomaly detection reads day-grain rows; scoping the window keeps the baseline
    recent and bounds the warm-up to the period a reviewer actually cares about.
    """
    if focus_df.empty:
        return focus_df
    last = focus_df["ChargePeriodStart"].max()
    cutoff = last - pd.Timedelta(days=lookback_days)
    return focus_df[focus_df["ChargePeriodStart"] >= cutoff]


@st.cache_data(show_spinner=False)
def _detect_dim(focus_df: pd.DataFrame, dim: str, method: str, threshold: float) -> pd.DataFrame:
    import anomaly

    return anomaly.detect_by_dimension(focus_df, dim=dim, method=method, threshold=threshold)


@st.cache_data(show_spinner=False)
def _detect_total(focus_df: pd.DataFrame, method: str, threshold: float) -> pd.DataFrame:
    import anomaly

    series = anomaly.daily_spend(focus_df)
    if series.empty:
        return series
    return anomaly.detect(series, method=method, threshold=threshold)


def render(ctx: DataContext) -> None:
    df = ctx.focus_df
    mode = ui.mode()

    if df.empty:
        ui.callout("No charge rows in the current selection. Widen the filters above.")
        return

    import anomaly

    # ---------------------------------------------------------------
    # Controls
    # ---------------------------------------------------------------
    ui.section("Detection controls", "Dimension, method and sensitivity apply to every panel below.")
    c1, c2, c3, c4 = st.columns([1.4, 1.2, 1.3, 1.3])
    with c1:
        dim = st.selectbox("Dimension", _DIMENSIONS, index=0, key="an_dim")
    with c2:
        method = st.selectbox("Method", _METHODS, index=0, key="an_method")
    with c3:
        threshold = st.slider("Threshold (modified z)", min_value=2.0, max_value=5.0, value=3.5, step=0.1, key="an_thr")
    with c4:
        span_days = int((df["ChargePeriodStart"].max() - df["ChargePeriodStart"].min()).days) + 1
        lookback = st.slider("Lookback (days)", min_value=30, max_value=max(span_days, 60),
                             value=min(180, max(span_days, 60)), step=10, key="an_lookback")

    scoped = _scope_lookback(df, lookback)
    flagged = _detect_dim(scoped, dim, method, threshold)
    total = _detect_total(scoped, method, threshold)

    last_data = pd.Timestamp(df["ChargePeriodStart"].max()).normalize()

    # ---------------------------------------------------------------
    # KPI row
    # ---------------------------------------------------------------
    n_found = int(len(flagged))
    if n_found:
        dev_abs = (flagged["cost"] - flagged["expected"])
        total_dev = float(dev_abs.abs().sum())
        largest = float(dev_abs.abs().max())
        last_anom = pd.Timestamp(flagged["period"].max()).normalize()
        days_since = int((last_data - last_anom).days)
    else:
        total_dev = largest = 0.0
        days_since = None

    k1, k2, k3, k4 = st.columns(4)
    with k1:
        ui.tile("Anomalies found", str(n_found), sub=f"by {dim} · {method}", accent=True,
                status="critical" if n_found else "good")
    with k2:
        ui.tile("Total deviation", ui.money(total_dev), sub="sum of |actual − expected|")
    with k3:
        ui.tile("Largest single deviation", ui.money(largest), sub="worst flagged day")
    with k4:
        ui.tile("Days since last anomaly", str(days_since) if days_since is not None else "—",
                sub="relative to latest charge date")

    # ---------------------------------------------------------------
    # Method explainer
    # ---------------------------------------------------------------
    ui.callout(
        "**stl_mad** decomposes the series into trend + seasonal + residual and applies a "
        "median-absolute-deviation test to the **residual**, so weekday/weekend and monthly cycles "
        "do not trip alerts. The modified z-score is `0.6745·(x − median)/MAD`, flagged past a "
        "threshold of ~3.5. This mirrors AWS Cost Anomaly Detection: a >= 10-day warm-up before "
        "anything is flagged, and dynamic thresholds relative to the series' own dispersion -- never "
        "a static dollar amount. "
        "https://docs.aws.amazon.com/cost-management/latest/userguide/manage-ad.html"
    )

    # ---------------------------------------------------------------
    # Estate-total chart
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Estate-total daily spend", "The pooled total. Small anomalies hide here -- the per-dimension grid below finds them.")
    if total is None or total.empty:
        ui.callout("No day-grain rows in this lookback window to chart.")
    else:
        st.plotly_chart(
            charts.anomaly_scatter(total, "period", "cost", "is_anomaly", mode=mode, height=320),
            use_container_width=True,
        )
        ui.table_view(total, key="an_total", label="Estate-total detection table view")

    # ---------------------------------------------------------------
    # Per-dimension small multiples
    # ---------------------------------------------------------------
    st.divider()
    ui.section(
        f"Per-{dim} detection",
        "Each dimension value scored against its own baseline. Only values with a flagged day are shown.",
    )
    if n_found == 0:
        ui.callout("No anomalies detected for this dimension at the current threshold and lookback.")
    else:
        flagged_values = (
            flagged.groupby(dim, observed=True)
            .apply(lambda g: (g["cost"] - g["expected"]).abs().max())
            .sort_values(ascending=False)
        )
        top_values = list(flagged_values.index[:6])
        series_all = anomaly.daily_spend(scoped, dim=dim)
        grid = st.columns(2)
        for i, value in enumerate(top_values):
            sub = series_all[series_all[dim] == value][["period", "cost"]]
            if len(sub) < 10:
                continue
            annotated = anomaly.detect(sub, method=method, threshold=threshold)
            with grid[i % 2]:
                st.markdown(f"**{value}**")
                st.plotly_chart(
                    charts.anomaly_scatter(annotated, "period", "cost", "is_anomaly", mode=mode, height=240),
                    use_container_width=True,
                    config={"displayModeBar": False},
                )

    # ---------------------------------------------------------------
    # Flagged table
    # ---------------------------------------------------------------
    st.divider()
    ui.section("Flagged anomalies", "Sorted by score. Severity carries an icon + label pill and a diamond marker -- never colour alone.")
    points = anomaly.summarise(flagged)
    if not points:
        ui.callout("Nothing flagged for this selection.")
    else:
        rows = []
        for p in points:
            rows.append(
                {
                    "Period": pd.Timestamp(p.period).strftime("%Y-%m-%d"),
                    "Dimension": p.dimension,
                    "Actual": ui.money(p.value),
                    "Expected": ui.money(p.expected),
                    "Deviation $": ui.money(p.deviation_abs),
                    "Deviation %": ui.pct(p.deviation_pct),
                    "Severity": p.severity,
                }
            )
        table = pd.DataFrame(rows)
        for p, (_, _r) in zip(points, table.iterrows()):
            st.markdown(
                f"{ui.status_pill(p.severity, p.severity.title())} &nbsp; "
                f"**{pd.Timestamp(p.period).strftime('%d %b %Y')}** · {p.dimension} &nbsp; "
                f"{ui.money(p.value)} actual vs {ui.money(p.expected)} expected &nbsp; "
                f"({p.deviation_pct:+.0f}%, {ui.money(p.deviation_abs)})",
                unsafe_allow_html=True,
            )
        st.caption(
            "Severity is redundant-coded: the pill's icon + label and the diamond marker on the chart "
            "both carry it, so it never depends on colour alone."
        )
        ui.table_view(table, key="an_flagged", label="Flagged anomalies table view")
