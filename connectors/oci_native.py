"""OCINativeConnector -- native Oracle Cloud Infrastructure billing.

OCI publishes cost data as **files, not an API**. Every six hours Oracle writes
gzipped CSV reports into an Oracle-owned Object Storage bucket that sits outside
your tenancy:

    namespace : bling                 (Oracle-owned, the same for every customer)
    bucket    : <your tenancy OCID>   (one bucket per customer)
    prefix    : FOCUS Reports/<yyyy>/<mm>/<dd>/

Two report families live in that bucket. The proprietary "cost reports" under
``reports/cost-csv/`` carry Oracle's own column names. The **FOCUS reports**
under ``FOCUS Reports/`` are already FOCUS-conformant, so this connector reads
those and the mapping is near identity: parse the CSV, canonicalise
``ProviderName``, hand it to ``focus.normalize``.

If Oracle's export is an older FOCUS revision than ours, ``focus.normalize``
adds the columns we know about as null rather than failing. That is the right
failure mode -- a missing optional column is not a broken bill.

**Reading the bucket is not implied by tenancy admin.** Oracle's own tenancy
owns it, so you must endorse your group into it explicitly:

    define tenancy usage-report as ocid1.tenancy.oc1..aaaaaaaaned4fkpkisbwjlr56u7cj63lf3wffbilvqknstgtvzub7vhqkggq
    endorse group <group> to read objects in tenancy usage-report

(That OCID is Oracle's published usage-report tenancy, identical for every
customer. It is documentation, not a credential.)

Auth is OCI **API-key request signing**: an RSA private key, identified by a
fingerprint, belonging to a user OCID in a tenancy OCID. There is no token to
mint. The key is read via ``self.secret(...)``, passed to the SDK as
``key_content``, and never written to disk or logged.

Cloud Advisor (the ``oci.optimizer`` service) supplies native recommendations.
Its savings are tenancy-level aggregates keyed by recommendation name, not
per-resource rows, so ``resource_id`` is empty and ``resource_count`` lives in
``detail``. We do not pretend otherwise.

The ``oci`` SDK is imported lazily. Importing this module with nothing installed
succeeds; ``test_connection()`` reports the missing SDK.

API references:
  Cost & FOCUS reports: https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costusagereportsoverview.htm
  Listing reports:      https://docs.oracle.com/en-us/iaas/Content/Billing/Tasks/list-cost-usage-report.htm
  Cloud Advisor:        https://docs.oracle.com/en-us/iaas/tools/python/latest/api/optimizer/client/oci.optimizer.OptimizerClient.html
  Budgets:              https://docs.oracle.com/en-us/iaas/tools/python/latest/api/budget.html
"""

from __future__ import annotations

import gzip
import io
import time
from datetime import date
from typing import Any, Dict, Iterator, List, Optional

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

# Oracle-owned, identical for every tenancy. Not a secret, not configurable.
REPORT_NAMESPACE = "bling"
FOCUS_PREFIX = "FOCUS Reports"

# Oracle's usage-report tenancy, quoted in the IAM `endorse` policy above.
USAGE_REPORT_TENANCY = (
    "ocid1.tenancy.oc1..aaaaaaaaned4fkpkisbwjlr56u7cj63lf3wffbilvqknstgtvzub7vhqkggq"
)

# Whatever Oracle stamps into ProviderName, the rest of this platform keys on
# "OCI" -- theme.PROVIDER_SLOT, optimize._PROFILES, finops_core.CLOUDS. Fold the
# known spellings and leave anything unrecognised alone, so a genuine change in
# Oracle's output shows up as an unstyled provider rather than being silently
# absorbed.
_PROVIDER_ALIASES = {
    "oci": "OCI",
    "oracle": "OCI",
    "oracle cloud": "OCI",
    "oracle cloud infrastructure": "OCI",
}

# Cloud Advisor recommendation name -> our lever vocabulary.
_ADVISOR_LEVER = {
    "compute-rightsizing": "rightsizing",
    "compute-instance-idle": "idle_resource",
    "block-volume-underutilized": "idle_resource",
    "unattached-boot-volume": "idle_resource",
    "unattached-block-volume": "idle_resource",
    "idle-load-balancer": "idle_resource",
    "unused-reserved-public-ip": "idle_resource",
    "object-storage-lifecycle": "storage_tiering",
}


