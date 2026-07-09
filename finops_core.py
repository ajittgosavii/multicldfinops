"""The application contract.

Two things live here and nothing else may define them:

1.  `Mode` -- DEMO or LIVE. Demo runs entirely on synthetic data generated
    in-process, with zero cloud credentials and zero network calls. Live pulls
    from real connectors. Every tab reads `ctx.mode` rather than sniffing for
    credentials, so the demo path is exercised by exactly the same code.

2.  `DataContext` -- the single object every tab receives. It carries a FOCUS
    1.2 conformant DataFrame plus the budget and business-driver frames the
    KPI engine needs. Nothing downstream knows which cloud or which vendor
    tool produced it.

The FinOps Framework vocabulary (domains, capabilities, phases, personas) is
also modelled here so the Reference tab and the AI agents can reason against
the same structure the dashboards are organised by.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import pandas as pd

import focus

APP_VERSION = "1.0.0"

# The clouds we ship a native connector for. NOT a closed set: FOCUS leaves
# ProviderName a free string, and `focus_file` will ingest a conformant export
# from any provider. Code that consumes bill data must therefore tolerate a
# ProviderName it has never seen -- see optimize._profile.
CLOUDS: List[str] = ["AWS", "Azure", "GCP", "OCI"]


# ==========================================================================
# Mode
# ==========================================================================


class Mode(str, Enum):
    DEMO = "demo"
    LIVE = "live"

    @property
    def label(self) -> str:
        return "Demo data" if self is Mode.DEMO else "Live connectors"

    @property
    def blurb(self) -> str:
        if self is Mode.DEMO:
            return (
                "Synthetic, deterministic FOCUS 1.2 data for a utility-shaped estate. "
                "No credentials, no network calls, no cloud spend."
            )
        return (
            "Reads real billing data through configured connectors. "
            "Requires credentials in Streamlit secrets or the environment."
        )


def resolve_mode(explicit: Optional[str] = None) -> Mode:
    """Demo unless something tells us otherwise.

    Order: explicit argument > FINOPS_MODE env/secret > DEMO. Defaulting to
    demo means a fresh clone runs, and a misconfigured secret degrades to a
    working app rather than a stack trace.
    """
    raw = explicit or os.environ.get("FINOPS_MODE", "").strip().lower()
    if raw == "live":
        return Mode.LIVE
    return Mode.DEMO


# ==========================================================================
# Configuration
# ==========================================================================


@dataclass(frozen=True)
class AccountBinding:
    """One credentialed scope to pull billing data from.

    A single binding usually covers *many* accounts already, because that is how
    the providers aggregate billing:

        AWS    Cost Explorer at the payer (management) account returns every
               linked account, which lands in `SubAccountId`.
        Azure  Billing-account scope covers every subscription beneath it;
               subscription scope covers exactly one.
        GCP    A billing account's BigQuery export carries every project it
               pays for, which lands in `SubAccountId`.

    A second binding is needed when there is a second *payer* -- a separate AWS
    organization, a second Azure tenant or billing account, another GCP billing
    account. A regulated utility typically has several, because the regulated
    and unregulated entities cannot share a bill.

    `secrets` overlay the global secret map for this binding only, so two AWS
    payers each carry their own key pair. `options` are passed to the connector
    constructor (an Azure `scope`, an AWS `profile`).
    """

    cloud: str
    connector: str
    name: str = ""
    secrets: Tuple[Tuple[str, str], ...] = ()
    options: Tuple[Tuple[str, Any], ...] = ()

    @property
    def label(self) -> str:
        return self.name or f"{self.cloud} · {self.connector}"

    @property
    def secret_map(self) -> Dict[str, str]:
        return dict(self.secrets)

    @property
    def option_map(self) -> Dict[str, Any]:
        return dict(self.options)


@dataclass
class AppConfig:
    """Runtime configuration, sourced from Streamlit secrets or the environment.

    Secrets are read lazily and never logged. A missing secret is not an
    error -- it disables the feature that needs it and the UI says so.
    """

    mode: Mode = Mode.DEMO
    organisation: str = "Con Edison"
    currency: str = "USD"
    fiscal_year_start_month: int = 1

    openai_api_key: Optional[str] = None
    # gpt-5 is the workhorse; gpt-5-mini backs the cheap routing tier so the
    # platform practises the small-model-first lever it recommends.
    openai_model: str = "gpt-5"
    openai_model_fast: str = "gpt-5-mini"

    # Which connector supplies each cloud in LIVE mode. The simple path: one
    # payer per cloud. `accounts` supersedes it when more than one is configured.
    connector_for: Dict[str, str] = field(
        default_factory=lambda: {
            "AWS": "aws_native",
            "Azure": "azure_native",
            "GCP": "gcp_native",
            "OCI": "oci_native",
        }
    )
    accounts: List[AccountBinding] = field(default_factory=list)

    database_url: Optional[str] = None

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openai_api_key)

    def bindings(self) -> List[AccountBinding]:
        """Every scope to pull, explicit or derived.

        With no `[[accounts]]` block we synthesise one binding per cloud from
        `connector_for`, which is exactly the old single-payer behaviour.
        """
        if self.accounts:
            return list(self.accounts)
        return [AccountBinding(cloud=c, connector=k, name=f"{c} (default)") for c, k in sorted(self.connector_for.items())]


def _secret(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read from Streamlit secrets, then the environment."""
    try:
        import streamlit as st

        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    return os.environ.get(key, default)


