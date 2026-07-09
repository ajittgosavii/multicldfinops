"""Kubecost and OpenCost connectors.

These two are Kubernetes cost-allocation engines, not multicloud billing
platforms. They answer one question the hyperscaler bills cannot: how much did
each namespace / controller / labelled workload actually cost, INCLUDING idle
(reserved-but-unused) capacity. Kubecost is the commercial superset; OpenCost is
the CNCF open-source core with a compatible allocation model.

The one thing that bites integrators
------------------------------------
There is NO auth by default -- these run in-cluster and are reached over a
port-forward or an internal service URL. So the "credential" is just the URL
(`KUBECOST_URL` / `OPENCOST_URL`). If you expose one publicly you must add your
own gateway auth; this connector assumes network-level trust.

FOCUS mapping
-------------
Allocation rows are not FOCUS, so we MAP them (`focus_support="map"`):
ServiceCategory='Compute', ServiceName='Kubernetes', ResourceType from the
aggregation (Namespace/Controller), Tags carrying the aggregation key. Idle cost
becomes a distinct row: ChargeDescription='Kubernetes idle capacity',
ConsumedQuantity=0 -- the same idle signature the optimizers already read.

Kubecost docs: https://docs.kubecost.com/apis/apis-overview/allocation
OpenCost docs: https://www.opencost.io/docs/integrations/api
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
    ConnectorSpec,
)
from connectors.vendors._http import VendorConnector, VendorSession, first

# Kubecost model API default port/path; OpenCost default port/path.
KUBECOST_DEFAULT = "http://localhost:9090/model"
OPENCOST_DEFAULT = "http://localhost:9003"

# Kubecost: https://docs.kubecost.com/apis/apis-overview/allocation
EP_KUBECOST_ALLOCATION = "/allocation"
# OpenCost: https://www.opencost.io/docs/integrations/api
EP_OPENCOST_ALLOCATION = "/allocation"


def _window(start: date, end: date) -> str:
    """Kubecost/OpenCost accept `today|7d|...` or an ISO `from,to` pair."""
    return f"{start.isoformat()}T00:00:00Z,{end.isoformat()}T00:00:00Z"


def _map_allocation(data: Dict[str, Any], cluster_label: str) -> List[Dict[str, Any]]:
    """Flatten the allocation response into FOCUS rows.

    The response is `data` -- a list of dicts keyed by aggregation name, each
    value carrying cpuCost/ramCost/gpuCost/pvCost/networkCost/totalCost. The
    special `__idle__` key becomes an idle-capacity row.
    """
    blocks = data.get("data")
    if isinstance(blocks, dict):
        blocks = [blocks]
    rows: List[Dict[str, Any]] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        for key, alloc in block.items():
            if not isinstance(alloc, dict):
                continue
            total = float(first(alloc, "totalCost", default=0.0) or 0.0)
            is_idle = key in ("__idle__", "__unmounted__") or str(key).lower() == "idle"
            rows.append(
                {
                    "BillingAccountId": cluster_label,
                    "BillingAccountName": cluster_label,
                    "BillingCurrency": "USD",
                    "InvoiceIssuerName": "Kubernetes",
                    "ProviderName": "Kubernetes",
                    "ChargeCategory": "Usage",
                    "ChargeDescription": "Kubernetes idle capacity" if is_idle else f"Kubernetes allocation: {key}",
                    "BilledCost": total,
                    "EffectiveCost": total,
                    "ListCost": total,
                    "ContractedCost": total,
                    # ConsumedQuantity=0 on idle is the universal idle signature.
                    "ConsumedQuantity": 0.0 if is_idle else float(first(alloc, "totalEfficiency", default=0.0) or 0.0),
                    "ServiceCategory": "Compute",
                    "ServiceSubcategory": "Containers",
                    "ServiceName": "Kubernetes",
                    "ResourceType": "Idle" if is_idle else "Namespace",
                    "ResourceId": str(key),
                    "ResourceName": str(key),
                    "Tags": {} if is_idle else {"namespace": str(key)},
                }
            )
    return rows


class _K8sAllocationConnector(VendorConnector):
    """Shared logic for the two Kubernetes allocation engines."""

    # subclasses set these
    _url_secret = ""
    _default_url = ""
    _allocation_path = "/allocation"

    def _base(self) -> str:
        return (self.secret(self._url_secret) or self._default_url).rstrip("/")

    def _session(self) -> VendorSession:
        return VendorSession(self._base(), headers={"Accept": "application/json"})

    def _allocation_params(self, start: date, end: date) -> Dict[str, Any]:
        return {"window": _window(start, end), "aggregate": "namespace", "accumulate": "true"}

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            data = self._session().get_json(
                self._allocation_path, params={"window": "today", "aggregate": "namespace"}
            )
            blocks = data.get("data") or []
            return {"url": self._base(), "allocation_blocks": len(blocks) if hasattr(blocks, "__len__") else 0}

        return self._probe(call)

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        if not self.configured:
            return self._empty_costs()
        try:
            data = self._session().get_json(self._allocation_path, params=self._allocation_params(start, end))
            rows = _map_allocation(data, cluster_label=self.spec.vendor)
        except Exception:
            return self._empty_costs()
        return self._stamp(focus.normalize(pd.DataFrame(rows))) if rows else self._empty_costs()


class KubecostConnector(_K8sAllocationConnector):
    """Kubecost allocation -> FOCUS. In-cluster, no auth by default."""

    _url_secret = "KUBECOST_URL"
    _default_url = KUBECOST_DEFAULT
    _allocation_path = EP_KUBECOST_ALLOCATION

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="kubecost",
            display_name="Kubecost",
            vendor="Kubecost",
            clouds=["Kubernetes"],
            auth=AuthKind.NONE,
            capabilities=[Capability.COSTS],
            required_secrets=["KUBECOST_URL"],
            optional_secrets=[],
            base_url=KUBECOST_DEFAULT,
            docs_url="https://docs.kubecost.com/apis/apis-overview/allocation",
            focus_support="map",
            notes=(
                "In-cluster, NO auth by default -- the 'credential' is the URL "
                "(model API on :9090/model). Allocation rows are mapped to FOCUS "
                "(ServiceName='Kubernetes'); idle capacity becomes a "
                "ConsumedQuantity=0 row. Expose publicly at your own risk."
            ),
        )


class OpenCostConnector(_K8sAllocationConnector):
    """OpenCost (CNCF) allocation -> FOCUS. Same model as Kubecost, :9003."""

    _url_secret = "OPENCOST_URL"
    _default_url = OPENCOST_DEFAULT
    _allocation_path = EP_OPENCOST_ALLOCATION

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="opencost",
            display_name="OpenCost",
            vendor="OpenCost",
            clouds=["Kubernetes"],
            auth=AuthKind.NONE,
            capabilities=[Capability.COSTS],
            required_secrets=["OPENCOST_URL"],
            optional_secrets=[],
            base_url=OPENCOST_DEFAULT,
            docs_url="https://www.opencost.io/docs/integrations/api",
            focus_support="map",
            notes=(
                "CNCF open-source core of the Kubecost allocation model. In-cluster, "
                "NO auth by default -- the 'credential' is the URL (:9003/allocation). "
                "Allocation mapped to FOCUS; idle capacity becomes a "
                "ConsumedQuantity=0 row."
            ),
        )
