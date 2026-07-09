"""Flexera One connector.

Flexera One is Flexera's SaaS platform; its cost module is the former
RightScale Optima. Two things make it unusual among the vendors here:

  * the identity plane and the data plane live on DIFFERENT hosts. You get an
    OAuth2 bearer token from `login.flexera.{com|eu|au}`, but you query costs
    against `optima.rightscale.com` (the legacy Optima host), passing an
    explicit `X-Api-Version: 1.0` header.
  * cost queries are POSTed to `/bill-analysis/orgs/{orgId}/costs/select` with a
    body describing dimensions, metrics, filter and granularity -- there is no
    GET form.

The one thing that bites integrators
------------------------------------
The token host and the region zone must agree. A NAM refresh token will not mint
a token that `optima.rightscale.com` accepts if you point it at the `.eu` login
host. Set `FLEXERA_ZONE` (com|eu|au) to keep them in sync.

FOCUS: Flexera ingests FOCUS v1.0 (plus selected v1.2 fields) via Bill Connect,
but does not emit it -> "ingest".

Docs: https://developer.flexera.com/  (Optima "costs/select")
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
)
from connectors.vendors._http import VendorConnector, VendorSession, first

OPTIMA_HOST = "https://optima.rightscale.com"

# --- endpoints ------------------------------------------------------------
# Token: https://developer.flexera.com/docs/guides/generating-api-token/
EP_TOKEN = "https://login.flexera.{zone}/oidc/token"
# Costs: https://developer.flexera.com/docs/api/optima/  (costs/select)
EP_COSTS_SELECT = "/bill-analysis/orgs/{org}/costs/select"
EP_COSTS_METRICS = "/bill-analysis/orgs/{org}/costs/metrics"
EP_COSTS_DIMENSIONS = "/bill-analysis/orgs/{org}/costs/dimensions"
EP_BUDGETS = "/bill-analysis/orgs/{org}/budgets"
EP_ANOMALIES = "/bill-analysis/orgs/{org}/anomalies/report"

_TOKEN_SKEW_SECONDS = 60  # refresh a minute before the stated expiry


class FlexeraConnector(VendorConnector):
    """Reads Optima cost, budgets and anomalies from Flexera One."""

    def __init__(self, secrets: Optional[Dict[str, str]] = None, **options: Any) -> None:
        super().__init__(secrets, **options)
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="flexera",
            display_name="Flexera One (Optima)",
            vendor="Flexera",
            clouds=["AWS", "Azure", "GCP"],
            auth=AuthKind.OAUTH2_CLIENT_CREDENTIALS,
            capabilities=[
                Capability.COSTS,
                Capability.BUDGETS,
                Capability.ANOMALIES,
            ],
            required_secrets=["FLEXERA_ORG_ID"],
            optional_secrets=[
                "FLEXERA_REFRESH_TOKEN",
                "FLEXERA_CLIENT_ID",
                "FLEXERA_CLIENT_SECRET",
                "FLEXERA_ZONE",
            ],
            base_url=OPTIMA_HOST,
            docs_url="https://developer.flexera.com/",
            focus_support="ingest",
            notes=(
                "Token host (login.flexera.{com|eu|au}) and data host "
                "(optima.rightscale.com) differ; costs/select needs header "
                "X-Api-Version: 1.0. Auth via refresh_token OR client_credentials. "
                "Set FLEXERA_ZONE to match your account region. Ingests FOCUS 1.0."
            ),
        )

    # -- auth -------------------------------------------------------------

    def _zone(self) -> str:
        return (self.secret("FLEXERA_ZONE") or "com").strip()

    def missing_secrets(self) -> List[str]:
        # ORG_ID plus at least one auth path (refresh token or client creds).
        missing = [s for s in self.spec.required_secrets if not self.secret(s)]
        has_refresh = bool(self.secret("FLEXERA_REFRESH_TOKEN"))
        has_client = bool(self.secret("FLEXERA_CLIENT_ID") and self.secret("FLEXERA_CLIENT_SECRET"))
        if not (has_refresh or has_client):
            missing.append("FLEXERA_REFRESH_TOKEN|FLEXERA_CLIENT_ID+FLEXERA_CLIENT_SECRET")
        return missing

    def _token(self) -> str:
        now = time.time()
        if self._access_token and now < self._expires_at:
            return self._access_token
        url = EP_TOKEN.format(zone=self._zone())
        if self.secret("FLEXERA_REFRESH_TOKEN"):
            body = {"grant_type": "refresh_token", "refresh_token": self.secret("FLEXERA_REFRESH_TOKEN")}
        else:
            body = {
                "grant_type": "client_credentials",
                "client_id": self.secret("FLEXERA_CLIENT_ID"),
                "client_secret": self.secret("FLEXERA_CLIENT_SECRET"),
            }
        resp = VendorSession().post(
            url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        resp.raise_for_status()
        payload = resp.json()
        self._access_token = payload.get("access_token")
        self._expires_at = now + float(payload.get("expires_in", 3600)) - _TOKEN_SKEW_SECONDS
        return self._access_token or ""

    def _optima(self) -> VendorSession:
        return VendorSession(
            OPTIMA_HOST,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "X-Api-Version": "1.0",
                "Content-Type": "application/json",
            },
        )

    def _org(self) -> str:
        return self.secret("FLEXERA_ORG_ID") or ""

    # -- connection -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            path = EP_COSTS_DIMENSIONS.format(org=self._org())
            data = self._optima().get_json(path)
            dims = data.get("dimensions") or data.get("values") or []
            return {"zone": self._zone(), "org": self._org(), "dimensions": len(dims)}

        return self._probe(call)

    # -- costs ------------------------------------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """POST a costs/select query and map the result onto FOCUS.

        Follows the cursor envelope (`nextPage`) until exhausted.
        """
        if not self.configured:
            return self._empty_costs()
        body = {
            "dimensions": ["vendor", "vendor_account", "category", "service", "instance_type", "usage_unit"],
            "granularity": "day",
            "start_at": start.strftime("%Y-%m"),
            "end_at": end.strftime("%Y-%m"),
            "metrics": ["usage_amount", "cost_amortized_unblended_adj", "cost_nonamortized_unblended_adj"],
            "limit": 1000,
        }
        try:
            rows = self._paged_select(body)
        except Exception:
            return self._empty_costs()
        return self._stamp(focus.normalize(pd.DataFrame(rows))) if rows else self._empty_costs()

    def _paged_select(self, body: Dict[str, Any]) -> List[Dict[str, Any]]:
        session = self._optima()
        path: Optional[str] = EP_COSTS_SELECT.format(org=self._org())
        rows: List[Dict[str, Any]] = []
        for _ in range(200):  # bounded: cursor loops shouldn't run forever
            resp = session.post(path, json=body)
            resp.raise_for_status()
            data = resp.json()
            for r in data.get("values", []) or []:
                rows.append(self._map_cost_row(r))
            path = data.get("nextPage")
            if not path:
                break
            body = {}  # nextPage is a fully-formed URL; no body on follow-ups
        return rows

    @staticmethod
    def _map_cost_row(r: Dict[str, Any]) -> Dict[str, Any]:
        dims = r.get("dimensions", r)
        metrics = r.get("metrics", r)
        amortized = float(first(metrics, "cost_amortized_unblended_adj", default=0.0) or 0.0)
        unblended = float(first(metrics, "cost_nonamortized_unblended_adj", default=amortized) or 0.0)
        vendor = str(first(dims, "vendor", default="")).upper()
        return {
            "BillingAccountId": first(dims, "vendor_account", default=""),
            "BillingAccountName": first(dims, "vendor_account", default=""),
            "BillingCurrency": "USD",
            "InvoiceIssuerName": "Flexera",
            "ProviderName": vendor,
            "SubAccountId": first(dims, "vendor_account", default=""),
            "ChargeCategory": "Usage",
            "ChargeDescription": first(dims, "service", "category", default="Cloud usage"),
            "BilledCost": unblended,
            "EffectiveCost": amortized,
            "ListCost": unblended,
            "ContractedCost": amortized,
            "ConsumedQuantity": float(first(metrics, "usage_amount", default=0.0) or 0.0),
            "ConsumedUnit": first(dims, "usage_unit", default=""),
            "ServiceCategory": "Other",
            "ServiceName": first(dims, "service", default="Unknown"),
        }

    # -- budgets ----------------------------------------------------------

    def fetch_budgets(self) -> pd.DataFrame:
        if not self.configured:
            return super().fetch_budgets()
        try:
            data = self._optima().get_json(EP_BUDGETS.format(org=self._org()))
        except Exception:
            return super().fetch_budgets()
        rows = []
        for b in data.get("values", []) or data.get("budgets", []) or []:
            rows.append(
                {
                    "period": first(b, "start_at", "period", default=None),
                    "cloud": first(b, "vendor", default="All"),
                    "application": first(b, "name", default="Budget"),
                    "budget": float(first(b, "amount", "budget", default=0.0) or 0.0),
                }
            )
        return pd.DataFrame(rows, columns=["period", "cloud", "application", "budget"]) if rows else super().fetch_budgets()
