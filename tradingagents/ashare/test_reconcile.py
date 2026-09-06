"""test_reconcile.py — 对账纯逻辑离线单测（合成行情，不联网不调 LLM）。

覆盖：方向命中 / 分层均值 / Spearman IC / abstain与invalid不计 / 样本不足 /
基准缺失降级 / 未到期样本计数。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # noqa: E402

from tradingagents.ashare.reconcile import (  # noqa: E402
    _direction,
    aggregate,
    future_close_at_h,
    spearman,
    window_ret_and_excess,
)

D0 = "2026-09-04"
# as_of 之后依次 6 根：D1..D6，收盘价
CLOSES = [10.5, 10.2, 10.8, 11.0, 12.0, 10.4]


def kline(ticker: str):
    assert ticker in ("AAA", "BBB", "sh000300")
    rows = []
    prev = 10.0
    d = D0
    for c in CLOSES:
        d = _next(d)
        rows.append({"date": d, "close": c, "high": c, "low": c, "volume": 1.0})
    return rows


def _next(d: str):
    from datetime import date, timedelta
    return (date.fromisoformat(d) + timedelta(days=1)).isoformat()


def _rec(ticker="AAA", mark=10.0, bench_mark=1000.0, as_of=D0):
    return {
        "version": 1, "ticker": ticker, "name": "X", "as_of": as_of,
        "decided_on": D0, "is_etf": False, "fundamentals_ok": True,
        "mark": mark, "cost": 10.0, "shares": 100, "benchmark": "000300",
        "benchmark_mark": bench_mark, "horizons": [5, 20],
        "masters": [{"master": "graham", "signal": "bullish", "conviction": 70},
                    {"master": "burry", "signal": "bearish", "conviction": 55},
                    {"master": "marks", "signal": "neutral", "conviction": 50}],
        "manager": {"recommendation": "增持", "confidence": 60},
        "trader": {"action": "持有"},
        "failures": [], "llm_tier": {}, "created_at": D0,
    }


def _kline_for_bench(_s):
    # 基准：as_of 后每根 +10%
    rows, base, d = [], 1000.0, D0
    for i in range(len(CLOSES)):
        d = _next(d)
        rows.append({"date": d, "close": base * 1.1, "high": 0, "low": 0, "volume": 0})
    return rows


def test_future_close_at_h():
    rows = kline("AAA")
    after = [r for r in rows if r["date"] > D0]
    assert future_close_at_h(rows, D0, 5) == CLOSES[4]     # 第5根=12.0
    assert future_close_at_h(rows, D0, 7) is None          # 不足


def test_window_ret_and_excess():
    w = window_ret_and_excess(_rec(), kline("AAA"), _kline_for_bench("x"), 5)
    assert w["ret"] == pytest.approx(12.0 / 10.0 - 1)       # +20%
    assert w["excess"] == pytest.approx(0.20 - 0.10)        # 个股−基准=+10%


def test_direction_mapping():
    assert _direction("master", "graham", "bullish") == 1
    assert _direction("master", "g", "abstain") == 0
    assert _direction("master", "g", "weird") is None
    assert _direction("manager", "m", "买入") == 1
    assert _direction("manager", "m", "减持") == -1
    assert _direction("manager", "m", "持有") == 0
    assert _direction("trader", "t", "止损") == -1
    assert _direction("trader", "t", "观望") == 0
    assert _direction("trader", "t", "???") is None


def test_aggregate_hits_and_stratification():
    agg = aggregate([_rec()], 5, kline_fn=kline, bench_kline_fn=_kline_for_bench)
    by = {(r["entity_type"], r["entity_id"]): r for r in agg["entities"]}
    # graham bullish 且 ret>0 → 命中 1/1
    g = by[("master", "graham")]
    assert g["n_dir"] == 1 and g["n_hit"] == 1 and g["hit_rate"] == 1.0
    assert g["mean_ret"] == pytest.approx(0.20)
    assert g["mean_excess"] == pytest.approx(0.10)
    # burry bearish 且 ret>0 → 不命中
    b = by[("master", "burry")]
    assert b["n_dir"] == 1 and b["n_hit"] == 0 and b["hit_rate"] == 0.0
    # marks neutral → 不计方向但计入 n_total
    m = by[("master", "marks")]
    assert m["n_dir"] == 0 and m["n_total"] == 1
    # manager 增持→多头命中；trader 持有→中性不计方向
    assert by[("manager", "manager")]["n_hit"] == 1
    assert by[("trader", "trader")]["n_dir"] == 0


def test_insufficient_and_benchmark_missing(tmp_path):
    # 未到期：基准行情只有1根 → h5 无第5根
    short = kline("AAA")[:1]
    agg = aggregate([_rec()], 5, kline_fn=lambda t: short,
                    bench_kline_fn=_kline_for_bench)
    assert agg["insufficient"] == 1
    assert agg["entities"] == []

    # 基准缺失 → excess=None 但不影响 ret
    rec_no_bench = _rec(bench_mark=None)
    agg2 = aggregate([rec_no_bench], 5, kline_fn=kline, bench_kline_fn=_kline_for_bench)
    row = agg2["entities"][0]
    assert row["mean_excess"] is None and row["mean_ret"] == pytest.approx(0.20)


def test_spearman_monotone_and_small_sample():
    xs = list(range(10, 20))
    assert spearman(xs, [x * 2 for x in xs]) == pytest.approx(1.0)
    assert spearman(xs[:5], xs[:5]) is None      # 样本 <10
    assert spearman([1] * 10, [1] * 10) is None  # 常数列


def test_unknown_direction_counted_not_guessed():
    rec = _rec()
    rec["trader"] = {"action": "???unknown"}
    agg = aggregate([rec], 5, kline_fn=kline, bench_kline_fn=_kline_for_bench)
    assert agg["unknown"] >= 1
