"""The architecture diagrams must not lie.

They name modules and counts. A rename or a deleted connector would leave the
diagram quietly wrong, which is worse than having no diagram -- a client reads
it as documentation. These tests are cheap and catch exactly that drift.
"""

from __future__ import annotations

import importlib
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

pytest.importorskip("matplotlib")
import diagrams as dg  # noqa: E402


def test_every_module_named_on_the_low_level_diagram_imports() -> None:
    for name in dg.LLD_MODULES:
        assert importlib.import_module(name), name


def test_diagrams_render_to_svg_and_png(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dg, "OUT_DIR", str(tmp_path))
    built = dg.build_all()
    assert set(built) == {"hld", "end_user_view", "lld"}
    for paths in built.values():
        assert len(paths) == 2
        for p in paths:
            assert os.path.exists(p) and os.path.getsize(p) > 5_000


def test_svg_keeps_text_as_text(tmp_path, monkeypatch) -> None:
    """`svg.fonttype='none'` keeps labels editable and searchable rather than
    outlining them to paths, which is the whole point of shipping SVG."""
    monkeypatch.setattr(dg, "OUT_DIR", str(tmp_path))
    svg_path = dg.hld()[0]
    svg = open(svg_path, encoding="utf-8").read()
    assert "<text" in svg
    assert "FOCUS 1.2 DataFrame" in svg
    assert "viewBox" in svg


def test_connector_count_on_the_diagram_matches_the_registry() -> None:
    """The high-level diagram advertises a connector count. Read it back."""
    import connectors

    src = open(os.path.join(ROOT, "tools", "diagrams.py"), encoding="utf-8").read()
    assert f"{len(connectors.REGISTRY)} connectors" in src


def test_lever_count_on_the_diagram_matches_the_catalog() -> None:
    import optimize

    src = open(os.path.join(ROOT, "tools", "diagrams.py"), encoding="utf-8").read()
    assert f"{len(optimize.LEVERS)} levers" in src
