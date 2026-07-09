"""The connector contract.

Every source of cost data -- a hyperscaler's native billing export, a
commercial FinOps platform, a CSV someone dropped on the upload widget, or the
in-process demo generator -- implements `Connector` and returns a FOCUS 1.2
conformant DataFrame.

That is the entire integration story. When Con Edison procures Cloudability, or
CloudHealth, or Flexera, or Finout, we implement one subclass. Every dashboard,
every KPI, every optimization detector and every agent tool keeps working,
because none of them has ever seen a vendor-specific field.

A connector must:
  * declare its `capabilities` honestly -- the Integrations tab renders them,
    and callers branch on them rather than catching exceptions;
  * `test_connection()` without raising, returning a structured result;
  * `fetch_costs()` returning a frame that passes `focus.validate()`;
  * never log or echo a credential.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import pandas as pd

import focus


class Capability(str, Enum):
    """What a given source can actually answer.

    Sourced from the vendor API research: most commercial platforms expose cost
    and recommendations, fewer expose budgets, and several expose no anomaly
    read API at all (Finout, for instance, only provisions alert channels).
    """

    COSTS = "costs"
    RECOMMENDATIONS = "recommendations"
    BUDGETS = "budgets"
    FORECAST = "forecast"
    ANOMALIES = "anomalies"
    ALLOCATION_RULES = "allocation_rules"
    NATIVE_FOCUS = "native_focus"


class AuthKind(str, Enum):
    """The four families every vendor collapses into."""

    NONE = "none"
    SIGV4 = "sigv4"  # AWS
    OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"  # Azure, GCP, Flexera
    BEARER_TOKEN = "bearer_token"  # Vantage, CloudHealth, Harness
    CUSTOM_HEADERS = "custom_headers"  # Finout, nOps, CloudZero, Cloudability


@dataclass
class ConnectionResult:
    ok: bool
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)
    latency_ms: Optional[float] = None


@dataclass
class Recommendation:
    """A normalized optimization recommendation.

    Every vendor shapes these differently -- Cloudability nests savings under
    `recommendations[].savings`, Compute Optimizer under
    `recommendationOptions[].estimatedMonthlySavings`, GCP under
    `primaryImpact.costProjection.cost` -- so connectors flatten to this.
    """

    source: str
    cloud: str
    resource_id: str
    resource_type: str
    lever: str
    action: str
    estimated_monthly_savings: float
    currency: str = "USD"
    effort: str = "Medium"
    risk: str = "Medium"
    confidence: float = 0.7
    account_id: str = ""
    region: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectorSpec:
    """Static metadata. Rendered on the Integrations tab so an admin can see
    what a connector needs before wiring it up."""

    key: str
    display_name: str
    vendor: str
    clouds: List[str]
    auth: AuthKind
    capabilities: List[Capability]
    required_secrets: List[str] = field(default_factory=list)
    optional_secrets: List[str] = field(default_factory=list)
    base_url: str = ""
    docs_url: str = ""
    focus_support: str = "none"  # 'native' | 'ingest' | 'map' | 'none'
    notes: str = ""


class Connector(abc.ABC):
    """Base class. Subclasses override `spec`, `test_connection`, `fetch_costs`."""

    def __init__(self, secrets: Optional[Dict[str, str]] = None, **options: Any) -> None:
        self._secrets = secrets or {}
        self.options = options

    # ---- identity -------------------------------------------------------

    @property
    @abc.abstractmethod
    def spec(self) -> ConnectorSpec:
        ...

    @property
    def key(self) -> str:
        return self.spec.key

    def supports(self, cap: Capability) -> bool:
        return cap in self.spec.capabilities

    # ---- credentials ----------------------------------------------------

    def secret(self, name: str) -> Optional[str]:
        """Read a credential. Never log the return value."""
        return self._secrets.get(name)

    def missing_secrets(self) -> List[str]:
        return [s for s in self.spec.required_secrets if not self.secret(s)]

    @property
    def configured(self) -> bool:
        return not self.missing_secrets()

    # ---- behaviour ------------------------------------------------------

    @abc.abstractmethod
    def test_connection(self) -> ConnectionResult:
        """Probe the source. Must not raise -- return `ok=False` with a reason."""

    @abc.abstractmethod
    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """Return FOCUS 1.2 conformant rows for [start, end)."""

    def fetch_recommendations(self) -> List[Recommendation]:
        """Native recommendations, if the source has any.

        The default is an empty list rather than `NotImplementedError` -- a
        source without a recommendations API is a normal state, not a bug, and
        `optimize.py` will still derive findings from the billing data itself.
        """
        return []

    def fetch_budgets(self) -> pd.DataFrame:
        """(period, cloud, application, budget). Empty frame if unsupported."""
        return pd.DataFrame(columns=["period", "cloud", "application", "budget"])

    # ---- helpers for subclasses -----------------------------------------

    @staticmethod
    def to_focus(rows: pd.DataFrame) -> pd.DataFrame:
        """Coerce a nearly-FOCUS frame into the canonical shape."""
        return focus.normalize(rows)

    def _stamp(self, df: pd.DataFrame) -> pd.DataFrame:
        """Attach provenance columns every connector owes the platform."""
        if "InvoiceIssuerName" not in df.columns or df["InvoiceIssuerName"].isna().all():
            df["InvoiceIssuerName"] = self.spec.vendor
        return df

    def __repr__(self) -> str:  # never include secrets
        return f"<{type(self).__name__} key={self.spec.key!r} configured={self.configured}>"


class UnconfiguredConnector(Connector):
    """Stands in for a connector whose credentials are absent.

    Live mode with a half-configured estate should show *which* cloud is
    missing and keep rendering the clouds that are wired, rather than failing
    the whole page. This is what the registry hands back in that case.
    """

    def __init__(self, spec: ConnectorSpec, missing: List[str]) -> None:
        super().__init__({})
        self._spec = spec
        self._missing = missing

    @property
    def spec(self) -> ConnectorSpec:
        return self._spec

    def test_connection(self) -> ConnectionResult:
        return ConnectionResult(
            ok=False,
            message=f"Not configured. Missing secret(s): {', '.join(self._missing)}",
            detail={"missing": self._missing},
        )

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        return focus.empty_frame()
