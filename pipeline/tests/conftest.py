"""``run_once`` 的假 I/O 夹具。放在这里，是为了让不止一个测试文件能用同一套
（``test_run_once_behavior.py`` 与 M13 的 ``test_preliminary.py``）—— 两套各写各的，
迟早有一套忘了拦某个新 I/O 点，于是测试真的去连网，失败后被 except 吞掉、照样变绿。
"""

from __future__ import annotations

import contextlib
from datetime import date
from typing import Any

import pandas as pd
import pytest


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Any:
    """把 ``run_once`` 的所有 I/O 换成可观测的假货。"""
    from pipeline import run_daily as rd

    calls: dict[str, list[Any]] = {
        "replace_strength": [],
        "upsert_metrics": [],
        "upsert_prices": [],
        "revalidate": [],
        "intraday": [],
    }

    monkeypatch.setattr(rd, "write_sessions", lambda *a, **k: None)
    monkeypatch.setattr(rd, "write_symbols", lambda *a, **k: None)

    def _upsert_prices(conn: Any, rows: list[dict[str, Any]]) -> int:
        calls["upsert_prices"].append(rows)
        return len(rows)

    monkeypatch.setattr(rd, "upsert_prices", _upsert_prices)
    monkeypatch.setattr(rd, "touch_fetch_state", lambda *a, **k: None)
    monkeypatch.setattr(rd, "revalidate_site", lambda r: calls["revalidate"].append(r))
    monkeypatch.setattr(rd, "_existing_prices", lambda *a, **k: {})
    monkeypatch.setattr(rd, "_already_done", lambda *a, **k: False)
    # M13 的三个新 I/O 点。**分钟线必须拦住** —— 不拦的话「有标的落后」的每条测试
    # 都会真的去连 Yahoo，失败后被 except 吞掉，测试照样绿（实测：全量从 10s 变成 29s）。
    # 默认：库里上一个 session 已全员定稿、没有挂着的临时行、分钟线什么都不给。
    monkeypatch.setattr(rd, "_final_rows", lambda conn, symbols, day: len(symbols))
    monkeypatch.setattr(rd, "_preliminary_rows", lambda *a, **k: [])

    def _no_intraday(symbols: Any, session: Any, **k: Any) -> pd.DataFrame:
        calls["intraday"].append((list(symbols), session.date))
        return pd.DataFrame()

    monkeypatch.setattr(rd, "intraday_frame", _no_intraday)
    monkeypatch.setattr(rd, "_carried_scores", lambda *a, **k: [])
    monkeypatch.setattr(rd, "_refresh_events", lambda *a, **k: ({}, True))
    monkeypatch.setattr(rd, "data_transaction", lambda conn: contextlib.nullcontext(conn))

    def _upsert_metrics(conn: Any, rows: list[dict[str, Any]]) -> int:
        calls["upsert_metrics"].append(rows)
        return len(rows)

    def _replace_strength(conn: Any, day: date, rows: list[dict[str, Any]]) -> int:
        calls["replace_strength"].append((day, rows))
        return len(rows)

    monkeypatch.setattr(rd, "upsert_metrics", _upsert_metrics)
    monkeypatch.setattr(rd, "replace_strength", _replace_strength)
    return calls, monkeypatch, rd
