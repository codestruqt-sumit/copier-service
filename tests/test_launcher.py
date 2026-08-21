"""Launcher tab management: keep exactly one Tradovate tab (dedupe / open-if-missing).

CDP is mocked - no real browser. These lock in the multi-tab and zombie-revive fixes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.launcher as launcher


def test_dedupe_closes_extra_tradovate_tabs(monkeypatch):
    pages = [
        {"type": "page", "id": "a", "url": "https://trader.tradovate.com/x"},
        {"type": "page", "id": "b", "url": "https://trader.tradovate.com/y"},
        {"type": "page", "id": "c", "url": "https://example.com/"},
    ]

    def close(host, port, tid):
        pages[:] = [t for t in pages if t["id"] != tid]
        return True

    monkeypatch.setattr(launcher, "_list_pages", lambda h, p: list(pages))
    monkeypatch.setattr(launcher, "_close_tab", close)
    monkeypatch.setattr(launcher, "_spawn", lambda *a, **k: pytest.fail("must not spawn"))

    out = launcher.ensure_single_tradovate_tab("h", 1, "edge.exe", Path("."))
    assert out["closed"] == 1 and out["opened"] == 0 and out["tabs"] == 1
    assert [t["id"] for t in launcher._tradovate(pages)] == ["a"]   # kept the first


def test_opens_a_tab_when_none_present(monkeypatch):
    pages = [{"type": "page", "id": "c", "url": "https://example.com/"}]
    spawned = []

    def spawn(binary, args):
        spawned.append(args)
        pages.append({"type": "page", "id": "t", "url": "https://trader.tradovate.com/"})

    monkeypatch.setattr(launcher, "_list_pages", lambda h, p: list(pages))
    monkeypatch.setattr(launcher, "_spawn", spawn)
    monkeypatch.setattr(launcher.time, "sleep", lambda *_: None)

    out = launcher.ensure_single_tradovate_tab("h", 1, "edge.exe", Path("."))
    assert out["opened"] == 1 and out["closed"] == 0 and out["tabs"] == 1
    assert spawned and any("trader.tradovate.com" in a for a in spawned[0])


def test_single_tab_is_left_alone(monkeypatch):
    pages = [{"type": "page", "id": "a", "url": "https://trader.tradovate.com/x"}]
    monkeypatch.setattr(launcher, "_list_pages", lambda h, p: list(pages))
    monkeypatch.setattr(launcher, "_spawn", lambda *a, **k: pytest.fail("must not spawn"))
    monkeypatch.setattr(launcher, "_close_tab", lambda *a, **k: pytest.fail("must not close"))

    out = launcher.ensure_single_tradovate_tab("h", 1, "edge.exe", Path("."))
    assert out == {"opened": 0, "closed": 0, "tabs": 1}
