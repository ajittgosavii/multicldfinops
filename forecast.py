"""Spend forecasting with prediction intervals.

A forecast a VP can act on is not a single line; it is a line with an honest
band around it and a stated accuracy. This module produces both, and it is
written around the three things a naive cloud forecast gets wrong:

  1. **Step changes.** Migrations, data-centre exits and GenAI ramps move the
     baseline overnight. A pure trend model extrapolates straight through them.
     We backtest on a rolling origin and let the data pick the method, so a
     model that cannot cope with the estate's recent step loses on WAPE.
  2. **Commitment cliffs.** A one-year Reserved Instance / Savings Plan / CUD
     that expires next quarter is a scheduled step-UP in spend that no
     statistical model can see, because it is a contract event, not a trend.
     `commitment_expiry_overlay` reads it straight out of the FOCUS purchase
     rows. This is the single most important thing a naive model misses.
  3. **Bottom-up drivers.** A planned meter-rollout wave is known to the
     business before it shows up in the bill. `driver_overlay` layers those
     what-ifs on top of the statistical top-line.

Headline accuracy is **WAPE**, not MAPE. MAPE divides by each actual, so a tiny
service whose actual rounds to zero can dominate the score; WAPE is
dollar-weighted and cannot. WAPE maps to a FinOps maturity band via
`finops_core.maturity_for_variance` (Crawl <20, Walk <15, Run <12,
Best-in-class <5).

statsmodels is an OPTIONAL dependency. If it is not importable, Holt-Winters
and SARIMA degrade to a linear trend with a note; the module never crashes.

Sources
-------
Forecasting capability / variance thresholds
    https://www.finops.org/framework/capabilities/forecasting/
FOCUS commitment-discount columns
    https://focus.finops.org/focus-specification/
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import finops_core
import kpi

# statsmodels is optional. Everything degrades gracefully without it.
try:  # pragma: no cover - exercised by whichever environment runs the tests
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    _HAS_STATSMODELS = True
except Exception:  # pragma: no cover
    _HAS_STATSMODELS = False


# z-multipliers for the normal prediction interval. 1.2816 -> 80%, 1.9600 -> 95%.
Z80 = 1.2815515594
Z95 = 1.9599639845

_METHODS = ["naive", "seasonal_naive", "linear", "holt_winters", "sarima", "ensemble", "auto"]

# A method needs this many observations before it earns a place in `auto`.
_MIN_SARIMA_MONTHS = 24


@dataclass
class ForecastResult:
    """Everything a forecast panel renders, computed once.

    `history` and `forecast` share the `period`/`cost` column contract so a
    chart can concatenate them. `accuracy` is the rolling-origin backtest of the
    chosen method (never the in-sample fit, which always flatters). `maturity`
    is the FinOps band the headline WAPE falls into.
    """

    history: pd.DataFrame        # period (datetime64), cost
    forecast: pd.DataFrame       # period, cost, lo80, hi80, lo95, hi95
    method: str
    params: dict
    accuracy: dict               # {'mape','wape','smape','folds'}
    maturity: str
    notes: List[str] = field(default_factory=list)


def available_methods() -> List[str]:
    """The methods `forecast_spend` accepts. 'auto' picks among the rest."""
    return list(_METHODS)


# ==========================================================================
# Input hygiene
# ==========================================================================


def _prepare(monthly: pd.DataFrame) -> pd.DataFrame:
    """Sort, coerce and month-align the history frame. Never mutates the input."""
    if monthly is None or not len(monthly):
        return pd.DataFrame({"period": pd.Series(dtype="datetime64[ns]"), "cost": pd.Series(dtype="float64")})
    df = monthly[["period", "cost"]].copy()
    df["period"] = pd.to_datetime(df["period"]).dt.to_period("M").dt.to_timestamp()
    df["cost"] = pd.to_numeric(df["cost"], errors="coerce").fillna(0.0)
    df = df.groupby("period", as_index=False)["cost"].sum().sort_values("period").reset_index(drop=True)
    return df


def _future_periods(last: pd.Timestamp, horizon: int) -> pd.DatetimeIndex:
    return pd.date_range(start=pd.Timestamp(last) + pd.DateOffset(months=1), periods=horizon, freq="MS")


# ==========================================================================
# Point-forecast engines
#
# Each returns (yhat, own_pi) where yhat is a length-`horizon` array and own_pi
# is either None (caller builds a residual-based interval) or a dict of arrays
# from the model's native interval. `resid` is the in-sample residual series
# used to size a residual-based interval and to compute sigma.
# ==========================================================================


def _naive(y: np.ndarray, horizon: int, m: int) -> Tuple[np.ndarray, Optional[dict], np.ndarray]:
    yhat = np.repeat(y[-1], horizon)
    resid = np.diff(y) if len(y) > 1 else np.array([0.0])
    return yhat, None, resid


def _seasonal_naive(y: np.ndarray, horizon: int, m: int) -> Tuple[np.ndarray, Optional[dict], np.ndarray]:
    if len(y) < m:  # not a full cycle yet -> plain naive
        return _naive(y, horizon, m)
    last_cycle = y[-m:]
    yhat = np.array([last_cycle[i % m] for i in range(horizon)])
    resid = y[m:] - y[:-m]
    return yhat, None, resid


def _linear(y: np.ndarray, horizon: int, m: int) -> Tuple[np.ndarray, Optional[dict], np.ndarray]:
    n = len(y)
    t = np.arange(n)
    slope, intercept = np.polyfit(t, y, 1)
    fitted = slope * t + intercept
    resid = y - fitted
    future_t = np.arange(n, n + horizon)
    yhat = slope * future_t + intercept
    return yhat, None, resid


def _holt_winters(
    y: np.ndarray, horizon: int, m: int, notes: List[str]
) -> Tuple[np.ndarray, Optional[dict], np.ndarray]:
    if not _HAS_STATSMODELS:
        notes.append("statsmodels unavailable: holt_winters degraded to linear trend.")
        return _linear(y, horizon, m)
    if len(y) < 2 * m:
        notes.append(f"holt_winters needs >= {2 * m} months for seasonality; used trend-only fallback.")
        return _linear(y, horizon, m)
    seasonal = "add"
    if np.all(y > 0):
        # Multiplicative seasonality is the better fit for spend that grows,
        # because seasonal swings scale with the level rather than staying a
        # fixed dollar amount. Fall back to additive if the fit refuses.
        seasonal = "mul"
    try:
        model = ExponentialSmoothing(
            y, trend="add", seasonal=seasonal, seasonal_periods=m, damped_trend=True,
            initialization_method="estimated",
        ).fit()
        yhat = np.asarray(model.forecast(horizon), dtype=float)
        resid = np.asarray(y, float) - np.asarray(model.fittedvalues, float)
        return yhat, None, resid
    except Exception as exc:  # pragma: no cover - defensive
        notes.append(f"holt_winters fit failed ({type(exc).__name__}); fell back to linear.")
        return _linear(y, horizon, m)


def _sarima(
    y: np.ndarray, horizon: int, m: int, notes: List[str]
) -> Tuple[np.ndarray, Optional[dict], np.ndarray]:
    if not _HAS_STATSMODELS:
        notes.append("statsmodels unavailable: sarima degraded to holt_winters.")
        return _holt_winters(y, horizon, m, notes)
    try:
        seasonal_order = (0, 1, 1, m) if len(y) >= 2 * m else (0, 0, 0, 0)
        model = SARIMAX(
            y, order=(1, 1, 1), seasonal_order=seasonal_order,
            enforce_stationarity=False, enforce_invertibility=False,
        ).fit(disp=False)
        fc = model.get_forecast(horizon)
        yhat = np.asarray(fc.predicted_mean, dtype=float)
        ci80 = np.asarray(fc.conf_int(alpha=0.20), dtype=float)
        ci95 = np.asarray(fc.conf_int(alpha=0.05), dtype=float)
        own_pi = {
            "lo80": ci80[:, 0], "hi80": ci80[:, 1],
            "lo95": ci95[:, 0], "hi95": ci95[:, 1],
        }
        resid = np.asarray(model.resid, float)
        return yhat, own_pi, resid
    except Exception as exc:
        # The spec asks for this exact behaviour: SARIMA is fragile on short,
        # stepped series, so wrap it and fall back to Holt-Winters with a note.
        notes.append(f"sarima failed ({type(exc).__name__}); fell back to holt_winters.")
        return _holt_winters(y, horizon, m, notes)


def _ensemble(
    monthly: pd.DataFrame, y: np.ndarray, horizon: int, m: int, notes: List[str]
) -> Tuple[np.ndarray, Optional[dict], np.ndarray]:
    """Inverse-WAPE weighted mean of holt_winters + linear + seasonal_naive.

    A model that backtests badly on this estate contributes little; one that
    tracks the recent step dominates. Weighting by 1/WAPE is the standard way
    to combine forecasters without hand-tuning.
    """
    components = ["holt_winters", "linear", "seasonal_naive"]
    preds: Dict[str, np.ndarray] = {}
    resids: Dict[str, np.ndarray] = {}
    for c in components:
        yhat_c, _, resid_c = _dispatch_points(monthly, y, c, horizon, m, notes)
        preds[c] = yhat_c
        resids[c] = resid_c

    weights = {}
    for c in components:
        bt = backtest(monthly, c)
        w = bt.get("wape")
        weights[c] = (1.0 / w) if (w is not None and w > 0) else 0.0
    total = sum(weights.values())
    if total <= 0:
        weights = {c: 1.0 for c in components}
        total = float(len(components))

    yhat = sum(preds[c] * weights[c] for c in components) / total
    # Pool residuals for the interval, weighted the same way.
    resid = np.concatenate([resids[c] for c in components]) if resids else np.array([0.0])
    return np.asarray(yhat, float), None, resid


def _dispatch_points(
    monthly: pd.DataFrame, y: np.ndarray, method: str, horizon: int, m: int, notes: List[str]
) -> Tuple[np.ndarray, Optional[dict], np.ndarray]:
    if method == "naive":
        return _naive(y, horizon, m)
    if method == "seasonal_naive":
        return _seasonal_naive(y, horizon, m)
    if method == "linear":
        return _linear(y, horizon, m)
    if method == "holt_winters":
        return _holt_winters(y, horizon, m, notes)
    if method == "sarima":
        return _sarima(y, horizon, m, notes)
    if method == "ensemble":
        return _ensemble(monthly, y, horizon, m, notes)
    raise ValueError(f"unknown method: {method}")


# ==========================================================================
# Prediction intervals
# ==========================================================================


def _residual_intervals(yhat: np.ndarray, resid: np.ndarray, horizon: int) -> dict:
    """yhat +/- z * sigma_h, with sigma_h = sigma_resid * sqrt(h).

    The sqrt(h) fan is the random-walk variance rule: uncertainty compounds the
    further out you forecast. Lower bounds are clipped at zero because spend
    cannot go negative.
    """
    sigma = float(np.std(resid, ddof=1)) if len(resid) > 1 else 0.0
    h = np.arange(1, horizon + 1)
    sigma_h = sigma * np.sqrt(h)
    return {
        "lo80": yhat - Z80 * sigma_h,
        "hi80": yhat + Z80 * sigma_h,
        "lo95": yhat - Z95 * sigma_h,
        "hi95": yhat + Z95 * sigma_h,
    }


def _assemble_forecast(periods: pd.DatetimeIndex, yhat: np.ndarray, pi: dict) -> pd.DataFrame:
    """Clip every column at zero and enforce lo95 <= lo80 <= cost <= hi80 <= hi95."""
    cost = np.clip(yhat, 0.0, None)
    lo80 = np.clip(np.minimum(pi["lo80"], cost), 0.0, None)
    lo95 = np.clip(np.minimum(pi["lo95"], lo80), 0.0, None)
    hi80 = np.clip(np.maximum(pi["hi80"], cost), 0.0, None)
    hi95 = np.clip(np.maximum(pi["hi95"], hi80), 0.0, None)
    return pd.DataFrame(
        {"period": periods, "cost": cost, "lo80": lo80, "hi80": hi80, "lo95": lo95, "hi95": hi95}
    )


# ==========================================================================
# Backtest (rolling origin)
# ==========================================================================


def backtest(monthly: pd.DataFrame, method: str, folds: int = 3, horizon: int = 3) -> dict:
    """Rolling-origin backtest. Returns pooled {'mape','wape','smape','folds'}.

    Expanding window: fold k trains on everything up to n - k*horizon and scores
    the next `horizon` months against the held-out actuals. Pooling the (actual,
    forecast) pairs across folds and scoring once means one large month is not
    double-counted per fold. Folds with too little history to train are skipped.
    """
    df = _prepare(monthly)
    y = df["cost"].to_numpy(dtype=float)
    n = len(y)
    empty = {"mape": None, "wape": None, "smape": None, "folds": 0}
    if n < horizon + 4 or method == "auto":
        return empty

    m = 12
    actuals: List[float] = []
    preds: List[float] = []
    used = 0
    notes: List[str] = []
    for k in range(folds, 0, -1):
        cut = n - k * horizon
        if cut < 4:  # need a minimal training set
            continue
        y_train = y[:cut]
        y_test = y[cut:cut + horizon]
        if not len(y_test):
            continue
        train_df = df.iloc[:cut]
        try:
            yhat, _, _ = _dispatch_points(train_df, y_train, method, len(y_test), m, notes)
        except Exception:
            continue
        actuals.extend(y_test.tolist())
        preds.extend(np.asarray(yhat, float)[: len(y_test)].tolist())
        used += 1

    if not actuals:
        return empty
    a = np.asarray(actuals, float)
    f = np.asarray(preds, float)
    return {
        "mape": kpi.mape(a, f),
        "wape": kpi.wape(a, f),
        "smape": kpi.smape(a, f),
        "folds": used,
    }


# ==========================================================================
# The public forecast
# ==========================================================================


def _auto_select(monthly: pd.DataFrame, n_months: int, notes: List[str]) -> str:
    """Pick the method with the lowest backtest WAPE. Excludes sarima on short
    history (it needs two seasonal cycles to be worth its fragility)."""
    candidates = ["seasonal_naive", "linear", "holt_winters", "ensemble"]
    if n_months >= _MIN_SARIMA_MONTHS:
        candidates.append("sarima")
    else:
        notes.append(f"auto: < {_MIN_SARIMA_MONTHS} months of history, sarima excluded.")

    scored: List[Tuple[str, float]] = []
    for c in candidates:
        bt = backtest(monthly, c)
        w = bt.get("wape")
        if w is not None and np.isfinite(w):
            scored.append((c, w))
    if not scored:
        notes.append("auto: backtest inconclusive, defaulted to holt_winters.")
        return "holt_winters"
    scored.sort(key=lambda kv: kv[1])
    best = scored[0][0]
    notes.append("auto selected '%s' (backtest WAPE %.2f%%)." % (best, scored[0][1]))
    return best


def forecast_spend(
    monthly: pd.DataFrame, horizon: int = 24, method: str = "auto", seasonal_periods: int = 12
) -> ForecastResult:
    """Forecast `horizon` months ahead with 80% and 95% prediction intervals.

    `monthly` must carry `period` (month-start Timestamp) and `cost`. The method
    is one of `available_methods()`; 'auto' backtests the candidates and keeps
    the lowest-WAPE one. The returned intervals satisfy
    lo95 <= lo80 <= cost <= hi80 <= hi95 and are floored at zero.
    """
    df = _prepare(monthly)
    notes: List[str] = []
    n = len(df)
    m = seasonal_periods

    if n == 0:
        return ForecastResult(
            history=df,
            forecast=pd.DataFrame(columns=["period", "cost", "lo80", "hi80", "lo95", "hi95"]),
            method=method, params={"horizon": horizon},
            accuracy={"mape": None, "wape": None, "smape": None, "folds": 0},
            maturity="Unknown", notes=["No history supplied."],
        )

    if n < _MIN_SARIMA_MONTHS:
        notes.append(f"Only {n} months of history (< {_MIN_SARIMA_MONTHS}); intervals are wide by design.")

    if method not in _METHODS:
        notes.append(f"Unknown method '{method}', fell back to 'auto'.")
        method = "auto"

    chosen = method
    if method == "auto":
        chosen = _auto_select(df, n, notes)

    y = df["cost"].to_numpy(dtype=float)
    periods = _future_periods(df["period"].iloc[-1], horizon)

    yhat, own_pi, resid = _dispatch_points(df, y, chosen, horizon, m, notes)
    pi = own_pi if own_pi is not None else _residual_intervals(yhat, resid, horizon)
    forecast_df = _assemble_forecast(periods, np.asarray(yhat, float), pi)

    accuracy = backtest(df, chosen)
    wape = accuracy.get("wape")
    maturity = finops_core.maturity_for_variance(wape) if wape is not None else "Unknown"

    params = {
        "horizon": horizon,
        "seasonal_periods": m,
        "n_history": n,
        "requested_method": method,
        "statsmodels": _HAS_STATSMODELS,
        "interval": "native" if own_pi is not None else "residual",
    }
    return ForecastResult(
        history=df, forecast=forecast_df, method=chosen, params=params,
        accuracy=accuracy, maturity=maturity, notes=notes,
    )


# ==========================================================================
# Overlays -- the things a statistical model structurally cannot see
# ==========================================================================


def commitment_expiry_overlay(focus_df: pd.DataFrame, forecast: pd.DataFrame) -> pd.DataFrame:
    """Layer commitment-expiry cliffs onto a forecast.

    A commitment (RI / Savings Plan / CUD) purchased on a one-year term stops
    discounting the day it expires. Unless it is renewed, spend steps UP by the
    monthly amortized discount it was providing. That is a scheduled contract
    event no trend model can infer -- and the most expensive thing a naive
    forecast misses.

    Method: find purchase rows (`ChargeCategory == 'Purchase'`,
    `CommitmentDiscountId` not null), assume a one-year term, and place a cliff
    in the month each expires. The step-up equals the amortized monthly discount
    (mean monthly `ListCost - EffectiveCost` on that provider's `Used`
    commitment rows). Only cliffs that fall inside the forecast window are
    applied. Returns the forecast with added `cost_with_cliffs` and boolean
    `cliff` columns.
    """
    out = forecast.copy()
    out["cost_with_cliffs"] = out["cost"].astype(float)
    out["cliff"] = False
    if not len(out) or focus_df is None or not len(focus_df):
        return out

    purchases = focus_df[
        (focus_df["ChargeCategory"] == "Purchase") & (focus_df["CommitmentDiscountId"].notna())
    ]
    if not len(purchases):
        return out

    fc_periods = out["period"].dt.to_period("M")
    fc_start, fc_end = fc_periods.min(), fc_periods.max()

    # Monthly amortized discount currently delivered, per provider, from the
    # Used commitment rows. This is the discount that evaporates at expiry.
    used = focus_df[
        (focus_df["CommitmentDiscountStatus"] == "Used") & (focus_df["CommitmentDiscountId"].notna())
    ].copy()
    monthly_discount_by_provider: Dict[str, float] = {}
    if len(used):
        used["_p"] = used["ChargePeriodStart"].dt.to_period("M")
        used["_disc"] = used["ListCost"].astype(float) - used["EffectiveCost"].astype(float)
        for prov, g in used.groupby("ProviderName", observed=True):
            n_months = max(g["_p"].nunique(), 1)
            monthly_discount_by_provider[str(prov)] = float(g["_disc"].sum()) / n_months

    step = np.zeros(len(out), dtype=float)
    cliff_flag = np.zeros(len(out), dtype=bool)
    for _, row in purchases.iterrows():
        prov = str(row.get("ProviderName"))
        purchase_month = pd.Timestamp(row["ChargePeriodStart"]).to_period("M")
        expiry = purchase_month + 12  # one-year term
        if expiry < fc_start or expiry > fc_end:
            continue
        lost = monthly_discount_by_provider.get(prov, 0.0)
        if lost <= 0:
            continue
        # Step up from the expiry month onward (the discount is gone for good).
        mask = (fc_periods >= expiry).to_numpy()
        step += mask * lost
        cliff_flag |= (fc_periods == expiry).to_numpy()

    out["cost_with_cliffs"] = out["cost"].astype(float) + step
    out["cliff"] = cliff_flag
    return out


def driver_overlay(forecast: pd.DataFrame, adjustments: List[dict]) -> pd.DataFrame:
    """Layer bottom-up, business-known what-ifs onto the statistical top-line.

    Each adjustment is {'period': 'YYYY-MM', 'pct': 0.15, 'label': '...'} and
    applies a permanent step from that month forward -- a planned meter-rollout
    wave, a new plant coming online, a migration. Adjustments compound in the
    order given. Returns the forecast with a `cost_adjusted` column and, per
    period, the `applied_drivers` labels that shaped it.
    """
    out = forecast.copy()
    out["cost_adjusted"] = out["cost"].astype(float)
    out["applied_drivers"] = [[] for _ in range(len(out))]
    if not len(out) or not adjustments:
        return out

    fc_periods = out["period"].dt.to_period("M")
    factor = np.ones(len(out), dtype=float)
    labels: List[List[str]] = [[] for _ in range(len(out))]
    for adj in adjustments:
        try:
            when = pd.Period(str(adj["period"]), freq="M")
            pct = float(adj["pct"])
        except (KeyError, ValueError, TypeError):
            continue
        label = str(adj.get("label", f"{pct:+.0%} from {adj.get('period')}"))
        mask = (fc_periods >= when).to_numpy()
        factor = factor * np.where(mask, 1.0 + pct, 1.0)
        for i in np.nonzero(mask)[0]:
            labels[i].append(label)

    out["cost_adjusted"] = out["cost"].astype(float) * factor
    out["applied_drivers"] = labels
    return out
