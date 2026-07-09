"""CloudHealth (Broadcom / VMware Tanzu) connector.

CloudHealth is Broadcom's (formerly VMware's) cost-management and governance
platform. It has TWO APIs that do not overlap cleanly:

  * a classic REST API (`https://chapi.cloudhealthtech.com`) for OLAP cost
    reports and Perspectives (its allocation model), and
  * a newer GraphQL API (`https://apps.cloudhealthtech.com/graphql`) that is the
    ONLY place rightsizing recommendations, cost anomalies, budgets and
    forecasting live.

The two things that bite integrators
------------------------------------
1. GraphQL auth is a two-step token dance: you exchange the long-lived API key
   for an accessToken that is valid for only ~15 MINUTES (refreshToken ~9 hours).
   A long-running session MUST refresh proactively -- this connector re-fetches
   at ~13 min and re-logs-in at ~8.5 h.
2. The REST GET URI has a hard 4000-character limit. Pile on dimensions and
   filters and the request silently 414s; batch or reduce instead.

FOCUS: CloudHealth ingests FOCUS but does not emit it, so we map -> "ingest".

REST docs:    https://apidocs.cloudhealthtech.com/
GraphQL docs: https://docs.cloudhealthtech.com/ (GraphQL Explorer)
"""

from __future__ import annotations

import time
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

DEFAULT_REST_HOST = "https://chapi.cloudhealthtech.com"
DEFAULT_GRAPHQL_URL = "https://apps.cloudhealthtech.com/graphql"

# --- REST endpoints -------------------------------------------------------
# https://apidocs.cloudhealthtech.com/#reporting_get-report-data
EP_OLAP_REPORTS = "/olap_reports"
EP_COST_HISTORY = "/olap_reports/cost/history"
EP_COST_CURRENT = "/olap_reports/cost/current"
# Perspectives (allocation): https://apidocs.cloudhealthtech.com/#perspectives
EP_PERSPECTIVE_SCHEMAS = "/v1/perspective_schemas"

# accessToken lifetime is ~15 min; refresh well before to avoid a mid-call 401.
_ACCESS_REFRESH_SECONDS = 13 * 60
_REFRESH_TOKEN_TTL_SECONDS = int(8.5 * 3600)
# REST GET URIs above this many characters are rejected by CloudHealth.
_URI_HARD_LIMIT = 4000


class _GraphQLTokens:
    """Minimal token manager for the CloudHealth GraphQL endpoint.

    Holds the short-lived accessToken and refreshes it before expiry. Kept
    separate so the REST path never pays the login cost.
    """

    def __init__(self, graphql_url: str, api_key: str) -> None:
        self._url = graphql_url
        self._api_key = api_key
        self._session = VendorSession()
        self._access: Optional[str] = None
        self._refresh: Optional[str] = None
        self._access_at = 0.0
        self._login_at = 0.0

    def _post(self, query: str, variables: Dict[str, Any], token: Optional[str]) -> Dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = self._session.post(self._url, json={"query": query, "variables": variables}, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"GraphQL error: {body['errors'][0].get('message', 'unknown')}")
        return body.get("data") or {}

    def _login(self) -> None:
        data = self._post(
            "mutation($apiKey:String!){ loginAPI(apiKey:$apiKey){ accessToken refreshToken } }",
            {"apiKey": self._api_key},
            token=None,
        )
        creds = data.get("loginAPI") or {}
        self._access = creds.get("accessToken")
        self._refresh = creds.get("refreshToken")
        now = time.time()
        self._access_at = now
        self._login_at = now

    def _do_refresh(self) -> None:
        data = self._post(
            "mutation($token:String!){ refresh(token:$token){ accessToken refreshToken } }",
            {"token": self._refresh},
            token=None,
        )
        creds = data.get("refresh") or {}
        self._access = creds.get("accessToken")
        self._refresh = creds.get("refreshToken") or self._refresh
        self._access_at = time.time()

    def token(self) -> str:
        now = time.time()
        if self._access is None or (now - self._login_at) > _REFRESH_TOKEN_TTL_SECONDS:
            self._login()
        elif (now - self._access_at) > _ACCESS_REFRESH_SECONDS:
            self._do_refresh()
        return self._access or ""

    def query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._post(query, variables or {}, token=self.token())


