"""AzureNativeConnector -- native Azure billing over the ARM REST API.

We deliberately talk to Azure Resource Manager with plain `requests` rather
than the `azure-mgmt-costmanagement` SDK. The SDK pulls a large dependency tree
(azure-core, azure-identity, msal, isodate ...) for what is, in the end, a
handful of POSTs and GETs. `requests` is already a platform dependency, so this
connector adds nothing to the install and keeps Demo Mode lean.

Auth is Entra ID (Azure AD) OAuth2 client-credentials: POST client_id +
client_secret to the tenant token endpoint with scope
``https://management.azure.com/.default`` and receive a Bearer token.

Two cost paths:
  * **Cost Management query** -- POST the Query API with an AmortizedCost
    dataset. This is the live, synchronous path. Azure returns one cost figure
    per grouped row; unlike a full FOCUS export it does not separate billed
    from amortized from list. We requested AmortizedCost, so that value maps to
    EffectiveCost, and we set BilledCost/ListCost/ContractedCost equal to it.
    That is a documented flattening -- ESR from this path is understated.
  * **FOCUS export** -- Azure Cost Management can be configured to write a
    ``FocusCost`` dataset to a storage account. When one exists, reading that
    Parquet/CSV is authoritative and lossless. Enable it by pointing the
    ``focus_export_uri`` option at the container path (lazy adlfs).

Throttling: the Query API is aggressive with HTTP 429 and supplies a
``Retry-After`` header; we honour it with bounded backoff.

Nothing here logs the client secret or the Bearer token.

API references:
  Token:     https://learn.microsoft.com/entra/identity-platform/v2-oauth2-client-creds-grant-flow
  Query:     https://learn.microsoft.com/rest/api/cost-management/query/usage
  Forecast:  https://learn.microsoft.com/rest/api/cost-management/forecast/usage
  Exports:   https://learn.microsoft.com/rest/api/cost-management/exports
  Budgets:   https://learn.microsoft.com/rest/api/consumption/budgets
  Advisor:   https://learn.microsoft.com/rest/api/advisor/recommendations
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
    Connector,
    ConnectorSpec,
    Recommendation,
)

ARM_BASE = "https://management.azure.com"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
ARM_SCOPE = "https://management.azure.com/.default"

# API versions, pinned so a preview bump cannot silently change response shape.
API_QUERY = "2025-03-01"  # CostManagement query & forecast
API_EXPORTS = "2023-08-01"
API_BUDGETS = "2024-08-01"  # Microsoft.Consumption budgets
API_ADVISOR = "2025-01-01"
API_RESERVATION_RECS = "2024-08-01"
API_BENEFIT_RECS = "2025-03-01"

_HTTP_TIMEOUT = 60
_MAX_RETRIES = 5

# Azure "meter/service" -> FOCUS ServiceCategory (closed enum). Azure's
# ServiceName dimension is coarse; unmapped values fall to 'Other'.
SERVICE_CATEGORY_MAP: Dict[str, str] = {
    "Virtual Machines": "Compute",
    "Virtual Machines Licenses": "Compute",
    "Azure Kubernetes Service": "Compute",
    "Azure App Service": "Compute",
    "Functions": "Compute",
    "Azure Container Instances": "Compute",
    "Cloud Services": "Compute",
    "Storage": "Storage",
    "Azure Backup": "Storage",
    "Azure NetApp Files": "Storage",
    "Bandwidth": "Networking",
    "Virtual Network": "Networking",
    "Azure Front Door Service": "Networking",
    "Load Balancer": "Networking",
    "Application Gateway": "Networking",
    "VPN Gateway": "Networking",
    "Azure DNS": "Networking",
    "Content Delivery Network": "Networking",
    "ExpressRoute": "Networking",
    "SQL Database": "Databases",
    "Azure Database for PostgreSQL": "Databases",
    "Azure Database for MySQL": "Databases",
    "Azure Cosmos DB": "Databases",
    "Azure Cache for Redis": "Databases",
    "SQL Managed Instance": "Databases",
    "Azure Synapse Analytics": "Analytics",
    "Azure Databricks": "Analytics",
    "Data Factory": "Analytics",
    "Event Hubs": "Analytics",
    "HDInsight": "Analytics",
    "Azure Machine Learning": "AI and Machine Learning",
    "Cognitive Services": "AI and Machine Learning",
    "Azure OpenAI": "AI and Machine Learning",
    "Azure Monitor": "Management and Governance",
    "Log Analytics": "Management and Governance",
    "Automation": "Management and Governance",
    "Microsoft Defender for Cloud": "Security",
    "Azure Firewall": "Security",
    "Key Vault": "Security",
    "Azure Active Directory": "Identity",
    "Microsoft Entra ID": "Identity",
    "API Management": "Integration",
    "Service Bus": "Integration",
    "Logic Apps": "Integration",
    "Event Grid": "Integration",
}


def _service_category(service_name: Optional[str]) -> str:
    if not service_name:
        return "Other"
    return SERVICE_CATEGORY_MAP.get(str(service_name), "Other")


class AzureNativeConnector(Connector):
    """Native Azure costs via the Cost Management REST API.

    Options:
      * ``scope`` -- ARM scope override. Defaults to
        ``subscriptions/{AZURE_SUBSCRIPTION_ID}`` or, if only a billing account
        is configured, ``providers/Microsoft.Billing/billingAccounts/{id}``.
      * ``granularity`` -- 'Daily' | 'Monthly' (default 'Daily').
      * ``focus_export_uri`` -- container path of a configured FocusCost export
        to read instead of the Query API (lazy adlfs).
    """

    # ---- spec -----------------------------------------------------------

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="azure_native",
            display_name="Azure (native billing)",
            vendor="Microsoft",
            clouds=["Azure"],
            auth=AuthKind.OAUTH2_CLIENT_CREDENTIALS,
            capabilities=[
                Capability.COSTS,
                Capability.RECOMMENDATIONS,
                Capability.BUDGETS,
                Capability.FORECAST,
                Capability.NATIVE_FOCUS,
            ],
            required_secrets=[
                "AZURE_TENANT_ID",
                "AZURE_CLIENT_ID",
                "AZURE_CLIENT_SECRET",
            ],
            optional_secrets=["AZURE_SUBSCRIPTION_ID", "AZURE_BILLING_ACCOUNT_ID"],
            base_url=ARM_BASE,
            docs_url="https://learn.microsoft.com/rest/api/cost-management/query/usage",
            focus_support="map",
            notes=(
                "Talks ARM REST with requests (no azure-mgmt SDK). Query API "
                "returns amortized cost only -> BilledCost/ListCost flattened to "
                "EffectiveCost, ESR understated. Configure a FocusCost export "
                "for lossless data."
            ),
        )

    # ---- scope / auth ---------------------------------------------------

    def _scope(self) -> Optional[str]:
        if self.options.get("scope"):
            return str(self.options["scope"])
        sub = self.secret("AZURE_SUBSCRIPTION_ID")
        if sub:
            return f"subscriptions/{sub}"
        ba = self.secret("AZURE_BILLING_ACCOUNT_ID")
        if ba:
            return f"providers/Microsoft.Billing/billingAccounts/{ba}"
        return None

    def _get_token(self) -> str:
        """Fetch a Bearer token. Lazy import; may raise on bad credentials."""
        import requests  # noqa: WPS433

        tenant = self.secret("AZURE_TENANT_ID")
        resp = requests.post(
            TOKEN_URL.format(tenant=tenant),
            data={
                "grant_type": "client_credentials",
                "client_id": self.secret("AZURE_CLIENT_ID"),
                "client_secret": self.secret("AZURE_CLIENT_SECRET"),
                "scope": ARM_SCOPE,
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    def _request(self, method: str, url: str, token: str, **kwargs):
        """Issue an ARM request, honouring 429 Retry-After with backoff."""
        import requests  # noqa: WPS433

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"
        for attempt in range(_MAX_RETRIES):
            resp = requests.request(
                method, url, headers=headers, timeout=_HTTP_TIMEOUT, **kwargs
            )
            if resp.status_code == 429:
                # Cost Management is rate-limited; the header tells us how long.
                wait = float(resp.headers.get("Retry-After", 2 ** attempt))
                time.sleep(min(wait, 60))
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    # ---- contract: test -------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        try:
            import requests  # noqa: F401
        except ImportError:
            return ConnectionResult(
                ok=False,
                message="requests not installed; required for the Azure native connector.",
            )
        missing = self.missing_secrets()
        if missing:
            return ConnectionResult(
                ok=False, message=f"Not configured. Missing secret(s): {', '.join(missing)}"
            )
        if not self._scope():
            return ConnectionResult(
                ok=False,
                message="No scope: set AZURE_SUBSCRIPTION_ID or AZURE_BILLING_ACCOUNT_ID.",
            )
        try:
            self._get_token()
        except Exception as exc:
            return ConnectionResult(
                ok=False,
                message=f"Entra token request failed: {type(exc).__name__}. "
                "Check tenant, client id and secret.",
            )
        return ConnectionResult(
            ok=True, message=f"Authenticated to Entra tenant; scope {self._scope()}."
        )

    # ---- contract: costs ------------------------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        if self.options.get("focus_export_uri"):
            try:
                return self._fetch_from_focus_export(start, end)
            except Exception:
                return focus.empty_frame()
        try:
            return self._fetch_from_query(start, end)
        except Exception:
            return focus.empty_frame()

    def _fetch_from_focus_export(self, start: date, end: date) -> pd.DataFrame:
        uri = self.options["focus_export_uri"]
        try:
            import adlfs  # noqa: F401
        except ImportError:
            return focus.empty_frame()
        raw = pd.read_parquet(uri) if str(uri).endswith((".parquet", ".pq")) else pd.read_csv(uri)
        df = self._stamp(focus.normalize(raw))
        if "ProviderName" in df.columns and df["ProviderName"].isna().all():
            df["ProviderName"] = "Azure"
        return df

    def _fetch_from_query(self, start: date, end: date) -> pd.DataFrame:
        scope = self._scope()
        if not scope:
            return focus.empty_frame()
        token = self._get_token()
        url = f"{ARM_BASE}/{scope}/providers/Microsoft.CostManagement/query?api-version={API_QUERY}"
        granularity = str(self.options.get("granularity", "Daily"))
        body: Dict[str, Any] = {
            "type": "AmortizedCost",  # -> EffectiveCost
            "timeframe": "Custom",
            "timePeriod": {"from": start.isoformat(), "to": end.isoformat()},
            "dataset": {
                "granularity": granularity,
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [
                    {"type": "Dimension", "name": "ResourceGroup"},
                    {"type": "Dimension", "name": "ServiceName"},
                ],
            },
        }

        rows: List[dict] = []
        sub = self.secret("AZURE_SUBSCRIPTION_ID") or ""
        while True:
            resp = self._request("POST", url, token, json=body)
            payload = resp.json().get("properties", {})
            columns = [c.get("name") for c in payload.get("columns", [])]
            idx = {name: i for i, name in enumerate(columns)}
            for row in payload.get("rows", []):
                rows.append(self._map_query_row(row, idx, sub, granularity))
            next_link = payload.get("nextLink")
            if not next_link:
                break
            # nextLink is an absolute URL with the same api-version.
            url, body = next_link, None  # subsequent pages are GET-with-token
            resp = self._request("GET", url, token)
            payload = resp.json().get("properties", {})
            columns = [c.get("name") for c in payload.get("columns", [])]
            idx = {name: i for i, name in enumerate(columns)}
            for row in payload.get("rows", []):
                rows.append(self._map_query_row(row, idx, sub, granularity))
            next_link = payload.get("nextLink")
            if not next_link:
                break

        if not rows:
            return focus.empty_frame()
        df = self._stamp(focus.normalize(pd.DataFrame(rows)))
        df.attrs["data_quality"] = (
            "Sourced from Azure Cost Management query (AmortizedCost only): "
            "BilledCost/ListCost flattened to EffectiveCost; ESR understated."
        )
        return df

    def _map_query_row(
        self, row: list, idx: Dict[str, int], sub: str, granularity: str
    ) -> dict:
        """Map one Query API row to a FOCUS record.

        The Query API returns a date as an int YYYYMMDD when granularity is
        Daily, and omits the date column when Monthly. Column presence varies,
        so we look everything up by name.
        """

        def cell(*names, default=None):
            for n in names:
                if n in idx and idx[n] < len(row):
                    return row[idx[n]]
            return default

        cost = float(cell("Cost", "PreTaxCost", default=0) or 0)
        currency = str(cell("Currency", "BillingCurrency", default="USD") or "USD")
        service = cell("ServiceName", default=None)
        resource_group = cell("ResourceGroup", default="")

        raw_date = cell("UsageDate", "BillingMonth", default=None)
        if raw_date is not None and str(raw_date).isdigit() and len(str(int(raw_date))) == 8:
            cp_start = pd.to_datetime(str(int(raw_date)), format="%Y%m%d")
        elif raw_date is not None:
            cp_start = pd.to_datetime(raw_date, errors="coerce")
        else:
            cp_start = pd.NaT
        if pd.isna(cp_start):
            cp_start = pd.Timestamp.today().normalize()
        cp_end = cp_start + (pd.Timedelta(days=1) if granularity.lower() == "daily" else pd.DateOffset(months=1))
        bp_start = cp_start.to_period("M").to_timestamp()

        return {
            "BillingAccountId": sub or "unknown",
            "BillingAccountName": f"Azure Subscription {sub}" if sub else "Azure Billing Account",
            "BillingCurrency": currency,
            "BillingPeriodStart": bp_start,
            "BillingPeriodEnd": bp_start + pd.DateOffset(months=1),
            "InvoiceIssuerName": "Microsoft",
            "SubAccountId": sub,
            "SubAccountName": f"Subscription {sub}" if sub else "",
            "SubAccountType": "Subscription",
            "ChargeCategory": "Usage",
            "ChargeDescription": f"{service or 'Azure'} usage in {resource_group}".strip(),
            "ChargePeriodStart": cp_start,
            "ChargePeriodEnd": cp_end,
            "BilledCost": cost,
            "EffectiveCost": cost,
            "ListCost": cost,
            "ContractedCost": cost,
            "ProviderName": "Azure",
            "PublisherName": "Microsoft",
            "ServiceName": service or "Azure",
            "ServiceCategory": _service_category(service),
            "Tags": {"resource_group": resource_group} if resource_group else {},
        }

    # ---- contract: recommendations --------------------------------------

    def fetch_recommendations(self) -> List[Recommendation]:
        """Azure Advisor cost recommendations (category eq 'Cost')."""
        try:
            import requests  # noqa: F401
        except ImportError:
            return []
        scope = self._scope()
        if not scope:
            return []
        try:
            token = self._get_token()
            url = (
                f"{ARM_BASE}/{scope}/providers/Microsoft.Advisor/recommendations"
                f"?api-version={API_ADVISOR}&$filter=category eq 'Cost'"
            )
            out: List[Recommendation] = []
            while url:
                resp = self._request("GET", url, token)
                data = resp.json()
                for item in data.get("value", []):
                    props = item.get("properties", {})
                    ext = props.get("extendedProperties", {}) or {}
                    monthly = float(ext.get("savingsAmount", 0) or 0)
                    annual = float(ext.get("annualSavingsAmount", 0) or 0)
                    if not monthly and annual:
                        monthly = annual / 12.0
                    out.append(
                        Recommendation(
                            source="azure_advisor",
                            cloud="Azure",
                            resource_id=props.get("resourceMetadata", {}).get("resourceId", ""),
                            resource_type=props.get("impactedField", ""),
                            lever="rate_optimization"
                            if "reserv" in str(props.get("shortDescription", {})).lower()
                            else "rightsizing",
                            action=props.get("shortDescription", {}).get("solution", "Optimize"),
                            estimated_monthly_savings=monthly,
                            currency=str(ext.get("savingsCurrency", "USD") or "USD"),
                            risk=str(props.get("impact", "Medium")),
                            confidence=0.75,
                            detail={"category": props.get("category", "Cost")},
                        )
                    )
                url = data.get("nextLink")
            return out
        except Exception:
            return []

    # ---- contract: budgets ----------------------------------------------

    def fetch_budgets(self) -> pd.DataFrame:
        empty = pd.DataFrame(columns=["period", "cloud", "application", "budget"])
        try:
            import requests  # noqa: F401
        except ImportError:
            return empty
        scope = self._scope()
        if not scope:
            return empty
        try:
            token = self._get_token()
            url = (
                f"{ARM_BASE}/{scope}/providers/Microsoft.Consumption/budgets"
                f"?api-version={API_BUDGETS}"
            )
            rows: List[dict] = []
            period = pd.Timestamp.today().to_period("M").to_timestamp()
            while url:
                resp = self._request("GET", url, token)
                data = resp.json()
                for item in data.get("value", []):
                    props = item.get("properties", {})
                    rows.append(
                        {
                            "period": period,
                            "cloud": "Azure",
                            "application": item.get("name", ""),
                            "budget": float(props.get("amount", 0) or 0),
                        }
                    )
                url = data.get("nextLink")
            return pd.DataFrame(rows, columns=["period", "cloud", "application", "budget"]) if rows else empty
        except Exception:
            return empty
