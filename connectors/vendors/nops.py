"""nOps connector.

nOps is an AWS-focused cloud-optimization platform. Its strongest, best-defined
surface is recommendations -- rightsizing and the "Compute Copilot" automation.

The one thing that bites integrators
------------------------------------
The cost surface is under-documented. The recommendation endpoints under
`/c/v3/...` are stable, but the cost endpoints live under `/c/admin/...` and
their exact request/response shapes are [UNVERIFIED]. We therefore declare
RECOMMENDATIONS honestly and mark COSTS best-effort: `fetch_costs` attempts a
map but returns an empty FOCUS frame on any shape mismatch rather than
fabricating rows.

Auth: header `X-Nops-Api-Key: <key>`.

FOCUS: not a FOCUS emitter -> "none".

Docs: https://help.nops.io/  (nOps API)
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

BASE = "https://app.nops.io"

# --- endpoints ------------------------------------------------------------
# https://help.nops.io/  (nOps public API; versioned under /c/v3)
EP_RIGHTSIZING = "/c/v3/rightsizing/recommendations"  # rightsizing recs
EP_COMPUTE_COPILOT = "/c/v3/compute-copilot/recommendations"
# Cost -- [UNVERIFIED] path and payload under /c/admin/... not confirmed.
EP_COST = "/c/admin/cost"  # [UNVERIFIED]


class NOpsConnector(VendorConnector):
    """Reads rightsizing / Compute Copilot recommendations from nOps.
    Cost is best-effort (endpoint UNVERIFIED)."""

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="nops",
            display_name="nOps",
            vendor="nOps",
            clouds=["AWS"],
            auth=AuthKind.CUSTOM_HEADERS,  # X-Nops-Api-Key
            capabilities=[
                Capability.RECOMMENDATIONS,
                Capability.COSTS,  # best-effort; /c/admin cost path UNVERIFIED
            ],
            required_secrets=["NOPS_API_KEY"],
            optional_secrets=[],
            base_url=BASE,
            docs_url="https://help.nops.io/",
            focus_support="none",
            notes=(
                "Auth header X-Nops-Api-Key. Recommendations (rightsizing + Compute "
                "Copilot) are the reliable surface. Cost endpoints under /c/admin "
                "are [UNVERIFIED] -- fetch_costs is best-effort and returns an empty "
                "FOCUS frame on any shape mismatch."
            ),
        )

    def _session(self) -> VendorSession:
        return VendorSession(
            BASE,
            headers={"X-Nops-Api-Key": self.secret("NOPS_API_KEY") or "", "Accept": "application/json"},
        )

    # -- connection -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            data = self._session().get_json(EP_RIGHTSIZING, params={"limit": 1})
            items = data.get("results") or data.get("data") or []
            return {"recommendations_probe": len(items)}

        return self._probe(call)

    # -- costs (best-effort; UNVERIFIED) ----------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """[UNVERIFIED] Attempt a cost read under /c/admin. Returns an empty
        FOCUS frame on any error or unexpected shape."""
        if not self.configured:
            return self._empty_costs()
        try:
            data = self._session().get_json(
                EP_COST, params={"start_date": start.isoformat(), "end_date": end.isoformat()}
            )
        except Exception:
            return self._empty_costs()
        records = data.get("results") or data.get("data") or []
        rows = [self._map_cost_row(r) for r in records if isinstance(r, dict)]
        return self._stamp(focus.normalize(pd.DataFrame(rows))) if rows else self._empty_costs()

    @staticmethod
    def _map_cost_row(r: Dict[str, Any]) -> Dict[str, Any]:
        amount = float(first(r, "cost", "amount", "total", default=0.0) or 0.0)
        return {
            "BillingAccountId": first(r, "account_id", default="nops"),
            "BillingAccountName": first(r, "account_name", default="nOps"),
            "BillingCurrency": "USD",
            "InvoiceIssuerName": "nOps",
            "ProviderName": "AWS",
            "ChargePeriodStart": first(r, "date", "period", default=None),
            "ChargeCategory": "Usage",
            "ChargeDescription": first(r, "service", default="Cloud usage"),
            "BilledCost": amount,
            "EffectiveCost": amount,
            "ListCost": amount,
            "ContractedCost": amount,
            "ServiceCategory": "Other",
            "ServiceName": first(r, "service", default="Unknown"),
        }

    # -- recommendations --------------------------------------------------

    def fetch_recommendations(self) -> List[Recommendation]:
        if not self.configured:
            return []
        session = self._session()
        out: List[Recommendation] = []
        for path, lever in ((EP_RIGHTSIZING, "rightsizing"), (EP_COMPUTE_COPILOT, "compute-copilot")):
            try:
                data = session.get_json(path, params={"limit": 200})
            except Exception:
                continue
            for r in data.get("results") or data.get("data") or []:
                out.append(
                    Recommendation(
                        source="nops",
                        cloud="AWS",
                        resource_id=str(first(r, "resource_id", "instance_id", default="")),
                        resource_type=str(first(r, "resource_type", default="")),
                        lever=lever,
                        action=str(first(r, "action", "recommendation", default="Optimize")),
                        estimated_monthly_savings=float(first(r, "monthly_savings", "savings", default=0.0) or 0.0),
                    )
                )
        return out
