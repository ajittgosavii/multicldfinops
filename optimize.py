"""The optimization lever catalog and its rule-based detectors.

Why rule-based detection over a FOCUS frame -- and not per-cloud API scraping?
-----------------------------------------------------------------------------
The FinOps Open Cost and Usage Specification normalises billing across every
provider and every procured tool into one set of spec columns. A detector that
reads only those columns therefore works *identically* for AWS, Azure, GCP, and
for any Cloudability / Finout / Apptio export that lands in the same frame. We
write the rule once; it runs everywhere. That is the same architectural bet the
connectors make -- and it is what lets this module light up in Demo Mode with
zero credentials, then run unchanged against a real estate.

A native recommendations API (Compute Optimizer, Azure Advisor, Recommender)
is a *complement*, not a substitute: it sees CPU and memory telemetry the bill
does not carry, but it stops at the edge of the cloud that emitted it, and it is
silent for a customer whose only feed is a third-party cost tool. Everything
here is derivable from the invoice, which every customer already has.

Honesty about what the bill cannot see
--------------------------------------
Billing data records *what was charged*, not *how hard a resource worked*. A
rule that needs utilisation, access patterns, or interruption tolerance is
making an assumption the invoice cannot confirm. Where a detector does that it
says so in its docstring, states the assumption in the opportunity's `evidence`,
and lowers `confidence` accordingly. We never dress an estimate up as a fact:
the two things the FOCUS frame proves outright -- unused commitment and the
zero-consumption idle signature -- are the only detectors that carry high
confidence. Everything that leans on an unseen signal is deliberately hedged.

Each `Lever` carries the vendor-published savings ceiling and its source URL, so
a number a VP challenges can always be traced back to the provider that quoted
it. The detectors then apply conservative haircuts to those ceilings, because a
published "up to 90%" is a best case, not a portfolio average.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

import kpi

COST = "EffectiveCost"
ODE = "ListCost"


# ==========================================================================
# The lever catalog
# ==========================================================================


@dataclass(frozen=True)
class Lever:
    id: str
    name: str
    category: str  # 'Rate' | 'Usage' | 'Architecture' | 'AI/GPU'
    clouds: Tuple[str, ...]
    savings_low: float  # fraction, e.g. 0.20
    savings_high: float
    effort: str  # 'Low' | 'Medium' | 'High'
    risk: str  # 'Low' | 'Medium' | 'High'
    time_to_value: str  # 'Hours' | 'Days' | 'Weeks' | 'Months'
    prerequisites: str
    detection: str
    source_url: str


ALL = ("AWS", "Azure", "GCP")

LEVERS: List[Lever] = [
    # ---- Rate optimization ------------------------------------------------
    Lever("R1", "AWS Compute Savings Plan", "Rate", ("AWS",), 0.10, 0.66,
          "Low", "Low", "Days",
          "12- or 36-month commitment; stable baseline spend",
          "Commitment-eligible Standard usage with no CommitmentDiscountId; commit to the sustained floor.",
          "https://aws.amazon.com/savingsplans/compute-pricing/"),
    Lever("R2", "AWS EC2 Instance Savings Plan", "Rate", ("AWS",), 0.10, 0.72,
          "Low", "Medium", "Days",
          "Commit to an instance family in a region",
          "Steady EC2 usage in one family/region uncovered by a commitment.",
          "https://aws.amazon.com/savingsplans/compute-pricing/"),
    Lever("R3", "AWS Reserved Instance (Standard)", "Rate", ("AWS",), 0.10, 0.72,
          "Low", "Medium", "Days",
          "Fixed instance attributes for the term",
          "Long-lived, unchanging instances uncovered by a commitment.",
          "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/reserved-instances-types.html"),
    Lever("R4", "AWS Convertible RI", "Rate", ("AWS",), 0.10, 0.66,
          "Low", "Low", "Days",
          "Commitment with the right to exchange attributes",
          "Steady spend where family may change; trade a little depth for flexibility.",
          "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/reserved-instances-types.html"),
    Lever("R5", "Azure Reservations", "Rate", ("Azure",), 0.10, 0.72,
          "Low", "Medium", "Days",
          "1- or 3-year reservation of a VM size in a region",
          "Commitment-eligible Standard usage with no reservation applied.",
          "https://learn.microsoft.com/en-us/azure/cost-management-billing/savings-plan/decide-between-savings-plan-reservation"),
    Lever("R6", "Azure Savings Plan for Compute", "Rate", ("Azure",), 0.10, 0.65,
          "Low", "Low", "Days",
          "Hourly compute-spend commitment for 1 or 3 years",
          "Dynamic compute footprint that a rigid reservation cannot track.",
          "https://learn.microsoft.com/en-us/azure/cost-management-billing/savings-plan/decide-between-savings-plan-reservation"),
    Lever("R7", "GCP Flexible (spend-based) CUD", "Rate", ("GCP",), 0.28, 0.46,
          "Low", "Low", "Days",
          "1-year (28%) or 3-year (46%) spend commitment",
          "Steady Compute/eligible spend with no committed-use discount.",
          "https://cloud.google.com/docs/cuds-spend-based"),
    Lever("R8", "GCP Resource-based CUD", "Rate", ("GCP",), 0.37, 0.70,
          "Low", "Medium", "Days",
          "Commit to vCPU/memory in a region",
          "Stable machine shapes; up to 57% compute / 70% memory-optimized.",
          "https://cloud.google.com/compute/docs/instances/committed-use-discounts-overview"),
    Lever("R9", "Spot / Preemptible / Azure Spot", "Rate", ALL, 0.60, 0.91,
          "Medium", "High", "Days",
          "Interruptible, stateless, checkpointable workloads",
          "Non-prod / fault-tolerant Standard compute not already priced Dynamic.",
          "https://aws.amazon.com/ec2/spot/"),
    Lever("R10", "EDP / MACC / Google commit", "Rate", ALL, 0.10, 0.35,
          "High", "Low", "Months",
          "$1M+/yr committed spend; procurement-led",
          "Large blended spend eligible for a negotiated enterprise agreement.",
          "https://aws.amazon.com/savingsplans/faqs/"),
    Lever("R11", "Azure Hybrid Benefit", "Rate", ("Azure",), 0.30, 0.76,
          "Low", "Low", "Hours",
          "Existing Windows Server / SQL licences with Software Assurance",
          "license-included SKUs where a licence you already own could be applied.",
          "https://azure.microsoft.com/en-us/pricing/offers/hybrid-benefit"),
    Lever("R12", "BYOL / licence mobility", "Rate", ALL, 0.20, 0.50,
          "Medium", "Medium", "Weeks",
          "Owned licences with mobility rights",
          "license-included compute that could run on owned entitlements.",
          "https://aws.amazon.com/windows/resources/licensemobility/"),
    Lever("R13", "Windows -> Linux migration", "Rate", ALL, 0.20, 0.40,
          "High", "Medium", "Months",
          "Workload portable off Windows",
          "Windows-licensed compute with a viable Linux target.",
          "https://aws.amazon.com/windows/products/ec2/migrate-to-linux/"),
    Lever("R14", "AWS Graviton / ARM", "Rate", ("AWS",), 0.10, 0.40,
          "Medium", "Medium", "Weeks",
          "ARM-compatible build/runtime",
          "x86 compute SKUs with an ARM (Graviton) equivalent; up to 40% better price-perf.",
          "https://aws.amazon.com/ec2/graviton/"),
    Lever("R15", "Azure Cobalt / Ampere ARM", "Rate", ("Azure",), 0.10, 0.40,
          "Medium", "Medium", "Weeks",
          "ARM-compatible build/runtime",
          "x86 compute SKUs with an Azure ARM equivalent.",
          "https://azure.microsoft.com/en-us/blog/azure-cobalt-100-based-virtual-machines-are-now-generally-available/"),
    Lever("R16", "GCP Axion (C4A) / Tau", "Rate", ("GCP",), 0.10, 0.40,
          "Medium", "Medium", "Weeks",
          "ARM-compatible build/runtime",
          "x86 compute SKUs with a Google Axion equivalent.",
          "https://cloud.google.com/products/compute/axion"),
    # ---- Usage optimization -----------------------------------------------
    Lever("U1", "Compute rightsizing", "Usage", ALL, 0.20, 0.40,
          "Medium", "Medium", "Days",
          "CPU/memory telemetry to confirm over-provisioning",
          "Instances whose utilisation sits far below the provisioned size.",
          "https://docs.aws.amazon.com/compute-optimizer/latest/ug/what-is-compute-optimizer.html"),
    Lever("U2", "RDS / Azure SQL / Cloud SQL rightsizing", "Usage", ALL, 0.20, 0.40,
          "Medium", "Medium", "Days",
          "Database CPU/IOPS telemetry",
          "Managed databases provisioned above their working set.",
          "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_BestPractices.html"),
    Lever("U3", "K8s requests/limits + bin-packing", "Usage", ALL, 0.20, 0.40,
          "Medium", "Medium", "Weeks",
          "Cluster utilisation metrics; VPA/HPA",
          "Nodes running well below capacity because requests are set too high.",
          "https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/"),
    Lever("U4", "Unattached EBS / managed disks", "Usage", ALL, 0.90, 1.00,
          "Low", "Low", "Hours",
          "Confirm the volume is truly orphaned",
          "Block-storage volumes billing with zero consumed quantity.",
          "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-volumes.html"),
    Lever("U5", "Idle load balancers", "Usage", ALL, 0.90, 1.00,
          "Low", "Low", "Hours",
          "Confirm no backend targets / traffic",
          "Load balancers billing with zero consumed quantity.",
          "https://docs.aws.amazon.com/compute-optimizer/latest/ug/view-idle-recommendations.html"),
    Lever("U6", "Unused Elastic / Public IPs", "Usage", ALL, 0.90, 1.00,
          "Low", "Low", "Hours",
          "Release the address",
          "Public IPs billing with zero consumed quantity (AWS $3.65/mo each since Feb-2024).",
          "https://aws.amazon.com/blogs/aws/new-aws-public-ipv4-address-charge-plus-public-ip-insights/"),
    Lever("U7", "Idle NAT gateways", "Usage", ALL, 0.90, 1.00,
          "Low", "Low", "Hours",
          "Confirm no route depends on it",
          "NAT gateways billing with zero consumed quantity (~$32/mo + $0.045/GB).",
          "https://aws.amazon.com/blogs/aws-cloud-financial-management/announcing-unused-nat-gateway-recommendations-in-aws-compute-optimizer/"),
    Lever("U8", "Orphaned snapshots / lifecycle", "Usage", ALL, 0.90, 1.00,
          "Low", "Low", "Hours",
          "Retention policy sign-off",
          "Snapshots billing beyond any retention need.",
          "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ebs-deleting-snapshot.html"),
    Lever("U9", "Stopped-but-billed resources", "Usage", ALL, 0.50, 1.00,
          "Low", "Low", "Hours",
          "Confirm the resource is abandoned",
          "Resources retaining billed storage/IP while stopped.",
          "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/how-ec2-instance-stop-starts.html"),
    Lever("U10", "Schedule / park non-prod", "Usage", ALL, 0.60, 0.70,
          "Low", "Low", "Days",
          "Confirm nights/weekends are idle",
          "Non-prod compute running 24x7 that could be parked (168h/wk -> ~50h/wk).",
          "https://docs.aws.amazon.com/solutions/latest/instance-scheduler-on-aws/solution-overview.html"),
    Lever("U11", "Autoscaling / consolidation", "Usage", ALL, 0.15, 0.35,
          "Medium", "Medium", "Weeks",
          "Elastic, horizontally scalable workload",
          "Fixed fleets sized for peak that could scale to demand.",
          "https://docs.aws.amazon.com/autoscaling/"),
    Lever("U12", "S3 Intelligent-Tiering / lifecycle", "Usage", ("AWS",), 0.40, 0.68,
          "Low", "Low", "Days",
          "Object access-pattern telemetry",
          "Object storage on the hot tier with cold access (IA -40%, Archive Instant -68%).",
          "https://aws.amazon.com/s3/storage-classes/intelligent-tiering/"),
    Lever("U13", "Azure Blob Cool / Cold / Archive", "Usage", ("Azure",), 0.30, 0.55,
          "Low", "Low", "Days",
          "Blob access-pattern telemetry",
          "Blob storage on Hot with cold access (Cold ~55% cheaper than Cool).",
          "https://learn.microsoft.com/en-us/azure/storage/blobs/access-tiers-overview"),
    Lever("U14", "GCS Nearline / Coldline / Archive / Autoclass", "Usage", ("GCP",), 0.30, 0.50,
          "Low", "Low", "Days",
          "Object access-pattern telemetry",
          "Standard-class objects with cold access.",
          "https://cloud.google.com/storage/docs/storage-classes"),
    Lever("U15", "gp2 -> gp3 EBS", "Usage", ("AWS",), 0.15, 0.20,
          "Low", "Low", "Hours",
          "Volumes within gp3 baseline IOPS/throughput",
          "gp2 volumes; migrating to gp3 saves up to 20% at equal or better performance.",
          "https://aws.amazon.com/blogs/storage/migrate-your-amazon-ebs-volumes-from-gp2-to-gp3-and-save-up-to-20-on-costs/"),
    Lever("U16", "Over-provisioned IOPS / throughput", "Usage", ALL, 0.15, 0.40,
          "Low", "Medium", "Days",
          "Provisioned vs consumed IOPS telemetry",
          "Volumes paying for provisioned performance they never use.",
          "https://docs.aws.amazon.com/ebs/latest/userguide/ebs-volume-types.html"),
    Lever("U17", "Previous-generation instance families", "Usage", ALL, 0.10, 0.15,
          "Low", "Low", "Days",
          "Current-gen equivalent available",
          "Previous-gen families (m4/c4/r4/t2, *_v2, n1-) with a cheaper current-gen swap.",
          "https://aws.amazon.com/ec2/previous-generation/"),
    Lever("U18", "CloudWatch / Log Analytics retention", "Usage", ALL, 0.20, 0.50,
          "Low", "Low", "Hours",
          "Retention-policy sign-off",
          "Log/metric data retained longer than any query or compliance need.",
          "https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/Working-with-log-groups-and-streams.html"),
    Lever("U19", "Data-transfer / egress", "Usage", ALL, 0.20, 0.60,
          "Medium", "Medium", "Weeks",
          "Traffic-flow analysis",
          "Egress via NAT ($0.045/GB) where a gateway endpoint ($0) or in-AZ path would serve.",
          "https://aws.amazon.com/blogs/architecture/overview-of-data-transfer-costs-for-common-architectures/"),
    Lever("U20", "Lambda power tuning", "Usage", ("AWS",), 0.30, 0.40,
          "Low", "Low", "Hours",
          "Representative invocation profile",
          "Functions on a memory setting far from their cost/perf optimum.",
          "https://github.com/alexcasalboni/aws-lambda-power-tuning"),
    # ---- Architecture -----------------------------------------------------
    Lever("C1", "Provisioned -> serverless DB", "Architecture", ALL, 0.30, 0.90,
          "High", "Medium", "Months",
          "Bursty / low-duty-cycle database workload",
          "Always-on DB under ~40% utilisation (Aurora Serverless v2 up to 90%; DynamoDB on-demand <100M ops/mo).",
          "https://aws.amazon.com/rds/aurora/serverless/"),
    Lever("C2", "Serverless vs always-on compute", "Architecture", ALL, 0.30, 0.80,
          "High", "Medium", "Months",
          "Event-driven / intermittent workload",
          "Always-on compute with low duty cycle that suits functions/containers-on-demand.",
          "https://aws.amazon.com/serverless/"),
    Lever("C3", "Managed-service consolidation", "Architecture", ALL, 0.15, 0.40,
          "High", "Medium", "Months",
          "Overlapping/self-managed services",
          "Self-managed stacks a managed equivalent would run cheaper.",
          "https://aws.amazon.com/products/"),
    Lever("C4", "Caching / CDN", "Architecture", ALL, 0.20, 0.60,
          "Medium", "Low", "Weeks",
          "Cacheable request mix",
          "Repeated origin fetches / egress a cache or CDN would absorb.",
          "https://aws.amazon.com/cloudfront/"),
    Lever("C5", "Multi-tenancy / pooling", "Architecture", ALL, 0.20, 0.50,
          "High", "High", "Months",
          "Tenant-isolatable workload",
          "Per-tenant dedicated infrastructure that could be pooled.",
          "https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/saas-lens.html"),
    Lever("C6", "Data lifecycle / retention policy", "Architecture", ALL, 0.15, 0.40,
          "Low", "Low", "Weeks",
          "Data-classification and retention rules",
          "Data kept indefinitely with no lifecycle transition or expiry.",
          "https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lifecycle-mgmt.html"),
    Lever("C7", "Region choice", "Architecture", ALL, 0.10, 0.30,
          "High", "Medium", "Months",
          "Latency / data-residency headroom",
          "Workloads in premium regions that a cheaper region could host.",
          "https://aws.amazon.com/about-aws/global-infrastructure/"),
    # ---- AI / GPU ---------------------------------------------------------
    Lever("G1", "GPU rightsizing", "AI/GPU", ALL, 0.20, 0.50,
          "Medium", "Medium", "Days",
          "GPU-utilisation telemetry",
          "Accelerators under-utilised for the model they serve.",
          "https://docs.aws.amazon.com/sagemaker/latest/dg/whatis.html"),
    Lever("G2", "Inference batching (vLLM / TensorRT-LLM)", "AI/GPU", ALL, 0.30, 0.90,
          "Medium", "Medium", "Weeks",
          "Batchable request stream",
          "Single-request inference where continuous batching would lift throughput 10-50x.",
          "https://docs.vllm.ai/en/latest/"),
    Lever("G3", "Model routing (small-model-first)", "AI/GPU", ALL, 0.30, 0.60,
          "Medium", "Low", "Weeks",
          "Task-complexity classifier / eval harness",
          "High-volume inference sending every request to the largest model.",
          "https://docs.aws.amazon.com/bedrock/latest/userguide/what-is-bedrock.html"),
    Lever("G4", "Prompt / context caching", "AI/GPU", ALL, 0.50, 0.90,
          "Low", "Low", "Days",
          "Repeated shared prefixes / system prompts",
          "Repeated context re-billed every call; cached tokens up to 90% off.",
          "https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html"),
    Lever("G5", "Token / thinking budgets", "AI/GPU", ALL, 0.15, 0.40,
          "Low", "Low", "Days",
          "Output-length and reasoning-budget controls",
          "Unbounded output / reasoning length inflating per-call token cost.",
          "https://docs.aws.amazon.com/bedrock/latest/userguide/inference-parameters.html"),
    Lever("G6", "Spot GPU", "AI/GPU", ALL, 0.60, 0.90,
          "Medium", "High", "Days",
          "Checkpointable training / batch inference",
          "Interruptible GPU workloads paying on-demand rates.",
          "https://aws.amazon.com/ec2/spot/"),
    Lever("G7", "Reserved GPU capacity", "AI/GPU", ALL, 0.20, 0.60,
          "Medium", "Low", "Weeks",
          "Predictable sustained GPU demand",
          "Steady GPU consumption paying on-demand rates.",
          "https://aws.amazon.com/ec2/capacityblocks/"),
    Lever("G8", "SageMaker Savings Plans", "AI/GPU", ("AWS",), 0.20, 0.64,
          "Low", "Low", "Days",
          "Steady SageMaker usage",
          "Uncommitted SageMaker compute; savings up to 64%.",
          "https://aws.amazon.com/savingsplans/faqs/"),
    Lever("G9", "Bedrock Provisioned Throughput vs On-Demand", "AI/GPU", ("AWS",), 0.40, 0.60,
          "Medium", "Low", "Weeks",
          "High-volume, predictable inference",
          "On-demand inference at a volume where provisioned throughput is 40-60% cheaper.",
          "https://docs.aws.amazon.com/bedrock/latest/userguide/prov-throughput.html"),
    Lever("G10", "Batch inference API", "AI/GPU", ALL, 0.50, 0.50,
          "Low", "Low", "Days",
          "Latency-tolerant workload",
          "Real-time inference for jobs that a 50%-off batch API could serve.",
          "https://docs.aws.amazon.com/bedrock/latest/userguide/batch-inference.html"),
]

LEVER_BY_ID: Dict[str, Lever] = {lv.id: lv for lv in LEVERS}


# ==========================================================================
# The opportunity -- one detected instance of a lever on this estate
# ==========================================================================


@dataclass
class Opportunity:
    lever_id: str
    lever_name: str
    category: str
    cloud: str
    scope: str
    monthly_savings: float
    annual_savings: float
    effort: str
    risk: str
    time_to_value: str
    confidence: float  # 0..1
    evidence: dict
    resource_count: int = 0
    resource_ids: tuple = ()


# Cloud-specific fractional discount rates (mirror connectors.demo so the demo
# numbers reconcile; these are also representative of published list discounts).
_COMMITMENT_RATE = {"AWS": 0.31, "Azure": 0.34, "GCP": 0.28}
_SPOT_DISCOUNT = {"AWS": 0.72, "Azure": 0.68, "GCP": 0.75}
_COMMITMENT_LEVER = {"AWS": "R1", "Azure": "R5", "GCP": "R7"}
_ARM_LEVER = {"AWS": "R14", "Azure": "R15", "GCP": "R16"}
_TIER_LEVER = {"AWS": "U12", "Azure": "U13", "GCP": "U14"}

# Idle-resource ResourceType -> lever.
_IDLE_LEVER = {
    "Volume": "U4",
    "LoadBalancer": "U5",
    "IpAddress": "U6",
    "Gateway": "U7",
    "Snapshot": "U8",
}

# Levers whose dollars are genuine *waste* -- money buying no business value.
# These feed kpi.cost_of_waste(usage_waste=...). Commitment waste is excluded:
# the KPI engine adds it separately, so counting it here would double it.
WASTE_LEVER_IDS = {"U4", "U5", "U6", "U7", "U8", "U10"}

MIN_MONTHLY_SAVINGS = 50.0


# ==========================================================================
# Frame helpers
# ==========================================================================


def _months(df: pd.DataFrame) -> int:
    """Distinct calendar months in the frame. The divisor that turns a
    window total into the monthly figure every KPI and roadmap expects."""
    if not len(df):
        return 1
    return max(1, int(df["ChargePeriodStart"].dt.to_period("M").nunique()))


def _skustr(df: pd.DataFrame) -> pd.Series:
    return df["SkuId"].astype("string").fillna("")


def _opp(
    lever_id: str,
    cloud: str,
    scope: str,
    monthly_savings: float,
    confidence: float,
    evidence: dict,
    resource_count: int = 0,
    resource_ids: tuple = (),
) -> Opportunity:
    lv = LEVER_BY_ID[lever_id]
    monthly = float(round(monthly_savings, 2))
    return Opportunity(
        lever_id=lv.id,
        lever_name=lv.name,
        category=lv.category,
        cloud=cloud,
        scope=scope,
        monthly_savings=monthly,
        annual_savings=round(monthly * 12.0, 2),
        effort=lv.effort,
        risk=lv.risk,
        time_to_value=lv.time_to_value,
        confidence=confidence,
        evidence=evidence,
        resource_count=resource_count,
        resource_ids=resource_ids,
    )


def _cloud_label(clouds) -> str:
    uniq = sorted({str(c) for c in clouds if pd.notna(c)})
    return ", ".join(uniq) if uniq else "Multi-cloud"


def _ids(series: pd.Series, limit: int = 5) -> tuple:
    vals = [str(v) for v in series.dropna().unique()[:limit]]
    return tuple(vals)


# ==========================================================================
# Detectors
#
# Each takes the FOCUS frame and returns a (possibly empty) list of
# Opportunity. They never mutate the frame and never import Streamlit.
# ==========================================================================


def _detect_commitment_gap(df: pd.DataFrame) -> List[Opportunity]:
    """Uncovered commitment-eligible usage -> a Savings Plan / Reservation / CUD.

    We commit to the *sustained monthly floor* -- the minimum uncovered monthly
    ListCost over the trailing six months -- never the peak. Committing to the
    peak is exactly how estates end up over-committed and generating the unused
    amortization that `_detect_commitment_waste` then has to flag. The floor is
    the amount we are confident will recur.
    """
    out: List[Opportunity] = []
    elig = df[
        (df["ChargeCategory"] == "Usage")
        & (df["CommitmentDiscountId"].isna())
        & (df["ServiceCategory"].isin(kpi.COMMITMENT_ELIGIBLE_CATEGORIES))
        & (df["PricingCategory"] == "Standard")
    ].copy()
    if not elig.empty:
        elig["month"] = elig["ChargePeriodStart"].dt.to_period("M")
        for cloud, g in elig.groupby("ProviderName", observed=True):
            monthly = g.groupby("month")[ODE].sum().sort_index()
            trailing = monthly.tail(6)
            if trailing.empty:
                continue
            floor = float(trailing.min())
            peak = float(trailing.max())
            rate = _COMMITMENT_RATE[str(cloud)]
            monthly_savings = floor * rate
            if monthly_savings < MIN_MONTHLY_SAVINGS:
                continue
            lever_id = _COMMITMENT_LEVER[str(cloud)]
            out.append(
                _opp(
                    lever_id,
                    str(cloud),
                    f"{cloud} commitment-eligible usage (uncovered)",
                    monthly_savings,
                    confidence=0.8,
                    evidence={
                        "sustained_monthly_floor_ode": round(floor, 2),
                        "peak_monthly_ode": round(peak, 2),
                        "months_observed": int(len(trailing)),
                        "commitment_discount_rate": rate,
                        "basis": "Committed to the trailing-6-month floor, not the peak, to avoid over-commitment.",
                    },
                    resource_count=int(g["ResourceId"].nunique()),
                )
            )
    return out


def _detect_commitment_waste(df: pd.DataFrame) -> List[Opportunity]:
    """Commitments that covered nothing. Not an estimate -- money already burned.

    FOCUS models this as a Usage row with CommitmentDiscountStatus == 'Unused'.
    The action is not "buy more" but "reduce or re-shape the commitment / lift
    utilization". Confidence 1.0: the bill states the number outright.
    """
    c = df[(df["CommitmentDiscountId"].notna()) & (df["CommitmentDiscountStatus"] == "Unused")]
    if c.empty:
        return []
    months = _months(df)
    out: List[Opportunity] = []
    for cloud, g in c.groupby("ProviderName", observed=True):
        monthly_savings = float(g[COST].sum()) / months
        if monthly_savings < MIN_MONTHLY_SAVINGS:
            continue
        lever_id = _COMMITMENT_LEVER[str(cloud)]
        out.append(
            _opp(
                lever_id,
                str(cloud),
                f"{cloud} unused commitment (reduce / improve utilization)",
                monthly_savings,
                confidence=1.0,
                evidence={
                    "amortized_unused_total": round(float(g[COST].sum()), 2),
                    "months_observed": months,
                    "action": "Right-size the commitment portfolio or raise coverage of eligible usage.",
                    "note": "Directly observed from CommitmentDiscountStatus=='Unused'; not modelled.",
                },
            )
        )
    return out


def _detect_idle_resources(df: pd.DataFrame) -> List[Opportunity]:
    """The universal idle signature: ConsumedQuantity == 0 while EffectiveCost > 0.

    A resource that consumed nothing yet still billed is, by definition, buying
    no value. This reads identically on every cloud because it uses only spec
    columns. Savings = 100% of the cost; confidence 0.9 (the one residual risk
    is a resource intentionally pre-provisioned, e.g. a DR standby).
    """
    idle = df[(df["ConsumedQuantity"] == 0) & (df[COST] > 0) & (df["ChargeCategory"] == "Usage")]
    if idle.empty:
        return []
    months = _months(df)
    out: List[Opportunity] = []
    for rtype, g in idle.groupby("ResourceType", observed=True):
        lever_id = _IDLE_LEVER.get(str(rtype))
        if lever_id is None:
            continue
        monthly_savings = float(g[COST].sum()) / months
        if monthly_savings < MIN_MONTHLY_SAVINGS:
            continue
        out.append(
            _opp(
                lever_id,
                _cloud_label(g["ProviderName"].unique()),
                f"Idle {rtype} resources",
                monthly_savings,
                confidence=0.9,
                evidence={
                    "resource_type": str(rtype),
                    "idle_signature": "ConsumedQuantity == 0 and EffectiveCost > 0",
                    "amortized_total": round(float(g[COST].sum()), 2),
                    "months_observed": months,
                },
                resource_count=int(g["ResourceId"].nunique()),
                resource_ids=_ids(g["ResourceId"]),
            )
        )
    return out


def _detect_gp2_volumes(df: pd.DataFrame) -> List[Opportunity]:
    """gp2 EBS volumes -> gp3. Up to 20% off at equal or better performance."""
    sku = _skustr(df)
    gp2 = df[sku.str.contains("gp2") & (df["ConsumedQuantity"] > 0)]
    if gp2.empty:
        return []
    months = _months(df)
    spend = float(gp2[COST].sum())
    monthly_savings = spend / months * 0.20
    if monthly_savings < MIN_MONTHLY_SAVINGS:
        return []
    return [
        _opp(
            "U15",
            _cloud_label(gp2["ProviderName"].unique()),
            "gp2 EBS volumes",
            monthly_savings,
            confidence=0.8,
            evidence={
                "gp2_amortized_spend": round(spend, 2),
                "months_observed": months,
                "savings_rate": 0.20,
                "note": "gp3 is a like-for-like swap; savings realised on migration.",
            },
            resource_count=int(gp2["ResourceId"].nunique()),
        )
    ]


def _detect_previous_generation(df: pd.DataFrame) -> List[Opportunity]:
    """Previous-generation instance families -> current gen. 10-15%.

    AWS m4/c4/r4/t2, Azure *_v2, GCP n1-. A current-gen swap is same-shape and
    typically cheaper for equal or better performance.
    """
    sku = _skustr(df)
    comp = df[df["ServiceCategory"] == "Compute"]
    sku_c = _skustr(comp)
    prev = comp[sku_c.str.contains(r"m4\.|c4\.|r4\.|t2\.|_v2|n1-", regex=True)]
    if prev.empty:
        return []
    months = _months(df)
    spend = float(prev[COST].sum())
    monthly_savings = spend / months * 0.125
    if monthly_savings < MIN_MONTHLY_SAVINGS:
        return []
    return [
        _opp(
            "U17",
            _cloud_label(prev["ProviderName"].unique()),
            "Previous-generation compute families",
            monthly_savings,
            confidence=0.7,
            evidence={
                "prev_gen_amortized_spend": round(spend, 2),
                "months_observed": months,
                "savings_rate": 0.125,
                "families": "m4/c4/r4/t2 (AWS), *_v2 (Azure), n1- (GCP)",
            },
            resource_count=int(prev["ResourceId"].nunique()),
        )
    ]


def _detect_nonprod_always_on(df: pd.DataFrame) -> List[Opportunity]:
    """Non-prod compute running 24x7 -> schedule/park nights + weekends.

    Assumption the bill cannot confirm: that nights and weekends are genuinely
    idle. Stated in the evidence. Where consumed quantity implies ~24x7 running
    (monthly hours >= 720 * 0.9), parking to a ~50h working week removes ~65% of
    the cost (168h/wk -> ~50h/wk).
    """
    if "tag_environment" not in df.columns:
        return []
    nonprod = df[
        (df["tag_environment"].isin(["nonprod", "dev", "test", "staging"]))
        & (df["ServiceCategory"] == "Compute")
        & (df["ChargeCategory"] == "Usage")
        & (df["ConsumedQuantity"] > 0)
    ].copy()
    if nonprod.empty:
        return []
    nonprod["month"] = nonprod["ChargePeriodStart"].dt.to_period("M")
    hours = nonprod.groupby(["ResourceId", "month"], observed=True)["ConsumedQuantity"].sum()
    always_on_ids = hours[hours >= 720 * 0.9].index.get_level_values("ResourceId").unique()
    running = nonprod[nonprod["ResourceId"].isin(always_on_ids)]
    if running.empty:
        return []
    months = _months(df)
    spend = float(running[COST].sum())
    monthly_savings = spend / months * 0.65
    if monthly_savings < MIN_MONTHLY_SAVINGS:
        return []
    return [
        _opp(
            "U10",
            _cloud_label(running["ProviderName"].unique()),
            "Non-prod compute running 24x7",
            monthly_savings,
            confidence=0.7,
            evidence={
                "nonprod_compute_amortized_spend": round(spend, 2),
                "months_observed": months,
                "savings_rate": 0.65,
                "assumption": "Nights/weekends are idle -- requires access-pattern confirmation before parking.",
                "schedule": "168h/wk -> ~50h/wk",
            },
            resource_count=int(running["ResourceId"].nunique()),
        )
    ]


def _detect_spot_candidates(df: pd.DataFrame) -> List[Opportunity]:
    """Non-prod compute on Standard pricing -> Spot / preemptible.

    Savings = the cloud's spot discount x a 40% adoption haircut: not every
    non-prod workload tolerates interruption, so we do not claim the full
    fleet. Rows already priced 'Dynamic' are Spot already and excluded.
    """
    cand = df[
        (df["tag_environment"].isin(["nonprod", "dev", "test", "staging"]))
        & (df["ServiceCategory"] == "Compute")
        & (df["ChargeCategory"] == "Usage")
        & (df["PricingCategory"] == "Standard")
        & (df["ConsumedQuantity"] > 0)
    ]
    if cand.empty:
        return []
    months = _months(df)
    out: List[Opportunity] = []
    for cloud, g in cand.groupby("ProviderName", observed=True):
        spend = float(g[COST].sum())
        rate = _SPOT_DISCOUNT[str(cloud)] * 0.40
        monthly_savings = spend / months * rate
        if monthly_savings < MIN_MONTHLY_SAVINGS:
            continue
        out.append(
            _opp(
                "R9",
                str(cloud),
                f"{cloud} non-prod compute (Spot-eligible)",
                monthly_savings,
                confidence=0.6,
                evidence={
                    "nonprod_standard_compute_spend": round(spend, 2),
                    "months_observed": months,
                    "spot_discount": _SPOT_DISCOUNT[str(cloud)],
                    "adoption_haircut": 0.40,
                    "assumption": "Only ~40% of non-prod compute is interruption-tolerant.",
                },
                resource_count=int(g["ResourceId"].nunique()),
            )
        )
    return out


def _detect_arm_migration(df: pd.DataFrame) -> List[Opportunity]:
    """x86 compute -> ARM (Graviton / Cobalt / Axion). ~20% x a 35% haircut.

    Portability is workload-specific: not everything recompiles cleanly, so we
    apply a 35% adoption haircut to the ~20% price-performance gain rather than
    claim the whole fleet moves.
    """
    comp = df[(df["ServiceCategory"] == "Compute") & (df["ChargeCategory"] == "Usage") & (df["ConsumedQuantity"] > 0)]
    if comp.empty:
        return []
    sku = _skustr(comp)
    # ARM SKUs already in the estate -- exclude them.
    already_arm = sku.str.contains(r"m7g|c4a|D4as|Cobalt|Ampere|Axion", regex=True)
    x86 = comp[~already_arm]
    if x86.empty:
        return []
    months = _months(df)
    out: List[Opportunity] = []
    for cloud, g in x86.groupby("ProviderName", observed=True):
        spend = float(g[COST].sum())
        monthly_savings = spend / months * 0.20 * 0.35
        if monthly_savings < MIN_MONTHLY_SAVINGS:
            continue
        out.append(
            _opp(
                _ARM_LEVER[str(cloud)],
                str(cloud),
                f"{cloud} x86 compute (ARM-migratable)",
                monthly_savings,
                confidence=0.6,
                evidence={
                    "x86_compute_spend": round(spend, 2),
                    "months_observed": months,
                    "price_perf_gain": 0.20,
                    "portability_haircut": 0.35,
                    "assumption": "Only ~35% of x86 workloads are cleanly ARM-portable.",
                },
                resource_count=int(g["ResourceId"].nunique()),
            )
        )
    return out


def _detect_license_included(df: pd.DataFrame) -> List[Opportunity]:
    """license-included SKUs -> Azure Hybrid Benefit (Azure) or BYOL (elsewhere).

    Split per cloud on purpose. Azure Hybrid Benefit is an Azure-only programme;
    naming it against AWS or GCP spend would send an engineer hunting for a
    lever that does not exist there. Off Azure the instrument is licence
    mobility / BYOL (R12) -- a wider savings band, more compliance risk, and a
    lower confidence because entitlement mobility rights are not visible in a
    bill.
    """
    sku = _skustr(df)
    lic = df[sku.str.contains("license-included") & (df["ChargeCategory"] == "Usage")]
    if lic.empty:
        return []

    months = _months(df)
    out: List[Opportunity] = []

    for cloud, grp in lic.groupby("ProviderName", observed=True):
        spend = float(grp[COST].sum())
        if spend <= 0:
            continue

        if str(cloud) == "Azure":
            lever_id, rate, conf = "R11", 0.36, 0.65  # AHB, Windows Server average
            assumption = (
                "Owned Windows Server / SQL Server entitlements with active Software "
                "Assurance are available to apply."
            )
        else:
            lever_id, rate, conf = "R12", 0.30, 0.50  # mid of the 20-50% BYOL band
            assumption = (
                "Owned licences carry mobility rights to this cloud. Amazon RDS is "
                "licence-included only, so BYOL does not apply to that spend."
            )

        monthly_savings = spend / months * rate
        if monthly_savings < MIN_MONTHLY_SAVINGS:
            continue

        out.append(
            _opp(
                lever_id,
                str(cloud),
                "License-included compute",
                monthly_savings,
                confidence=conf,
                evidence={
                    "license_included_spend": round(spend, 2),
                    "months_observed": months,
                    "savings_rate": rate,
                    "assumption": assumption,
                },
                resource_count=int(grp["ResourceId"].nunique()),
            )
        )
    return out


def _detect_storage_tiering(df: pd.DataFrame) -> List[Opportunity]:
    """Hot-tier storage with (assumed) cold data -> intelligent tiering / archive.

    The bill does NOT carry object access patterns, so this cannot be proven
    from FOCUS alone. We approximate: prod block/object storage with material
    spend, and assume 25% of it is cold, saving 30-40% on that slice. Confidence
    is deliberately 0.5 and the evidence says telemetry is required to confirm --
    we do not overclaim a number the invoice cannot support.
    """
    stor = df[
        (df["ServiceCategory"] == "Storage")
        & (df["tag_environment"] == "prod")
        & (df["ResourceType"].isin(["Volume", "Object"]))
        & (df["ConsumedQuantity"] > 0)
    ]
    if stor.empty:
        return []
    months = _months(df)
    out: List[Opportunity] = []
    cold_fraction = 0.25
    tier_saving = 0.35
    for cloud, g in stor.groupby("ProviderName", observed=True):
        lever_id = _TIER_LEVER.get(str(cloud))
        if lever_id is None:
            continue
        spend = float(g[COST].sum())
        monthly_savings = spend / months * cold_fraction * tier_saving
        if monthly_savings < MIN_MONTHLY_SAVINGS:
            continue
        out.append(
            _opp(
                lever_id,
                str(cloud),
                f"{cloud} prod storage (cold-tier candidate)",
                monthly_savings,
                confidence=0.5,
                evidence={
                    "prod_storage_spend": round(spend, 2),
                    "months_observed": months,
                    "assumed_cold_fraction": cold_fraction,
                    "tier_saving_on_cold": tier_saving,
                    "caveat": "Access-pattern telemetry is required to confirm which objects are cold; this is an upper-bound estimate.",
                },
                resource_count=int(g["ResourceId"].nunique()),
            )
        )
    return out


def _detect_ai_opportunities(df: pd.DataFrame) -> List[Opportunity]:
    """Fast-growing AI spend -> model routing + prompt caching (+ provisioned).

    Flags only when AI spend is growing -- trailing-6-month average MoM > 15% --
    because a small, flat AI line does not justify the engineering. The bill
    cannot see prompt structure or model mix, so confidence is 0.45 and the
    savings are a blended estimate, split across the applicable levers.
    """
    ai = df[(df["ServiceCategory"] == "AI and Machine Learning") & (df["ChargeCategory"] == "Usage")]
    if ai.empty:
        return []
    monthly = (
        ai.assign(month=ai["ChargePeriodStart"].dt.to_period("M"))
        .groupby("month")[COST]
        .sum()
        .sort_index()
    )
    if len(monthly) < 3:
        return []
    trailing_mom = monthly.pct_change().tail(6)
    avg_mom = float(trailing_mom.mean()) if len(trailing_mom) else 0.0
    if avg_mom <= 0.15:
        return []
    recent_monthly = float(monthly.iloc[-1])
    if recent_monthly <= 0:
        return []
    cloud = _cloud_label(ai["ProviderName"].unique())
    # Blended 40% opportunity, apportioned across the applicable levers.
    splits = [("G3", 0.20), ("G4", 0.12), ("G9", 0.08)]
    out: List[Opportunity] = []
    for lever_id, rate in splits:
        lv = LEVER_BY_ID[lever_id]
        eligible_cloud = cloud if "AWS" not in lv.clouds or "AWS" in cloud else cloud
        # G9 is AWS-only; only surface it if there is AWS AI spend.
        if lever_id == "G9" and "AWS" not in cloud:
            continue
        monthly_savings = recent_monthly * rate
        if monthly_savings < MIN_MONTHLY_SAVINGS:
            continue
        out.append(
            _opp(
                lever_id,
                cloud,
                f"AI/ML spend growing {avg_mom * 100:.0f}%/mo -- {lv.name}",
                monthly_savings,
                confidence=0.45,
                evidence={
                    "recent_monthly_ai_spend": round(recent_monthly, 2),
                    "trailing_6mo_avg_mom_pct": round(avg_mom * 100, 1),
                    "blended_savings_rate_for_lever": rate,
                    "total_blended_target": 0.40,
                    "caveat": "Billing cannot see prompt structure or model mix; savings are a growth-triggered estimate.",
                },
            )
        )
    return out


def _detect_untagged(df: pd.DataFrame) -> List[Opportunity]:
    """Untagged spend -> a governance prerequisite, not a dollar saving.

    Reported with monthly_savings == 0 so the roadmap surfaces it as the thing
    that must be fixed before allocation-driven optimization can be trusted.
    You cannot optimize what you cannot attribute.
    """
    if "tag_application" not in df.columns:
        return []
    usage = df[df["ChargeCategory"] == "Usage"]
    if usage.empty:
        return []
    total = float(usage[COST].sum())
    unalloc = float(usage.loc[usage["tag_application"] == "Unallocated", COST].sum())
    if total == 0 or unalloc == 0:
        return []
    months = _months(df)
    pct = unalloc / total * 100.0
    return [
        Opportunity(
            lever_id="U18",  # closest catalog lever; this is a governance flag
            lever_name="Tag / allocation coverage remediation",
            category="Usage",
            cloud=_cloud_label(usage.loc[usage["tag_application"] == "Unallocated", "ProviderName"].unique()),
            scope="Untagged / unallocated spend",
            monthly_savings=0.0,
            annual_savings=0.0,
            effort="Medium",
            risk="Low",
            time_to_value="Weeks",
            confidence=0.9,
            evidence={
                "unallocated_amortized_total": round(unalloc, 2),
                "unallocated_monthly": round(unalloc / months, 2),
                "unallocated_pct_of_usage": round(pct, 1),
                "why_zero_savings": "This is a prerequisite: allocation must be fixed before chargeback and app-level optimization can be trusted.",
            },
            resource_count=0,
        )
    ]


_DETECTORS = [
    _detect_commitment_gap,
    _detect_commitment_waste,
    _detect_idle_resources,
    _detect_gp2_volumes,
    _detect_previous_generation,
    _detect_nonprod_always_on,
    _detect_spot_candidates,
    _detect_arm_migration,
    _detect_license_included,
    _detect_storage_tiering,
    _detect_ai_opportunities,
    _detect_untagged,
]


# ==========================================================================
# Orchestration + rollups
# ==========================================================================


def detect_all(focus_df: pd.DataFrame) -> List[Opportunity]:
    """Run every detector, drop sub-$50/mo dollar opportunities, sort by value.

    Governance flags (monthly_savings == 0) are kept regardless of the floor --
    they are prerequisites, not dollar opportunities.
    """
    opps: List[Opportunity] = []
    for det in _DETECTORS:
        opps.extend(det(focus_df))
    opps = [o for o in opps if o.monthly_savings >= MIN_MONTHLY_SAVINGS or o.monthly_savings == 0.0]
    opps.sort(key=lambda o: o.annual_savings, reverse=True)
    return opps


def opportunities_frame(opps: List[Opportunity]) -> pd.DataFrame:
    if not opps:
        return pd.DataFrame(
            columns=[
                "lever_id", "lever_name", "category", "cloud", "scope",
                "monthly_savings", "annual_savings", "effort", "risk",
                "time_to_value", "confidence", "resource_count",
            ]
        )
    rows = [
        {
            "lever_id": o.lever_id,
            "lever_name": o.lever_name,
            "category": o.category,
            "cloud": o.cloud,
            "scope": o.scope,
            "monthly_savings": o.monthly_savings,
            "annual_savings": o.annual_savings,
            "effort": o.effort,
            "risk": o.risk,
            "time_to_value": o.time_to_value,
            "confidence": o.confidence,
            "resource_count": o.resource_count,
        }
        for o in opps
    ]
    return pd.DataFrame(rows)


def usage_waste_total(opps: List[Opportunity]) -> float:
    """Monthly usage waste -- idle resources + parked-but-running non-prod.

    Feeds kpi.cost_of_waste(df, usage_waste=usage_waste_total(opps)). Commitment
    waste is deliberately excluded here: the KPI engine adds it from the frame
    directly, so including it would double-count.
    """
    return float(sum(o.monthly_savings for o in opps if o.lever_id in WASTE_LEVER_IDS))


def savings_by_category(opps: List[Opportunity]) -> pd.DataFrame:
    if not opps:
        return pd.DataFrame(columns=["category", "monthly_savings", "annual_savings", "opportunities"])
    df = pd.DataFrame(
        [{"category": o.category, "monthly_savings": o.monthly_savings, "annual_savings": o.annual_savings} for o in opps]
    )
    out = (
        df.groupby("category", as_index=False)
        .agg(monthly_savings=("monthly_savings", "sum"), annual_savings=("annual_savings", "sum"), opportunities=("category", "size"))
        .sort_values("annual_savings", ascending=False, ignore_index=True)
    )
    return out


def roadmap(opps: List[Opportunity]) -> pd.DataFrame:
    """Sequence opportunities into execution waves with a cumulative total.

    Wave 1 = quick wins (effort Low AND risk Low). Wave 2 = anything else at Low
    or Medium effort. Wave 3 = the rest (High effort). Within a wave, order by
    annual savings. Governance prerequisites (zero-dollar) are pulled into Wave 1
    because they gate everything that follows.
    """
    def wave_of(o: Opportunity) -> int:
        if o.monthly_savings == 0.0:
            return 1  # prerequisite -- do it first
        if o.effort == "Low" and o.risk == "Low":
            return 1
        if o.effort in ("Low", "Medium"):
            return 2
        return 3

    rows = []
    for o in opps:
        rows.append(
            {
                "wave": wave_of(o),
                "lever_id": o.lever_id,
                "lever": o.lever_name,
                "scope": o.scope,
                "cloud": o.cloud,
                "monthly_savings": o.monthly_savings,
                "annual_savings": o.annual_savings,
                "effort": o.effort,
                "risk": o.risk,
                "time_to_value": o.time_to_value,
                "confidence": o.confidence,
            }
        )
    if not rows:
        return pd.DataFrame(
            columns=[
                "wave", "lever_id", "lever", "scope", "cloud", "monthly_savings",
                "annual_savings", "effort", "risk", "time_to_value", "confidence",
                "cumulative_annual_savings",
            ]
        )
    df = pd.DataFrame(rows).sort_values(
        ["wave", "annual_savings"], ascending=[True, False], ignore_index=True
    )
    df["cumulative_annual_savings"] = df["annual_savings"].cumsum()
    return df


def lever_catalog_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": lv.id,
                "name": lv.name,
                "category": lv.category,
                "clouds": ", ".join(lv.clouds),
                "savings_low": lv.savings_low,
                "savings_high": lv.savings_high,
                "effort": lv.effort,
                "risk": lv.risk,
                "time_to_value": lv.time_to_value,
                "prerequisites": lv.prerequisites,
                "detection": lv.detection,
                "source_url": lv.source_url,
            }
            for lv in LEVERS
        ]
    )


# ==========================================================================
# Rate-optimization impact on ESR + the waste definition
# ==========================================================================


def effective_savings_rate_uplift(focus_df: pd.DataFrame, opps: List[Opportunity]) -> dict:
    """What the Effective Savings Rate would become if the Rate levers executed.

    ESR = (ODE - EffectiveCost) / ODE, computed by kpi over the window. Executing
    the rate levers (Savings Plans, Reservations, CUDs, Spot, ARM, licence) lowers
    the effective cost by their savings; we project the new ESR from that. Usage
    and architecture levers are excluded -- they cut usage, not the rate, and so
    move a different lever of the same equation.
    """
    ode = kpi.on_demand_equivalent(focus_df)
    usage = focus_df[focus_df["ChargeCategory"] == "Usage"]
    effective = float(usage[COST].sum())
    current = kpi.effective_savings_rate_pct(focus_df)

    months = _months(focus_df)
    rate_monthly = sum(o.monthly_savings for o in opps if o.category == "Rate")
    rate_window = rate_monthly * months
    rate_annual = rate_monthly * 12.0

    if ode <= 0:
        return {
            "current_esr_pct": current,
            "projected_esr_pct": current,
            "uplift_pts": 0.0,
            "rate_savings_annual": round(rate_annual, 2),
        }

    projected = (ode - max(effective - rate_window, 0.0)) / ode * 100.0
    uplift = None if current is None else projected - current
    return {
        "current_esr_pct": None if current is None else round(current, 2),
        "projected_esr_pct": round(projected, 2),
        "uplift_pts": None if uplift is None else round(uplift, 2),
        "rate_savings_annual": round(rate_annual, 2),
    }


def waste_definition() -> str:
    """The FinOps definition of waste, with the benchmark it is measured against."""
    return (
        "Cloud waste is spend that produces no business value -- resources that are "
        "idle, orphaned, over-provisioned, redundant, or committed-but-unused. It is "
        "the complement of value, not of usage: a fully-utilised but oversized instance "
        "is still partly waste. Flexera's State of the Cloud report puts self-estimated "
        "waste at roughly 27-32% of IaaS/PaaS spend annually; ad-hoc organisations "
        "without a FinOps practice typically run 35-40%, while mature FinOps programs "
        "hold it to 20-25%. The two components this platform measures directly are "
        "commitment waste (unused RIs/SPs/CUDs, provable from FOCUS alone) and usage "
        "waste (idle and parked-but-running resources, surfaced by the detectors here)."
    )
