"""The tools the agents are allowed to use.

Every tool closes over a live `DataContext`, so the model queries the *real*
FOCUS frame that produced the dashboards -- it can never answer from a frame it
imagined. Each tool returns a compact, JSON-serialisable dict or string; never a
raw DataFrame, because a 200k-row frame in the context window is both useless to
the model and ruinous on tokens.

The docstring on each tool is not documentation -- it *is* the specification the
model reads to decide when and how to call it. So each one states its units
(USD), that costs are amortized `EffectiveCost`, and what shape it returns.

The analytics engines (`forecast`, `budget`, `anomaly`, `allocation`,
`optimize`) are optional imports. If one is absent -- because it is still being
written, or a deployment does not ship it -- the tools that need it are simply
not registered, and `missing_tools()` reports which. The rest of the agent layer
keeps working. That is the graceful-degradation contract.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

import focus
import kpi
from finops_core import DataContext

# ------------------------------------------------------------------------
# Optional analytics engines. A missing engine disables its tools rather
# than breaking the import of this module.
# ------------------------------------------------------------------------

_MISSING: List[str] = []

try:
    import forecast as _forecast
except ImportError:
    _forecast = None
    _MISSING.append("forecast")

try:
    import budget as _budget
except ImportError:
    _budget = None
    _MISSING.append("budget")

try:
    import anomaly as _anomaly
except ImportError:
    _anomaly = None
    _MISSING.append("anomaly")

try:
    import allocation as _allocation
except ImportError:
    _allocation = None
    _MISSING.append("allocation")

try:
    import optimize as _optimize
except ImportError:
    _optimize = None
    _MISSING.append("optimize")


from langchain_core.tools import tool


def missing_tools() -> List[str]:
    """Engines that failed to import, and so are not exposed as tools.

    The UI renders this so a demo never silently drops a capability -- an empty
    list means the full toolset is live.
    """
    return list(_MISSING)


# ------------------------------------------------------------------------
# Serialisation helpers -- keep every payload small and JSON-clean.
# ------------------------------------------------------------------------

_COST_COLUMNS = {"EffectiveCost", "BilledCost", "ListCost", "ContractedCost"}

# Comparison operators we allow in query_spend. The map is the whitelist:
# an operator not present here is rejected, which is what keeps a string like
# "__import__" from ever reaching an eval (there is no eval -- we only ever
# index the frame with these vetted callables).
_OPERATORS: Dict[str, Callable[[pd.Series, Any], pd.Series]] = {
    "==": lambda s, v: s == v,
    "!=": lambda s, v: s != v,
    ">": lambda s, v: pd.to_numeric(s, errors="coerce") > v,
    "<": lambda s, v: pd.to_numeric(s, errors="coerce") < v,
    ">=": lambda s, v: pd.to_numeric(s, errors="coerce") >= v,
    "<=": lambda s, v: pd.to_numeric(s, errors="coerce") <= v,
    "in": lambda s, v: s.isin(v if isinstance(v, (list, tuple, set)) else [v]),
    "not in": lambda s, v: ~s.isin(v if isinstance(v, (list, tuple, set)) else [v]),
    "contains": lambda s, v: s.astype(str).str.contains(str(v), case=False, na=False),
}


def _num(x: Any, digits: int = 2) -> Optional[float]:
    """Round to money precision; pass None through; never raise on NaN."""
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
        return round(float(x), digits)
    except (TypeError, ValueError):
        return None


def _record(obj: Any) -> Dict[str, Any]:
    """Best-effort flatten of a dataclass / pydantic model / plain object to a
    JSON-friendly dict. Used for Opportunity and Lever records whose exact field
    names live in `optimize.py`, so we read them generically rather than guess."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        raw = obj
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        raw = dataclasses.asdict(obj)
    elif hasattr(obj, "model_dump"):
        raw = obj.model_dump()
    elif hasattr(obj, "__dict__"):
        raw = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    else:
        return {"value": str(obj)}
    out: Dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, float):
            out[k] = _num(v)
        elif isinstance(v, (str, int, bool, type(None))):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [x if isinstance(x, (str, int, float, bool, type(None))) else str(x) for x in v]
        else:
            out[k] = str(v)
    return out


