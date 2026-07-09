"""Harness Cloud Cost Management (CCM) connector.

Harness CCM is the cost module of the Harness software-delivery platform. It
exposes cost via Perspectives, recommendations, budgets and anomalies under
`/ccm/api`.

The one thing that bites integrators
------------------------------------
EVERY call requires an `accountIdentifier` query parameter -- omit it and you
get a 400 regardless of a valid `x-api-key`. This connector attaches it to every
request from the `HARNESS_ACCOUNT_ID` secret.

Verification note
-----------------
The Perspective cost endpoints (`/ccm/api/perspective/grid`, `.../timeSeries`)
are marked [UNVERIFIED] below -- the request/response bodies were not confirmed
against primary docs at build time. Recommendations, budgets and anomalies
paths are as documented. Treat cost mapping as best-effort until confirmed
against your Harness instance.

Docs: https://apidocs.harness.io/  (Cloud Cost Management)
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import pandas as pd

import focus
from connectors.base import (
    AuthKind,
    Capability,
    ConnectionResult,
    ConnectorSpec,
    Recommendation,
)
from connectors.vendors._http import VendorConnector, VendorSession, first

BASE = "https://app.harness.io"

# --- endpoints ------------------------------------------------------------
# Recommendations: https://apidocs.harness.io/tag/Cloud-Cost-Recommendations
EP_RECOMMENDATIONS = "/ccm/api/recommendation/overview/list"
# Budgets: https://apidocs.harness.io/tag/Cloud-Cost-Budgets
EP_BUDGETS = "/ccm/api/budgets"
# Anomalies: https://apidocs.harness.io/tag/Cloud-Cost-Anomalies
EP_ANOMALIES = "/ccm/api/anomaly"
# Perspectives cost -- [UNVERIFIED] request/response bodies not confirmed.
EP_PERSPECTIVE_GRID = "/ccm/api/perspective/grid"        # [UNVERIFIED]
EP_PERSPECTIVE_TIMESERIES = "/ccm/api/perspective/timeSeries"  # [UNVERIFIED]
EP_PERSPECTIVES = "/ccm/api/perspective"


class HarnessConnector(VendorConnector):
    """Reads recommendations, budgets and anomalies from Harness CCM.
    Cost via Perspectives is best-effort (endpoints UNVERIFIED)."""

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="harness",
            display_name="Harness Cloud Cost Management",
            vendor="Harness",
            clouds=["AWS", "Azure", "GCP"],
            auth=AuthKind.BEARER_TOKEN,  # x-api-key header (PAT/SAT)
            capabilities=[
                Capability.COSTS,          # best-effort; Perspective grid UNVERIFIED
                Capability.RECOMMENDATIONS,
                Capability.BUDGETS,
                Capability.ANOMALIES,
            ],
            required_secrets=["HARNESS_API_KEY", "HARNESS_ACCOUNT_ID"],
            optional_secrets=[],
            base_url=BASE,
            docs_url="https://apidocs.harness.io/",
            focus_support="none",
            notes=(
                "Every call needs accountIdentifier (from HARNESS_ACCOUNT_ID). "
                "Auth is header x-api-key. Perspective cost endpoints "
                "(/ccm/api/perspective/grid, /timeSeries) are [UNVERIFIED] -- cost "
                "mapping is best-effort until confirmed on your instance."
            ),
        )

    def _account(self) -> str:
        return self.secret("HARNESS_ACCOUNT_ID") or ""

    def _session(self) -> VendorSession:
        return VendorSession(
            BASE,
            headers={
                "x-api-key": self.secret("HARNESS_API_KEY") or "",
                "Content-Type": "application/json",
            },
        )

    def _acct_params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = {"accountIdentifier": self._account()}
        if extra:
            params.update(extra)
        return params

    # -- connection -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            data = self._session().get_json(
                EP_RECOMMENDATIONS, params=self._acct_params({"limit": 1, "offset": 0})
            )
            recs = (data.get("data") or {}).get("items") or data.get("items") or []
            return {"account": self._account(), "recommendations_probe": len(recs)}

        return self._probe(call)

    # -- costs (best-effort; UNVERIFIED endpoint) -------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """Query the Perspective grid and map to FOCUS.

        [UNVERIFIED] The grid request/response body is not confirmed against
        primary docs; on any shape mismatch this returns an empty FOCUS frame
        rather than guessing.
        """
        if not self.configured:
            return self._empty_costs()
        body = {
            "filters": [],
            "groupBy": [{"entityGroupBy": {"fieldName": "service", "identifier": "COMMON"}}],
            "aggregations": [{"operationType": "SUM", "columnName": "cost"}],
            "timeRange": {"from": start.isoformat(), "to": end.isoformat()},
        }
        try:
            data = self._session().post_json(EP_PERSPECTIVE_GRID, params=self._acct_params(), json=body)
        except Exception:
            return self._empty_costs()
        grid = (data.get("data") or {}).get("gridData") or data.get("gridData") or []
        rows = [self._map_grid_row(g) for g in grid]
        return self._stamp(focus.normalize(pd.DataFrame(rows))) if rows else self._empty_costs()

    @staticmethod
    def _map_grid_row(g: Dict[str, Any]) -> Dict[str, Any]:
        cost = float(first(g, "cost", "value", default=0.0) or 0.0)
        name = first(g, "name", "id", default="Unknown")
        return {
            "BillingAccountId": "harness",
            "BillingAccountName": "Harness CCM",
            "BillingCurrency": "USD",
            "InvoiceIssuerName": "Harness",
            "ProviderName": "",
            "ChargeCategory": "Usage",
            "ChargeDescription": str(name),
            "BilledCost": cost,
            "EffectiveCost": cost,
            "ListCost": cost,
            "ContractedCost": cost,
            "ServiceCategory": "Other",
            "ServiceName": str(name),
        }

    # -- recommendations --------------------------------------------------

    def fetch_recommendations(self) -> List[Recommendation]:
        if not self.configured:
            return []
        try:
            data = self._session().get_json(
                EP_RECOMMENDATIONS, params=self._acct_params({"limit": 100, "offset": 0})
            )
        except Exception:
            return []
        items = (data.get("data") or {}).get("items") or data.get("items") or []
        out: List[Recommendation] = []
        for r in items:
            out.append(
                Recommendation(
                    source="harness",
                    cloud=str(first(r, "cloudProvider", default="")).upper(),
                    resource_id=str(first(r, "resourceName", "id", default="")),
                    resource_type=str(first(r, "resourceType", default="")),
                    lever=str(first(r, "recommendationType", default="rightsizing")),
                    action="Harness CCM recommendation",
                    estimated_monthly_savings=float(first(r, "monthlySaving", "monthlySavings", default=0.0) or 0.0),
                )
            )
        return out

    # -- budgets ----------------------------------------------------------

    def fetch_budgets(self) -> pd.DataFrame:
        if not self.configured:
            return super().fetch_budgets()
        try:
            data = self._session().get_json(EP_BUDGETS, params=self._acct_params())
        except Exception:
            return super().fetch_budgets()
        rows = []
        for b in (data.get("data") or data.get("resource") or []) or []:
            rows.append(
                {
                    "period": first(b, "startTime", "period", default=None),
                    "cloud": "All",
                    "application": first(b, "name", default="Budget"),
                    "budget": float(first(b, "budgetAmount", "amount", default=0.0) or 0.0),
                }
            )
        return pd.DataFrame(rows, columns=["period", "cloud", "application", "budget"]) if rows else super().fetch_budgets()
