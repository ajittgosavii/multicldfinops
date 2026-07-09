"""Showback and chargeback with shared-cost splitting.

Allocation is where a cloud bill becomes a management tool. Raw FOCUS rows tell
you what was spent; allocation tells you *who owes what* -- which only works
once two hard problems are solved:

  1. **Shared cost.** A landing zone, a security stack, a central observability
     platform: real spend that belongs to no single team. It has to be split by
     an agreed rule before any showback number is defensible. The rule is a
     policy decision, not an accident of tagging, so it lives in
     `SharedCostPolicy` and is auditable.
  2. **Untagged spend.** Every estate has it, and it drags allocation coverage
     below the point where a chargeback invoice can survive a dispute. We make
     it explicit: an "Untagged allocation" line, spread by the same policy, so
     the number reconciles to the penny instead of quietly vanishing.

The five methods are the ones practitioners actually use:
    direct              charge only what is directly attributable
    even_split          pool / N targets
    proportional        target_share = direct_spend / total_direct_spend  (the
                        standard "fair" shared-cost tax)
    fixed_percentage    a negotiated split that must sum to 100
    usage_driver        proportional to a measured driver (seats, requests, ...)

The ~90% coverage line below which chargeback becomes showback is practitioner
consensus, NOT a published FinOps Foundation number.

Sources
-------
Allocation capability     https://www.finops.org/framework/capabilities/allocation/
Invoicing and Chargeback  https://www.finops.org/framework/capabilities/invoicing-chargeback/
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

import focus

COST = "EffectiveCost"
UNALLOCATED = "Unallocated"

ALLOCATION_METHODS = ["direct", "even_split", "proportional", "fixed_percentage", "usage_driver"]

# Provider tag/label ceilings. They bite at ingest, not here, but the governance
# report cites them so a practitioner knows the headroom before adding a tag.
#   AWS   50 tags/resource; up to 500 ACTIVATED cost-allocation keys;
#         activation is NOT retroactive (spend before activation stays untagged).
#   Azure 50 name/value pairs; tag inheritance is a Cost Management setting
#         applied to usage records, not a property of the resource.
#   GCP   64 labels; key and value each <= 63 chars, lowercase only.
#   OCI   two systems: free-form tags are ungoverned key/value pairs, while
#         defined tags live in an administrator-controlled namespace. Only the
#         defined ones are safe to build chargeback on. Compartments also carry
#         cost, and they nest -- so OCI allocation is a hierarchy problem as
#         much as a tagging one.
PROVIDER_TAG_LIMITS: Dict[str, Dict[str, object]] = {
    "AWS": {"max_tags": 50, "max_cost_allocation_keys": 500, "retroactive": False},
    "Azure": {"max_tags": 50, "inheritance": "cost-management-setting"},
    "GCP": {"max_labels": 64, "max_key_len": 63, "max_value_len": 63, "lowercase": True},
    "OCI": {"max_defined_tags": 64, "max_freeform_tags": 64, "namespaced": True},
}


@dataclass
class SharedCostPolicy:
    """How shared and untagged cost is split, and onto what.

    Defaults describe the demo estate: a 'Shared Platform Services' application
    plus central Management/Security categories form the shared pool, split
    proportionally to each target's direct spend.
    """

    method: str = "proportional"
    shared_applications: tuple = ("Shared Platform Services",)
    shared_service_categories: tuple = ("Management and Governance", "Security")
    fixed_percentages: Optional[dict] = None      # {target: pct}, must sum to 100
    driver_metric: Optional[str] = None           # column to allocate proportionally to
    include_untagged: bool = True                 # spread Unallocated as a pool
    def validate(self) -> List[str]:
        """Return human-readable problems; empty list means the policy is usable."""
        errs: List[str] = []
        if self.method not in ALLOCATION_METHODS:
            errs.append(f"method '{self.method}' is not one of {ALLOCATION_METHODS}")
        if self.method == "fixed_percentage":
            if not self.fixed_percentages:
                errs.append("fixed_percentage requires fixed_percentages")
            else:
                total = float(sum(self.fixed_percentages.values()))
                if abs(total - 100.0) > 0.01:
                    errs.append(f"fixed_percentages must sum to 100, got {total:.2f}")
        if self.method == "usage_driver" and not self.driver_metric:
            errs.append("usage_driver requires driver_metric (a column name)")
        return errs


# ==========================================================================
# Pool identification
# ==========================================================================


def _is_untagged(df: pd.DataFrame, dim: str) -> pd.Series:
    if dim not in df.columns:
        return pd.Series(False, index=df.index)
    return df[dim].astype(str) == UNALLOCATED


def _is_shared(df: pd.DataFrame, policy: SharedCostPolicy) -> pd.Series:
    shared_app = pd.Series(False, index=df.index)
    if "tag_application" in df.columns and policy.shared_applications:
        shared_app = df["tag_application"].astype(str).isin(policy.shared_applications)
    shared_cat = pd.Series(False, index=df.index)
    if "ServiceCategory" in df.columns and policy.shared_service_categories:
        shared_cat = df["ServiceCategory"].astype(str).isin(policy.shared_service_categories)
    return shared_app | shared_cat


def shared_pool(focus_df: pd.DataFrame, policy: SharedCostPolicy) -> float:
    """Total shared cost to be split: shared applications plus shared service
    categories, excluding anything already untagged (untagged is its own pool)."""
    if focus_df is None or not len(focus_df):
        return 0.0
    untagged = _is_untagged(focus_df, "tag_business_unit")
    shared = _is_shared(focus_df, policy) & ~untagged
    return float(focus_df.loc[shared, COST].sum())


def direct_costs(focus_df: pd.DataFrame, policy: SharedCostPolicy, dim: str = "tag_business_unit") -> pd.DataFrame:
    """Directly attributable cost per target: not shared, not untagged."""
    cols = [dim, "direct_cost"]
    if focus_df is None or not len(focus_df) or dim not in focus_df.columns:
        return pd.DataFrame(columns=cols)
    untagged = _is_untagged(focus_df, dim)
    shared = _is_shared(focus_df, policy)
    direct = focus_df[~untagged & ~shared]
    out = direct.groupby(dim, as_index=False, observed=True)[COST].sum().rename(columns={COST: "direct_cost"})
    return out.sort_values("direct_cost", ascending=False).reset_index(drop=True)


# ==========================================================================
# Weighting
# ==========================================================================


def _weights(
    targets: List[str], direct: pd.DataFrame, policy: SharedCostPolicy,
    focus_df: pd.DataFrame, dim: str
) -> Dict[str, float]:
    """A normalised weight per target for spreading a pool. Always sums to 1."""
    n = len(targets)
    if n == 0:
        return {}

    if policy.method == "even_split":
        return {t: 1.0 / n for t in targets}

    if policy.method == "fixed_percentage" and policy.fixed_percentages:
        raw = {t: float(policy.fixed_percentages.get(t, 0.0)) for t in targets}
        s = sum(raw.values())
        return {t: (raw[t] / s if s > 0 else 1.0 / n) for t in targets}

    if policy.method == "usage_driver" and policy.driver_metric and policy.driver_metric in focus_df.columns:
        drv = (
            focus_df.groupby(dim, observed=True)[policy.driver_metric].sum()
            if dim in focus_df.columns else pd.Series(dtype=float)
        )
        raw = {t: float(drv.get(t, 0.0)) for t in targets}
        s = sum(raw.values())
        if s > 0:
            return {t: raw[t] / s for t in targets}
        return {t: 1.0 / n for t in targets}

    # proportional (and the safe default): weight by direct spend.
    dmap = dict(zip(direct[dim].astype(str), direct["direct_cost"]))
    raw = {t: float(dmap.get(t, 0.0)) for t in targets}
    s = sum(raw.values())
    if s > 0:
        return {t: raw[t] / s for t in targets}
    return {t: 1.0 / n for t in targets}


# ==========================================================================
# Allocation
# ==========================================================================


def allocate(focus_df: pd.DataFrame, policy: SharedCostPolicy, dim: str = "tag_business_unit") -> pd.DataFrame:
    """Full allocation: direct + spread shared + (optionally) spread untagged.

    Returns one row per target with direct_cost, shared_cost, untagged_cost,
    total_cost, pct_of_total and shared_pct. With `include_untagged=True` the
    partition is exhaustive and disjoint, so `total_cost` sums to the estate's
    entire `EffectiveCost` -- the reconciliation guarantee a finance team needs.
    """
    cols = ["direct_cost", "shared_cost", "untagged_cost", "total_cost", "pct_of_total", "shared_pct"]
    out_cols = [dim] + cols
    if focus_df is None or not len(focus_df) or dim not in focus_df.columns:
        return pd.DataFrame(columns=out_cols)

    direct = direct_costs(focus_df, policy, dim)
    targets = [str(t) for t in direct[dim].tolist()]
    if not targets:
        return pd.DataFrame(columns=out_cols)

    weights = _weights(targets, direct, policy, focus_df, dim)
    pool_shared = shared_pool(focus_df, policy)

    untagged_mask = _is_untagged(focus_df, dim)
    pool_untagged = float(focus_df.loc[untagged_mask, COST].sum()) if policy.include_untagged else 0.0

    rows = []
    dmap = dict(zip(direct[dim].astype(str), direct["direct_cost"]))
    for t in targets:
        w = weights.get(t, 0.0)
        d = float(dmap.get(t, 0.0))
        sh = pool_shared * w
        ut = pool_untagged * w
        total = d + sh + ut
        rows.append({dim: t, "direct_cost": d, "shared_cost": sh, "untagged_cost": ut,
                     "total_cost": total})

    res = pd.DataFrame(rows)
    grand = float(res["total_cost"].sum())
    res["pct_of_total"] = np.where(grand > 0, res["total_cost"] / grand * 100.0, 0.0)
    res["shared_pct"] = np.where(
        res["total_cost"] > 0, res["shared_cost"] / res["total_cost"] * 100.0, 0.0
    )
    return res[out_cols].sort_values("total_cost", ascending=False).reset_index(drop=True)


def showback(focus_df: pd.DataFrame, dim: str = "tag_business_unit") -> pd.DataFrame:
    """Informational allocation -- what each team's fully-loaded cost is.

    Showback is chargeback without the invoice: same proportional split, no money
    moves. It is the safe default when allocation coverage is too low to defend a
    real charge-back.
    """
    return allocate(focus_df, SharedCostPolicy(method="proportional"), dim)


def chargeback(
    focus_df: pd.DataFrame, policy: SharedCostPolicy, dim: str = "tag_business_unit",
    period: Optional[str] = None,
) -> pd.DataFrame:
    """Invoice-shaped chargeback: one line item per component, per target.

    Line items are 'Direct', 'Shared allocation' and (when untagged is spread)
    'Untagged tax'. `period` optionally scopes to a single month ('YYYY-MM').
    The sum of `amount` equals the allocated total, so the invoice reconciles.
    """
    cols = ["period", dim, "line_item", "amount"]
    if focus_df is None or not len(focus_df) or dim not in focus_df.columns:
        return pd.DataFrame(columns=cols)

    df = focus_df
    period_label = "all"
    if period is not None:
        p = pd.Period(str(period), freq="M")
        month = df["ChargePeriodStart"].dt.to_period("M")
        df = df[month == p]
        period_label = str(period)
        if not len(df):
            return pd.DataFrame(columns=cols)

    alloc = allocate(df, policy, dim)
    rows: List[dict] = []
    for _, r in alloc.iterrows():
        target = r[dim]
        rows.append({"period": period_label, dim: target, "line_item": "Direct", "amount": float(r["direct_cost"])})
        rows.append({"period": period_label, dim: target, "line_item": "Shared allocation", "amount": float(r["shared_cost"])})
        if policy.include_untagged and r["untagged_cost"] > 0:
            rows.append({"period": period_label, dim: target, "line_item": "Untagged allocation", "amount": float(r["untagged_cost"])})
    return pd.DataFrame(rows, columns=cols)


# ==========================================================================
# Coverage and governance
# ==========================================================================


def _coverage_status(pct: float) -> str:
    if pct >= 95:
        return "good"
    if pct >= 90:
        return "good"
    if pct >= 80:
        return "warning"
    return "critical"


def coverage_report(focus_df: pd.DataFrame) -> pd.DataFrame:
    """Allocation coverage per canonical tag key.

    One row per `focus.CANONICAL_TAGS` key: coverage_pct (allocated / total),
    the dollars still sitting in 'Unallocated', and a status. The ~90%
    chargeback-readiness line is practitioner consensus, not a published FinOps
    Foundation figure.
    """
    cols = ["tag_key", "coverage_pct", "unallocated_cost", "status"]
    if focus_df is None or not len(focus_df):
        return pd.DataFrame([{"tag_key": t, "coverage_pct": 0.0, "unallocated_cost": 0.0,
                              "status": "critical"} for t in focus.CANONICAL_TAGS], columns=cols)

    total = float(focus_df[COST].sum())
    rows = []
    for tag in focus.CANONICAL_TAGS:
        col = f"tag_{tag}"
        if col not in focus_df.columns or total == 0:
            rows.append({"tag_key": tag, "coverage_pct": 0.0, "unallocated_cost": 0.0, "status": "critical"})
            continue
        unallocated = float(focus_df.loc[focus_df[col].astype(str) == UNALLOCATED, COST].sum())
        coverage = (total - unallocated) / total * 100.0
        rows.append({"tag_key": tag, "coverage_pct": coverage, "unallocated_cost": unallocated,
                     "status": _coverage_status(coverage)})
    return pd.DataFrame(rows, columns=cols)


def untagged_breakdown(focus_df: pd.DataFrame, by: str = "ServiceName") -> pd.DataFrame:
    """Where the untagged money is, so it can be chased down at source.

    Untagged is defined by a missing business_unit -- the tag that gates
    chargeback. Grouped by `by` (ServiceName by default) and sorted by cost, this
    is the remediation worklist.
    """
    cols = [by, "cost", "pct_of_untagged"]
    if focus_df is None or not len(focus_df) or by not in focus_df.columns:
        return pd.DataFrame(columns=cols)
    untagged = focus_df[focus_df["tag_business_unit"].astype(str) == UNALLOCATED]
    if not len(untagged):
        return pd.DataFrame(columns=cols)
    out = untagged.groupby(by, as_index=False, observed=True)[COST].sum().rename(columns={COST: "cost"})
    grand = float(out["cost"].sum())
    out["pct_of_untagged"] = np.where(grand > 0, out["cost"] / grand * 100.0, 0.0)
    return out.sort_values("cost", ascending=False).reset_index(drop=True)


def tag_governance(focus_df: pd.DataFrame) -> pd.DataFrame:
    """Per canonical tag: coverage, unallocated cost, and a violation count.

    `violations` counts rows carrying no value for the key -- the population a
    tagging-policy enforcement (SCP, Azure Policy, GCP org policy) would target.
    Provider ceilings live in `PROVIDER_TAG_LIMITS` for the headroom check.
    """
    cols = ["tag_key", "coverage_pct", "unallocated_cost", "violations", "status"]
    if focus_df is None or not len(focus_df):
        return pd.DataFrame(columns=cols)
    total = float(focus_df[COST].sum())
    rows = []
    for tag in focus.CANONICAL_TAGS:
        col = f"tag_{tag}"
        if col not in focus_df.columns:
            rows.append({"tag_key": tag, "coverage_pct": 0.0, "unallocated_cost": 0.0,
                         "violations": 0, "status": "critical"})
            continue
        missing = focus_df[col].astype(str) == UNALLOCATED
        unallocated = float(focus_df.loc[missing, COST].sum())
        coverage = (total - unallocated) / total * 100.0 if total else 0.0
        rows.append({"tag_key": tag, "coverage_pct": coverage, "unallocated_cost": unallocated,
                     "violations": int(missing.sum()), "status": _coverage_status(coverage)})
    return pd.DataFrame(rows, columns=cols)