def _frame_records(df: pd.DataFrame, limit: int = 15) -> List[Dict[str, Any]]:
    """Top-N rows of a result frame as clean records, money columns rounded."""
    if df is None or not len(df):
        return []
    head = df.head(limit).copy()
    for c in head.columns:
        if pd.api.types.is_float_dtype(head[c]):
            head[c] = head[c].map(lambda x: _num(x))
        elif pd.api.types.is_datetime64_any_dtype(head[c]):
            head[c] = head[c].dt.strftime("%Y-%m")
    return head.where(pd.notna(head), None).to_dict(orient="records")


def _last_n_months(df: pd.DataFrame, months: Optional[int]) -> pd.DataFrame:
    """Scope a FOCUS frame to its trailing `months`. None -> whole frame."""
    if not months or not len(df):
        return df
    last = df["ChargePeriodStart"].max().to_period("M")
    cutoff = (last - (months - 1)).to_timestamp()
    return df[df["ChargePeriodStart"] >= cutoff]


# ------------------------------------------------------------------------
# make_tools -- everything above is plumbing; this is the surface.
# ------------------------------------------------------------------------


def make_tools(ctx: DataContext) -> List:
    """Build the toolset bound to one DataContext.

    Called once per graph build. Tools that depend on a missing engine are
    skipped, so the returned list length reflects what is actually available.
    """
    df = ctx.focus_df
    tools: List = []

    # -- Always available (kpi + focus only) ------------------------------

    @tool
    def get_spend_summary(group_by: str = "ProviderName", months: int = 12) -> dict:
        """Total amortized spend with month-over-month and year-over-year change,
        plus a per-group breakdown.

        Units: USD, amortized EffectiveCost (the executive basis). `group_by` is a
        FOCUS column such as ProviderName, ServiceCategory, tag_business_unit or
        tag_application. `months` scopes to the trailing N months. Returns total,
        mom_pct, yoy_pct, run_rate_annualised and the top groups by spend.
        """
        scoped = _last_n_months(df, months)
        if group_by not in scoped.columns:
            return {"error": f"Unknown group_by column '{group_by}'. Call list_focus_columns for valid names."}
        breakdown = (
            scoped.groupby(group_by, observed=True)[kpi.COST]
            .sum()
            .sort_values(ascending=False)
            .head(12)
        )
        return {
            "units": "USD (amortized EffectiveCost)",
            "months": months,
            "group_by": group_by,
            "total_spend": _num(kpi.total_spend(scoped)),
            "mom_pct": _num(kpi.mom_delta_pct(scoped)),
            "yoy_pct": _num(kpi.yoy_delta_pct(df)),  # YoY needs 13m; use full frame
            "run_rate_annualised": _num(kpi.run_rate_annualised(scoped)),
            "breakdown": {str(k): _num(v) for k, v in breakdown.items()},
        }

    tools.append(get_spend_summary)

    @tool
    def get_executive_kpis() -> dict:
        """The one-shot executive KPI panel a VP sees on the landing page.

        Units: USD, amortized. Returns total spend, MoM/YoY %, annualised run
        rate, Effective Savings Rate %, commitment coverage % and utilization %,
        commitment waste $, cost of waste $ (commitment + usage), waste %,
        allocation coverage % and chargeback readiness, and baseline drift %.
        Usage waste is folded in from the optimizer when that engine is present.
        """
        usage_waste = 0.0
        if _optimize is not None:
            try:
                opps = _optimize.detect_all(df)
                usage_waste = float(sum(_annual_savings(o) for o in opps))
            except Exception:
                usage_waste = 0.0
        k = kpi.executive_kpis(df, usage_waste=usage_waste)
        d = dataclasses.asdict(k)
        for key, val in list(d.items()):
            if isinstance(val, float):
                d[key] = _num(val)
        d["units"] = "USD (amortized EffectiveCost); percentages in %"
        return d

    tools.append(get_executive_kpis)

    @tool
    def get_commitment_position() -> dict:
        """Rate-optimization posture: how well commitments (RI / Savings Plans /
        CUDs) are covering and being used.

        Units: USD amortized and %. Returns commitment coverage % and
        utilization %, Effective Savings Rate %, the three-factor ESR
        decomposition (utilization x coverage x discount), and commitment waste
        in dollars (amortized cost of commitments that covered nothing). A high
        coverage with low utilization is a bad deal that only ESR reveals.
        """
        comps = kpi.esr_components(df)
        return {
            "units": "USD amortized; percentages in %",
            "coverage_pct": _num(kpi.commitment_coverage_pct(df)),
            "utilization_pct": _num(kpi.commitment_utilization_pct(df)),
            "esr_pct": _num(kpi.effective_savings_rate_pct(df)),
            "esr_components": {k: _num(v) for k, v in comps.items()},
            "commitment_waste_usd": _num(kpi.commitment_waste(df)),
            "commitment_waste_pct": _num(kpi.commitment_waste_pct(df)),
        }

    tools.append(get_commitment_position)

    @tool
    def list_focus_columns() -> dict:
        """The columns available to query, so the model never guesses a name.

        Returns the FOCUS 1.2 column names present plus the exploded `tag_*`
        allocation columns, each with its dtype. Use this before query_spend.
        """
        cols = {}
        for c in focus.SCHEMA:
            if c.name in df.columns:
                cols[c.name] = c.dtype
        for c in df.columns:
            if c.startswith("tag_"):
                cols[c] = "string"
        return {
            "columns": cols,
            "cost_metrics": sorted(_COST_COLUMNS),
            "note": "Costs are USD. EffectiveCost is amortized and is the executive default.",
        }

    tools.append(list_focus_columns)

    @tool
    def query_spend(
        filters: Optional[list] = None,
        group_by: Optional[list] = None,
        metric: str = "EffectiveCost",
    ) -> dict:
        """Run a constrained aggregation over the FOCUS frame.

        `filters` is a list of {"column","op","value"} objects; `op` is one of
        == != > < >= <= in "not in" contains. `group_by` is a list of columns.
        `metric` is a cost column (EffectiveCost default, amortized USD; also
        BilledCost, ListCost, ContractedCost). Every column and operator is
        checked against a whitelist -- an unknown column or operator returns an
        error string and nothing is executed. There is no eval; this cannot run
        arbitrary code. Returns grouped sums (top 20) and the grand total in USD.
        """
        allowed_cols = set(focus.COLUMN_NAMES) | {c for c in df.columns if c.startswith("tag_")}

        if metric not in _COST_COLUMNS:
            return {"error": f"metric must be one of {sorted(_COST_COLUMNS)}; got '{metric}'."}
        if metric not in df.columns:
            return {"error": f"metric column '{metric}' is not in the data."}

        work = df
        for f in filters or []:
            if not isinstance(f, dict):
                return {"error": "each filter must be an object with column/op/value."}
            col, op, val = f.get("column"), f.get("op", "=="), f.get("value")
            if col not in allowed_cols:
                return {"error": f"column '{col}' is not permitted. Call list_focus_columns for the whitelist."}
            if col not in df.columns:
                return {"error": f"column '{col}' is not present in the data."}
            if op not in _OPERATORS:
                return {"error": f"operator '{op}' is not permitted. Allowed: {sorted(_OPERATORS)}."}
            try:
                mask = _OPERATORS[op](work[col], val)
            except Exception as exc:  # a bad value type, not a security issue
                return {"error": f"filter on '{col}' failed: {exc}"}
            work = work[mask]

        for g in group_by or []:
            if g not in allowed_cols:
                return {"error": f"group_by column '{g}' is not permitted. Call list_focus_columns."}
            if g not in df.columns:
                return {"error": f"group_by column '{g}' is not present in the data."}

        total = float(work[metric].sum()) if len(work) else 0.0
        if group_by:
            grouped = (
                work.groupby(group_by, observed=True)[metric]
                .sum()
                .sort_values(ascending=False)
                .head(20)
            )
            rows = [
                {"group": (str(k) if not isinstance(k, tuple) else [str(x) for x in k]), "cost": _num(v)}
                for k, v in grouped.items()
            ]
        else:
            rows = []
        return {
            "units": f"USD ({metric})",
            "metric": metric,
            "row_count": int(len(work)),
            "total": _num(total),
            "groups": rows,
        }

    tools.append(query_spend)

    # -- Forecasting ------------------------------------------------------

    if _forecast is not None:

        @tool
        def get_forecast(horizon_months: int = 12, method: str = "auto") -> dict:
            """Forecast total monthly spend with prediction intervals.

            Units: USD amortized. `horizon_months` is how far ahead; `method` is
            'auto' unless a specific model is wanted. Returns the point forecast
            per future month with 80% and 95% bands (lo80/hi80/lo95/hi95),
            backtest accuracy (MAPE/WAPE/sMAPE), the maturity band that accuracy
            implies, the method used, and any model notes. Report the band, not
            just the point -- a point estimate with no interval is a guess.
            """
            monthly = ctx.monthly()
            res = _forecast.forecast_spend(monthly, horizon=horizon_months, method=method)
            fc = getattr(res, "forecast", None)
            return {
                "units": "USD (amortized EffectiveCost)",
                "method": getattr(res, "method", method),
                "accuracy": {k: _num(v) for k, v in (getattr(res, "accuracy", {}) or {}).items()},
                "maturity": getattr(res, "maturity", None),
                "notes": getattr(res, "notes", None),
                "horizon_months": horizon_months,
                "forecast": _frame_records(fc, limit=horizon_months) if fc is not None else [],
            }

        tools.append(get_forecast)

    # -- Budgeting --------------------------------------------------------

    if _budget is not None:

        @tool
        def get_budget_variance(by: Optional[list] = None) -> dict:
            """Budget vs actual variance, sliced by the given dimensions.

            Units: USD amortized. `by` is a list of columns such as
            ['cloud','application']. Returns the top 15 variance rows with actual,
            budget, variance_abs, variance_pct, direction (Overrun/Underrun) and
            status (good/serious/critical/warning). Positive variance is an
            overrun. A large underrun is a planning miss too, not a win.
            """
            dims = by or ["cloud", "application"]
            vt = _budget.variance_table(df, ctx.budgets, by=dims)
            vt_sorted = vt
            if "variance_abs" in getattr(vt, "columns", []):
                vt_sorted = vt.reindex(vt["variance_abs"].abs().sort_values(ascending=False).index)
            return {
                "units": "USD (amortized EffectiveCost)",
                "by": dims,
                "rows": _frame_records(vt_sorted, limit=15),
            }

        tools.append(get_budget_variance)

    # -- Anomaly detection ------------------------------------------------

    if _anomaly is not None:

        @tool
        def get_anomalies(dimension: str = "ServiceCategory", lookback_days: int = 90) -> dict:
            """Spend anomalies flagged against an expected baseline.

            Units: USD amortized. `dimension` is the axis to scan (ServiceCategory,
            ProviderName, tag_application...). `lookback_days` bounds how recent.
            Returns only the flagged points: period, dimension value, actual cost,
            expected cost, anomaly score, severity and deviation %. If nothing is
            flagged, says so rather than inventing a spike.
            """
            res = _anomaly.detect_by_dimension(df, dim=dimension)
            flagged = res
            if "is_anomaly" in getattr(res, "columns", []):
                flagged = res[res["is_anomaly"]]
            if lookback_days and "period" in getattr(flagged, "columns", []):
                cutoff = df["ChargePeriodStart"].max() - pd.Timedelta(days=lookback_days)
                flagged = flagged[pd.to_datetime(flagged["period"]) >= cutoff]
            rows = _frame_records(flagged, limit=20)
            return {
                "units": "USD (amortized EffectiveCost)",
                "dimension": dimension,
                "lookback_days": lookback_days,
                "count": len(rows),
                "anomalies": rows,
                "message": None if rows else "No anomalies flagged in the window.",
            }

        tools.append(get_anomalies)

    # -- Allocation -------------------------------------------------------

    if _allocation is not None:

        @tool
        def get_allocation(dimension: str = "tag_business_unit", method: str = "proportional") -> dict:
            """Showback / chargeback split, including the shared-cost pool.

            Units: USD amortized. `dimension` is the allocation axis (default
            tag_business_unit). `method` governs how shared and untagged cost is
            spread ('proportional' by default). Returns per-dimension direct cost,
            allocated shared cost, untagged cost, total and % of total. Shared and
            untagged cost must be spread before any chargeback figure is defensible.
            """
            policy = _allocation.SharedCostPolicy(method=method)
            res = _allocation.allocate(df, policy, dim=dimension)
            return {
                "units": "USD (amortized EffectiveCost)",
                "dimension": dimension,
                "method": method,
                "rows": _frame_records(res, limit=20),
            }

        tools.append(get_allocation)

        @tool
        def get_allocation_coverage() -> dict:
            """Tagging coverage per allocation tag, and chargeback readiness.

            Units: %. Returns, per tag key, the coverage % (tagged spend / total),
            the unallocated cost in USD, and a status. Coverage below ~90% means a
            chargeback invoice is disputable, so the practice runs chargeback on
            the tagged portion and showback on the rest.
            """
            res = _allocation.coverage_report(df)
            return {
                "units": "coverage in %, unallocated_cost in USD",
                "rows": _frame_records(res, limit=20),
            }

        tools.append(get_allocation_coverage)

    # -- Optimization -----------------------------------------------------

    if _optimize is not None:

        @tool
        def find_optimization_opportunities(min_annual_savings: float = 0.0, category: str = "") -> dict:
            """Concrete savings opportunities detected from the billing data.

            Units: USD/year. `min_annual_savings` filters out small items;
            `category` optionally narrows to one lever family (e.g. 'Rate
            Optimization', 'Workload Optimization'). Returns each opportunity with
            its estimated annual savings, category/lever, effort and risk, plus the
            total addressable annual savings. These are derived from spend, so they
            work even when a provider exposes no recommendations API.
            """
            opps = _optimize.detect_all(df)
            frame = _optimize.opportunities_frame(opps)
            recs = _frame_records(frame, limit=200) if frame is not None else [_record(o) for o in opps]

            def _saving(r: dict) -> float:
                for key in ("annual_savings", "annual_savings_usd", "estimated_annual_savings", "savings_annual", "savings"):
                    if key in r and isinstance(r[key], (int, float)):
                        return float(r[key])
                return 0.0

            def _cat(r: dict) -> str:
                for key in ("category", "lever", "lever_category", "family", "type"):
                    if key in r and r[key]:
                        return str(r[key])
                return ""

            filtered = [r for r in recs if _saving(r) >= float(min_annual_savings)]
            if category:
                filtered = [r for r in filtered if category.lower() in _cat(r).lower()]
            filtered.sort(key=_saving, reverse=True)
            return {
                "units": "USD per year",
                "count": len(filtered),
                "total_annual_savings": _num(sum(_saving(r) for r in filtered)),
                "opportunities": filtered[:20],
            }

        tools.append(find_optimization_opportunities)

        @tool
        def explain_lever(lever_id: str) -> dict:
            """The playbook for one optimization lever.

            Returns the lever's savings range, effort, risk, prerequisites and
            source URL, so a recommendation can be made responsibly rather than as
            a bare number. Look up available lever ids from
            find_optimization_opportunities. Amounts are USD.
            """
            levers = getattr(_optimize, "LEVERS", []) or []
            for lev in levers:
                rec = _record(lev)
                lid = rec.get("id") or rec.get("lever_id") or rec.get("key") or rec.get("name")
                if lid is not None and str(lid).lower() == str(lever_id).lower():
                    return rec
            available = []
            for lev in levers:
                rec = _record(lev)
                available.append(rec.get("id") or rec.get("lever_id") or rec.get("name"))
            return {"error": f"No lever '{lever_id}'.", "available": [a for a in available if a]}

        tools.append(explain_lever)

    return tools


def _annual_savings(opp: Any) -> float:
    """Pull an annual-savings figure off an Opportunity without knowing its exact
    field name -- used to feed usage-waste into the executive KPIs."""
    rec = _record(opp)
    for key in ("annual_savings", "annual_savings_usd", "estimated_annual_savings", "savings_annual", "savings"):
        v = rec.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0
