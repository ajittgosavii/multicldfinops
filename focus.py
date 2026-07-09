"""FOCUS -- the FinOps Open Cost and Usage Specification.

This module is the *lingua franca* of the platform. Every connector, whatever
it talks to -- an AWS Data Export, an Azure Cost Management export, a GCP
BigQuery billing view, Cloudability's OLAP API, a Finout MegaFilter query --
must return a DataFrame conforming to `SCHEMA` below. Every analytics engine
reads only that DataFrame. Nothing downstream of a connector knows or cares
which cloud or which vendor the row came from.

That is the whole architectural bet: when Con Edison procures a FinOps tool,
we write one connector, and every dashboard, forecast, and optimization
detector works unchanged.

Canonical version
-----------------
We model **FOCUS 1.2 Cost and Usage** as the canonical schema, because that is
what AWS, Azure and GCP actually emit today:

    AWS    Data Exports -> "FOCUS 1.0 with AWS columns" (GA 2024-11-25)
                           "FOCUS 1.2 with AWS columns" (GA 2025-11-19)
    Azure  Cost Management exports -> "Cost and usage details (FOCUS)" 1.0 / 1.2
    GCP    FOCUS Cloud Billing export to BigQuery -> 1.0 (registry lists 1.2)

FOCUS 1.3 (ratified Dec 2025) columns are carried as OPTIONAL extensions --
notably the split-cost-allocation columns and the ServiceProviderName /
HostProviderName pair that deprecates ProviderName / PublisherName. We keep
`ProviderName` as the working column and treat `ServiceProviderName` as an
alias on ingest, so a 1.3 feed and a 1.0 feed land in the same frame.

Spec: https://focus.finops.org/focus-specification/
Changelog: https://focus.finops.org/changelog/
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set

import pandas as pd

FOCUS_CANONICAL_VERSION = "1.2"
FOCUS_SUPPORTED_VERSIONS = ["1.0", "1.0-preview", "1.1", "1.2", "1.3"]


# ==========================================================================
# Feature levels (how the spec describes a column's existence requirement)
# ==========================================================================

MANDATORY = "Mandatory"
CONDITIONAL = "Conditional"
RECOMMENDED = "Recommended"
OPTIONAL = "Optional"


@dataclass(frozen=True)
class Column:
    name: str
    feature_level: str
    dtype: str  # 'string' | 'decimal' | 'datetime' | 'json'
    nullable: bool = True
    since: str = "1.0"
    note: str = ""


# ==========================================================================
# The Cost and Usage dataset -- FOCUS 1.2 normative columns
# ==========================================================================

SCHEMA: List[Column] = [
    # --- Billing account & period -------------------------------------
    Column("BillingAccountId", MANDATORY, "string", False),
    Column("BillingAccountName", MANDATORY, "string", False),
    Column("BillingAccountType", CONDITIONAL, "string", True, since="1.2"),
    Column("BillingCurrency", MANDATORY, "string", False),
    Column("BillingPeriodStart", MANDATORY, "datetime", False),
    Column("BillingPeriodEnd", MANDATORY, "datetime", False),
    Column("InvoiceId", MANDATORY, "string", True, since="1.2"),
    Column("InvoiceIssuerName", MANDATORY, "string", False),
    Column("SubAccountId", CONDITIONAL, "string"),
    Column("SubAccountName", CONDITIONAL, "string"),
    Column("SubAccountType", CONDITIONAL, "string", since="1.2"),
    # --- Charge classification -----------------------------------------
    Column("ChargeCategory", MANDATORY, "string", False),
    Column(
        "ChargeClass",
        MANDATORY,
        "string",
        True,
        note="Nullable. The only non-null value is 'Correction'.",
    ),
    Column("ChargeDescription", MANDATORY, "string", False),
    Column("ChargeFrequency", RECOMMENDED, "string"),
    Column("ChargePeriodStart", MANDATORY, "datetime", False),
    Column("ChargePeriodEnd", MANDATORY, "datetime", False),
    # --- Costs -----------------------------------------------------------
    Column("BilledCost", MANDATORY, "decimal", False),
    Column(
        "EffectiveCost",
        MANDATORY,
        "decimal",
        False,
        note="Amortized. THE column for every executive KPI.",
    ),
    Column("ListCost", MANDATORY, "decimal", False),
    Column("ContractedCost", MANDATORY, "decimal", False),
    Column("ContractedUnitPrice", CONDITIONAL, "decimal"),
    Column("ListUnitPrice", CONDITIONAL, "decimal"),
    # --- Pricing ---------------------------------------------------------
    Column("PricingCategory", CONDITIONAL, "string"),
    Column("PricingCurrency", CONDITIONAL, "string", since="1.2"),
    Column("PricingCurrencyContractedUnitPrice", CONDITIONAL, "decimal", since="1.2"),
    Column("PricingCurrencyEffectiveCost", CONDITIONAL, "decimal", since="1.2"),
    Column("PricingCurrencyListUnitPrice", CONDITIONAL, "decimal", since="1.2"),
    Column("PricingQuantity", CONDITIONAL, "decimal"),
    Column("PricingUnit", CONDITIONAL, "string"),
    # --- Quantity ---------------------------------------------------------
    Column("ConsumedQuantity", CONDITIONAL, "decimal"),
    Column("ConsumedUnit", CONDITIONAL, "string"),
    # --- Commitment discounts (RI / SP / CUD) -----------------------------
    Column("CommitmentDiscountCategory", CONDITIONAL, "string"),
    Column("CommitmentDiscountId", CONDITIONAL, "string"),
    Column("CommitmentDiscountName", CONDITIONAL, "string"),
    Column("CommitmentDiscountQuantity", CONDITIONAL, "decimal", since="1.1"),
    Column("CommitmentDiscountStatus", CONDITIONAL, "string"),
    Column("CommitmentDiscountType", CONDITIONAL, "string", note="Free-form; no closed enum."),
    Column("CommitmentDiscountUnit", CONDITIONAL, "string", since="1.1"),
    # --- Capacity reservations --------------------------------------------
    Column("CapacityReservationId", CONDITIONAL, "string", since="1.1"),
    Column("CapacityReservationStatus", CONDITIONAL, "string", since="1.1"),
    # --- Provider / service ------------------------------------------------
    Column("ProviderName", MANDATORY, "string", False, note="Deprecated in 1.3."),
    Column("PublisherName", CONDITIONAL, "string", note="Deprecated in 1.3."),
    Column("ServiceCategory", MANDATORY, "string", False),
    Column("ServiceName", MANDATORY, "string", False),
    Column("ServiceSubcategory", RECOMMENDED, "string", since="1.1"),
    # --- Resource ------------------------------------------------------------
    Column("ResourceId", CONDITIONAL, "string"),
    Column("ResourceName", CONDITIONAL, "string"),
    Column("ResourceType", CONDITIONAL, "string"),
    Column("RegionId", CONDITIONAL, "string"),
    Column("RegionName", CONDITIONAL, "string"),
    Column("AvailabilityZone", RECOMMENDED, "string"),
    # --- SKU -----------------------------------------------------------------
    Column("SkuId", CONDITIONAL, "string"),
    Column("SkuMeter", CONDITIONAL, "string", since="1.1"),
    Column("SkuPriceDetails", CONDITIONAL, "json", since="1.1"),
    Column("SkuPriceId", CONDITIONAL, "string"),
    # --- Metadata --------------------------------------------------------------
    Column("Tags", CONDITIONAL, "json"),
]

# FOCUS 1.3 extensions. Optional here so a 1.0/1.2 feed validates cleanly.
SCHEMA_13_EXTENSIONS: List[Column] = [
    Column("ServiceProviderName", OPTIONAL, "string", since="1.3", note="Replaces ProviderName."),
    Column("HostProviderName", OPTIONAL, "string", since="1.3", note="Replaces PublisherName."),
    Column("AllocatedMethodId", OPTIONAL, "string", since="1.3"),
    Column("AllocatedMethodDetails", OPTIONAL, "json", since="1.3"),
    Column("AllocatedResourceId", OPTIONAL, "string", since="1.3"),
    Column("AllocatedResourceName", OPTIONAL, "string", since="1.3"),
    Column("AllocatedTags", OPTIONAL, "json", since="1.3"),
    Column("ContractApplied", OPTIONAL, "string", since="1.3"),
]

ALL_COLUMNS: List[Column] = SCHEMA + SCHEMA_13_EXTENSIONS
COLUMN_BY_NAME: Dict[str, Column] = {c.name: c for c in ALL_COLUMNS}

MANDATORY_COLUMNS: List[str] = [c.name for c in SCHEMA if c.feature_level == MANDATORY]
COLUMN_NAMES: List[str] = [c.name for c in SCHEMA]


# ==========================================================================
# Closed enumerations (verbatim from the spec)
# ==========================================================================

CHARGE_CATEGORY: Set[str] = {"Usage", "Purchase", "Tax", "Credit", "Adjustment"}

# ChargeClass is nullable and has exactly one non-null value.
CHARGE_CLASS: Set[str] = {"Correction"}

CHARGE_FREQUENCY: Set[str] = {"One-Time", "Recurring", "Usage-Based"}

SERVICE_CATEGORY: Set[str] = {
    "AI and Machine Learning",
    "Analytics",
    "Business Applications",
    "Compute",
    "Databases",
    "Developer Tools",
    "Multicloud",
    "Identity",
    "Integration",
    "Internet of Things",
    "Management and Governance",
    "Media",
    "Migration",
    "Mobile",
    "Networking",
    "Security",
    "Storage",
    "Web",
    "Other",
}

SERVICE_SUBCATEGORY: Dict[str, List[str]] = {
    "AI and Machine Learning": [
        "AI Platforms",
        "Bots",
        "Generative AI",
        "Machine Learning",
        "Natural Language Processing",
        "Other (AI and Machine Learning)",
    ],
    "Analytics": [
        "Analytics Platforms",
        "Business Intelligence",
        "Data Processing",
        "Search",
        "Streaming Analytics",
        "Other (Analytics)",
    ],
    "Business Applications": [
        "Productivity and Collaboration",
        "Other (Business Applications)",
    ],
    "Compute": [
        "Containers",
        "End User Computing",
        "Quantum Compute",
        "Serverless Compute",
        "Virtual Machines",
        "Other (Compute)",
    ],
    "Databases": [
        "Caching",
        "Data Warehouses",
        "Ledger Databases",
        "NoSQL Databases",
        "Relational Databases",
        "Time Series Databases",
        "Other (Databases)",
    ],
    "Developer Tools": [
        "Developer Platforms",
        "Continuous Integration and Deployment",
        "Development Environments",
        "Source Code Management",
        "Quality Assurance",
        "Other (Developer Tools)",
    ],
    "Identity": ["Identity and Access Management", "Other (Identity)"],
    "Integration": [
        "API Management",
        "Messaging",
        "Workflow Orchestration",
        "Other (Integration)",
    ],
    "Internet of Things": ["IoT Analytics", "IoT Platforms", "Other (Internet of Things)"],
    "Management and Governance": [
        "Architecture",
        "Compliance",
        "Cost Management",
        "Data Governance",
        "Disaster Recovery",
        "Endpoint Management",
        "Observability",
        "Support",
        "Other (Management and Governance)",
    ],
    "Media": [
        "Content Creation",
        "Gaming",
        "Media Streaming",
        "Mixed Reality",
        "Other (Media)",
    ],
    "Migration": ["Data Migration", "Resource Migration", "Other (Migration)"],
    "Mobile": ["Other (Mobile)"],
    "Multicloud": ["Multicloud Integration", "Other (Multicloud)"],
    "Networking": [
        "Application Networking",
        "Content Delivery",
        "Network Connectivity",
        "Network Infrastructure",
        "Network Routing",
        "Network Security",
        "Other (Networking)",
    ],
    "Security": [
        "Secret Management",
        "Security Posture Management",
        "Threat Detection and Response",
        "Other (Security)",
    ],
    "Storage": [
        "Backup Storage",
        "Block Storage",
        "File Storage",
        "Object Storage",
        "Storage Platforms",
        "Other (Storage)",
    ],
    "Web": ["Application Platforms", "Other (Web)"],
    "Other": ["Other (Other)"],
}

PRICING_CATEGORY: Set[str] = {"Standard", "Dynamic", "Committed", "Other"}

COMMITMENT_DISCOUNT_CATEGORY: Set[str] = {"Spend", "Usage"}
COMMITMENT_DISCOUNT_STATUS: Set[str] = {"Used", "Unused"}
CAPACITY_RESERVATION_STATUS: Set[str] = {"Used", "Unused"}

# CommitmentDiscountType is free-form. These are the values the three
# hyperscalers actually emit -- used for grouping, never for validation.
KNOWN_COMMITMENT_TYPES: List[str] = [
    "Reserved Instance",
    "Savings Plan",
    "Committed Use Discount",
    "Reservation",
]

ENUMS: Dict[str, Set[str]] = {
    "ChargeCategory": CHARGE_CATEGORY,
    "ChargeClass": CHARGE_CLASS,
    "ChargeFrequency": CHARGE_FREQUENCY,
    "ServiceCategory": SERVICE_CATEGORY,
    "PricingCategory": PRICING_CATEGORY,
    "CommitmentDiscountCategory": COMMITMENT_DISCOUNT_CATEGORY,
    "CommitmentDiscountStatus": COMMITMENT_DISCOUNT_STATUS,
    "CapacityReservationStatus": CAPACITY_RESERVATION_STATUS,
}


# ==========================================================================
# Canonical allocation tags
#
# FOCUS says nothing about *which* tags exist -- that is an enterprise
# decision. These are the keys we normalize every provider's tags/labels onto.
# Provider constraints worth remembering (they bite at ingest, not here):
#   AWS   50 tags/resource; up to 500 *activated* cost-allocation tag keys;
#         activation is NOT retroactive.
#   Azure 50 name/value pairs per resource/RG/subscription; tag inheritance is
#         a Cost Management setting applied to usage records, not resources.
#   GCP   64 labels; key and value both <= 63 chars, lowercase only.
# ==========================================================================

CANONICAL_TAGS: List[str] = [
    "application",
    "business_unit",
    "cost_center",
    "environment",
    "owner",
    "project",
]

# Provider tag/label keys we fold onto each canonical key, lowercased.
TAG_ALIASES: Dict[str, List[str]] = {
    "application": ["app", "appname", "app_name", "application_name", "service", "workload"],
    "business_unit": ["bu", "businessunit", "business-unit", "division", "org", "department"],
    "cost_center": ["costcenter", "cost-center", "cc", "costcentre", "gl_code", "glcode"],
    "environment": ["env", "envt", "stage", "tier", "lifecycle"],
    "owner": ["owned_by", "ownedby", "contact", "team_email", "technical_owner"],
    "project": ["proj", "program", "initiative", "product"],
}

# Column-name aliases resolved on ingest so 1.0, 1.2 and 1.3 feeds converge.
COLUMN_ALIASES: Dict[str, str] = {
    "ServiceProviderName": "ProviderName",  # 1.3 -> canonical
    "HostProviderName": "PublisherName",  # 1.3 -> canonical
    "AmortizedCost": "EffectiveCost",  # 1.0-preview -> GA rename
    "ChargeType": "ChargeCategory",  # 1.0-preview -> GA rename
    "UsageQuantity": "ConsumedQuantity",
    "UsageUnit": "ConsumedUnit",
}


# ==========================================================================
# Empty frame, coercion, validation
# ==========================================================================

_PANDAS_DTYPE = {
    "string": "object",
    "decimal": "float64",
    "datetime": "datetime64[ns]",
    "json": "object",
}


def empty_frame(include_13: bool = False) -> pd.DataFrame:
    cols = SCHEMA + (SCHEMA_13_EXTENSIONS if include_13 else [])
    return pd.DataFrame({c.name: pd.Series(dtype=_PANDAS_DTYPE[c.dtype]) for c in cols})


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Bring an arbitrary FOCUS-ish frame to the canonical shape.

    Resolves column aliases, adds missing non-mandatory columns as null, and
    coerces dtypes. Does NOT invent mandatory values -- `validate` will tell
    you what is missing.
    """
    out = df.copy()
    out = out.rename(columns={k: v for k, v in COLUMN_ALIASES.items() if k in out.columns and v not in out.columns})

    for c in SCHEMA:
        if c.name not in out.columns:
            out[c.name] = pd.NA

    for c in SCHEMA:
        if c.name not in out.columns:
            continue
        if c.dtype == "decimal":
            out[c.name] = pd.to_numeric(out[c.name], errors="coerce")
        elif c.dtype == "datetime":
            out[c.name] = pd.to_datetime(out[c.name], errors="coerce", utc=False)

    ordered = [c.name for c in SCHEMA]
    extras = [c for c in out.columns if c not in ordered]
    return out[ordered + extras]


