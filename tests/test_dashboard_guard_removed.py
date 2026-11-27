# -*- coding: utf-8 -*-
"""面板回归：守护页已整体移除（2026-11-27）。"""
import re
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "views" / "dashboard" / "index.html"


def _text():
    return HTML.read_text(encoding="utf-8")


def test_guard_ui_is_gone():
    text = _text()
    assert "守护" not in text
    for marker in ('data-tab="guard"', 'id="panel-guard"', 'data-guard=', "loadGuard", "renderGuard"):
        assert marker not in text, marker


def test_tabs_have_matching_panels():
    text = _text()
    tabs = re.findall(r'data-tab="([^"]+)"', text)
    panels = re.findall(r'id="panel-([^"]+)"', text)
    assert tabs, "导航为空"
    assert sorted(tabs) == sorted(panels), (tabs, panels)
    assert "guard" not in tabs
