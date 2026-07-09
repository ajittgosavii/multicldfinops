"""Turbonomic (IBM) connector.

Turbonomic is IBM's application resource-management platform. It continuously
generates *actions* -- resize, scale, move, delete -- to keep workloads
performant while cutting waste. Those actions are the recommendations we surface.

The one thing that bites integrators
------------------------------------
Turbonomic is NOT a billing source. It knows what a resize would SAVE, but it
does not hold your cloud invoice. So `fetch_costs` deliberately returns an EMPTY
FOCUS frame -- pairing Turbonomic with a real billing connector (a hyperscaler
or one of the FinOps platforms here) is the intended pattern. Capabilities are
RECOMMENDATIONS only; `focus_support="none"`.

Auth is a classic form login: `POST /api/v3/login` with username/password ->
a JSESSIONID cookie carried on subsequent calls.

Docs: https://www.ibm.com/docs/en/tarm  (REST API v3)
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
    Recommendation,
)
from connectors.vendors._http import VendorConnector, VendorSession, first

# --- endpoints (under https://<host>/api/v3) ------------------------------
# https://www.ibm.com/docs/en/tarm  (Authentication / Markets / Actions)
EP_LOGIN = "/api/v3/login"
EP_MARKETS = "/api/v3/markets"
EP_MARKET_ACTIONS = "/api/v3/markets/{market}/actions"

# Turbonomic actions that carry a cloud savings figure.
_SAVINGS_ACTIONS = {"RESIZE", "SCALE", "DELETE", "SUSPEND"}


class TurbonomicConnector(VendorConnector):
    """Reads optimization actions (recommendations) from Turbonomic.
    Not a billing source -- fetch_costs returns an empty FOCUS frame."""

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="turbonomic",
            display_name="Turbonomic (IBM)",
            vendor="Turbonomic",
            clouds=["AWS", "Azure", "GCP"],
            auth=AuthKind.BEARER_TOKEN,  # session cookie via form login
            capabilities=[Capability.RECOMMENDATIONS],  # NOT a cost source
            required_secrets=["TURBONOMIC_HOST", "TURBONOMIC_USERNAME", "TURBONOMIC_PASSWORD"],
            optional_secrets=[],
            base_url="",
            docs_url="https://www.ibm.com/docs/en/tarm",
            focus_support="none",
            notes=(
                "Not a billing source: fetch_costs returns an EMPTY FOCUS frame by "
                "design -- pair with a real billing connector. Auth is form login "
                "(POST /api/v3/login) -> JSESSIONID cookie. Actions (RESIZE/SCALE/"
                "MOVE/DELETE) are the recommendations."
            ),
        )

    def _host(self) -> str:
        return (self.secret("TURBONOMIC_HOST") or "").rstrip("/")

    def _login(self) -> VendorSession:
        """Form-login and return a session carrying the JSESSIONID cookie."""
        session = VendorSession(self._host(), headers={"Accept": "application/json"})
        resp = session.post(
            EP_LOGIN,
            data={"username": self.secret("TURBONOMIC_USERNAME"), "password": self.secret("TURBONOMIC_PASSWORD")},
        )
        resp.raise_for_status()
        return session

    # -- connection -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        def call() -> Dict[str, Any]:
            session = self._login()
            markets = session.get_json(EP_MARKETS)
            count = len(markets) if isinstance(markets, list) else 0
            return {"host": self._host(), "markets": count}

        return self._probe(call)

    # -- costs (intentionally empty) --------------------------------------

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """Empty by design. Turbonomic is an action engine, not a billing
        source: it has no invoice to report. Use it for recommendations and let
        a billing connector supply the FOCUS cost rows."""
        return self._empty_costs()

    # -- recommendations --------------------------------------------------

    def _live_market(self, session: VendorSession) -> Optional[str]:
        markets = session.get_json(EP_MARKETS)
        if not isinstance(markets, list):
            return None
        for m in markets:
            if str(m.get("state", "")).upper() == "RUNNING" or m.get("displayName") == "Market":
                return first(m, "uuid", default=None)
        return first(markets[0], "uuid", default=None) if markets else None

    def fetch_recommendations(self) -> List[Recommendation]:
        if not self.configured:
            return []
        try:
            session = self._login()
            market = self._live_market(session)
            if not market:
                return []
            actions = session.get_json(
                EP_MARKET_ACTIONS.format(market=market), params={"ascending": "false"}
            )
        except Exception:
            return []
        out: List[Recommendation] = []
        for a in actions if isinstance(actions, list) else []:
            target = a.get("target") or {}
            current = a.get("currentEntity") or {}
            out.append(
                Recommendation(
                    source="turbonomic",
                    cloud=str(first(target, "environmentType", default="")).upper(),
                    resource_id=str(first(target, "uuid", "displayName", default="")),
                    resource_type=str(first(current, "className", default="") or first(target, "className", default="")),
                    lever=str(first(a, "actionType", default="RESIZE")).lower(),
                    action=str(first(a, "details", "actionType", default="Optimize")),
                    estimated_monthly_savings=float(first(a, "savingsAmount", default=0.0) or 0.0),
                    risk=str((a.get("risk") or {}).get("severity", "Medium")).title() or "Medium",
                    detail={"action_type": first(a, "actionType", default="")},
                )
            )
        return out