class CloudHealthConnector(VendorConnector):
    """Reads OLAP cost + Perspectives over REST, and recommendations /
    anomalies / budgets over GraphQL."""

    def __init__(self, secrets: Optional[Dict[str, str]] = None, **options: Any) -> None:
        super().__init__(secrets, **options)
        self._tokens: Optional[_GraphQLTokens] = None

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="cloudhealth",
            display_name="CloudHealth (Broadcom)",
            vendor="CloudHealth",
            clouds=["AWS", "Azure", "GCP"],
            auth=AuthKind.BEARER_TOKEN,
            capabilities=[
                Capability.COSTS,
                Capability.RECOMMENDATIONS,  # GraphQL rightsizingRecommendations
                Capability.BUDGETS,          # GraphQL only
                Capability.FORECAST,         # GraphQL only
                Capability.ANOMALIES,        # GraphQL costAnomalies
                Capability.ALLOCATION_RULES, # Perspectives
            ],
            required_secrets=["CLOUDHEALTH_API_KEY"],
            optional_secrets=[
                "CLOUDHEALTH_REST_HOST",
                "CLOUDHEALTH_GRAPHQL_URL",
                "CLOUDHEALTH_ORG_ID",
            ],
            base_url=DEFAULT_REST_HOST,
            docs_url="https://apidocs.cloudhealthtech.com/",
            focus_support="ingest",
            notes=(
                "Two APIs: REST (chapi) for OLAP cost + Perspectives, GraphQL (apps) "
                "for recommendations/anomalies/budgets/forecast. GraphQL accessToken "
                "lasts ~15 min (refreshToken ~9 h) -- token manager refreshes at 13 min. "
                "REST GET URI hard limit is 4000 chars; POST/PUT require "
                "Content-Type: application/json or HTTP 422."
            ),
        )

    # -- sessions ---------------------------------------------------------

    def _rest_host(self) -> str:
        return (self.secret("CLOUDHEALTH_REST_HOST") or DEFAULT_REST_HOST).rstrip("/")

    def _rest(self) -> VendorSession:
        key = self.secret("CLOUDHEALTH_API_KEY") or ""
        return VendorSession(
            self._rest_host(),
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )

    def _graphql(self) -> _GraphQLTokens:
        if self._tokens is None:
            self._tokens = _GraphQLTokens(
                self.secret("CLOUDHEALTH_GRAPHQL_URL") or DEFAULT_GRAPHQL_URL,
                self.secret("CLOUDHEALTH_API_KEY") or "",
            )
        return self._tokens

    def _rest_params(self) -> Dict[str, Any]:
        org = self.secret("CLOUDHEALTH_ORG_ID")
        return {"org_id": org} if org else {}

    # -- connection -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            # List OLAP report types -- cheapest authenticated REST call.
            data = self._rest().get_json(EP_OLAP_REPORTS, params=self._rest_params())
            links = data.get("links") or data
            return {"rest_host": self._rest_host(), "report_types": len(links) if hasattr(links, "__len__") else 0}

        return self._probe(call)

    # -- costs ------------------------------------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """Pull the `cost/history` OLAP report and map it onto FOCUS.

        Keep the dimension/filter set small: the REST GET URI is capped at 4000
        characters, so we request a coarse (account x service) grain by month.
        """
        if not self.configured:
            return self._empty_costs()
        params = dict(self._rest_params())
        params.update(
            {
                "interval": "monthly",
                "dimensions[]": ["time", "AWS-Account", "AWS-Service-Category"],
                "measures[]": ["cost"],
            }
        )
        try:
            data = self._rest().get_json(EP_COST_HISTORY, params=params)
            rows = self._map_olap(data, start, end)
        except Exception:
            return self._empty_costs()
        return self._stamp(focus.normalize(pd.DataFrame(rows))) if rows else self._empty_costs()

    @staticmethod
    def _map_olap(data: Dict[str, Any], start: date, end: date) -> List[Dict[str, Any]]:
        """OLAP responses are nested numeric arrays keyed by `dimensions`.

        The response carries `dimensions:[{name:[members]}]` and a parallel
        nested `data` array. We flatten the (time, account, service) cube into
        one FOCUS row per cell. Shapes vary by report, so we defend heavily.
        """
        dims = data.get("dimensions") or []

        def members(name: str) -> List[Any]:
            for d in dims:
                if name in d:
                    return d[name]
            return []

        times = members("time")
        accounts = members("AWS-Account") or [{"label": ""}]
        services = members("AWS-Service-Category") or [{"label": "Unknown"}]
        cube = data.get("data") or []

        rows: List[Dict[str, Any]] = []
        for ti, t in enumerate(times):
            period = first(t, "name", "label", default="") if isinstance(t, dict) else str(t)
            for ai, acct in enumerate(accounts):
                acct_label = first(acct, "label", "name", default="") if isinstance(acct, dict) else str(acct)
                for si, svc in enumerate(services):
                    svc_label = first(svc, "label", "name", default="Unknown") if isinstance(svc, dict) else str(svc)
                    try:
                        cost = float(cube[ti][ai][si][0])
                    except (IndexError, TypeError, ValueError):
                        continue
                    if not cost:
                        continue
                    rows.append(
                        {
                            "BillingAccountId": acct_label,
                            "BillingAccountName": acct_label,
                            "BillingCurrency": "USD",
                            "InvoiceIssuerName": "CloudHealth",
                            "ProviderName": "AWS",
                            "ChargeCategory": "Usage",
                            "ChargeDescription": f"{svc_label} ({period})",
                            "BilledCost": cost,
                            "EffectiveCost": cost,
                            "ListCost": cost,
                            "ContractedCost": cost,
                            "ServiceCategory": "Other",
                            "ServiceName": svc_label,
                        }
                    )
        return rows

    # -- recommendations (GraphQL only) -----------------------------------

    def fetch_recommendations(self) -> List[Recommendation]:
        if not self.configured:
            return []
        query = """
        query { rightsizingRecommendations {
            nodes { resourceId resourceType cloud recommendedAction estimatedMonthlySavings }
        } }
        """
        try:
            data = self._graphql().query(query)
        except Exception:
            return []
        nodes = ((data.get("rightsizingRecommendations") or {}).get("nodes")) or []
        out: List[Recommendation] = []
        for n in nodes:
            out.append(
                Recommendation(
                    source="cloudhealth",
                    cloud=str(first(n, "cloud", default="")).upper(),
                    resource_id=str(first(n, "resourceId", default="")),
                    resource_type=str(first(n, "resourceType", default="")),
                    lever="rightsizing",
                    action=str(first(n, "recommendedAction", default="Rightsize")),
                    estimated_monthly_savings=float(first(n, "estimatedMonthlySavings", default=0.0) or 0.0),
                )
            )
        return out
