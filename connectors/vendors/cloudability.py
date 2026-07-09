"""Cloudability (IBM / Apptio) connector.

Cloudability is Apptio's multicloud cost-management platform, now part of IBM.
It exposes a mature REST API (`/v3`) covering cost reporting, business mappings
(its allocation model), rightsizing recommendations, budgets, forecasting and
anomaly detection.

The one thing that bites integrators
-------------------------------------
Authentication is Basic auth where the API key is the *username* and the
password is EMPTY -- `requests` `auth=(api_key, "")`. People reflexively send
`Authorization: Bearer <key>` and get a 401. GovCloud is the exception: it uses
Apptio Frontdoor headers (`apptio-opentoken` + `apptio-environmentid`) instead.
This connector supports both; set the opentoken/environment secrets to switch.

FOCUS
-----
Cloudability *ingests* FOCUS v1.0 and v1.1, but its own cost export is not
FOCUS-conformant, so we map its report rows onto FOCUS ourselves ->
`focus_support = "ingest"`.

API docs: https://help.apptio.com/en-us/cloudability/api/v3/reporting.htm
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

# Default host. Regional hosts swap the leading label: api.usgov / api-au /
# api-eu / api-me / api-ca / api-in / api-jp / api-sg .cloudability.com
DEFAULT_HOST = "https://api.cloudability.com"

# --- endpoints (paths under the host) -------------------------------------
# Reporting (sync run): https://help.apptio.com/en-us/cloudability/api/v3/reporting.htm
EP_COST_RUN = "/v3/reporting/cost/run"
EP_COST_ENQUEUE = "/v3/reporting/cost/enqueue"  # async; 20 req/user then 429
EP_REPORT_STATE = "/v3/reporting/reports/{id}/state"      # enqueued|running|errored|finished
EP_REPORT_RESULTS = "/v3/reporting/reports/{id}/results"
EP_COST_MEASURES = "/v3/reporting/cost/measures"
EP_COST_FILTERS = "/v3/reporting/cost/filters"
# Business mappings (allocation): https://help.apptio.com/en-us/cloudability/api/v3/business-mappings-api.htm
EP_BUSINESS_MAPPINGS = "/v3/business-mappings"
# Rightsizing (read-only): https://help.apptio.com/en-us/cloudability/api/v3/rightsizing-api.htm
EP_RIGHTSIZING = "/v3/rightsizing/{vendor}/recommendations/{service}"
# Budgets / forecast / anomalies
EP_BUDGETS = "/v3/budgets"
EP_FORECAST = "/v3/forecast"
EP_ANOMALIES = "/v3/anomalies"  # ?viewId=&startDate=&endDate=

# Rightsizing surfaces we sweep for fetch_recommendations().
_RIGHTSIZING_TARGETS = [
    ("aws", "ec2"), ("aws", "rds"), ("aws", "ebs"),
    ("azure", "compute"), ("azure", "sql"),
    ("gcp", "compute"), ("gcp", "disk"),
]


class CloudabilityConnector(VendorConnector):
    """Reads cost, rightsizing, budgets and anomalies from Cloudability v3."""

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="cloudability",
            display_name="Cloudability (IBM Apptio)",
            vendor="Cloudability",
            clouds=["AWS", "Azure", "GCP"],
            auth=AuthKind.CUSTOM_HEADERS,  # Basic-with-empty-password OR Frontdoor headers
            capabilities=[
                Capability.COSTS,
                Capability.RECOMMENDATIONS,
                Capability.BUDGETS,
                Capability.FORECAST,
                Capability.ANOMALIES,
                Capability.ALLOCATION_RULES,
            ],
            required_secrets=["CLOUDABILITY_API_KEY"],
            optional_secrets=[
                "CLOUDABILITY_HOST",
                "CLOUDABILITY_OPENTOKEN",
                "CLOUDABILITY_ENVIRONMENT_ID",
            ],
            base_url=DEFAULT_HOST,
            docs_url="https://help.apptio.com/en-us/cloudability/api/v3/reporting.htm",
            focus_support="ingest",
            notes=(
                "Auth is Basic with the API key as USERNAME and an EMPTY password "
                "(not Bearer). GovCloud uses Frontdoor headers apptio-opentoken + "
                "apptio-environmentid instead. Async cost enqueue is capped at 20 "
                "requests/user before 429. Export is not FOCUS-conformant (mapped here)."
            ),
        )

    # -- auth / session ---------------------------------------------------

    def _host(self) -> str:
        return (self.secret("CLOUDABILITY_HOST") or DEFAULT_HOST).rstrip("/")

    def _session(self) -> VendorSession:
        opentoken = self.secret("CLOUDABILITY_OPENTOKEN")
        env_id = self.secret("CLOUDABILITY_ENVIRONMENT_ID")
        if opentoken and env_id:
            # Auth B: Apptio Frontdoor (required for GovCloud).
            return VendorSession(
                self._host(),
                headers={"apptio-opentoken": opentoken, "apptio-environmentid": env_id},
            )
        # Auth A (default): Basic where the API key is the username, password empty.
        return VendorSession(self._host(), auth=(self.secret("CLOUDABILITY_API_KEY") or "", ""))

    # -- connection -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            data = self._session().get_json(EP_COST_MEASURES)
            measures = data.get("result") or data.get("results") or []
            return {"host": self._host(), "measures": len(measures)}

        return self._probe(call)

    # -- costs ------------------------------------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """Pull a cost report and map it onto FOCUS.

        Uses the synchronous `cost/run` endpoint with a fixed dimension/metric
        set. For > 10,000 rows Cloudability paginates via a `token` query param
        (or `limit=0` to raise the cap to 64,000); we page on `token` here.
        """
        if not self.configured:
            return self._empty_costs()
        try:
            rows = self._run_cost_report(start, end)
        except Exception:
            return self._empty_costs()
        if not rows:
            return self._empty_costs()
        return self._stamp(focus.normalize(pd.DataFrame(rows)))

    def _run_cost_report(self, start: date, end: date) -> List[Dict[str, Any]]:
        session = self._session()
        params = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            # Max 15 dimensions / 8 metrics.
            "dimensions": ",".join(
                ["vendor", "vendor_account_identifier", "vendor_account_name",
                 "service_name", "region", "resource_identifier", "usage_family"]
            ),
            "metrics": ",".join(["unblended_cost", "amortized_cost", "list_cost", "usage_quantity"]),
            "sort": "amortized_cost",
        }
        out: List[Dict[str, Any]] = []
        token: Optional[str] = None
        for _ in range(64):  # hard stop so a runaway token loop cannot hang
            if token:
                params["token"] = token
            data = session.get_json(EP_COST_RUN, params=params)
            results = data.get("results", []) or []
            out.extend(self._map_cost_row(r) for r in results)
            token = (data.get("meta") or {}).get("token")
            if not token:
                break
        return out

    @staticmethod
    def _map_cost_row(r: Dict[str, Any]) -> Dict[str, Any]:
        vendor = first(r, "vendor", default="")
        amortized = float(first(r, "amortized_cost", "amortizedCost", default=0.0) or 0.0)
        unblended = float(first(r, "unblended_cost", "unblendedCost", default=amortized) or 0.0)
        list_cost = float(first(r, "list_cost", "listCost", default=unblended) or 0.0)
        return {
            "BillingAccountId": first(r, "vendor_account_identifier", default=""),
            "BillingAccountName": first(r, "vendor_account_name", default=""),
            "BillingCurrency": "USD",
            "InvoiceIssuerName": vendor or "Cloudability",
            "ProviderName": vendor,
            "SubAccountId": first(r, "vendor_account_identifier", default=""),
            "SubAccountName": first(r, "vendor_account_name", default=""),
            "ChargeCategory": "Usage",
            "ChargeDescription": first(r, "usage_family", "service_name", default="Cloud usage"),
            "BilledCost": unblended,
            "EffectiveCost": amortized,
            "ListCost": list_cost,
            "ContractedCost": amortized,
            "ConsumedQuantity": float(first(r, "usage_quantity", default=0.0) or 0.0),
            "ServiceCategory": "Other",  # Cloudability usage_family is not the FOCUS enum
            "ServiceName": first(r, "service_name", default="Unknown"),
            "RegionId": first(r, "region", default=""),
            "ResourceId": first(r, "resource_identifier", default=""),
        }

    # -- recommendations --------------------------------------------------

    def fetch_recommendations(self) -> List[Recommendation]:
        if not self.configured:
            return []
        session = self._session()
        out: List[Recommendation] = []
        for vendor, service in _RIGHTSIZING_TARGETS:
            path = EP_RIGHTSIZING.format(vendor=vendor, service=service)
            try:
                data = session.get_json(
                    path,
                    params={"basis": "effective", "duration": "thirty-day",
                            "sort": "-recommendations.savings"},
                )
            except Exception:
                continue
            for item in data.get("result", []) or data.get("results", []) or []:
                recs = item.get("recommendations") or []
                best = recs[0] if recs else {}
                savings = float(first(best, "savings", default=0.0) or 0.0)
                out.append(
                    Recommendation(
                        source="cloudability",
                        cloud=vendor.upper(),
                        resource_id=str(first(item, "resourceIdentifier", "resourceId", default="")),
                        resource_type=service,
                        lever="rightsizing",
                        action=str(first(best, "action", "name", default="Rightsize")),
                        estimated_monthly_savings=savings,
                        detail={"service": service, "vendor": vendor},
                    )
                )
        return out

    # -- budgets ----------------------------------------------------------

    def fetch_budgets(self) -> pd.DataFrame:
        if not self.configured:
            return super().fetch_budgets()
        try:
            data = self._session().get_json(EP_BUDGETS)
        except Exception:
            return super().fetch_budgets()
        rows = []
        for b in data.get("result", []) or data.get("results", []) or []:
            rows.append(
                {
                    "period": first(b, "startDate", "start_date", default=None),
                    "cloud": first(b, "vendor", default="All"),
                    "application": first(b, "name", default="Budget"),
                    "budget": float(first(b, "amount", "budgetAmount", default=0.0) or 0.0),
                }
            )
        return pd.DataFrame(rows, columns=["period", "cloud", "application", "budget"]) if rows else super().fetch_budgets()
