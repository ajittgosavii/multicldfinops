"""GCPNativeConnector -- native Google Cloud billing.

Google Cloud has no synchronous "give me my costs" API. The authoritative cost
surface is the **Cloud Billing export to BigQuery**: GCP streams detailed usage
rows into a dataset you own, and you query them with SQL. So this connector's
cost path is a BigQuery query, not a REST call.

Two flavours of export, in decreasing order of fidelity:
  * **FOCUS export** -- a Google-managed, immutable FOCUS table
    (``gcp_billing_export_focus_<ACCOUNT>``). When ``GCP_FOCUS_TABLE`` is set we
    query it directly; the columns are already FOCUS, so the map is near
    identity.
  * **Detailed usage export** -- ``gcp_billing_export_resource_v1_<ACCOUNT>``.
    Richer than the standard export (it has resource-level rows) but NOT FOCUS,
    so we map: ``cost -> BilledCost``; ``cost + SUM(credits.amount) ->
    EffectiveCost`` (credits are stored negative, so adding them nets the
    discount); ``service.description -> ServiceName``; ``project.id ->
    SubAccountId``; ``location.region -> RegionId``; ``labels -> Tags``.

Budgets and Recommender do have REST APIs; we call them with `requests` using a
Bearer token minted from the service-account credentials, so we do not pull the
individual Google client libraries for those two.

One sharp edge worth stating: Recommender's cost projection stores savings as a
NEGATIVE ``units`` value (a projected cost *reduction*). We take ``abs()``.

`google-cloud-bigquery` and `google.oauth2` are imported lazily. Importing this
module with nothing installed succeeds; `test_connection()` reports the missing
SDK. The service-account JSON is read via `self.secret(...)` and never logged.

API references:
  BigQuery export: https://cloud.google.com/billing/docs/how-to/export-data-bigquery-tables
  FOCUS export:    https://cloud.google.com/billing/docs/how-to/export-data-focus
  Budgets API:     https://cloud.google.com/billing/docs/reference/budget/rest/v1/billingAccounts.budgets/list
  Recommender API: https://cloud.google.com/recommender/docs/reference/rest/v1/projects.locations.recommenders.recommendations/list
"""

from __future__ import annotations

import json
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

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
BUDGETS_BASE = "https://billingbudgets.googleapis.com/v1"
RECOMMENDER_BASE = "https://recommender.googleapis.com/v1"

# EXACT recommender ids (verified). Each is a cost recommender we surface.
COST_RECOMMENDERS: List[str] = [
    "google.compute.instance.MachineTypeRecommender",
    "google.compute.instance.IdleResourceRecommender",
    "google.compute.disk.IdleResourceRecommender",
    "google.compute.address.IdleResourceRecommender",
    "google.compute.image.IdleResourceRecommender",
    "google.compute.commitment.UsageCommitmentRecommender",
    "google.cloudsql.instance.IdleRecommender",
    "google.cloudsql.instance.OverprovisionedRecommender",
]

_RECOMMENDER_LEVER = {
    "google.compute.instance.MachineTypeRecommender": "rightsizing",
    "google.compute.instance.IdleResourceRecommender": "idle_resource",
    "google.compute.disk.IdleResourceRecommender": "idle_resource",
    "google.compute.address.IdleResourceRecommender": "idle_resource",
    "google.compute.image.IdleResourceRecommender": "idle_resource",
    "google.compute.commitment.UsageCommitmentRecommender": "rate_optimization",
    "google.cloudsql.instance.IdleRecommender": "idle_resource",
    "google.cloudsql.instance.OverprovisionedRecommender": "rightsizing",
}

# GCP service.description -> FOCUS ServiceCategory (closed enum). Unmapped -> Other.
SERVICE_CATEGORY_MAP: Dict[str, str] = {
    "Compute Engine": "Compute",
    "Kubernetes Engine": "Compute",
    "Cloud Run": "Compute",
    "App Engine": "Compute",
    "Cloud Functions": "Compute",
    "Cloud Storage": "Storage",
    "Persistent Disk": "Storage",
    "Filestore": "Storage",
    "Cloud SQL": "Databases",
    "Cloud Spanner": "Databases",
    "Cloud Bigtable": "Databases",
    "Firestore": "Databases",
    "Memorystore": "Databases",
    "BigQuery": "Analytics",
    "Dataflow": "Analytics",
    "Dataproc": "Analytics",
    "Pub/Sub": "Analytics",
    "Cloud Composer": "Analytics",
    "Looker": "Analytics",
    "Vertex AI": "AI and Machine Learning",
    "Cloud Vision API": "AI and Machine Learning",
    "Cloud Natural Language": "AI and Machine Learning",
    "Networking": "Networking",
    "Cloud Load Balancing": "Networking",
    "Cloud CDN": "Networking",
    "Cloud DNS": "Networking",
    "Cloud NAT": "Networking",
    "Cloud Interconnect": "Networking",
    "Cloud Key Management Service (KMS)": "Security",
    "Security Command Center": "Security",
    "Cloud Armor": "Security",
    "Cloud Logging": "Management and Governance",
    "Cloud Monitoring": "Management and Governance",
    "Cloud Identity": "Identity",
    "API Gateway": "Integration",
    "Cloud Tasks": "Integration",
    "Eventarc": "Integration",
}