@dataclass
class ValidationResult:
    ok: bool
    errors: List[str]
    warnings: List[str]
    row_count: int

    def summary(self) -> str:
        if self.ok and not self.warnings:
            return f"Conformant. {self.row_count:,} rows."
        head = "Conformant with warnings" if self.ok else "Non-conformant"
        return f"{head}. {len(self.errors)} error(s), {len(self.warnings)} warning(s), {self.row_count:,} rows."


def validate(df: pd.DataFrame, strict: bool = False) -> ValidationResult:
    """Check a frame against the canonical FOCUS 1.2 Cost and Usage dataset.

    Errors block ingest (a missing mandatory column, a value outside a closed
    enum). Warnings do not (a null in a recommended column, a conditional
    column absent). `strict=True` promotes warnings to errors.
    """
    errors: List[str] = []
    warnings: List[str] = []

    for name in MANDATORY_COLUMNS:
        if name not in df.columns:
            errors.append(f"Missing mandatory column: {name}")

    for name in MANDATORY_COLUMNS:
        col = COLUMN_BY_NAME[name]
        if name in df.columns and not col.nullable and df[name].isna().any():
            n = int(df[name].isna().sum())
            errors.append(f"{name} is mandatory and not nullable, but has {n:,} null value(s)")

    for name, allowed in ENUMS.items():
        if name not in df.columns:
            continue
        present = set(df[name].dropna().astype(str).unique())
        bad = present - allowed
        if bad:
            sample = ", ".join(sorted(bad)[:5])
            errors.append(f"{name} has value(s) outside the FOCUS enum: {sample}")

    # Cross-column constraints the spec states normatively.
    if {"ChargeFrequency", "ChargeCategory"}.issubset(df.columns):
        bad = df[(df["ChargeCategory"] == "Purchase") & (df["ChargeFrequency"] == "Usage-Based")]
        if len(bad):
            errors.append(
                f"ChargeFrequency must not be 'Usage-Based' when ChargeCategory is 'Purchase' ({len(bad):,} rows)"
            )

    if {"CommitmentDiscountId", "CommitmentDiscountType"}.issubset(df.columns):
        bad = df[df["CommitmentDiscountId"].notna() & df["CommitmentDiscountType"].isna()]
        if len(bad):
            errors.append(
                f"CommitmentDiscountType must be non-null when CommitmentDiscountId is non-null ({len(bad):,} rows)"
            )

    if {"ServiceCategory", "ServiceSubcategory"}.issubset(df.columns):
        pairs = df[["ServiceCategory", "ServiceSubcategory"]].dropna().drop_duplicates()
        for _, r in pairs.iterrows():
            allowed = SERVICE_SUBCATEGORY.get(str(r["ServiceCategory"]), [])
            if str(r["ServiceSubcategory"]) not in allowed:
                warnings.append(
                    f"ServiceSubcategory '{r['ServiceSubcategory']}' is not valid under "
                    f"ServiceCategory '{r['ServiceCategory']}'"
                )

    for c in SCHEMA:
        if c.feature_level == RECOMMENDED and c.name in df.columns and df[c.name].isna().all():
            warnings.append(f"Recommended column {c.name} is entirely null")

    if strict:
        errors.extend(warnings)
        warnings = []

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings, row_count=len(df))


