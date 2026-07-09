"""Finout connector.

Finout is a cost-observability platform built around its "MegaBill" (a unified,
FOCUS-aligned cost dataset) and "Virtual Tags" (its allocation model). Cost
queries are asynchronous: you generate a query, poll its status, then page the
results.

The two things that bite integrators
------------------------------------
1. V1 and V2 spell the same field DIFFERENTLY. V1 uses
   `unixTimeMillSecondsStart` (no "i": Mill-Seconds); V2 uses
   `unixTimeMillisecondsStart` (Milli-seconds). Copy the wrong one across
   versions and the API rejects the body. This connector uses V2 throughout.
2. A V2 cost query is capped at a 60-DAY date range. Longer ranges must be
   chunked client-side; `fetch_costs` does this automatically.

Capabilities honesty
---------------------
Finout has NO budget or anomaly *read* API -- its only budget/alert surface is
`/v1/Endpoints`, which provisions alert *channels*, not readable budgets. So we
declare COSTS, RECOMMENDATIONS (CostGuard) and ALLOCATION_RULES only. No BUDGETS,
no ANOMALIES.

FOCUS: MegaBill is FOCUS-aligned and ingests FOCUS via a Custom Cost Center ->
"ingest".

Docs: https://docs.finout.io/  (Public API)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

import focus
from connectors.base import (
    AuthKind,
    Capability,
    ConnectionResult,
    ConnectorSpec,
    Recommendation,
)
from connectors.vendors._http import VendorConnector, VendorSession, chunk_date_ranges, first

V2_HOST = "https://app.finout.io"

# --- V2 endpoints ---------------------------------------------------------
# https://docs.finout.io/  (Cost & Usage async query)
EP_GENERATE_QUERY = "/v2/data/cost-usage/generate-query"
EP_QUERY_STATUS = "/v2/data/cost-usage/{query_id}/status"     # queued|running|completed|failed|expired
EP_QUERY_RESULTS = "/v2/data/cost-usage/{query_id}/results"
EP_QUERY_KEYS = "/v2/query-language/keys"
EP_VIRTUAL_TAGS = "/v2/virtual-tags"
# --- V1 (CostGuard recommendations only) ---------------------------------
EP_COST_GUARD_SCANS = "/v1/cost-guard/scans"
EP_COST_GUARD_RECS = "/v1/cost-guard/scans-recommendations"

_MAX_RANGE_DAYS = 60      # hard V2 limit; chunk longer ranges
_POLL_ATTEMPTS = 30       # status polls before giving up
_PAGE_SIZE = 10000        # V2 pageSize cap


def _epoch_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


class FinoutConnector(VendorConnector):
    """Reads MegaBill cost (V2 async) and CostGuard recommendations (V1)."""

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="finout",
            display_name="Finout",
            vendor="Finout",
            clouds=["AWS", "Azure", "GCP"],
            auth=AuthKind.CUSTOM_HEADERS,  # x-finout-client-id + x-finout-secret-key
            capabilities=[
                Capability.COSTS,
                Capability.RECOMMENDATIONS,   # CostGuard (V1)
                Capability.ALLOCATION_RULES,  # Virtual Tags
                # NO BUDGETS / ANOMALIES: Finout only exposes alert channels, not a read API.
            ],
            required_secrets=["FINOUT_CLIENT_ID", "FINOUT_SECRET_KEY"],
            optional_secrets=[],
            base_url=V2_HOST,
            docs_url="https://docs.finout.io/",
            focus_support="ingest",
            notes=(
                "V2 cost query is async (generate-query -> status -> results) and "
                "capped at a 60-day range (chunked here). Beware the V1/V2 field "
                "spelling: V1 'unixTimeMillSecondsStart' vs V2 "
                "'unixTimeMillisecondsStart'. No budget/anomaly read API -- only "
                "alert channels at /v1/Endpoints -- so those capabilities are omitted."
            ),
        )

    def _session(self) -> VendorSession:
        return VendorSession(
            V2_HOST,
            headers={
                "x-finout-client-id": self.secret("FINOUT_CLIENT_ID") or "",
                "x-finout-secret-key": self.secret("FINOUT_SECRET_KEY") or "",
                "Content-Type": "application/json",
            },
        )

    # -- connection -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            # query-language/keys is a cheap authenticated read.
            data = self._session().get_json(EP_QUERY_KEYS)
            keys = (data.get("data") or {}).get("keys") or data.get("keys") or []
            return {"query_keys": len(keys)}

        return self._probe(call)

    # -- costs ------------------------------------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """Run one async V2 query per <=60-day chunk and concatenate.

        Each chunk: generate-query -> poll status -> page results.
        """
        if not self.configured:
            return self._empty_costs()
        session = self._session()
        frames: List[Dict[str, Any]] = []
        for a, b in chunk_date_ranges(start, end, _MAX_RANGE_DAYS):
            try:
                frames.extend(self._run_chunk(session, a, b))
            except Exception:
                continue
        return self._stamp(focus.normalize(pd.DataFrame(frames))) if frames else self._empty_costs()

    def _run_chunk(self, session: VendorSession, start: date, end: date) -> List[Dict[str, Any]]:
        body = {
            "date": {"unixTimeMillisecondsStart": _epoch_ms(start), "unixTimeMillisecondsEnd": _epoch_ms(end)},
            "timeInterval": {"interval": "day", "sortDirection": "ascending"},
            "measurements": [{"type": "amortizedCost", "operator": "sum"},
                             {"type": "unblendedCost", "operator": "sum"},
                             {"type": "listCost", "operator": "sum"},
                             {"type": "usageAmount", "operator": "sum"}],
            "dimensions": [{"costCenter": "", "key": "provider", "type": "billing_dimension"},
                           {"costCenter": "", "key": "service", "type": "billing_dimension"}],
            "filters": {"AND": []},
            "rowLimit": 100000,
        }
        gen = session.post_json(EP_GENERATE_QUERY, json=body)
        query_id = (gen.get("data") or {}).get("queryId") or gen.get("queryId")
        if not query_id:
            return []

        # Poll status until completed. Retry/backoff on the HTTP layer handles
        # 429; this loop handles the async job state machine.
        import time as _time
        for _ in range(_POLL_ATTEMPTS):
            status_data = session.get_json(EP_QUERY_STATUS.format(query_id=query_id))
            status = (status_data.get("data") or {}).get("status") or status_data.get("status")
            if status == "completed":
                break
            if status in ("failed", "expired"):
                return []
            _time.sleep(1.0)
        else:
            return []

        rows: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(100):
            params = {"pageSize": _PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            page = session.get_json(EP_QUERY_RESULTS.format(query_id=query_id), params=params)
            rows.extend(self._map_row(r) for r in page.get("data", []) or [])
            pagination = page.get("pagination") or {}
            if not pagination.get("hasMore"):
                break
            cursor = pagination.get("nextCursor")
            if not cursor:
                break
        return rows

    @staticmethod
    def _map_row(r: Dict[str, Any]) -> Dict[str, Any]:
        """Finout result rows key dimensions as `<costCenter>_<type>_<key>` and
        measurements as `<measurementType>_<operator>` -> {"amount","unit"}."""

        def measure(name: str) -> float:
            cell = r.get(f"{name}_sum") or {}
            return float(cell.get("amount", 0.0) or 0.0) if isinstance(cell, dict) else float(cell or 0.0)

        provider = str(r.get("_billing_dimension_provider", "") or "").upper()
        service = r.get("_billing_dimension_service", "") or "Unknown"
        amortized = measure("amortizedCost")
        unblended = measure("unblendedCost") or amortized
        return {
            "BillingAccountId": provider or "finout",
            "BillingAccountName": provider or "Finout",
            "BillingCurrency": "USD",
            "InvoiceIssuerName": "Finout",
            "ProviderName": provider,
            "ChargeCategory": "Usage",
            "ChargeDescription": str(service),
            "BilledCost": unblended,
            "EffectiveCost": amortized,
            "ListCost": measure("listCost") or unblended,
            "ContractedCost": amortized,
            "ConsumedQuantity": measure("usageAmount"),
            "ServiceCategory": "Other",
            "ServiceName": str(service),
        }

    # -- recommendations (CostGuard, V1) ----------------------------------

    def fetch_recommendations(self) -> List[Recommendation]:
        if not self.configured:
            return []
        session = self._session()
        try:
            scans = session.get_json(EP_COST_GUARD_SCANS)
        except Exception:
            return []
        out: List[Recommendation] = []
        for scan in scans.get("data", []) or scans.get("scans", []) or []:
            scan_id = first(scan, "id", "scanId", default=None)
            lever = ((scan.get("scanMetadata") or {}).get("type")) or "rightsizing"
            if not scan_id:
                continue
            try:
                recs = session.post_json(EP_COST_GUARD_RECS, json={"scanId": scan_id})
            except Exception:
                continue
            for group in recs.get("data", []) or []:
                out.append(
                    Recommendation(
                        source="finout",
                        cloud="",
                        resource_id=str(first(group, "resourceId", default="")),
                        resource_type=str(lever),
                        lever=str(lever),
                        action="CostGuard recommendation",
                        estimated_monthly_savings=float(
                            first(group, "groupYearlyPotentialSavings", default=0.0) or 0.0
                        ) / 12.0,
                        detail={"scan_id": scan_id},
                    )
                )
        return out
