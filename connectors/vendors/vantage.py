"""Vantage connector.

Vantage is a developer-friendly cost platform with a clean, well-documented
REST API and -- unusually among the tools here -- native FOCUS export.

The one thing that bites integrators
------------------------------------
`GET /v2/costs` does NOT accept a free-form query. It reads back an EXISTING,
saved "Cost Report", so it REQUIRES a `cost_report_token`. You cannot fetch
costs until a Cost Report exists in the workspace. This connector discovers the
first available report token via `GET /v2/cost_reports` when one is not supplied
via the `VANTAGE_COST_REPORT_TOKEN` secret.

FOCUS: Vantage supports FOCUS export natively -> "native".

Docs: https://vantage.readme.io/reference/
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

BASE = "https://api.vantage.sh/v2"

# --- endpoints ------------------------------------------------------------
# https://vantage.readme.io/reference/getcosts
EP_COSTS = "/v2/costs"
EP_COST_REPORTS = "/v2/cost_reports"
EP_RECOMMENDATIONS = "/v2/recommendations"
EP_FORECASTS = "/v2/forecasted_costs"
EP_ANOMALIES = "/v2/anomaly_notifications"

_PAGE_LIMIT = 1000


class VantageConnector(VendorConnector):
    """Reads costs, recommendations and forecasts from Vantage v2."""

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="vantage",
            display_name="Vantage",
            vendor="Vantage",
            clouds=["AWS", "Azure", "GCP"],
            auth=AuthKind.BEARER_TOKEN,
            capabilities=[
                Capability.COSTS,
                Capability.RECOMMENDATIONS,
                Capability.FORECAST,
                Capability.ANOMALIES,
                Capability.NATIVE_FOCUS,
            ],
            required_secrets=["VANTAGE_API_TOKEN"],
            optional_secrets=["VANTAGE_COST_REPORT_TOKEN"],
            base_url=BASE,
            docs_url="https://vantage.readme.io/reference/",
            focus_support="native",
            notes=(
                "GET /v2/costs requires an existing saved Cost Report "
                "(cost_report_token). If VANTAGE_COST_REPORT_TOKEN is unset, the "
                "first report from /v2/cost_reports is used. Native FOCUS export."
            ),
        )

    def _session(self) -> VendorSession:
        return VendorSession(
            BASE,
            headers={
                "Authorization": f"Bearer {self.secret('VANTAGE_API_TOKEN') or ''}",
                "Accept": "application/json",
            },
        )

    # -- connection -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            data = self._session().get_json(EP_COST_REPORTS, params={"limit": 1})
            reports = data.get("cost_reports") or data.get("reports") or []
            return {"cost_reports": len(reports)}

        return self._probe(call)

    # -- costs ------------------------------------------------------------

    def _report_token(self, session: VendorSession) -> Optional[str]:
        token = self.secret("VANTAGE_COST_REPORT_TOKEN")
        if token:
            return token
        data = session.get_json(EP_COST_REPORTS, params={"limit": 1})
        reports = data.get("cost_reports") or data.get("reports") or []
        return first(reports[0], "token", "id", default=None) if reports else None

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        if not self.configured:
            return self._empty_costs()
        session = self._session()
        try:
            report_token = self._report_token(session)
            if not report_token:
                return self._empty_costs()
            rows = self._paged_costs(session, report_token, start, end)
        except Exception:
            return self._empty_costs()
        return self._stamp(focus.normalize(pd.DataFrame(rows))) if rows else self._empty_costs()

    def _paged_costs(self, session: VendorSession, report_token: str, start: date, end: date) -> List[Dict[str, Any]]:
        params = {
            "cost_report_token": report_token,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "date_bin": "day",
            "limit": _PAGE_LIMIT,
        }
        rows: List[Dict[str, Any]] = []
        path: Optional[str] = EP_COSTS
        page_params: Optional[Dict[str, Any]] = params
        for _ in range(500):
            data = session.get_json(path, params=page_params)
            rows.extend(self._map_cost_row(c) for c in data.get("costs", []) or [])
            nxt = (data.get("links") or {}).get("next")
            if not nxt:
                break
            path, page_params = nxt, None  # links.next is a full URL
        return rows

    @staticmethod
    def _map_cost_row(c: Dict[str, Any]) -> Dict[str, Any]:
        amount = float(first(c, "amount", "cost", default=0.0) or 0.0)
        provider = str(first(c, "provider", default="")).upper()
        return {
            "BillingAccountId": first(c, "account_id", default=provider or "vantage"),
            "BillingAccountName": provider or "Vantage",
            "BillingCurrency": "USD",
            "InvoiceIssuerName": "Vantage",
            "ProviderName": provider,
            "ChargePeriodStart": first(c, "accrued_at", "date", default=None),
            "ChargeCategory": "Usage",
            "ChargeDescription": first(c, "service", "category", default="Cloud usage"),
            "BilledCost": amount,
            "EffectiveCost": amount,
            "ListCost": amount,
            "ContractedCost": amount,
            "ServiceCategory": "Other",
            "ServiceName": first(c, "service", default="Unknown"),
        }

    # -- recommendations --------------------------------------------------

    def fetch_recommendations(self) -> List[Recommendation]:
        if not self.configured:
            return []
        try:
            data = self._session().get_json(EP_RECOMMENDATIONS, params={"limit": _PAGE_LIMIT})
        except Exception:
            return []
        out: List[Recommendation] = []
        for r in data.get("recommendations", []) or []:
            out.append(
                Recommendation(
                    source="vantage",
                    cloud=str(first(r, "provider", default="")).upper(),
                    resource_id=str(first(r, "resource_id", "token", default="")),
                    resource_type=str(first(r, "category", default="")),
                    lever=str(first(r, "category", default="rightsizing")),
                    action=str(first(r, "title", "description", default="Optimize")),
                    estimated_monthly_savings=float(first(r, "potential_savings", "savings", default=0.0) or 0.0),
                )
            )
        return out
