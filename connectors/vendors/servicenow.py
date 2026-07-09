"""ServiceNow Cloud Cost Management connector.

ServiceNow's Cloud Cost Management (part of ITOM / Cloud Operations) stores cost
data in tables inside a scoped application. It is reached through the generic
Table API, which is stable and well documented -- but the COST table names are
NOT.

The one thing that bites integrators
------------------------------------
The CCM cost table names live in the scoped app and are not publicly documented,
and they differ across instances and app versions. Do NOT hardcode one. This
connector:
  * lets the table be set explicitly via `SERVICENOW_COST_TABLE`, and
  * offers `discover_tables()` (queries `sys_db_object` for `sn_`-prefixed
    tables) so an integrator can find the right one on their instance.
You MUST confirm the table name against the customer's instance before trusting
`fetch_costs`.

Auth: HTTP Basic, or OAuth2 (`POST /oauth_token.do`) -> Bearer.

FOCUS: not a FOCUS source -> "none". The field->FOCUS mapping is instance
specific and best-effort.

Docs: https://docs.servicenow.com/  (Table API / Cloud Cost Management)
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

# --- endpoints (under https://<instance>.service-now.com) -----------------
# https://docs.servicenow.com/bundle/latest-application-development/page/integrate/inbound-rest/concept/c_TableAPI.html
EP_OAUTH = "/oauth_token.do"
EP_TABLE = "/api/now/table/{table}"
EP_SYS_DB_OBJECT = "/api/now/table/sys_db_object"

_PAGE = 1000
_TOKEN_SKEW_SECONDS = 60


class ServiceNowConnector(VendorConnector):
    """Reads cost rows out of a ServiceNow CCM table via the Table API.
    The cost table name is instance-specific and MUST be confirmed."""

    def __init__(self, secrets: Optional[Dict[str, str]] = None, **options: Any) -> None:
        super().__init__(secrets, **options)
        self._access_token: Optional[str] = None
        self._expires_at = 0.0

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="servicenow",
            display_name="ServiceNow Cloud Cost Management",
            vendor="ServiceNow",
            clouds=["AWS", "Azure", "GCP"],
            auth=AuthKind.OAUTH2_CLIENT_CREDENTIALS,  # or Basic
            capabilities=[Capability.COSTS, Capability.ALLOCATION_RULES],
            required_secrets=["SERVICENOW_INSTANCE", "SERVICENOW_USERNAME", "SERVICENOW_PASSWORD"],
            optional_secrets=[
                "SERVICENOW_CLIENT_ID",
                "SERVICENOW_CLIENT_SECRET",
                "SERVICENOW_COST_TABLE",
            ],
            base_url="",
            docs_url="https://docs.servicenow.com/",
            focus_support="none",
            notes=(
                "CCM cost table names live in a scoped app and are NOT publicly "
                "documented -- they vary by instance/version. Set "
                "SERVICENOW_COST_TABLE and confirm via discover_tables() before "
                "trusting fetch_costs. Auth is Basic or OAuth2 (/oauth_token.do). "
                "Field->FOCUS mapping is instance-specific and best-effort."
            ),
        )

    # -- auth / session ---------------------------------------------------

    def _host(self) -> str:
        instance = self.secret("SERVICENOW_INSTANCE") or ""
        if instance.startswith("http"):
            return instance.rstrip("/")
        return f"https://{instance}.service-now.com" if instance else ""

    def _oauth_token(self) -> Optional[str]:
        if not (self.secret("SERVICENOW_CLIENT_ID") and self.secret("SERVICENOW_CLIENT_SECRET")):
            return None
        now = time.time()
        if self._access_token and now < self._expires_at:
            return self._access_token
        resp = VendorSession(self._host()).post(
            EP_OAUTH,
            data={
                "grant_type": "password",
                "client_id": self.secret("SERVICENOW_CLIENT_ID"),
                "client_secret": self.secret("SERVICENOW_CLIENT_SECRET"),
                "username": self.secret("SERVICENOW_USERNAME"),
                "password": self.secret("SERVICENOW_PASSWORD"),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        payload = resp.json()
        self._access_token = payload.get("access_token")
        self._expires_at = now + float(payload.get("expires_in", 1800)) - _TOKEN_SKEW_SECONDS
        return self._access_token

    def _session(self) -> VendorSession:
        token = self._oauth_token()
        if token:
            return VendorSession(self._host(), headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        # Fall back to HTTP Basic.
        return VendorSession(
            self._host(),
            auth=(self.secret("SERVICENOW_USERNAME") or "", self.secret("SERVICENOW_PASSWORD") or ""),
            headers={"Accept": "application/json"},
        )

    def _cost_table(self) -> Optional[str]:
        return self.secret("SERVICENOW_COST_TABLE")

    # -- table discovery --------------------------------------------------

    def discover_tables(self, prefix: str = "sn_") -> List[Dict[str, str]]:
        """List scoped-app tables whose name starts with `prefix`.

        Use this on the customer's instance to find the CCM cost table, then set
        SERVICENOW_COST_TABLE. Never raises -- returns [] on error.
        """
        if not self.configured:
            return []
        try:
            data = self._session().get_json(
                EP_SYS_DB_OBJECT,
                params={
                    "sysparm_query": f"nameSTARTSWITH{prefix}",
                    "sysparm_fields": "name,label,sys_scope",
                    "sysparm_limit": _PAGE,
                },
            )
        except Exception:
            return []
        return [
            {"name": first(r, "name", default=""), "label": first(r, "label", default="")}
            for r in data.get("result", []) or []
        ]

    # -- connection -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            # sys_user is present on every instance -- a cheap auth probe that
            # does not depend on the (unknown) cost table name.
            data = self._session().get_json(
                EP_TABLE.format(table="sys_user"), params={"sysparm_limit": 1, "sysparm_fields": "sys_id"}
            )
            configured_table = self._cost_table()
            return {
                "host": self._host(),
                "auth_ok": bool(data.get("result") is not None),
                "cost_table": configured_table or "(unset -- run discover_tables())",
            }

        return self._probe(call)

    # -- costs ------------------------------------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """Page the configured cost table and best-effort map to FOCUS.

        Returns an empty FOCUS frame when SERVICENOW_COST_TABLE is unset -- the
        table name cannot be guessed. Field names are instance-specific; the
        mapping below covers the common ITOM CCM field spellings and falls back
        gracefully.
        """
        table = self._cost_table()
        if not self.configured or not table:
            return self._empty_costs()
        session = self._session()
        rows: List[Dict[str, Any]] = []
        offset = 0
        try:
            for _ in range(1000):
                data = session.get_json(
                    EP_TABLE.format(table=table),
                    params={"sysparm_limit": _PAGE, "sysparm_offset": offset},
                )
                result = data.get("result", []) or []
                if not result:
                    break
                rows.extend(self._map_row(r) for r in result)
                if len(result) < _PAGE:
                    break
                offset += _PAGE
        except Exception:
            return self._empty_costs() if not rows else self._stamp(focus.normalize(pd.DataFrame(rows)))
        return self._stamp(focus.normalize(pd.DataFrame(rows))) if rows else self._empty_costs()

    @staticmethod
    def _map_row(r: Dict[str, Any]) -> Dict[str, Any]:
        amount = float(first(r, "cost", "amount", "u_cost", "total_cost", default=0.0) or 0.0)
        provider = str(first(r, "cloud_provider", "u_cloud_provider", "provider", default=""))
        return {
            "BillingAccountId": first(r, "account", "u_account_id", "account_id", default="servicenow"),
            "BillingAccountName": first(r, "account_name", "u_account_name", default="ServiceNow CCM"),
            "BillingCurrency": first(r, "currency", default="USD") or "USD",
            "InvoiceIssuerName": "ServiceNow",
            "ProviderName": provider.upper(),
            "ChargePeriodStart": first(r, "usage_date", "u_date", "date", default=None),
            "ChargeCategory": "Usage",
            "ChargeDescription": first(r, "service", "u_service", "short_description", default="Cloud usage"),
            "BilledCost": amount,
            "EffectiveCost": amount,
            "ListCost": amount,
            "ContractedCost": amount,
            "ServiceCategory": "Other",
            "ServiceName": first(r, "service", "u_service", default="Unknown"),
        }