def _load_account_bindings() -> List[AccountBinding]:
    """Parse the `[[accounts]]` array from Streamlit secrets.

        [[accounts]]
        cloud     = "AWS"
        name      = "ConEd Regulated payer"
        connector = "aws_native"
        [accounts.secrets]
        AWS_ACCESS_KEY_ID     = "..."
        AWS_SECRET_ACCESS_KEY = "..."

    A malformed entry is skipped rather than fatal -- one bad binding must not
    take down the estate. Secret VALUES are never echoed anywhere.
    """
    try:
        import streamlit as st

        raw = st.secrets.get("accounts")
    except Exception:
        raw = None
    if not raw:
        return []

    out: List[AccountBinding] = []
    for entry in raw:
        try:
            cloud = str(entry["cloud"])
            connector = str(entry["connector"])
        except Exception:
            continue
        secrets = {str(k): str(v) for k, v in dict(entry.get("secrets", {})).items()}
        options = {str(k): v for k, v in dict(entry.get("options", {})).items()}
        out.append(
            AccountBinding(
                cloud=cloud,
                connector=connector,
                name=str(entry.get("name", "")),
                secrets=tuple(sorted(secrets.items())),
                options=tuple(sorted(options.items())),
            )
        )
    return out


def load_config(mode: Optional[Mode] = None) -> AppConfig:
    cfg = AppConfig()
    cfg.mode = mode or resolve_mode(_secret("FINOPS_MODE"))
    cfg.organisation = _secret("ORGANISATION", "Con Edison") or "Con Edison"
    cfg.openai_api_key = _secret("OPENAI_API_KEY")
    cfg.openai_model = _secret("OPENAI_MODEL", "gpt-5") or "gpt-5"
    cfg.openai_model_fast = _secret("OPENAI_MODEL_FAST", "gpt-5-mini") or "gpt-5-mini"
    cfg.database_url = _secret("DATABASE_URL")
    cfg.accounts = _load_account_bindings()
    return cfg


# ==========================================================================
# DataContext -- what every tab receives
# ==========================================================================


@dataclass
class SourceInfo:
    """Provenance for one slice of the data. Rendered on the Integrations tab."""

    connector: str
    cloud: str
    rows: int
    account: str = ""  # which payer / billing account / tenant this row came from
    focus_version: str = focus.FOCUS_CANONICAL_VERSION
    fetched_at: Optional[datetime] = None
    live: bool = False
    note: str = ""