def _service_category(service_name: Optional[str]) -> str:
    if not service_name:
        return "Other"
    return SERVICE_CATEGORY_MAP.get(str(service_name), "Other")


class GCPNativeConnector(Connector):
    """Native GCP costs from the BigQuery billing export.

    Options:
      * ``location`` -- Recommender location (default 'global').
    Secrets: GCP_SERVICE_ACCOUNT_JSON, GCP_PROJECT_ID, GCP_BILLING_ACCOUNT_ID,
    optional GCP_BQ_DATASET and GCP_FOCUS_TABLE.
    """

    # ---- spec -----------------------------------------------------------

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="gcp_native",
            display_name="Google Cloud (native billing)",
            vendor="Google Cloud",
            clouds=["GCP"],
            auth=AuthKind.OAUTH2_CLIENT_CREDENTIALS,
            capabilities=[
                Capability.COSTS,
                Capability.RECOMMENDATIONS,
                Capability.BUDGETS,
                Capability.NATIVE_FOCUS,
            ],
            required_secrets=[
                "GCP_SERVICE_ACCOUNT_JSON",
                "GCP_PROJECT_ID",
                "GCP_BILLING_ACCOUNT_ID",
            ],
            optional_secrets=["GCP_BQ_DATASET", "GCP_FOCUS_TABLE"],
            base_url=RECOMMENDER_BASE,
            docs_url="https://cloud.google.com/billing/docs/how-to/export-data-bigquery-tables",
            focus_support="native",
            notes=(
                "Cost path is a BigQuery query over the billing export. Prefers "
                "the FOCUS export table (GCP_FOCUS_TABLE); otherwise maps the "
                "detailed export. Recommender savings are negative -> abs()."
            ),
        )

    # ---- credentials (lazy) ---------------------------------------------

    def _credentials(self):
        """Build service-account Credentials from the JSON secret.

        Lazy import; may raise ImportError (SDK absent) or ValueError (bad JSON).
        """
        from google.oauth2 import service_account  # noqa: WPS433

        raw = self.secret("GCP_SERVICE_ACCOUNT_JSON")
        info = json.loads(raw) if isinstance(raw, str) else raw
        return service_account.Credentials.from_service_account_info(
            info, scopes=[CLOUD_PLATFORM_SCOPE]
        )

    def _bearer_token(self) -> str:
        """Mint an OAuth2 access token for the REST (Budgets/Recommender) paths."""
        from google.auth.transport.requests import Request  # noqa: WPS433

        creds = self._credentials()
        creds.refresh(Request())
        return creds.token

    def _bq_client(self):
        from google.cloud import bigquery  # noqa: WPS433

        return bigquery.Client(
            project=self.secret("GCP_PROJECT_ID"), credentials=self._credentials()
        )

    def _table_suffix(self) -> str:
        # Billing-account id dashes become underscores in the table name.
        return str(self.secret("GCP_BILLING_ACCOUNT_ID") or "").replace("-", "_")

    # ---- contract: test -------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        try:
            from google.cloud import bigquery  # noqa: F401
            from google.oauth2 import service_account  # noqa: F401
        except ImportError:
            return ConnectionResult(
                ok=False,
                message="google-cloud-bigquery not installed; required for the GCP "
                "native connector. Install google-cloud-bigquery to enable it.",
            )
        missing = self.missing_secrets()
        if missing:
            return ConnectionResult(
                ok=False, message=f"Not configured. Missing secret(s): {', '.join(missing)}"
            )
        try:
            # Building credentials validates the JSON and the private key without
            # running a (billable) query.
            self._credentials()
        except Exception as exc:
            return ConnectionResult(
                ok=False,
                message=f"Service-account credentials invalid: {type(exc).__name__}.",
            )
        return ConnectionResult(
            ok=True,
            message=f"Service account loaded for project {self.secret('GCP_PROJECT_ID')}.",
        )

    # ---- contract: costs ------------------------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        try:
            if self.secret("GCP_FOCUS_TABLE"):
                return self._fetch_from_focus_table(start, end)
            return self._fetch_from_detailed_export(start, end)
        except ImportError:
            return focus.empty_frame()
        except Exception:
            return focus.empty_frame()

    def _dataset(self) -> str:
        return str(self.secret("GCP_BQ_DATASET") or "billing_export")

    def _fetch_from_focus_table(self, start: date, end: date) -> pd.DataFrame:
        client = self._bq_client()
        project = self.secret("GCP_PROJECT_ID")
        table = self.secret("GCP_FOCUS_TABLE")
        # The FOCUS export already conforms; select all and normalize.
        sql = f"""
            SELECT *
            FROM `{project}.{self._dataset()}.{table}`
            WHERE ChargePeriodStart >= @start AND ChargePeriodStart < @end
        """
        raw = self._run_query(client, sql, start, end)
        df = self._stamp(focus.normalize(raw))
        if "ProviderName" in df.columns and df["ProviderName"].isna().all():
            df["ProviderName"] = "GCP"
        return df

    def _fetch_from_detailed_export(self, start: date, end: date) -> pd.DataFrame:
        client = self._bq_client()
        project = self.secret("GCP_PROJECT_ID")
        table = f"gcp_billing_export_resource_v1_{self._table_suffix()}"
        # cost + SUM(credits.amount): credits.amount is negative, so the sum
        # nets the discount and yields amortized/effective cost.
        sql = f"""
            SELECT
              billing_account_id,
              service.description         AS service_description,
              sku.description             AS sku_description,
              sku.id                      AS sku_id,
              project.id                  AS project_id,
              project.name                AS project_name,
              location.region             AS region,
              location.location           AS location,
              usage_start_time,
              usage_end_time,
              invoice.month               AS invoice_month,
              currency,
              cost,
              usage.amount                AS usage_amount,
              usage.unit                  AS usage_unit,
              resource.name               AS resource_name,
              labels,
              (SELECT SUM(c.amount) FROM UNNEST(credits) c) AS credit_amount
            FROM `{project}.{self._dataset()}.{table}`
            WHERE usage_start_time >= @start AND usage_start_time < @end
        """
        raw = self._run_query(client, sql, start, end)
        if raw.empty:
            return focus.empty_frame()

        rows: List[dict] = []
        for _, r in raw.iterrows():
            cost = float(r.get("cost", 0) or 0)
            credit = float(r.get("credit_amount", 0) or 0)  # negative
            cp_start = pd.to_datetime(r.get("usage_start_time"), errors="coerce")
            cp_end = pd.to_datetime(r.get("usage_end_time"), errors="coerce")
            if pd.isna(cp_start):
                cp_start = pd.Timestamp.today().normalize()
            if pd.isna(cp_end):
                cp_end = cp_start + pd.Timedelta(days=1)
            bp_start = cp_start.to_period("M").to_timestamp()
            service = r.get("service_description")
            rows.append(
                {
                    "BillingAccountId": r.get("billing_account_id", "") or "unknown",
                    "BillingAccountName": r.get("billing_account_id", "") or "GCP Billing Account",
                    "BillingCurrency": r.get("currency", "USD") or "USD",
                    "BillingPeriodStart": bp_start,
                    "BillingPeriodEnd": bp_start + pd.DateOffset(months=1),
                    "InvoiceIssuerName": "Google Cloud",
                    "InvoiceId": str(r.get("invoice_month", "") or ""),
                    "SubAccountId": r.get("project_id", "") or "",
                    "SubAccountName": r.get("project_name", "") or "",
                    "SubAccountType": "Project",
                    "ChargeCategory": "Usage",
                    "ChargeDescription": r.get("sku_description", "") or f"{service} usage",
                    "ChargePeriodStart": cp_start,
                    "ChargePeriodEnd": cp_end,
                    "BilledCost": cost,
                    "EffectiveCost": cost + credit,  # credit is negative
                    "ListCost": cost,
                    "ContractedCost": cost + credit,
                    "ConsumedQuantity": float(r.get("usage_amount", 0) or 0),
                    "ConsumedUnit": r.get("usage_unit", "") or "",
                    "PricingQuantity": float(r.get("usage_amount", 0) or 0),
                    "PricingUnit": r.get("usage_unit", "") or "",
                    "ProviderName": "GCP",
                    "PublisherName": "Google Cloud",
                    "ServiceName": service or "Google Cloud",
                    "ServiceCategory": _service_category(service),
                    "RegionId": r.get("region", "") or "",
                    "SkuId": r.get("sku_id", "") or "",
                    "ResourceName": r.get("resource_name", "") or "",
                    "Tags": self._labels_to_dict(r.get("labels")),
                }
            )
        df = self._stamp(focus.normalize(pd.DataFrame(rows)))
        return df

    @staticmethod
    def _labels_to_dict(labels: Any) -> dict:
        """BigQuery returns labels as a repeated {key,value} struct."""
        out: Dict[str, str] = {}
        if isinstance(labels, (list, tuple)):
            for item in labels:
                if isinstance(item, dict) and "key" in item:
                    out[str(item["key"])] = str(item.get("value", ""))
        elif isinstance(labels, dict):
            out = {str(k): str(v) for k, v in labels.items()}
        return out

    def _run_query(self, client, sql: str, start: date, end: date) -> pd.DataFrame:
        from google.cloud import bigquery  # noqa: WPS433

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("start", "TIMESTAMP", pd.Timestamp(start).to_pydatetime()),
                bigquery.ScalarQueryParameter("end", "TIMESTAMP", pd.Timestamp(end).to_pydatetime()),
            ]
        )
        return client.query(sql, job_config=job_config).to_dataframe()

    # ---- contract: recommendations --------------------------------------

    def fetch_recommendations(self) -> List[Recommendation]:
        try:
            import requests  # noqa: F401
            from google.oauth2 import service_account  # noqa: F401
        except ImportError:
            return []
        project = self.secret("GCP_PROJECT_ID")
        if not project:
            return []
        location = str(self.options.get("location", "global"))
        try:
            token = self._bearer_token()
        except Exception:
            return []

        import requests  # noqa: WPS433

        out: List[Recommendation] = []
        headers = {"Authorization": f"Bearer {token}"}
        for recommender in COST_RECOMMENDERS:
            url = (
                f"{RECOMMENDER_BASE}/projects/{project}/locations/{location}"
                f"/recommenders/{recommender}/recommendations"
            )
            page_token: Optional[str] = None
            while True:
                try:
                    params = {"pageToken": page_token} if page_token else {}
                    resp = requests.get(url, headers=headers, params=params, timeout=60)
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                except Exception:
                    break
                for rec in data.get("recommendations", []):
                    primary = rec.get("primaryImpact", {})
                    cost_proj = primary.get("costProjection", {})
                    cost = cost_proj.get("cost", {})
                    units = float(cost.get("units", 0) or 0)
                    nanos = float(cost.get("nanos", 0) or 0) / 1e9
                    # NEGATIVE units means projected savings -> take magnitude.
                    monthly = abs(units + nanos)
                    out.append(
                        Recommendation(
                            source="gcp_recommender",
                            cloud="GCP",
                            resource_id=rec.get("name", ""),
                            resource_type=recommender.split(".")[-1],
                            lever=_RECOMMENDER_LEVER.get(recommender, "other"),
                            action=rec.get("recommenderSubtype", "Optimize"),
                            estimated_monthly_savings=monthly,
                            currency=str(cost.get("currencyCode", "USD") or "USD"),
                            risk=str(rec.get("priority", "P3")),
                            confidence=0.75,
                            detail={
                                "state": rec.get("stateInfo", {}).get("state", ""),
                                "recommender": recommender,
                            },
                        )
                    )
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        return out

    # ---- contract: budgets ----------------------------------------------

    def fetch_budgets(self) -> pd.DataFrame:
        empty = pd.DataFrame(columns=["period", "cloud", "application", "budget"])
        try:
            import requests  # noqa: F401
            from google.oauth2 import service_account  # noqa: F401
        except ImportError:
            return empty
        billing_account = self.secret("GCP_BILLING_ACCOUNT_ID")
        if not billing_account:
            return empty
        try:
            token = self._bearer_token()
        except Exception:
            return empty

        import requests  # noqa: WPS433

        rows: List[dict] = []
        period = pd.Timestamp.today().to_period("M").to_timestamp()
        url = f"{BUDGETS_BASE}/billingAccounts/{billing_account}/budgets"
        headers = {"Authorization": f"Bearer {token}"}
        page_token: Optional[str] = None
        try:
            while True:
                params = {"pageToken": page_token} if page_token else {}
                resp = requests.get(url, headers=headers, params=params, timeout=60)
                if resp.status_code != 200:
                    break
                data = resp.json()
                for b in data.get("budgets", []):
                    amount = b.get("amount", {}).get("specifiedAmount", {})
                    units = float(amount.get("units", 0) or 0)
                    nanos = float(amount.get("nanos", 0) or 0) / 1e9
                    rows.append(
                        {
                            "period": period,
                            "cloud": "GCP",
                            "application": b.get("displayName", ""),
                            "budget": units + nanos,
                        }
                    )
                page_token = data.get("nextPageToken")
                if not page_token:
                    break
        except Exception:
            return pd.DataFrame(rows, columns=["period", "cloud", "application", "budget"]) if rows else empty
        return pd.DataFrame(rows, columns=["period", "cloud", "application", "budget"]) if rows else empty
