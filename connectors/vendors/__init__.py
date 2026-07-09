"""Pluggable connectors for commercial FinOps platforms.

The architectural bet in `focus.py` is that a customer's *procured* tool is just
another source of FOCUS rows. Con Edison might already run Cloudability, or
CloudHealth, or Flexera One; a Kubernetes-heavy team might run Kubecost; a
platform team might read allocation out of ServiceNow. Each becomes one subclass
of `connectors.base.Connector` that returns a `focus.normalize()`d frame, and
every dashboard, KPI and optimizer keeps working unchanged.

Each module here targets exactly one vendor, documents the ONE thing that will
bite an integrator in its module docstring, and declares its `Capability` set
honestly -- several of these tools have no anomaly or budget *read* API, so they
simply omit that capability rather than fake it.

The registry in `connectors/__init__.py` resolves these lazily by key, so
importing this package pulls in nothing but `requests`.
"""

from __future__ import annotations

from connectors.vendors._http import VendorConnector, VendorSession

__all__ = ["VendorConnector", "VendorSession"]
