"""Demo Mode -- a synthetic but structurally honest FOCUS 1.2 estate.

Zero credentials, zero network calls, deterministic given a seed. The point is
not "some numbers to draw charts with"; it is a dataset in which every feature
of the platform has something true to find:

  * a **commitment portfolio** with real coverage and genuine unused amortization,
    so Effective Savings Rate and commitment waste are non-trivial;
  * **untagged spend**, so allocation coverage is < 100% and chargeback
    readiness is a live question rather than a foregone conclusion;
  * a **shared-services pool** that must be split before showback means anything;
  * **usage waste** planted as detectable signals -- unattached volumes, idle
    NAT gateways, unassociated public IPs, gp2 volumes, previous-generation
    instance families, non-prod running 24x7;
  * a **step change** from a data-centre exit wave and a **GenAI ramp**, because
    a forecast model that has never met a step change is not worth shipping;
  * two **injected anomalies** so the detector has something to catch;
  * genuine **seasonality** -- this is a utility, so summer cooling load and
    autumn storm season move the grid workloads.

The estate is Con Edison-shaped: electric, gas and steam operations, an AMI
meter-data platform, an outage management system, and a grid-forecasting AI
workload that started consuming GPUs eight months ago.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
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

SEED = 20260708
DEFAULT_MONTHS = 24


# ==========================================================================
# The estate
# ==========================================================================


@dataclass(frozen=True)
class App:
    name: str
    business_unit: str
    cost_center: str
    clouds: Tuple[str, ...]
    services: Tuple[str, ...]
    monthly_base: float  # USD at t0
    growth: float  # monthly compound growth
    seasonality: str  # 'summer' | 'storm' | 'flat' | 'ramp'
    prod_share: float


APPS: List[App] = [
    App("Customer Billing (CIS)", "Customer Operations", "CC-1010",
        ("AWS",), ("Compute", "Databases", "Storage"), 210_000, 0.004, "flat", 0.72),
    App("Outage Management (OMS)", "Electric Operations", "CC-2010",
        ("AWS", "Azure"), ("Compute", "Databases", "Networking"), 165_000, 0.006, "storm", 0.80),
    App("AMI Meter Data (MDM)", "Electric Operations", "CC-2020",
        ("AWS",), ("Compute", "Storage", "Analytics"), 240_000, 0.009, "summer", 0.78),
    App("SCADA / EMS Historian", "Electric Operations", "CC-2030",
        ("Azure",), ("Compute", "Databases", "Security"), 118_000, 0.002, "summer", 0.88),
    App("GIS & Asset Registry", "Gas Operations", "CC-3010",
        ("Azure",), ("Compute", "Databases", "Storage"), 74_000, 0.003, "flat", 0.70),
    App("Work & Asset Management", "Gas Operations", "CC-3020",
        ("Azure", "AWS"), ("Compute", "Databases", "Networking"), 96_000, 0.004, "flat", 0.68),
    App("Customer Portal & Mobile", "Customer Operations", "CC-1020",
        ("AWS", "GCP"), ("Compute", "Networking", "Storage"), 88_000, 0.008, "summer", 0.66),
    App("Data Lake & Analytics", "Corporate IT", "CC-9010",
        ("GCP", "AWS"), ("Analytics", "Storage", "Compute"), 152_000, 0.011, "flat", 0.55),
    App("Grid AI Forecasting", "Clean Energy", "CC-4010",
        ("AWS", "GCP"), ("AI and Machine Learning", "Compute", "Storage"), 26_000, 0.075, "ramp", 0.60),
    App("Steam Distribution Ops", "Steam Operations", "CC-5010",
        ("Azure",), ("Compute", "Databases"), 42_000, 0.001, "winter", 0.85),
    # The shared pool. Deliberately carries no business unit -- it is the cost
    # that showback has to split before anyone can be charged for it.
    App("Shared Platform Services", "Corporate IT", "CC-9000",
        ("AWS", "Azure", "GCP"), ("Management and Governance", "Security", "Networking"),
        130_000, 0.005, "flat", 1.0),
]

ENVIRONMENTS = ["prod", "nonprod", "dr"]

SERVICE_NAMES: Dict[Tuple[str, str], List[str]] = {
    ("AWS", "Compute"): ["Amazon EC2", "AWS Lambda", "Amazon EKS"],
    ("AWS", "Storage"): ["Amazon S3", "Amazon EBS", "Amazon EFS"],
    ("AWS", "Databases"): ["Amazon RDS", "Amazon Aurora", "Amazon DynamoDB"],
    ("AWS", "Networking"): ["Amazon VPC", "Amazon CloudFront", "Elastic Load Balancing"],
    ("AWS", "Analytics"): ["Amazon Redshift", "AWS Glue", "Amazon Athena"],
    ("AWS", "AI and Machine Learning"): ["Amazon SageMaker", "Amazon Bedrock"],
    ("AWS", "Management and Governance"): ["Amazon CloudWatch", "AWS Config"],
    ("AWS", "Security"): ["AWS WAF", "Amazon GuardDuty"],
    ("Azure", "Compute"): ["Virtual Machines", "Azure Kubernetes Service", "Azure Functions"],
    ("Azure", "Storage"): ["Storage Accounts", "Managed Disks"],
    ("Azure", "Databases"): ["Azure SQL Database", "Azure Cosmos DB"],
    ("Azure", "Networking"): ["Virtual Network", "Azure Front Door", "Load Balancer"],
    ("Azure", "Analytics"): ["Azure Synapse Analytics", "Azure Data Factory"],
    ("Azure", "AI and Machine Learning"): ["Azure Machine Learning", "Azure OpenAI Service"],
    ("Azure", "Management and Governance"): ["Azure Monitor", "Log Analytics"],
    ("Azure", "Security"): ["Microsoft Defender for Cloud", "Azure Firewall"],
    ("GCP", "Compute"): ["Compute Engine", "Google Kubernetes Engine", "Cloud Run"],
    ("GCP", "Storage"): ["Cloud Storage", "Persistent Disk"],
    ("GCP", "Databases"): ["Cloud SQL", "Bigtable"],
    ("GCP", "Networking"): ["Cloud Load Balancing", "Cloud CDN"],
    ("GCP", "Analytics"): ["BigQuery", "Dataflow"],
    ("GCP", "AI and Machine Learning"): ["Vertex AI"],
    ("GCP", "Management and Governance"): ["Cloud Monitoring", "Cloud Logging"],
    ("GCP", "Security"): ["Cloud Armor", "Security Command Center"],
}

REGIONS: Dict[str, List[Tuple[str, str]]] = {
    "AWS": [("us-east-1", "US East (N. Virginia)"), ("us-east-2", "US East (Ohio)")],
    "Azure": [("eastus", "East US"), ("eastus2", "East US 2")],
    "GCP": [("us-east4", "Northern Virginia"), ("us-central1", "Iowa")],
}

BILLING_ACCOUNT = {
    "AWS": ("471820193004", "Con Edison AWS Payer"),
    "Azure": ("f0e2c7b1-9a44-4d1e-9d2b-3c8e1a6b7f22", "Con Edison Azure EA"),
    "GCP": ("01A2B3-C4D5E6-F70819", "Con Edison GCP Billing Account"),
}

COMMITMENT_TYPE = {
    "AWS": ("Savings Plan", "Spend"),
    "Azure": ("Reservation", "Usage"),
    "GCP": ("Committed Use Discount", "Spend"),
}

# Negotiated enterprise discount off list (EDP / MACC / Google commit).
NEGOTIATED_DISCOUNT = {"AWS": 0.12, "Azure": 0.14, "GCP": 0.10}

# Additional discount when a commitment covers the usage.
COMMITMENT_DISCOUNT = {"AWS": 0.31, "Azure": 0.34, "GCP": 0.28}

# Spot / preemptible discount off list.
SPOT_DISCOUNT = {"AWS": 0.72, "Azure": 0.68, "GCP": 0.75}


# ==========================================================================
# Shape functions
# ==========================================================================


def _seasonal(kind: str, month: int) -> float:
    """A multiplier on base spend. Month is 1-12.

    A utility's cloud estate is not flat. Meter-data ingestion and grid
    telemetry spike with summer cooling load; outage management spikes in the
    autumn storm season; steam peaks in winter.
    """
    if kind == "summer":
        return 1.0 + 0.22 * np.sin((month - 4) / 12.0 * 2 * np.pi)
    if kind == "winter":
        return 1.0 + 0.20 * np.sin((month - 10) / 12.0 * 2 * np.pi)
    if kind == "storm":
        # Two humps: late-summer hurricanes, late-autumn nor'easters.
        return 1.0 + 0.16 * np.exp(-((month - 9) ** 2) / 3.0) + 0.10 * np.exp(-((month - 12) ** 2) / 2.5)
    return 1.0


def _step_change(months_ago: int, wave_at: int = 10) -> float:
    """A data-centre exit wave landed `wave_at` months ago.

    A pure trend model will happily extrapolate straight through this. That is
    the point -- the forecast tab has to earn its prediction interval.
    """
    return 1.0 if months_ago > wave_at else 1.28


def _ai_ramp(months_ago: int, start_at: int = 8) -> float:
    """GenAI/GPU spend that did not exist before `start_at` months ago."""
    if months_ago > start_at:
        return 0.04
    elapsed = start_at - months_ago
    return min(1.0, 0.04 + 0.28 * elapsed)


# ==========================================================================
# Generator
# ==========================================================================


CATEGORICAL_COLUMNS = [
    "BillingAccountId", "BillingAccountName", "BillingAccountType", "BillingCurrency",
    "InvoiceId", "InvoiceIssuerName", "ProviderName", "PublisherName",
    "SubAccountId", "SubAccountName", "SubAccountType",
    "ChargeCategory", "ChargeClass", "ChargeDescription", "ChargeFrequency",
    "PricingCategory", "PricingCurrency", "PricingUnit", "ConsumedUnit",
    "CommitmentDiscountId", "CommitmentDiscountName", "CommitmentDiscountType",
    "CommitmentDiscountCategory", "CommitmentDiscountStatus",
    "ServiceCategory", "ServiceName", "ServiceSubcategory",
    "RegionId", "RegionName", "AvailabilityZone",
    "ResourceId", "ResourceName", "ResourceType", "SkuId", "SkuMeter",
]


def _compact(df: pd.DataFrame) -> pd.DataFrame:
    """Downcast the low-cardinality string columns.

    Every one of these has at most a few hundred distinct values across the
    whole estate, so `category` dtype cuts the frame by roughly 4x. Streamlit
    Cloud gives us 1 GB and pandas copies on nearly every groupby.
    """
    for col in CATEGORICAL_COLUMNS:
        if col in df.columns:
            df[col] = df[col].astype("category")
    return df


class _Gen:
    def __init__(self, seed: int, months: int, end: Optional[date], daily_months: int = 12) -> None:
        self.rng = np.random.default_rng(seed)
        self.months = months
        self.daily_months = daily_months
        self.end = end or date.today().replace(day=1) - timedelta(days=1)
        self.rows: List[dict] = []

    # -- helpers -------------------------------------------------------

    def _month_starts(self) -> List[pd.Timestamp]:
        last = pd.Timestamp(self.end).to_period("M").to_timestamp()
        return [last - pd.DateOffset(months=i) for i in range(self.months - 1, -1, -1)]

    def _region(self, cloud: str) -> Tuple[str, str]:
        idx = int(self.rng.integers(0, len(REGIONS[cloud])))
        return REGIONS[cloud][idx]

    def _service_name(self, cloud: str, category: str) -> str:
        names = SERVICE_NAMES.get((cloud, category), [f"{cloud} {category}"])
        return names[int(self.rng.integers(0, len(names)))]

    def _base_row(self, cloud: str, period_start: pd.Timestamp, period_end: pd.Timestamp) -> dict:
        acct_id, acct_name = BILLING_ACCOUNT[cloud]
        billing_start = period_start.to_period("M").to_timestamp()
        billing_end = billing_start + pd.DateOffset(months=1)
        return {
            "BillingAccountId": acct_id,
            "BillingAccountName": acct_name,
            "BillingAccountType": {"AWS": "Payer Account", "Azure": "Billing Account", "GCP": "Billing Account"}[cloud],
            "BillingCurrency": "USD",
            "BillingPeriodStart": billing_start,
            "BillingPeriodEnd": billing_end,
            "InvoiceId": f"{cloud[:3].upper()}-{billing_start:%Y%m}",
            "InvoiceIssuerName": {"AWS": "Amazon Web Services", "Azure": "Microsoft", "GCP": "Google Cloud"}[cloud],
            "ProviderName": cloud,
            "PublisherName": {"AWS": "Amazon Web Services", "Azure": "Microsoft", "GCP": "Google Cloud"}[cloud],
            "ChargePeriodStart": period_start,
            "ChargePeriodEnd": period_end,
            "ChargeClass": pd.NA,
            "PricingCurrency": "USD",
        }

    # -- usage rows ----------------------------------------------------

    def _emit_usage(
        self,
        app: App,
        cloud: str,
        day: pd.Timestamp,
        month_factor: float,
        span_days: int = 1,
    ) -> None:
        """Emit one charge period per (service category, environment).

        `span_days` lets the same code emit a daily row or a whole-month row.
        We keep daily grain only for the trailing year -- that is all the
        anomaly detector and the 90-day baseline ever look at -- and roll the
        older history up to monthly. It halves the frame at zero analytical
        cost, because forecasting resamples to months anyway.
        """
        sub_id, sub_name = self._subaccount(app, cloud)
        period_end = day + pd.Timedelta(days=span_days)

        for category in app.services:
            # Split the app's daily spend across its service categories with a
            # stable-ish weighting, then across environments.
            cat_weight = {
                "Compute": 0.45,
                "Databases": 0.20,
                "Storage": 0.15,
                "Networking": 0.10,
                "Analytics": 0.22,
                "AI and Machine Learning": 0.30,
                "Management and Governance": 0.40,
                "Security": 0.30,
            }.get(category, 0.2)

            daily_base = app.monthly_base / 30.0 * cat_weight * month_factor
            if category == "AI and Machine Learning":
                daily_base *= _ai_ramp(self._months_ago(day))

            for env in ENVIRONMENTS:
                env_share = {
                    "prod": app.prod_share,
                    "nonprod": (1 - app.prod_share) * 0.72,
                    "dr": (1 - app.prod_share) * 0.28,
                }[env]
                if env_share <= 0.001:
                    continue

                amount = daily_base * env_share * span_days * float(self.rng.normal(1.0, 0.06))
                if amount <= 0:
                    continue

                self._usage_row(app, cloud, category, env, day, period_end, amount, sub_id, sub_name)

    def _months_ago(self, day: pd.Timestamp) -> int:
        last = pd.Timestamp(self.end).to_period("M")
        return (last - day.to_period("M")).n

    def _subaccount(self, app: App, cloud: str) -> Tuple[str, str]:
        slug = app.name.split("(")[0].strip().lower().replace(" ", "-").replace("&", "and")
        if cloud == "AWS":
            return f"{abs(hash(slug)) % 10**12:012d}", f"coned-{slug}"
        if cloud == "Azure":
            return f"sub-{abs(hash(slug)) % 10**8:08x}", f"coned-{slug}-sub"
        return f"coned-{slug}", f"coned-{slug}"

    def _usage_row(
        self,
        app: App,
        cloud: str,
        category: str,
        env: str,
        day: pd.Timestamp,
        period_end: pd.Timestamp,
        list_cost: float,
        sub_id: str,
        sub_name: str,
    ) -> None:
        row = self._base_row(cloud, day, period_end)
        region_id, region_name = self._region(cloud)
        service_name = self._service_name(cloud, category)

        # ---- pricing path -------------------------------------------------
        contracted = list_cost * (1 - NEGOTIATED_DISCOUNT[cloud])

        commitment_id = pd.NA
        commitment_name = pd.NA
        commitment_type = pd.NA
        commitment_category = pd.NA
        commitment_status = pd.NA
        pricing_category = "Standard"
        effective = contracted

        is_committable = category in ("Compute", "Databases", "Analytics", "AI and Machine Learning")
        spot_eligible = env == "nonprod" and category == "Compute"

        if spot_eligible and self.rng.random() < 0.35:
            pricing_category = "Dynamic"  # Spot / preemptible
            effective = list_cost * (1 - SPOT_DISCOUNT[cloud])
        elif is_committable and env == "prod" and self.rng.random() < 0.66:
            ctype, ccat = COMMITMENT_TYPE[cloud]
            commitment_id = f"{cloud[:3].lower()}-cmt-{abs(hash((cloud, category))) % 10**6:06d}"
            commitment_name = f"{ctype} - {category}"
            commitment_type = ctype
            commitment_category = ccat
            commitment_status = "Used"
            pricing_category = "Committed"
            effective = contracted * (1 - COMMITMENT_DISCOUNT[cloud])

        quantity = list_cost / max(float(self.rng.normal(0.09, 0.02)), 0.01)

        row.update(
            {
                "SubAccountId": sub_id,
                "SubAccountName": sub_name,
                "SubAccountType": "Account" if cloud == "AWS" else ("Subscription" if cloud == "Azure" else "Project"),
                "ChargeCategory": "Usage",
                "ChargeDescription": f"{service_name} usage in {region_id}",
                "ChargeFrequency": "Usage-Based",
                "BilledCost": round(effective, 4),
                "EffectiveCost": round(effective, 4),
                "ListCost": round(list_cost, 4),
                "ContractedCost": round(contracted, 4),
                "PricingCategory": pricing_category,
                "PricingQuantity": round(quantity, 4),
                "PricingUnit": "Hours" if category == "Compute" else "GB-Month",
                "ConsumedQuantity": round(quantity, 4),
                "ConsumedUnit": "Hours" if category == "Compute" else "GB",
                "CommitmentDiscountId": commitment_id,
                "CommitmentDiscountName": commitment_name,
                "CommitmentDiscountType": commitment_type,
                "CommitmentDiscountCategory": commitment_category,
                "CommitmentDiscountStatus": commitment_status,
                "ServiceCategory": category,
                "ServiceName": service_name,
                "ServiceSubcategory": self._subcategory(category),
                "RegionId": region_id,
                "RegionName": region_name,
                "AvailabilityZone": f"{region_id}a",
                "ResourceId": self._resource_id(cloud, category, app, env),
                "ResourceName": f"{app.name[:12]}-{env}",
                "ResourceType": self._resource_type(category),
                "SkuId": self._sku(cloud, category, env),
                "SkuMeter": f"{service_name} {category}",
                "Tags": self._tags(app, env, cloud),
            }
        )
        self.rows.append(row)

    @staticmethod
    def _subcategory(category: str) -> str:
        return {
            "Compute": "Virtual Machines",
            "Storage": "Object Storage",
            "Databases": "Relational Databases",
            "Networking": "Network Connectivity",
            "Analytics": "Data Processing",
            "AI and Machine Learning": "Generative AI",
            "Management and Governance": "Observability",
            "Security": "Threat Detection and Response",
        }.get(category, "Other (Other)")

    @staticmethod
    def _resource_type(category: str) -> str:
        return {
            "Compute": "Instance",
            "Storage": "Volume",
            "Databases": "Database",
            "Networking": "Gateway",
            "Analytics": "Cluster",
            "AI and Machine Learning": "Endpoint",
            "Management and Governance": "Workspace",
            "Security": "Policy",
        }.get(category, "Resource")

    def _resource_id(self, cloud: str, category: str, app: App, env: str) -> str:
        h = abs(hash((cloud, category, app.name, env))) % 10**10
        if cloud == "AWS":
            return f"arn:aws:{category[:3].lower()}:us-east-1:471820193004:resource/{h:010d}"
        if cloud == "Azure":
            return f"/subscriptions/sub/resourceGroups/{env}/providers/{category}/{h:010d}"
        return f"//compute.googleapis.com/projects/coned/{category}/{h:010d}"

    def _sku(self, cloud: str, category: str, env: str) -> str:
        """SKU strings carry the optimization signal.

        `gp2` volumes, previous-generation `m4`/`Standard_D2_v2` families and
        `license-included` Windows SKUs are planted here on purpose -- the
        detectors in `optimize.py` read exactly these patterns.
        """
        if category == "Storage":
            gen = "gp2" if self.rng.random() < 0.3 else "gp3"
            return f"{cloud}:{gen}:{env}"
        if category == "Compute":
            if cloud == "AWS":
                fam = "m4.xlarge" if self.rng.random() < 0.22 else "m7g.xlarge"
            elif cloud == "Azure":
                fam = "Standard_D2_v2" if self.rng.random() < 0.20 else "Standard_D4as_v5"
            else:
                fam = "n1-standard-4" if self.rng.random() < 0.18 else "c4a-standard-4"
            lic = ":license-included" if self.rng.random() < 0.15 else ""
            return f"{cloud}:{fam}{lic}"
        return f"{cloud}:{category.lower().replace(' ', '-')}"

    @staticmethod
    def _tags(app: App, env: str, cloud: str) -> dict:
        return {
            "application": app.name,
            "business_unit": app.business_unit,
            "cost_center": app.cost_center,
            "environment": env,
            "owner": f"{app.business_unit.split()[0].lower()}-platform@coned.com",
            "project": app.name.split("(")[0].strip(),
        }

    # -- non-usage rows ------------------------------------------------

    def _emit_unallocated(
        self, cloud: str, day: pd.Timestamp, month_factor: float, span_days: int = 1
    ) -> None:
        """Untagged spend. Every real estate has it; this is what drags
        allocation coverage below the 90% chargeback threshold."""
        amount = float(self.rng.normal(1_450, 240)) * month_factor * span_days
        if amount <= 0:
            return
        row = self._base_row(cloud, day, day + pd.Timedelta(days=span_days))
        region_id, region_name = self._region(cloud)
        contracted = amount * (1 - NEGOTIATED_DISCOUNT[cloud])
        row.update(
            {
                "SubAccountId": "unknown",
                "SubAccountName": "untagged",
                "ChargeCategory": "Usage",
                "ChargeDescription": "Untagged resource usage",
                "ChargeFrequency": "Usage-Based",
                "BilledCost": round(contracted, 4),
                "EffectiveCost": round(contracted, 4),
                "ListCost": round(amount, 4),
                "ContractedCost": round(contracted, 4),
                "PricingCategory": "Standard",
                "PricingQuantity": round(amount / 0.08, 2),
                "PricingUnit": "Hours",
                "ConsumedQuantity": round(amount / 0.08, 2),
                "ConsumedUnit": "Hours",
                "ServiceCategory": "Compute",
                "ServiceName": self._service_name(cloud, "Compute"),
                "ServiceSubcategory": "Virtual Machines",
                "RegionId": region_id,
                "RegionName": region_name,
                "ResourceType": "Instance",
                "SkuId": f"{cloud}:untagged",
                "Tags": {},
            }
        )
        self.rows.append(row)

    def _emit_unused_commitment(self, cloud: str, month_start: pd.Timestamp) -> None:
        """Amortized cost of commitment that covered nothing.

        FOCUS models this as a Usage row with `CommitmentDiscountStatus='Unused'`,
        no ResourceId, zero consumed quantity and zero ListCost -- no usage
        occurred, yet the money was spent. It is the cleanest definition of
        waste in the whole specification.
        """
        ctype, ccat = COMMITMENT_TYPE[cloud]
        # Utilization drifts: worse in the months right after a purchase wave.
        ago = self._months_ago(month_start)
        base_unused = {"AWS": 5_600, "Azure": 4_200, "GCP": 2_100}[cloud]
        factor = 1.9 if ago in (9, 10, 11) else 1.0
        amount = float(self.rng.normal(base_unused * factor, base_unused * 0.15))
        if amount <= 0:
            return

        row = self._base_row(cloud, month_start, month_start + pd.DateOffset(months=1))
        row.update(
            {
                "ChargeCategory": "Usage",
                "ChargeDescription": f"Unused {ctype} commitment",
                "ChargeFrequency": "Recurring",
                "BilledCost": round(amount, 4),
                "EffectiveCost": round(amount, 4),
                "ListCost": 0.0,
                "ContractedCost": round(amount, 4),
                "PricingCategory": "Committed",
                "ConsumedQuantity": 0.0,
                "ConsumedUnit": "Hours",
                "CommitmentDiscountId": f"{cloud[:3].lower()}-cmt-unused",
                "CommitmentDiscountName": f"{ctype} - unused",
                "CommitmentDiscountType": ctype,
                "CommitmentDiscountCategory": ccat,
                "CommitmentDiscountStatus": "Unused",
                "ServiceCategory": "Compute",
                "ServiceName": self._service_name(cloud, "Compute"),
                "RegionId": self._region(cloud)[0],
                "Tags": {},
            }
        )
        self.rows.append(row)

    def _emit_purchase(self, cloud: str, month_start: pd.Timestamp) -> None:
        """A commitment purchase. `EffectiveCost` is zero because the value is
        amortized into the Usage rows -- that is what keeps the executive view
        free of lumpy one-off spikes."""
        if self._months_ago(month_start) not in (11, 23):
            return
        ctype, ccat = COMMITMENT_TYPE[cloud]
        amount = {"AWS": 1_450_000, "Azure": 980_000, "GCP": 410_000}[cloud]
        row = self._base_row(cloud, month_start, month_start + pd.DateOffset(months=1))
        row.update(
            {
                "ChargeCategory": "Purchase",
                "ChargeDescription": f"{ctype} 1-year all-upfront purchase",
                "ChargeFrequency": "One-Time",
                "BilledCost": float(amount),
                "EffectiveCost": 0.0,
                "ListCost": float(amount),
                "ContractedCost": float(amount),
                "PricingCategory": "Committed",
                "CommitmentDiscountId": f"{cloud[:3].lower()}-cmt-purchase",
                "CommitmentDiscountName": f"{ctype} purchase",
                "CommitmentDiscountType": ctype,
                "CommitmentDiscountCategory": ccat,
                "ServiceCategory": "Compute",
                "ServiceName": self._service_name(cloud, "Compute"),
                "Tags": {},
            }
        )
        self.rows.append(row)

    def _emit_tax_and_credits(self, cloud: str, month_start: pd.Timestamp) -> None:
        month_usage = 0.0  # filled by caller ordering; approximate off base
        base = sum(a.monthly_base for a in APPS if cloud in a.clouds) * 0.35
        for kind, sign, rate in (("Tax", 1, 0.041), ("Credit", -1, 0.018)):
            amount = base * rate * float(self.rng.normal(1.0, 0.05)) * sign
            row = self._base_row(cloud, month_start, month_start + pd.DateOffset(months=1))
            row.update(
                {
                    "ChargeCategory": kind,
                    "ChargeDescription": f"{kind} for billing period",
                    "ChargeFrequency": "One-Time",
                    "BilledCost": round(amount, 2),
                    "EffectiveCost": round(amount, 2),
                    "ListCost": round(abs(amount), 2),
                    "ContractedCost": round(amount, 2),
                    "PricingCategory": pd.NA if kind == "Tax" else "Other",
                    "ServiceCategory": "Other",
                    "ServiceName": f"{cloud} {kind}",
                    "Tags": {},
                }
            )
            self.rows.append(row)

    def _emit_waste(self, cloud: str, month_start: pd.Timestamp) -> None:
        """Planted, detectable usage waste.

        Each of these is a row the detectors in `optimize.py` are written to
        find: an unattached volume, an idle NAT gateway, an unassociated public
        IP, an orphaned snapshot. `ConsumedQuantity == 0` with `EffectiveCost > 0`
        is the universal idle signature.
        """
        wastes = [
            ("Volume", "Unattached block storage volume", "Storage", 3_100),
            ("Gateway", "Idle NAT gateway", "Networking", 1_240),
            ("IpAddress", "Unassociated public IP address", "Networking", 380),
            ("Snapshot", "Orphaned snapshot beyond retention", "Storage", 890),
            ("LoadBalancer", "Idle load balancer", "Networking", 620),
        ]
        for rtype, desc, category, base in wastes:
            amount = float(self.rng.normal(base, base * 0.12))
            if amount <= 0:
                continue
            row = self._base_row(cloud, month_start, month_start + pd.DateOffset(months=1))
            region_id, region_name = self._region(cloud)
            row.update(
                {
                    "ChargeCategory": "Usage",
                    "ChargeDescription": desc,
                    "ChargeFrequency": "Recurring",
                    "BilledCost": round(amount, 2),
                    "EffectiveCost": round(amount, 2),
                    "ListCost": round(amount * 1.12, 2),
                    "ContractedCost": round(amount, 2),
                    "PricingCategory": "Standard",
                    "ConsumedQuantity": 0.0,  # <- the idle signature
                    "ConsumedUnit": "Hours",
                    "ServiceCategory": category,
                    "ServiceName": self._service_name(cloud, category),
                    "RegionId": region_id,
                    "RegionName": region_name,
                    "ResourceType": rtype,
                    "ResourceId": f"{cloud}-{rtype}-{abs(hash((cloud, rtype, month_start))) % 10**8:08d}",
                    "SkuId": f"{cloud}:{rtype.lower()}:idle",
                    "Tags": {},
                }
            )
            self.rows.append(row)

    # -- driver --------------------------------------------------------

    def build(self) -> pd.DataFrame:
        month_starts = self._month_starts()

        for m in month_starts:
            ago = self._months_ago(m)
            days_in_month = int((m + pd.DateOffset(months=1) - m).days)

            for cloud in ("AWS", "Azure", "GCP"):
                self._emit_unused_commitment(cloud, m)
                self._emit_purchase(cloud, m)
                self._emit_tax_and_credits(cloud, m)
                self._emit_waste(cloud, m)

            # Daily grain for the trailing year, monthly before that.
            if ago < self.daily_months:
                periods = [(m + pd.Timedelta(days=d), 1) for d in range(days_in_month)]
            else:
                periods = [(m, days_in_month)]

            for day, span in periods:
                step = _step_change(ago)
                for app in APPS:
                    growth = (1 + app.growth) ** (self.months - ago)
                    season = _seasonal(app.seasonality, day.month)
                    factor = growth * season * step
                    for cloud in app.clouds:
                        share = 1.0 if len(app.clouds) == 1 else (0.68 if cloud == app.clouds[0] else 0.32)
                        self._emit_usage(app, cloud, day, factor * share, span_days=span)

                for cloud in ("AWS", "Azure", "GCP"):
                    self._emit_unallocated(cloud, day, step, span_days=span)

        df = pd.DataFrame(self.rows)
        df = self._inject_anomalies(df)
        return _compact(focus.normalize(df))

    def _inject_anomalies(self, df: pd.DataFrame) -> pd.DataFrame:
        """Two spikes worth catching.

        One is a runaway Databricks-style analytics cluster left running over a
        weekend; the other is an egress blow-out from a misconfigured CDN
        origin. Both are multiplicative on a single day and a single service,
        which is exactly the shape a residual-based detector should surface.
        """
        end = pd.Timestamp(self.end)
        spikes = [
            (end - pd.Timedelta(days=41), "Analytics", 6.4),
            (end - pd.Timedelta(days=17), "Networking", 4.8),
        ]
        for spike_day, category, mult in spikes:
            mask = (df["ChargePeriodStart"].dt.date == spike_day.date()) & (df["ServiceCategory"] == category)
            if mask.any():
                for col in ("BilledCost", "EffectiveCost", "ListCost", "ContractedCost"):
                    df.loc[mask, col] = df.loc[mask, col] * mult
                df.loc[mask, "ChargeDescription"] = df.loc[mask, "ChargeDescription"] + " (runaway workload)"
        return df


# ==========================================================================
# Budgets and business drivers
# ==========================================================================


def generate_budgets(focus_df: pd.DataFrame, seed: int = SEED) -> pd.DataFrame:
    """Budgets set from a plan, not from the actuals.

    Deliberately imperfect: budgets were set from last year's run-rate plus a
    flat 6% uplift, which means the data-centre exit wave and the AI ramp both
    blow through them. That is the realistic case, and it is what makes the
    variance tab worth reading.
    """
    rng = np.random.default_rng(seed + 1)
    df = focus_df.copy()
    df["period"] = df["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp()

    actual = (
        df[df["ChargeCategory"] == "Usage"]
        .groupby(["period", "ProviderName", "tag_application"], as_index=False)["EffectiveCost"]
        .sum()
        .rename(columns={"ProviderName": "cloud", "tag_application": "application", "EffectiveCost": "actual"})
    )

    first_year = actual["period"].min() + pd.DateOffset(months=11)
    baseline = (
        actual[actual["period"] <= first_year]
        .groupby(["cloud", "application"], as_index=False)["actual"]
        .mean()
        .rename(columns={"actual": "base"})
    )

    out = actual.merge(baseline, on=["cloud", "application"], how="left")
    out["base"] = out["base"].fillna(out["actual"])
    months_out = (out["period"] - out["period"].min()).dt.days / 30.44
    out["budget"] = out["base"] * (1.06 ** (months_out / 12.0)) * rng.normal(1.0, 0.03, len(out))
    return out[["period", "cloud", "application", "budget"]]


def generate_drivers(focus_df: pd.DataFrame, seed: int = SEED) -> pd.DataFrame:
    """The denominators for unit economics.

    A utility's unit-cost story is not "cost per vCPU" -- that is a resource
    efficiency metric. It is cost per customer served, per kWh delivered, per
    meter read, per work order closed. Those are the numbers a VP can defend
    in a rate case.
    """
    rng = np.random.default_rng(seed + 2)
    periods = sorted(focus_df["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp().unique())
    rows: List[dict] = []
    specs = {
        "Customers served": (3_600_000, 0.0012, 0.004),
        "kWh delivered (millions)": (5_100, 0.0018, 0.075),
        "Smart meter reads (millions)": (312, 0.0031, 0.012),
        "Work orders closed": (128_000, 0.0022, 0.03),
        "Digital self-service sessions (millions)": (7.4, 0.0095, 0.05),
    }
    for i, p in enumerate(periods):
        month = pd.Timestamp(p).month
        for metric, (base, growth, noise) in specs.items():
            season = 1.0
            if metric.startswith("kWh"):
                season = 1.0 + 0.19 * np.sin((month - 4) / 12.0 * 2 * np.pi)
            value = base * ((1 + growth) ** i) * season * rng.normal(1.0, noise)
            rows.append({"period": pd.Timestamp(p), "metric": metric, "value": float(value)})
    return pd.DataFrame(rows)


# ==========================================================================
# The connector
# ==========================================================================


class DemoConnector(Connector):
    """Demo Mode. Generates the estate in-process; never touches a network."""

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="demo",
            display_name="Demo data (synthetic)",
            vendor="Multi-Cloud FinOps Command Center",
            clouds=["AWS", "Azure", "GCP"],
            auth=AuthKind.NONE,
            capabilities=[
                Capability.COSTS,
                Capability.RECOMMENDATIONS,
                Capability.BUDGETS,
                Capability.FORECAST,
                Capability.ANOMALIES,
                Capability.NATIVE_FOCUS,
            ],
            base_url="",
            docs_url="https://focus.finops.org/",
            focus_support="native",
            notes=(
                "Deterministic FOCUS 1.2 estate for a utility. Contains planted "
                "commitment waste, untagged spend, idle resources and two anomalies."
            ),
        )

    def test_connection(self) -> ConnectionResult:
        return ConnectionResult(ok=True, message="Demo generator ready (no credentials required).", latency_ms=0.0)

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        months = max(1, int((end.year - start.year) * 12 + end.month - start.month))
        gen = _Gen(seed=int(self.options.get("seed", SEED)), months=months, end=end)
        return gen.build()

    def fetch_recommendations(self) -> List[Recommendation]:
        """Demo Mode returns none here on purpose.

        Native recommendations are what a *provider* hands you. In Demo Mode the
        opportunities are derived from the billing data by `optimize.py`, which
        is the path that also works for a customer whose procured tool has no
        recommendations API.
        """
        return []


def build_demo_dataset(months: int = DEFAULT_MONTHS, seed: int = SEED, end: Optional[date] = None):
    """Convenience: the full (focus_df, budgets, drivers) triple."""
    end = end or (date.today().replace(day=1) - timedelta(days=1))
    gen = _Gen(seed=seed, months=months, end=end)
    df = focus.explode_tags(gen.build())
    for t in focus.CANONICAL_TAGS:
        df[f"tag_{t}"] = df[f"tag_{t}"].astype("category")
    df = focus.serialize_tags(df)
    return df, generate_budgets(df, seed), generate_drivers(df, seed)
