"""Cost anomaly detection.

Mirrors the semantics of AWS Cost Anomaly Detection rather than a generic
outlier finder, because the failure mode that matters in FinOps is not "this
point is unusual" but "this point is unusual *after accounting for the natural
shape of spend*". A utility's cloud bill has weekday/weekend structure and a
monthly cycle; a detector that ignores them pages someone every Monday.

Three levers, in increasing robustness:

  * `zscore`   -- |x - mean| > k*sigma. Cheap, but a single past spike inflates
                  sigma and hides the next one.
  * `mad`      -- modified z-score 0.6745*(x - median)/MAD. Median and MAD are
                  outlier-resistant, so one spike does not blind the detector to
                  the next. Threshold ~3.5 is the Iglewicz-Hoaglin convention.
  * `stl_mad`  -- STL-decompose first, then MAD on the residual, so seasonality
                  is modelled out instead of tripping the alert. DEFAULT.

Design rules taken from AWS Cost Anomaly Detection:
  * a >= 10-day warm-up before anything can be flagged;
  * dynamic thresholds (relative to the series' own dispersion), never a static
    dollar amount;
  * natural growth is modelled (the trend/seasonal component), not alarmed on.

statsmodels STL is an OPTIONAL import. Without it, `stl_mad` degrades to a
centred rolling-median detrend and says so in the point's `method`.

Severity is graded off the deviation from expected:
    |dev| > 100% -> critical,  > 50% -> serious,  > 25% -> warning,  else good.
These map to `theme.STATUS` keys so a flagged anomaly colours consistently with
every other status in the app.

Source: AWS Cost Anomaly Detection (concept parity, not code).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

COST = "EffectiveCost"

try:  # pragma: no cover - environment dependent
    from statsmodels.tsa.seasonal import STL

    _HAS_STL = True
except Exception:  # pragma: no cover
    _HAS_STL = False

# 0.6745 is the 0.75 quantile of the standard normal; it rescales the MAD so a
# modified z-score is comparable to an ordinary z-score.
_MAD_SCALE = 0.6745


@dataclass
class AnomalyPoint:
    period: pd.Timestamp
    dimension: str
    value: float
    expected: float
    deviation_abs: float
    deviation_pct: float
    score: float
    severity: str   # 'good' | 'warning' | 'serious' | 'critical' (theme.STATUS keys)
    method: str


def _severity(deviation_pct: float) -> str:
    d = abs(float(deviation_pct))
    if d > 100:
        return "critical"
    if d > 50:
        return "serious"
    if d > 25:
        return "warning"
    return "good"


# ==========================================================================
# Series construction
# ==========================================================================


def daily_spend(focus_df: pd.DataFrame, dim: Optional[str] = None) -> pd.DataFrame:
    """Daily spend series, optionally split by a dimension.

    Only day-grain rows are used: the demo (and any real export we roll up)
    keeps daily detail for the trailing year and monthly rows before that, and a
    month-span row dumped onto its start date would masquerade as a colossal
    one-day spike. We isolate the daily rows by charge-period length.
    """
    base_cols = ["period"] + ([dim] if dim else []) + ["cost"]
    if focus_df is None or not len(focus_df):
        return pd.DataFrame(columns=base_cols)

    df = focus_df.copy()
    span = (df["ChargePeriodEnd"] - df["ChargePeriodStart"]).dt.days
    df = df[span <= 2]  # day-grain rows only
    if not len(df):
        return pd.DataFrame(columns=base_cols)

    df["period"] = df["ChargePeriodStart"].dt.normalize()
    group = ["period"] + ([dim] if dim else [])
    out = df.groupby(group, as_index=False, observed=True)[COST].sum().rename(columns={COST: "cost"})
    return out.sort_values(group).reset_index(drop=True)


# ==========================================================================
# Detrending
# ==========================================================================


def _stl_residual(y: np.ndarray, period: int = 7) -> tuple[np.ndarray, np.ndarray, str]:
    """Return (expected, residual, method). STL if available, else rolling median."""
    n = len(y)
    if _HAS_STL and n >= 2 * period:
        try:
            res = STL(y, period=period, robust=True).fit()
            trend_seasonal = np.asarray(res.trend, float) + np.asarray(res.seasonal, float)
            return trend_seasonal, y - trend_seasonal, "stl_mad"
        except Exception:  # pragma: no cover - defensive
            pass
    # Degrade: centred rolling-median detrend models the natural level without
    # statsmodels. The note travels in the returned method label.
    s = pd.Series(y)
    win = min(max(period, 3), n if n % 2 else n - 1)
    if win % 2 == 0:
        win += 1
    expected = s.rolling(win, center=True, min_periods=1).median().to_numpy()
    return expected, y - expected, "rolling_median_mad (stl unavailable)"


# ==========================================================================
# Detection
# ==========================================================================


def detect(
    series: pd.DataFrame,
    method: str = "stl_mad",
    threshold: float = 3.5,
    min_history_days: int = 10,
    min_deviation_pct: float = 25.0,
    min_cost: float = 0.0,
) -> pd.DataFrame:
    """Annotate a single spend series with anomaly scores.

    `series` has `period` and `cost` (as produced by `daily_spend`). Returns
    every row with `expected`, `residual`, `score`, `is_anomaly`, `severity` and
    `deviation_pct` attached, so a chart can draw the band and mark the flags in
    one pass. Below `min_history_days` nothing is flagged -- the AWS warm-up
    rule -- because a dispersion estimate from a handful of points is noise.

    A point is an anomaly only if it is BOTH statistically odd and financially
    material:

        |modified z| > threshold          AND      |deviation| >= min_deviation_pct

    The statistical test alone is not enough. In a low-variance series the MAD
    collapses toward zero, so a 5% wobble scores a z of 6 and gets flagged. That
    is how this detector once returned 347 "anomalies" on a 24-month estate, 318
    of which it simultaneously graded `severity == "good"` -- the two signals
    were computed from different quantities and contradicted each other. An
    alert nobody can act on is worse than no alert: it trains people to ignore
    the channel.

    `min_cost` additionally suppresses a doubling of a $3/day service, which is
    a 100% deviation and still not worth a VP's attention.
    """
    cols = ["period", "cost", "expected", "residual", "score", "is_anomaly", "severity", "deviation_pct"]
    if series is None or not len(series):
        return pd.DataFrame(columns=cols)

    s = series.sort_values("period").reset_index(drop=True)
    y = s["cost"].to_numpy(dtype=float)
    n = len(y)

    if n < min_history_days:
        s = s.assign(expected=y, residual=0.0, score=0.0, is_anomaly=False,
                     severity="good", deviation_pct=0.0)
        return s[cols]

    used_method = method
    if method == "stl_mad":
        expected, residual, used_method = _stl_residual(y)
    elif method == "zscore":
        mu = float(np.mean(y))
        sigma = float(np.std(y, ddof=1)) or 1.0
        expected = np.repeat(mu, n)
        residual = y - expected
    else:  # 'mad'
        med = float(np.median(y))
        expected = np.repeat(med, n)
        residual = y - expected

    # Score the residual with the method's dispersion measure.
    if used_method.startswith("stl_mad") or used_method.startswith("rolling_median") or method == "mad":
        mad = float(np.median(np.abs(residual - np.median(residual))))
        scale = mad if mad > 0 else (float(np.std(residual, ddof=1)) or 1.0)
        score = _MAD_SCALE * residual / scale
    else:  # zscore
        sigma = float(np.std(residual, ddof=1)) or 1.0
        score = residual / sigma

    with np.errstate(divide="ignore", invalid="ignore"):
        safe_expected = np.where(np.abs(expected) > 1e-9, expected, np.nan)
        deviation_pct = (y - expected) / safe_expected * 100.0
    deviation_pct = np.nan_to_num(deviation_pct, nan=0.0, posinf=0.0, neginf=0.0)

    # Statistically odd AND financially material. Either alone produces noise.
    is_anomaly = (
        (np.abs(score) > threshold)
        & (np.abs(deviation_pct) >= min_deviation_pct)
        & (y >= min_cost)
    )
    severity = np.where(is_anomaly, [_severity(d) for d in deviation_pct], "good")

    s = s.assign(
        expected=expected, residual=residual, score=score,
        is_anomaly=is_anomaly, severity=severity, deviation_pct=deviation_pct,
    )
    s.attrs["method"] = used_method
    return s[cols]


def detect_by_dimension(focus_df: pd.DataFrame, dim: str = "ServiceCategory", **kw) -> pd.DataFrame:
    """Run `detect` independently per value of `dim` and return the flagged rows.

    Per-dimension detection is what lets a spike in one small service surface at
    all: pooled into the estate total it would be lost in the noise, but against
    its own baseline it is unmistakable. The returned frame carries the `dim`
    column plus everything `detect` produces, for `is_anomaly` rows only.
    """
    out_cols = ["period", dim, "cost", "expected", "residual", "score",
                "is_anomaly", "severity", "deviation_pct", "method"]
    series = daily_spend(focus_df, dim=dim)
    if not len(series):
        return pd.DataFrame(columns=out_cols)

    method = kw.get("method", "stl_mad")
    frames: List[pd.DataFrame] = []
    for value, g in series.groupby(dim, observed=True):
        annotated = detect(g[["period", "cost"]], **kw)
        if not len(annotated):
            continue
        annotated = annotated.copy()
        annotated[dim] = value
        annotated["method"] = annotated.attrs.get("method", method)
        frames.append(annotated)

    if not frames:
        return pd.DataFrame(columns=out_cols)
    allrows = pd.concat(frames, ignore_index=True)
    flagged = allrows[allrows["is_anomaly"]].copy()
    return flagged[out_cols].sort_values(["period", dim]).reset_index(drop=True)


def summarise(flagged: pd.DataFrame) -> List[AnomalyPoint]:
    """Turn a flagged frame into typed AnomalyPoints, ready for a briefing list."""
    if flagged is None or not len(flagged):
        return []
    dim_col = next((c for c in flagged.columns
                    if c not in ("period", "cost", "expected", "residual", "score",
                                 "is_anomaly", "severity", "deviation_pct", "method")), None)
    points: List[AnomalyPoint] = []
    for _, r in flagged.iterrows():
        expected = float(r["expected"])
        value = float(r["cost"])
        points.append(
            AnomalyPoint(
                period=pd.Timestamp(r["period"]),
                dimension=str(r[dim_col]) if dim_col else "all",
                value=value,
                expected=expected,
                deviation_abs=value - expected,
                deviation_pct=float(r["deviation_pct"]),
                score=float(r["score"]),
                severity=str(r["severity"]),
                method=str(r.get("method", "stl_mad")),
            )
        )
    points.sort(key=lambda p: abs(p.score), reverse=True)
    return points
