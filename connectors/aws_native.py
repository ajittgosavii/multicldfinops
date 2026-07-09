"""AWSNativeConnector -- native AWS billing, two ingest paths.

AWS exposes cost data two ways, and they are not equivalent:

1. **Data Exports (FOCUS)** -- the modern, authoritative path. AWS writes a
   FOCUS 1.0/1.2 dataset to S3 as Parquet/CSV on a schedule. The columns are
   already FOCUS, so ingest is near-identity: read the objects, drop the
   AWS-specific ``x_*`` extension columns, coerce dtypes, done. This is the
   path to prefer for any production estate -- it is complete, has list price,
   commitment amortization and resource-level granularity, and costs nothing
   per query (you pay only S3 storage).

2. **Cost Explorer (CE)** -- a synchronous API, convenient for a live probe or
   a small estate, but **lossy and metered**. Two limitations we do NOT paper
   over:
     * Each `get_cost_and_usage` request costs ~$0.01. We paginate, so a wide
       date range is real money. The Data Exports path is free by comparison.
     * CE returns no list/public price. We therefore set ``ListCost =
       BilledCost`` and record a `data_quality` note. Any Effective Savings
       Rate computed from a CE-sourced frame is UNDERSTATED, because ESR is
       (ListCost - EffectiveCost) / ListCost and we have flattened list to
       billed. Do not present ESR from this path as authoritative.

Recommendations come from Cost Optimization Hub (the unified surface that rolls
up Compute Optimizer, Trusted Advisor and rightsizing), with a CE rightsizing
fallback. Budgets come from the AWS Budgets API.

All boto3 imports are lazy: importing this module with no SDK installed
succeeds, and `test_connection()` reports the missing dependency rather than
raising. Credentials are read via `self.secret(...)`, falling back to the
default boto3 credential chain (instance role, SSO, env) when explicit keys are
absent. Nothing here ever logs a credential.

API references:
  Data Exports:  https://docs.aws.amazon.com/cur/latest/userguide/what-is-data-exports.html
  Cost Explorer: https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_Operations_AWS_Cost_Explorer_Service.html
  Cost Opt Hub:  https://docs.aws.amazon.com/cost-management/latest/APIReference/API_Operations_AWS_Cost_Optimization_Hub.html
  Budgets:       https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_Operations_AWS_Budgets.html
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
    Connector,
    ConnectorSpec,
    Recommendation,
)

# CE and Budgets are global services that must be SIGNED in us-east-1 regardless
# of the caller's default region.
_GLOBAL_SIGNING_REGION = "us-east-1"

# FOCUS table ids AWS assigns in bcm-data-exports (documented enum). Retained
# for reference / export creation; the reader itself just reads whatever
# Parquet lands in the configured S3 prefix.
DATA_EXPORT_TABLES = {
    "focus_1_0": "FOCUS_1_0_AWS",
    "focus_1_2": "FOCUS_1_2_AWS",
    "cur2": "COST_AND_USAGE_REPORT",
}

# ServiceName -> FOCUS ServiceCategory. FOCUS mandates ServiceCategory from a
# closed enum (see focus.SERVICE_CATEGORY); CE gives us only the service name,
# so we map. Anything unmapped falls to 'Other' -- honest, not invented.
SERVICE_CATEGORY_MAP: Dict[str, str] = {
    "Amazon Elastic Compute Cloud - Compute": "Compute",
    "Amazon EC2 - Other": "Compute",
    "Amazon Elastic Compute Cloud": "Compute",
    "EC2 - Other": "Compute",
    "AWS Lambda": "Compute",
    "Amazon Elastic Container Service": "Compute",
    "Amazon Elastic Kubernetes Service": "Compute",
    "AWS Fargate": "Compute",
    "Amazon Lightsail": "Compute",
    "Amazon Simple Storage Service": "Storage",
    "Amazon Elastic Block Store": "Storage",
    "Amazon Elastic File System": "Storage",
    "Amazon FSx": "Storage",
    "AWS Backup": "Storage",
    "Amazon Simple Storage Service Glacier": "Storage",
    "Amazon Relational Database Service": "Databases",
    "Amazon Aurora": "Databases",
    "Amazon DynamoDB": "Databases",
    "Amazon ElastiCache": "Databases",
    "Amazon Redshift": "Analytics",
    "Amazon Neptune": "Databases",
    "Amazon DocumentDB (with MongoDB compatibility)": "Databases",
    "Amazon Athena": "Analytics",
    "AWS Glue": "Analytics",
    "Amazon EMR": "Analytics",
    "Amazon Kinesis": "Analytics",
    "Amazon OpenSearch Service": "Analytics",
    "Amazon QuickSight": "Analytics",
    "Amazon Managed Streaming for Apache Kafka": "Analytics",
    "Amazon Virtual Private Cloud": "Networking",
    "Amazon CloudFront": "Networking",
    "Elastic Load Balancing": "Networking",
    "Amazon Route 53": "Networking",
    "AWS Direct Connect": "Networking",
    "Amazon API Gateway": "Integration",
    "Amazon Simple Queue Service": "Integration",
    "Amazon Simple Notification Service": "Integration",
    "Amazon EventBridge": "Integration",
    "AWS Step Functions": "Integration",
    "Amazon SageMaker": "AI and Machine Learning",
    "Amazon Bedrock": "AI and Machine Learning",
    "Amazon Comprehend": "AI and Machine Learning",
    "Amazon Rekognition": "AI and Machine Learning",
    "AWS WAF": "Security",
    "Amazon GuardDuty": "Security",
    "AWS Key Management Service": "Security",
    "AWS Secrets Manager": "Security",
    "Amazon Inspector": "Security",
    "AWS Security Hub": "Security",
    "AWS Identity and Access Management": "Identity",
    "AWS IAM Identity Center": "Identity",
    "Amazon Cognito": "Identity",
    "AmazonCloudWatch": "Management and Governance",
    "AWS CloudTrail": "Management and Governance",
    "AWS Config": "Management and Governance",
    "AWS Systems Manager": "Management and Governance",
    "AWS Cost Explorer": "Management and Governance",
    "Amazon Connect": "Business Applications",
    "Amazon WorkSpaces": "Compute",
    "AWS Database Migration Service": "Migration",
    "AWS Migration Hub": "Migration",
}

# Cost Optimization Hub actionType -> our normalized lever vocabulary.
_HUB_ACTION_TO_LEVER = {
    "Rightsize": "rightsizing",
    "Stop": "idle_resource",
    "Upgrade": "modernization",
    "MigrateToGraviton": "modernization",
    "PurchaseSavingsPlans": "rate_optimization",
    "PurchaseReservedInstances": "rate_optimization",
    "UpgradeLambdaConfiguration": "rightsizing",
    "DeleteUnusedEbsVolume": "idle_resource",
}


def _service_category(service_name: Optional[str]) -> str:
    if not service_name:
        return "Other"
    return SERVICE_CATEGORY_MAP.get(str(service_name), "Other")


class AWSNativeConnector(Connector):
    """Native AWS costs via Data Exports (preferred) or Cost Explorer.

    Options:
      * ``source`` -- 'data_exports' | 'cost_explorer'. Defaults to
        'data_exports' when an AWS_FOCUS_S3_URI is present, else 'cost_explorer'.
      * ``granularity`` -- 'DAILY' | 'MONTHLY' for the Cost Explorer path.
    """

    # ---- spec -----------------------------------------------------------

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="aws_native",
            display_name="AWS (native billing)",
            vendor="Amazon Web Services",
            clouds=["AWS"],
            auth=AuthKind.SIGV4,
            capabilities=[
                Capability.COSTS,
                Capability.RECOMMENDATIONS,
                Capability.BUDGETS,
                Capability.FORECAST,
                Capability.ANOMALIES,
                Capability.NATIVE_FOCUS,
            ],
            required_secrets=[],  # default credential chain is valid config
            optional_secrets=[
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_REGION",
                "AWS_FOCUS_S3_URI",
            ],
            base_url="https://ce.us-east-1.amazonaws.com",
            docs_url="https://docs.aws.amazon.com/cur/latest/userguide/what-is-data-exports.html",
            focus_support="native",
            notes=(
                "Prefers Data Exports (FOCUS 1.0/1.2 Parquet in S3, free, "
                "complete). Cost Explorer fallback is metered (~$0.01/request) "
                "and has no list price -> ListCost=BilledCost, ESR understated."
            ),
        )

    # ---- session / clients (lazy) ---------------------------------------

    def _source(self) -> str:
        explicit = self.options.get("source")
        if explicit:
            return str(explicit)
        return "data_exports" if self.secret("AWS_FOCUS_S3_URI") else "cost_explorer"

    def _session(self):
        """Build a boto3 Session. Lazy import; may raise ImportError."""
        import boto3  # noqa: WPS433 (lazy on purpose)

        key = self.secret("AWS_ACCESS_KEY_ID")
        secret = self.secret("AWS_SECRET_ACCESS_KEY")
        token = self.secret("AWS_SESSION_TOKEN")
        region = self.secret("AWS_REGION") or _GLOBAL_SIGNING_REGION
        if key and secret:
            return boto3.Session(
                aws_access_key_id=key,
                aws_secret_access_key=secret,
                aws_session_token=token,
                region_name=region,
            )
        # No explicit keys -> default chain (instance role, SSO, env). This is
        # the recommended production posture; absence of keys is not an error.
        return boto3.Session(region_name=region)

    # ---- contract: test -------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        try:
            import boto3  # noqa: F401
        except ImportError:
            return ConnectionResult(
                ok=False,
                message="boto3 not installed; required for the AWS native connector. "
                "Install boto3 to enable it.",
            )
        try:
            session = self._session()
            sts = session.client("sts", region_name=_GLOBAL_SIGNING_REGION)
            ident = sts.get_caller_identity()
        except Exception as exc:  # credential/network failure -> not-ok, no raise
            return ConnectionResult(
                ok=False,
                message=f"AWS credentials not usable: {type(exc).__name__}. "
                "Provide keys or a working default credential chain.",
            )
        return ConnectionResult(
            ok=True,
            message=f"Authenticated as account {ident.get('Account', 'unknown')} "
            f"via {self._source()}.",
            detail={"account": ident.get("Account", ""), "source": self._source()},
        )

    # ---- contract: costs ------------------------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        try:
            if self._source() == "data_exports":
                return self._fetch_from_data_exports(start, end)
            return self._fetch_from_cost_explorer(start, end)
        except ImportError:
            # SDK / s3fs absent: an empty conformant frame, never a crash.
            return focus.empty_frame()

    def _fetch_from_data_exports(self, start: date, end: date) -> pd.DataFrame:
        uri = self.secret("AWS_FOCUS_S3_URI")
        if not uri:
            return focus.empty_frame()
        # Lazy: s3fs is only needed for this path. pandas reads the partitioned
        # Parquet dataset directly from the prefix.
        try:
            import s3fs  # noqa: F401
        except ImportError:
            return focus.empty_frame()

        raw = pd.read_parquet(uri)
        # Drop AWS FOCUS extension columns (x_CostCategories, x_Operation, ...).
        # They are provider-specific and not part of canonical FOCUS.
        raw = raw[[c for c in raw.columns if not str(c).lower().startswith("x_")]]

        df = self._stamp(focus.normalize(raw))
        if "ProviderName" in df.columns and df["ProviderName"].isna().all():
            df["ProviderName"] = "AWS"
        if "ChargePeriodStart" in df.columns:
            mask = (df["ChargePeriodStart"] >= pd.Timestamp(start)) & (
                df["ChargePeriodStart"] < pd.Timestamp(end)
            )
            df = df[mask.fillna(False)]
        return df

    def _fetch_from_cost_explorer(self, start: date, end: date) -> pd.DataFrame:
        session = self._session()
        # CE is global; the client must be created in (and thus sign for)
        # us-east-1. https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_GetCostAndUsage.html
        ce = session.client("ce", region_name=_GLOBAL_SIGNING_REGION)

        granularity = str(self.options.get("granularity", "DAILY")).upper()
        try:
            account_id = session.client(
                "sts", region_name=_GLOBAL_SIGNING_REGION
            ).get_caller_identity().get("Account", "")
        except Exception:
            account_id = ""

        request: Dict[str, Any] = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": granularity,
            "Metrics": ["AmortizedCost", "UnblendedCost", "UsageQuantity"],
            # CE allows a MAXIMUM of two GroupBy entries.
            "GroupBy": [
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
            ],
        }

        rows: List[dict] = []
        next_token: Optional[str] = None
        while True:
            if next_token:
                request["NextPageToken"] = next_token
            resp = ce.get_cost_and_usage(**request)
            for period in resp.get("ResultsByTime", []):
                tp = period.get("TimePeriod", {})
                cp_start = pd.Timestamp(tp.get("Start"))
                cp_end = pd.Timestamp(tp.get("End"))
                bp_start = cp_start.to_period("M").to_timestamp()
                bp_end = bp_start + pd.DateOffset(months=1)
                for group in period.get("Groups", []):
                    keys = group.get("Keys", [])
                    service = keys[0] if len(keys) > 0 else "Unknown"
                    linked = keys[1] if len(keys) > 1 else account_id
                    metrics = group.get("Metrics", {})
                    amort = float(metrics.get("AmortizedCost", {}).get("Amount", 0) or 0)
                    unblended = float(metrics.get("UnblendedCost", {}).get("Amount", 0) or 0)
                    qty = float(metrics.get("UsageQuantity", {}).get("Amount", 0) or 0)
                    currency = (
                        metrics.get("AmortizedCost", {}).get("Unit")
                        or metrics.get("UnblendedCost", {}).get("Unit")
                        or "USD"
                    )
                    rows.append(
                        {
                            "BillingAccountId": account_id or linked,
                            "BillingAccountName": f"AWS Account {account_id or linked}",
                            "BillingCurrency": currency,
                            "BillingPeriodStart": bp_start,
                            "BillingPeriodEnd": bp_end,
                            "InvoiceIssuerName": "Amazon Web Services",
                            "SubAccountId": linked,
                            "SubAccountName": f"Account {linked}",
                            "SubAccountType": "Account",
                            "ChargeCategory": "Usage",
                            "ChargeDescription": f"{service} usage",
                            "ChargePeriodStart": cp_start,
                            "ChargePeriodEnd": cp_end,
                            # AmortizedCost -> EffectiveCost; UnblendedCost ->
                            # BilledCost. CE has NO list price: ListCost is set
                            # to BilledCost and ESR from this frame is understated.
                            "BilledCost": unblended,
                            "EffectiveCost": amort,
                            "ListCost": unblended,
                            "ContractedCost": amort,
                            "PricingQuantity": qty,
                            "ConsumedQuantity": qty,
                            "ProviderName": "AWS",
                            "PublisherName": "Amazon Web Services",
                            "ServiceName": service,
                            "ServiceCategory": _service_category(service),
                        }
                    )
            next_token = resp.get("NextPageToken")
            if not next_token:
                break

        if not rows:
            return focus.empty_frame()
        df = self._stamp(focus.normalize(pd.DataFrame(rows)))
        # Provenance the platform can surface to warn about the ESR limitation.
        df.attrs["data_quality"] = (
            "Sourced from Cost Explorer: ListCost=BilledCost (no list price "
            "available); Effective Savings Rate is understated."
        )
        return df

    # ---- contract: recommendations --------------------------------------

    def fetch_recommendations(self) -> List[Recommendation]:
        """Prefer Cost Optimization Hub; fall back to CE rightsizing.

        Cost Optimization Hub is the unified surface AWS recommends: it already
        rolls up Compute Optimizer, rightsizing and commitment recommendations
        with a consistent savings shape, so we do not call each producer API
        separately.
        """
        try:
            import boto3  # noqa: F401
        except ImportError:
            return []
        try:
            return self._recommendations_from_hub()
        except Exception:
            try:
                return self._recommendations_from_ce_rightsizing()
            except Exception:
                return []

    def _recommendations_from_hub(self) -> List[Recommendation]:
        session = self._session()
        # cost-optimization-hub is a global service homed in us-east-1.
        # https://docs.aws.amazon.com/cost-management/latest/APIReference/API_Operations_AWS_Cost_Optimization_Hub.html
        client = session.client(
            "cost-optimization-hub", region_name=_GLOBAL_SIGNING_REGION
        )
        out: List[Recommendation] = []
        next_token: Optional[str] = None
        while True:
            kwargs: Dict[str, Any] = {"includeAllRecommendations": True, "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = client.list_recommendations(**kwargs)
            for item in resp.get("items", []):
                action = item.get("actionType", "")
                monthly = float(item.get("estimatedMonthlySavings", 0) or 0)
                out.append(
                    Recommendation(
                        source="aws_cost_optimization_hub",
                        cloud="AWS",
                        resource_id=item.get("resourceId", "")
                        or item.get("recommendationId", ""),
                        resource_type=item.get("currentResourceType", ""),
                        lever=_HUB_ACTION_TO_LEVER.get(action, "other"),
                        action=action or "Optimize",
                        estimated_monthly_savings=monthly,
                        currency=item.get("currencyCode", "USD"),
                        account_id=item.get("accountId", ""),
                        region=item.get("region", ""),
                        confidence=0.8,
                        detail={
                            "recommendationId": item.get("recommendationId", ""),
                            "recommendedResourceType": item.get("recommendedResourceType", ""),
                            "estimatedSavingsPercentage": item.get("estimatedSavingsPercentage"),
                        },
                    )
                )
            next_token = resp.get("nextToken")
            if not next_token:
                break
        return out

    def _recommendations_from_ce_rightsizing(self) -> List[Recommendation]:
        session = self._session()
        ce = session.client("ce", region_name=_GLOBAL_SIGNING_REGION)
        resp = ce.get_rightsizing_recommendation(
            Service="AmazonEC2",
            Configuration={
                "RecommendationTarget": "SAME_INSTANCE_FAMILY",
                "BenefitsConsidered": True,
            },
        )
        out: List[Recommendation] = []
        for rec in resp.get("RightsizingRecommendations", []):
            rtype = rec.get("RightsizingType", "")  # 'TERMINATE' | 'MODIFY'
            current = rec.get("CurrentInstance", {})
            if rtype == "TERMINATE":
                detail = rec.get("TerminateRecommendationDetail", {})
                monthly = float(detail.get("EstimatedMonthlySavings", 0) or 0)
                lever, action = "idle_resource", "Terminate"
            else:
                detail = rec.get("ModifyRecommendationDetail", {})
                targets = detail.get("TargetInstances", [])
                monthly = max(
                    (float(t.get("EstimatedMonthlySavings", 0) or 0) for t in targets),
                    default=0.0,
                )
                lever, action = "rightsizing", "Modify"
            out.append(
                Recommendation(
                    source="aws_cost_explorer_rightsizing",
                    cloud="AWS",
                    resource_id=current.get("ResourceId", ""),
                    resource_type="Amazon EC2 instance",
                    lever=lever,
                    action=action,
                    estimated_monthly_savings=monthly,
                    account_id=rec.get("AccountId", ""),
                    confidence=0.7,
                )
            )
        return out

    # ---- contract: budgets ----------------------------------------------

    def fetch_budgets(self) -> pd.DataFrame:
        empty = pd.DataFrame(columns=["period", "cloud", "application", "budget"])
        try:
            import boto3  # noqa: F401
        except ImportError:
            return empty
        try:
            session = self._session()
            account_id = session.client(
                "sts", region_name=_GLOBAL_SIGNING_REGION
            ).get_caller_identity().get("Account", "")
            # Budgets is a global service signed in us-east-1.
            # https://docs.aws.amazon.com/aws-cost-management/latest/APIReference/API_DescribeBudgets.html
            budgets = session.client("budgets", region_name=_GLOBAL_SIGNING_REGION)
        except Exception:
            return empty

        rows: List[dict] = []
        next_token: Optional[str] = None
        period = pd.Timestamp.today().to_period("M").to_timestamp()
        try:
            while True:
                kwargs: Dict[str, Any] = {"AccountId": account_id, "MaxResults": 100}
                if next_token:
                    kwargs["NextToken"] = next_token
                resp = budgets.describe_budgets(**kwargs)
                for b in resp.get("Budgets", []):
                    limit = b.get("BudgetLimit", {})
                    rows.append(
                        {
                            "period": period,
                            "cloud": "AWS",
                            "application": b.get("BudgetName", ""),
                            "budget": float(limit.get("Amount", 0) or 0),
                        }
                    )
                next_token = resp.get("NextToken")
                if not next_token:
                    break
        except Exception:
            return pd.DataFrame(rows, columns=["period", "cloud", "application", "budget"]) if rows else empty
        return pd.DataFrame(rows, columns=["period", "cloud", "application", "budget"]) if rows else empty
