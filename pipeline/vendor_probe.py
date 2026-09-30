"""供应商结算时刻探针（**临时诊断工具，不是管道的一部分**）。

M12 把两个生产阈值（``settle_minutes: 330`` 与 ``vendor_deadline_et: 22:30``）
建立在**一天的两个观测点**上：2026-09-29 的 21:05 ET 拿到半根 bar、22:09 ET
拿到完整的。两者之间只有 15 分钟余量，而 M11/M12 反复栽的正是
「把一条局部观察升格成通用判据」。这个探针把那两个数换成有分布支撑的数。

**它只读。** 不连数据库、不写任何表、不碰 ``runs``。工作流里没有
``MARKME_DB_URL``，`test_vendor_probe.py` 把这条钉住了。

**它必须绕开 ``pipeline.fetch``。** `fetch.py` 会把 ``adj_close`` 为 NaN 的行
整行丢掉（库里那一列是 NOT NULL），而那正是这个探针要观测的东西 ——
走 `fetch_window` 的话，半成品 bar 在被测量之前就已经消失了。
所以这里直接调 yfinance，并且**逐字照抄 `fetch.py` 的那组参数**：
观测对象必须和生产用的是同一条路径，否则测到的是另一件事。

输出是 CSV，一行一个 (采样时刻, 标的)。用 ``sampled_at_utc``（真实墙上时刻）
而不是 cron 的预定时刻 —— GitHub 会迟到 3 小时以上，预定时刻没有意义。

跑够两周之后：算出「17 只全部结算完」的时刻分布，用它重定
``settle_minutes`` 与 ``vendor_deadline_et``，然后**删掉这个文件与那个工作流**。
"""

from __future__ import annotations

import csv
import sys
import time as _time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.calendar_gate import ET, session_on_or_before
from pipeline.config import load_config
from pipeline.sessions import build_sessions, load_calendar

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from collections.abc import Sequence

__all__ = ["FIELDS", "Sample", "main", "sample_once"]

#: CSV 的列。``has_row`` 与 ``adj_close`` 是关键的两列 ——
#: 闸门 3 看的就是「这个标的今天这根 bar 在不在、`adj_close` 是不是 NaN」。
FIELDS = (
    "sampled_at_utc",
    "sampled_at_et",
    "session_date",
    "symbol",
    "has_row",
    "adj_close",
    "close",
    "open",
    "high",
    "low",
    "volume",
)

#: 采样间隔与每跑的采样次数。4 条错开的 cron × 8 次 = 每个交易日约 32 个时刻，
#: 而 GitHub 的延迟会把它们摊得更开 —— 这正是我们想要的覆盖面。
INTERVAL_SECONDS = 15 * 60
SAMPLES_PER_RUN = 8


@dataclass(frozen=True, slots=True)
class Sample:
    sampled_at_utc: datetime
    session_date: date
    symbol: str
    has_row: bool
    values: dict[str, float | None]

    def as_row(self) -> dict[str, Any]:
        return {
            "sampled_at_utc": self.sampled_at_utc.isoformat(timespec="seconds"),
            "sampled_at_et": self.sampled_at_utc.astimezone(ET).isoformat(timespec="seconds"),
            "session_date": self.session_date.isoformat(),
            "symbol": self.symbol,
            "has_row": int(self.has_row),
            **{k: self.values.get(k) for k in FIELDS[5:]},
        }


def _finite(value: Any) -> float | None:
    """NaN / ±Inf → ``None``。CSV 里 ``NaN`` 和空格子读起来是两回事，别混。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def sample_once(
    symbols: Sequence[str],
    session_date: date,
    *,
    now: datetime | None = None,
    download: Any = None,
) -> list[Sample]:
    """拉一次，给每个标的记一行。

    ``download`` 可注入，测试用；默认走 yfinance，参数与 `fetch.py` 一致。
    """
    stamp = now or datetime.now(tz=UTC)
    fn = download or _yf_download
    raw = fn(
        list(symbols),
        start=session_date.isoformat(),
        end=(session_date + timedelta(days=1)).isoformat(),
        auto_adjust=False,  # 与 fetch.py 相同 —— 必须观测生产走的那条路径
        progress=False,
        group_by="column",
        threads=False,
    )
    out: list[Sample] = []
    for sym in symbols:
        values: dict[str, float | None] = {}
        has_row = False
        for field, column in (
            ("adj_close", "Adj Close"),
            ("close", "Close"),
            ("open", "Open"),
            ("high", "High"),
            ("low", "Low"),
            ("volume", "Volume"),
        ):
            values[field] = _cell(raw, column, sym, session_date)
        # 「这一行在不在」= 供应商给没给这一天；与「值是不是 NaN」是两件事。
        has_row = any(v is not None for v in values.values())
        out.append(Sample(stamp, session_date, sym, has_row, values))
    return out


def _cell(raw: Any, column: str, symbol: str, day: date) -> float | None:
    import pandas as pd

    if raw is None or getattr(raw, "empty", True):
        return None
    try:
        frame: pd.DataFrame = raw
        col = frame[(column, symbol)] if isinstance(frame.columns, pd.MultiIndex) else frame[column]
        index = pd.DatetimeIndex(col.index)
        if index.tz is not None:
            index = index.tz_localize(None)
        hits = col[index.normalize() == pd.Timestamp(day)]
        return _finite(hits.iloc[0]) if len(hits) else None
    except (KeyError, IndexError, AttributeError):
        return None


def _yf_download(*args: object, **kwargs: object) -> Any:
    import yfinance as yf

    return yf.download(*args, **kwargs)


def _append(path: Path, rows: list[dict[str, Any]]) -> None:
    """追加写。文件不存在时补表头 —— 多个工作流各自追加，谁先跑到谁建表头。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(FIELDS))
        if fresh:
            writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    cfg = load_config()
    symbols = [s.symbol for s in cfg.universe.symbols if s.enabled]
    now = datetime.now(tz=UTC)
    today_et = now.astimezone(ET).date()

    sessions = build_sessions(load_calendar(), today_et - timedelta(days=14), today_et)
    session = session_on_or_before(sessions, today_et)
    if session is None or session.date != today_et:
        # 非交易日（含延迟到次日凌晨、ET 日期已翻页的情形）。**不采样、不报错。**
        print(f"vendor-probe: {today_et} 不是交易日，跳过")
        return 0

    out = Path("docs/probe/vendor-settle.csv")
    for i in range(SAMPLES_PER_RUN):
        if i:
            _time.sleep(INTERVAL_SECONDS)
        try:
            rows = [s.as_row() for s in sample_once(symbols, session.date)]
        except Exception as exc:
            print(f"vendor-probe: 第 {i + 1} 次采样失败：{exc}")
            continue
        _append(out, rows)
        settled = sum(1 for r in rows if r["adj_close"] is not None)
        et = datetime.now(tz=UTC).astimezone(ET)
        print(f"vendor-probe: {et:%H:%M} ET — {settled}/{len(rows)} 只已结算")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