@dataclass
class DataContext:
    """Everything the dashboards read.

    `focus_df` is FOCUS 1.2 conformant with `tag_*` columns already exploded.
    `budgets` is (period, cloud, application, budget) at month grain.
    `drivers` is (period, metric, value) -- the denominators for unit economics
    (customers served, kWh billed, work orders, meter reads).
    """

    focus_df: pd.DataFrame
    budgets: pd.DataFrame
    drivers: pd.DataFrame
    mode: Mode = Mode.DEMO
    config: AppConfig = field(default_factory=AppConfig)
    sources: List[SourceInfo] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    validation: Optional[focus.ValidationResult] = None

    # ---- convenience accessors -------------------------------------------

    @property
    def clouds(self) -> List[str]:
        return sorted(self.focus_df["ProviderName"].dropna().unique().tolist())

    @property
    def applications(self) -> List[str]:
        return sorted(self.focus_df["tag_application"].dropna().unique().tolist())

    @property
    def business_units(self) -> List[str]:
        return sorted(self.focus_df["tag_business_unit"].dropna().unique().tolist())

    @property
    def environments(self) -> List[str]:
        return sorted(self.focus_df["tag_environment"].dropna().unique().tolist())

    @property
    def period_range(self):
        p = self.focus_df["ChargePeriodStart"]
        return p.min(), p.max()

    def monthly(self, cost_col: str = "EffectiveCost") -> pd.DataFrame:
        """Month-grain spend. `EffectiveCost` (amortized) is the executive view;
        blended/unblended never belong on a VP dashboard."""
        df = self.focus_df.copy()
        df["period"] = df["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp()
        return df.groupby("period", as_index=False, observed=True)[cost_col].sum().rename(columns={cost_col: "cost"})

    def monthly_by(self, dim: str, cost_col: str = "EffectiveCost") -> pd.DataFrame:
        """Month-grain spend split by one dimension.

        `observed=True` is load-bearing: the dimension columns are categorical,
        and a filtered slice keeps every original category. Without it, a
        filter to one cloud would emit a zero-cost row for every application
        that cloud never ran.
        """
        df = self.focus_df.copy()
        df["period"] = df["ChargePeriodStart"].dt.to_period("M").dt.to_timestamp()
        return (
            df.groupby(["period", dim], as_index=False, observed=True)[cost_col]
            .sum()
            .rename(columns={cost_col: "cost"})
        )

    def filtered(
        self,
        clouds: Optional[List[str]] = None,
        applications: Optional[List[str]] = None,
        business_units: Optional[List[str]] = None,
        environments: Optional[List[str]] = None,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> "DataContext":
        """Return a new context scoped to a slice.

        One filter row above everything it scopes -- charts never carry their
        own filters, so every panel on a page always shows the same slice.
        """
        df = self.focus_df
        if clouds:
            df = df[df["ProviderName"].isin(clouds)]
        if applications:
            df = df[df["tag_application"].isin(applications)]
        if business_units:
            df = df[df["tag_business_unit"].isin(business_units)]
        if environments:
            df = df[df["tag_environment"].isin(environments)]
        if start is not None:
            df = df[df["ChargePeriodStart"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["ChargePeriodStart"] <= pd.Timestamp(end)]

        b = self.budgets
        if clouds and "cloud" in b.columns:
            b = b[b["cloud"].isin(clouds)]
        if applications and "application" in b.columns:
            b = b[b["application"].isin(applications)]

        return DataContext(
            focus_df=df,
            budgets=b,
            drivers=self.drivers,
            mode=self.mode,
            config=self.config,
            sources=self.sources,
            generated_at=self.generated_at,
            validation=self.validation,
        )


# ==========================================================================
# FinOps Framework vocabulary (2025 Framework; 2026 renames carried as aliases)
#
# Source: https://www.finops.org/framework/
# ==========================================================================

DOMAINS: List[str] = [
    "Understand Usage and Cost",
    "Quantify Business Value",
    "Optimize Usage and Cost",
    "Manage the FinOps Practice",
]

CAPABILITIES: Dict[str, List[str]] = {
    "Understand Usage and Cost": [
        "Data Ingestion",
        "Allocation",
        "Reporting and Analytics",
        "Anomaly Management",
    ],
    "Quantify Business Value": [
        "Planning and Estimating",
        "Forecasting",
        "Budgeting",
        "Benchmarking",
        "Unit Economics",
    ],
    "Optimize Usage and Cost": [
        "Architecting for the Cloud",
        "Workload Optimization",
        "Rate Optimization",
        "Licensing and SaaS",
        "Cloud Sustainability",
    ],
    "Manage the FinOps Practice": [
        "FinOps Education and Enablement",
        "FinOps Practice Operations",
        "Onboarding Workloads",
        "Policy and Governance",
        "Invoicing and Chargeback",
        "FinOps Assessment",
        "FinOps Tools and Services",
        "Intersecting Frameworks",
    ],
}

# The 2026 Framework kept the four domains, renamed six capabilities and added
# one (Executive Strategy Alignment -> 23 total). We surface both names so a
# practitioner on either vintage finds what they expect.
CAPABILITY_2026_RENAMES: Dict[str, str] = {
    "Workload Optimization": "Usage Optimization",
    "Policy and Governance": "Governance, Policy & Risk",
    "FinOps Tools and Services": "Automation, Tools & Services",
    "Benchmarking": "KPI & Benchmarking",
    "Architecting for the Cloud": "Architecting & Workload Placement",
    "Cloud Sustainability": "Sustainability",
}
CAPABILITY_2026_ADDED: List[str] = ["Executive Strategy Alignment"]

PHASES: List[str] = ["Inform", "Optimize", "Operate"]
MATURITY: List[str] = ["Crawl", "Walk", "Run"]

CORE_PERSONAS: Dict[str, str] = {
    "Leadership": "Align technology spend to business value. Cloud spend as % of revenue, COGS, unit-cost stability, forecast predictability.",
    "FinOps Practitioner": "Bridge business, engineering and finance. Coverage, allocation %, anomalies, savings realised, forecast accuracy.",
    "Engineering": "Balance cost, speed and quality. Cost per service, utilisation, rightsizing and anomaly signals.",
    "Finance": "Budget, forecast, report, chargeback. Budget vs actual variance, invoice reconciliation, cost-centre chargeback.",
    "Product": "Product margin and unit economics. Cost per feature, per customer, unit-cost trend.",
    "Procurement": "Vendor contracts and commitments. Coverage, utilisation, rate performance, renewal calendar.",
}

ALLIED_PERSONAS: List[str] = ["ITAM", "ITFM / TBM", "Sustainability", "ITSM / ITIL", "Security"]

# A Scope is a segment of technology spend a practice is responsible for.
# Scopes overlap; they are not mutually exclusive.
SCOPES: List[str] = ["Public Cloud", "SaaS", "Licensing", "Data Center", "AI", "Private Cloud"]

# Forecast variance thresholds by maturity. These are the most citable numbers
# the Foundation publishes on forecasting -- everything else in that space is
# practitioner folklore.
# https://www.finops.org/framework/capabilities/forecasting/
FORECAST_VARIANCE_THRESHOLD: Dict[str, float] = {
    "Crawl": 20.0,
    "Walk": 15.0,
    "Run": 12.0,
    "Best-in-class": 5.0,
}


def maturity_for_variance(variance_pct: float) -> str:
    v = abs(variance_pct)
    if v < FORECAST_VARIANCE_THRESHOLD["Best-in-class"]:
        return "Best-in-class"
    if v < FORECAST_VARIANCE_THRESHOLD["Run"]:
        return "Run"
    if v < FORECAST_VARIANCE_THRESHOLD["Walk"]:
        return "Walk"
    if v < FORECAST_VARIANCE_THRESHOLD["Crawl"]:
        return "Crawl"
    return "Below Crawl"