def _month_prefixes(start: date, end: date) -> Iterator[str]:
    """`FOCUS Reports/yyyy/mm/` for each month the window touches.

    We list per month, not per day. A two-year window is 25 list calls instead
    of 730, and listing the bare `FOCUS Reports/` prefix would pull every object
    Oracle has ever written for the tenancy.
    """
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield f"{FOCUS_PREFIX}/{y:04d}/{m:02d}/"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _day_of(object_name: str) -> Optional[date]:
    """`FOCUS Reports/2026/05/24/report-00001.csv.gz` -> date(2026, 5, 24)."""
    parts = object_name.split("/")
    if len(parts) < 4:
        return None
    try:
        return date(int(parts[1]), int(parts[2]), int(parts[3]))
    except (ValueError, IndexError):
        return None


class OCINativeConnector(Connector):
    """Reads Oracle's FOCUS cost reports out of the `bling` bucket."""

    # ---- spec -----------------------------------------------------------

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="oci_native",
            display_name="Oracle Cloud Infrastructure (native billing)",
            vendor="Oracle Cloud Infrastructure",
            clouds=["OCI"],
            auth=AuthKind.API_KEY_SIGNING,
            capabilities=[
                Capability.COSTS,
                Capability.RECOMMENDATIONS,
                Capability.BUDGETS,
                Capability.NATIVE_FOCUS,
            ],
            required_secrets=[
                "OCI_TENANCY_OCID",
                "OCI_USER_OCID",
                "OCI_FINGERPRINT",
                "OCI_PRIVATE_KEY",
                "OCI_REGION",
            ],
            optional_secrets=["OCI_PRIVATE_KEY_PASSPHRASE"],
            base_url=f"https://objectstorage.<region>.oraclecloud.com/n/{REPORT_NAMESPACE}",
            docs_url="https://docs.oracle.com/en-us/iaas/Content/Billing/Concepts/costusagereportsoverview.htm",
            focus_support="native",
            notes=(
                "Cost path reads gzipped FOCUS CSVs from the Oracle-owned 'bling' "
                "bucket (bucket name = your tenancy OCID). Requires an IAM "
                "'endorse group ... to read objects in tenancy usage-report' "
                "policy -- tenancy admin alone is not sufficient. Recommendations "
                "come from Cloud Advisor and are tenancy-level aggregates."
            ),
        )

    # ---- credentials (lazy) ---------------------------------------------

    def _config(self) -> Dict[str, Any]:
        """Build an OCI SDK config from secrets. The key stays in memory."""
        cfg: Dict[str, Any] = {
            "user": self.secret("OCI_USER_OCID"),
            "tenancy": self.secret("OCI_TENANCY_OCID"),
            "fingerprint": self.secret("OCI_FINGERPRINT"),
            "key_content": self.secret("OCI_PRIVATE_KEY"),
            "region": self.secret("OCI_REGION"),
        }
        passphrase = self.secret("OCI_PRIVATE_KEY_PASSPHRASE")
        if passphrase:
            cfg["pass_phrase"] = passphrase
        return cfg

    def _object_storage(self):
        import oci  # noqa: WPS433

        return oci.object_storage.ObjectStorageClient(self._config())

    def _tenancy(self) -> str:
        return str(self.secret("OCI_TENANCY_OCID") or "")

    # ---- contract: test -------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        try:
            import oci  # noqa: F401
        except ImportError:
            return ConnectionResult(
                ok=False,
                message="The 'oci' SDK is not installed; required for the OCI "
                "native connector. Install oci to enable it.",
            )
        missing = self.missing_secrets()
        if missing:
            return ConnectionResult(
                ok=False,
                message=f"Not configured. Missing secret(s): {', '.join(missing)}",
                detail={"missing": missing},
            )

        started = time.perf_counter()
        try:
            client = self._object_storage()
            # One object is enough to prove the endorse policy is in place. The
            # bucket is Oracle's, so a 404 here means the policy, not the data.
            resp = client.list_objects(
                namespace_name=REPORT_NAMESPACE,
                bucket_name=self._tenancy(),
                prefix=FOCUS_PREFIX,
                limit=1,
            )
        except Exception as exc:  # noqa: BLE001 -- must not raise, by contract
            return ConnectionResult(
                ok=False,
                message=f"Could not read the OCI cost-report bucket: {type(exc).__name__}. "
                "Check the 'endorse group ... to read objects in tenancy "
                "usage-report' policy and the region.",
                detail={"namespace": REPORT_NAMESPACE, "bucket": "<tenancy OCID>"},
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        found = len(getattr(resp.data, "objects", []) or [])
        return ConnectionResult(
            ok=True,
            message="Connected to the OCI FOCUS cost reports."
            if found
            else "Connected, but no FOCUS reports found yet (Oracle writes them every six hours).",
            detail={"namespace": REPORT_NAMESPACE, "prefix": FOCUS_PREFIX, "objects_seen": found},
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    # ---- contract: costs ------------------------------------------------

    def _list_report_objects(self, client, start: date, end: date) -> List[str]:
        names: List[str] = []
        for prefix in _month_prefixes(start, end):
            page = None
            while True:
                resp = client.list_objects(
                    namespace_name=REPORT_NAMESPACE,
                    bucket_name=self._tenancy(),
                    prefix=prefix,
                    start=page,
                )
                for obj in resp.data.objects or []:
                    day = _day_of(obj.name)
                    if day is not None and start <= day < end:
                        names.append(obj.name)
                page = getattr(resp.data, "next_start_with", None)
                if not page:
                    break
        return names

    def _read_report(self, client, name: str) -> pd.DataFrame:
        blob = client.get_object(
            namespace_name=REPORT_NAMESPACE,
            bucket_name=self._tenancy(),
            object_name=name,
        ).data.content
        return pd.read_csv(io.BytesIO(gzip.decompress(blob)), low_memory=False)

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        """FOCUS rows for [start, end). One report file per six hours per day."""
        client = self._object_storage()
        names = self._list_report_objects(client, start, end)
        if not names:
            return focus.empty_frame()

        frames = [self._read_report(client, n) for n in sorted(names)]
        raw = pd.concat(frames, ignore_index=True)

        if "ProviderName" in raw.columns:
            raw["ProviderName"] = raw["ProviderName"].map(
                lambda v: _PROVIDER_ALIASES.get(str(v).strip().lower(), v)
            )
        else:
            raw["ProviderName"] = "OCI"

        return self._stamp(self.to_focus(raw))

    # ---- contract: recommendations --------------------------------------

    def fetch_recommendations(self) -> List[Recommendation]:
        """Cloud Advisor findings. Returns [] rather than raising, by contract."""
        try:
            import oci  # noqa: WPS433

            client = oci.optimizer.OptimizerClient(self._config())
            resp = client.list_recommendations(
                compartment_id=self._tenancy(),
                compartment_id_in_subtree=True,
            )
        except Exception:  # noqa: BLE001 -- a source without Advisor is a normal state
            return []

        out: List[Recommendation] = []
        for item in getattr(resp.data, "items", []) or []:
            saving = float(getattr(item, "estimated_cost_saving", 0.0) or 0.0)
            if saving <= 0:
                continue
            name = str(getattr(item, "name", "") or "")
            out.append(
                Recommendation(
                    source="OCI Cloud Advisor",
                    cloud="OCI",
                    # Advisor aggregates by recommendation, not by resource.
                    resource_id="",
                    resource_type=str(getattr(item, "category_id", "") or ""),
                    lever=_ADVISOR_LEVER.get(name, "other"),
                    action=str(getattr(item, "description", "") or name),
                    estimated_monthly_savings=saving,
                    confidence=0.8,
                    account_id=self._tenancy(),
                    detail={
                        "recommendation": name,
                        "importance": str(getattr(item, "importance", "") or ""),
                        "status": str(getattr(item, "status", "") or ""),
                        "basis": "Cloud Advisor reports a tenancy-level aggregate, not resource rows.",
                    },
                )
            )
        return out

    # ---- contract: budgets ----------------------------------------------

    def fetch_budgets(self) -> pd.DataFrame:
        """OCI budgets are per-compartment amounts, with no application dimension.

        We map compartment -> application, which is true only where compartments
        are the allocation boundary. Where they are not, the Allocation tab's
        tag-based view is the honest one.
        """
        cols = ["period", "cloud", "application", "budget"]
        try:
            import oci  # noqa: WPS433

            client = oci.budget.BudgetClient(self._config())
            resp = client.list_budgets(compartment_id=self._tenancy())
        except Exception:  # noqa: BLE001
            return pd.DataFrame(columns=cols)

        period = date.today().replace(day=1).isoformat()
        rows = [
            {
                "period": period,
                "cloud": "OCI",
                "application": str(getattr(b, "display_name", "") or "Unallocated"),
                "budget": float(getattr(b, "amount", 0.0) or 0.0),
            }
            for b in (resp.data or [])
        ]
        return pd.DataFrame(rows, columns=cols)
