"""FocusFileConnector -- the escape hatch.

The whole platform is a bet that FOCUS is the only integration surface that
matters. This connector cashes that bet in the most direct way possible: it
ingests ANY FOCUS-conformant CSV or Parquet, from a local path, an uploaded
file object, or an object-store URI (s3://, az://, gs://).

That means every procured FinOps tool that can export FOCUS -- CloudZero,
Vantage, Finout's MegaBill, Flexera CBI, Cloudability's FOCUS export -- plugs
in here with *zero* new code. The customer configures a scheduled export to a
bucket, points this connector at it, and every dashboard works. We only write a
bespoke vendor connector when a tool cannot emit FOCUS at all.

Because the input is already FOCUS, the transform is deliberately thin:
`focus.normalize()` (resolve column aliases, add missing optional columns,
coerce dtypes) followed by `focus.validate()`. We surface the ValidationResult
verbatim in `test_connection().detail` so an admin can see exactly why a file
was rejected rather than getting a silent empty frame.

Object-store access uses fsspec back-ends (s3fs / adlfs / gcsfs), imported
lazily so a customer who only reads local files never needs them installed.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Optional

import pandas as pd

import focus
from connectors.base import (
    AuthKind,
    Capability,
    ConnectionResult,
    Connector,
    ConnectorSpec,
)

# URI scheme -> the fsspec back-end package pandas needs to read it. These are
# optional; a local-file user never installs them.
_REMOTE_BACKENDS = {
    "s3://": "s3fs",
    "az://": "adlfs",
    "abfs://": "adlfs",
    "gs://": "gcsfs",
    "gcs://": "gcsfs",
}


class FocusFileConnector(Connector):
    """Load a FOCUS-conformant file from a path, a file object, or a URI.

    Options:
      * ``path``   -- a local path or an s3://, az://, gs:// URI.
      * ``file``   -- a file-like object (e.g. a Streamlit upload).
      * ``format`` -- optional override: 'csv' | 'parquet'. Inferred from the
                      extension when absent.
      * ``storage_options`` -- optional dict passed to pandas for the back-end
                      (credentials, endpoint). Never logged.
    """

    @property
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            key="focus_file",
            display_name="FOCUS file / object store",
            vendor="FOCUS",
            # Any provider that emits a conformant export, named or not. The
            # list below is what we style and price levers for; a ProviderName
            # outside it still loads, allocates and forecasts.
            clouds=["AWS", "Azure", "GCP", "OCI"],
            auth=AuthKind.NONE,
            capabilities=[Capability.COSTS, Capability.NATIVE_FOCUS],
            required_secrets=[],
            optional_secrets=[],
            base_url="",
            docs_url="https://focus.finops.org/focus-specification/",
            focus_support="native",
            notes=(
                "Ingests any FOCUS-conformant CSV/Parquet from a local path, an "
                "uploaded file, or an s3://, az:// or gs:// URI. The universal "
                "adapter for any tool that can export FOCUS (CloudZero, Vantage, "
                "Finout, Flexera CBI, Cloudability)."
            ),
        )

    # ---- source resolution ----------------------------------------------

    def _has_source(self) -> bool:
        return bool(self.options.get("path")) or self.options.get("file") is not None

    @staticmethod
    def _remote_backend_for(path: str) -> Optional[str]:
        for scheme, pkg in _REMOTE_BACKENDS.items():
            if path.startswith(scheme):
                return pkg
        return None

    def _detect_format(self) -> str:
        explicit = self.options.get("format")
        if explicit:
            return str(explicit).lower().lstrip(".")
        path = self.options.get("path")
        name = path
        if name is None:
            f = self.options.get("file")
            name = getattr(f, "name", "") or ""
        ext = os.path.splitext(str(name))[1].lower().lstrip(".")
        if ext in ("parquet", "pq", "parq"):
            return "parquet"
        # Default to CSV -- it is the one format every tool can emit and the
        # only one we can read from a nameless file object without guessing.
        return "csv"

    def _load(self) -> pd.DataFrame:
        """Read the source into a raw DataFrame. May raise -- callers wrap it."""
        fmt = self._detect_format()
        source: Any = self.options.get("file")
        storage_options = self.options.get("storage_options") or None

        if source is None:
            path = self.options.get("path")
            if not path:
                raise ValueError("No 'path' or 'file' option provided.")
            backend = self._remote_backend_for(str(path))
            if backend is not None:
                # Lazy: the fsspec back-end is only needed for remote URIs.
                try:
                    __import__(backend)
                except ImportError as exc:  # pragma: no cover - env dependent
                    raise ImportError(
                        f"{backend} not installed; required to read {path.split('://')[0]}:// URIs. "
                        f"Install it to enable object-store ingest."
                    ) from exc
            source = path

        if fmt == "parquet":
            return pd.read_parquet(source, storage_options=storage_options)
        return pd.read_csv(source, storage_options=storage_options)

    # ---- contract -------------------------------------------------------

    def test_connection(self) -> ConnectionResult:
        if not self._has_source():
            return ConnectionResult(
                ok=False,
                message="No file configured. Provide a 'path' or 'file' option.",
            )
        try:
            raw = self._load()
        except ImportError as exc:
            return ConnectionResult(ok=False, message=str(exc))
        except Exception as exc:  # never raise out of test_connection
            return ConnectionResult(ok=False, message=f"Could not read source: {exc}")

        normalized = focus.normalize(raw)
        result = focus.validate(normalized)
        return ConnectionResult(
            ok=result.ok,
            message=result.summary(),
            detail={
                "errors": result.errors,
                "warnings": result.warnings,
                "row_count": result.row_count,
                "format": self._detect_format(),
            },
        )

    def fetch_costs(self, start: date, end: date) -> pd.DataFrame:
        if not self._has_source():
            return focus.empty_frame()
        try:
            raw = self._load()
        except Exception:
            # A broken source yields an empty (still-conformant) frame rather
            # than a crashed page; test_connection() carries the reason.
            return focus.empty_frame()

        df = self._stamp(focus.normalize(raw))
        if {"ChargePeriodStart"}.issubset(df.columns):
            mask = (df["ChargePeriodStart"] >= pd.Timestamp(start)) & (
                df["ChargePeriodStart"] < pd.Timestamp(end)
            )
            # Only filter when the column is usable; a file with unparseable
            # dates should surface as a validation error, not vanish silently.
            if mask.notna().any():
                df = df[mask.fillna(False)]
        return df