# ==========================================================================
# Tag helpers
# ==========================================================================


def _canonical_tag_key(raw_key: str) -> Optional[str]:
    k = str(raw_key).strip().lower().replace("-", "_").replace(" ", "_")
    if k in CANONICAL_TAGS:
        return k
    for canonical, aliases in TAG_ALIASES.items():
        if k in aliases:
            return canonical
    return None


def explode_tags(df: pd.DataFrame, prefix: str = "tag_") -> pd.DataFrame:
    """Lift the `Tags` map into first-class `tag_<canonical>` columns.

    Provider keys are folded onto `CANONICAL_TAGS` via `TAG_ALIASES`, so
    `app`, `AppName` and `application` all land in `tag_application`. A row
    with no usable value gets `Unallocated` -- which is what makes the
    allocation-coverage KPI computable rather than a guess.
    """
    out = df.copy()
    for t in CANONICAL_TAGS:
        out[f"{prefix}{t}"] = "Unallocated"

    if "Tags" not in out.columns:
        return out

    def _row(tags) -> Dict[str, str]:
        found: Dict[str, str] = {}
        if isinstance(tags, dict):
            for k, v in tags.items():
                ck = _canonical_tag_key(k)
                if ck and v not in (None, "", "null"):
                    found[ck] = str(v)
        return found

    extracted = out["Tags"].apply(_row)
    for t in CANONICAL_TAGS:
        vals = extracted.apply(lambda d, t=t: d.get(t))
        out[f"{prefix}{t}"] = vals.fillna("Unallocated")
    return out


def allocation_coverage(df: pd.DataFrame, tag: str = "application", cost_col: str = "EffectiveCost") -> float:
    """Allocated cost / total cost, as a percentage.

    The FinOps Foundation Allocation capability's headline KPI. Its complement
    is untagged/unallocated spend %. Practitioner consensus (vendor-sourced,
    not a published Foundation number) puts the chargeback-readiness threshold
    around 90%.
    """
    col = f"tag_{tag}"
    if col not in df.columns or not len(df):
        return 0.0
    total = float(df[cost_col].sum())
    if total == 0:
        return 0.0
    allocated = float(df.loc[df[col] != "Unallocated", cost_col].sum())
    return allocated / total * 100.0
