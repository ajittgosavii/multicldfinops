"""Tabs.

Each module exposes a single `render(ctx: DataContext) -> None`. Tabs render;
they never compute. Every number they show comes from `kpi.py` or one of the
engines, so "what does this figure mean?" always has exactly one answer, in
exactly one file.

Tabs receive an already-filtered context. The filter row lives in `app.py`,
above everything it scopes, so every panel on a page shows the same slice.
"""
