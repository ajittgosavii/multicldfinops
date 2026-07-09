"""CloudZero connector.

CloudZero is a cost-intelligence platform built around "CostFormation" (its
allocation-as-code model) and "AnyCost" (its ingest of arbitrary cost feeds,
including FOCUS). It can both ingest and emit FOCUS.

The two things that bite integrators
------------------------------------
1. The API key goes in the `Authorization` header DIRECTLY, with NO `Bearer `
   prefix. (The MCP surface also accepts `X-Api-Key`.) Send `Bearer <key>` and
   you get a 401.
2. There is a HARD RATE LIMIT of 60 REQUESTS PER DAY, a 30-second server
   timeout, and results paginate in 10,000-record cursor blocks. You cannot poll
   this API; cache aggressively. `fetch_costs` therefore makes ONE query and
   pages it, and callers should memoise the result for the day.

FOCUS: AnyCost ingests FOCUS and CloudZero can emit FOCUS -> "native".

Docs: https://docs.cloudzero.com/reference/
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

BASE = "https://api.cloudzero.com/v2"

# --- endpoints ------------------------------------------------------------
# https://docs.cloudzero.com/reference/getbillingcosts
EP_BILLING_COSTS = "/v2/billing/costs"
EP_BILLING_DIMENSIONS = "/v2/billing/dimensions"

# 60 requests/DAY. Do NOT poll. One query, paged, then cache.
_DAILY_BUDGET_NOTE = "CloudZero API: 60 requests/day, 30s timeout, 10k-record pages."


class CloudZeroConnector(VendorConnector):
    """Reads costs and dimensions from CloudZero v2. Rate-limit-sensitive:
    60 requests/day. Cache the result of `fetch_costs` for the day."""

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="cloudzero",
            display_name="CloudZero",
            vendor="CloudZero",
            clouds=["AWS", "Azure", "GCP"],
            auth=AuthKind.CUSTOM_HEADERS,  # raw Authorization: <key>, no Bearer
            capabilities=[
                Capability.COSTS,
                Capability.NATIVE_FOCUS,
                # No recommendations / budgets / anomalies read API in v2/billing.
            ],
            required_secrets=["CLOUDZERO_API_KEY"],
            optional_secrets=[],
            base_url=BASE,
            docs_url="https://docs.cloudzero.com/reference/",
            focus_support="native",
            notes=(
                "API key goes in Authorization with NO 'Bearer ' prefix. "
                + _DAILY_BUDGET_NOTE
                + " Cache aggressively; this connector makes one paged query per call."
            ),
        )

    def _session(self) -> VendorSession:
        # NOTE: value is the raw key, not 'Bearer <key>'.
        return VendorSession(
            BASE,
            headers={
                "Authorization": self.secret("CLOUDZERO_API_KEY") or "",
                "Accept": "application/json",
            },
            timeout=30.0,  # server-side hard timeout
        )

    # -- connection -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            data = self._session().get_json(EP_BILLING_DIMENSIONS)
            dims = data.get("dimensions") or data.get("data") or []
            return {"dimensions": len(dims), "note": _DAILY_BUDGET_NOTE}

        return self._probe(call)

    # -- costs ------------------------------------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """One daily-granularity query, paged on the cursor.

        Deliberately does NOT retry beyond the shared HTTP backoff: at 60
        requests/day, aggressive retry would exhaust the budget. Cache the
        returned frame for the calendar day.
        """
        if not self.configured:
            return self._empty_costs()
        session = self._session()
        params = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "granularity": "DAILY",
            "group_by": "service",
            "cost_type": "real_cost",
        }
        rows: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        try:
            for _ in range(50):  # 10k-record pages; bounded so we never busy-loop
                if cursor:
                    params["cursor"] = cursor
                data = session.get_json(EP_BILLING_COSTS, params=params)
                rows.extend(self._map_cost_row(c) for c in data.get("costs", []) or data.get("data", []) or [])
                cursor = (data.get("pagination") or {}).get("cursor") or data.get("cursor")
                if not cursor:
                    break
        except Exception:
            return self._empty_costs() if not rows else self._stamp(focus.normalize(pd.DataFrame(rows)))
        return self._stamp(focus.normalize(pd.DataFrame(rows))) if rows else self._empty_costs()

    @staticmethod
    def _map_cost_row(c: Dict[str, Any]) -> Dict[str, Any]:
        amount = float(first(c, "cost", "real_cost", "amount", default=0.0) or 0.0)
        return {
            "BillingAccountId": first(c, "account_id", "billing_account", default="cloudzero"),
            "BillingAccountName": first(c, "account_name", default="CloudZero"),
            "BillingCurrency": "USD",
            "InvoiceIssuerName": "CloudZero",
            "ProviderName": str(first(c, "cloud_provider", "provider", default="")).upper(),
            "ChargePeriodStart": first(c, "date", "usage_date", default=None),
            "ChargeCategory": "Usage",
            "ChargeDescription": first(c, "service", "element", default="Cloud usage"),
            "BilledCost": amount,
            "EffectiveCost": amount,
            "ListCost": amount,
            "ContractedCost": amount,
            "ServiceCategory": "Other",
            "ServiceName": first(c, "service", default="Unknown"),
        }
