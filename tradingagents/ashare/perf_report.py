#!/usr/bin/env python3
"""perf_report.py — 绩效跟踪：当前持仓组合 vs 沪深300（回放近似）

组合净值 = 当前持仓(shares 固定) 用日K回放；因历史每日持仓快照仅 9/8 起
有 record，早期用"当前持仓"近似（标注）。运行几次后换真实快照。
输出区间收益/MDD/超额。建议每周跑一次归档 logs/perf-*.md。
用法: .venv/bin/python -m tradingagents.ashare.perf_report [--days 60]
"""
from __future__ import annotations

import argparse
import math
import statistics
from datetime import date
from pathlib import Path

import yaml

from tradingagents.ashare.material import fetch_daily_kline, fetch_symbol_kline

TICKERS = "/Users/vega/git/ai-hedge-fund/config/tickers.yaml"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    data = yaml.safe_load(Path(TICKERS).read_text(encoding="utf-8"))
    hold = {t["code"]: float(t.get("shares") or 0) for t in data["tickers"]}
    hold = {c: s for c, s in hold.items() if s > 0}
    if not hold:
        print("无持仓")
        return

    # 每票日收益 + 市值权重
    rets, marks = {}, {}
    for c in hold:
        try:
            rows = [k for k in fetch_daily_kline(c, 400) if k["date"] <= args.date]
            if len(rows) > args.days + 5:
                rets[c] = {r["date"]: rows[i + 1]["close"] / r["close"] - 1
                           for i, r in enumerate(rows[:-1])}
                marks[c] = rows[-1]["close"]
        except Exception:
            pass
    common = sorted(set.intersection(*[set(r) for r in rets.values()]))[-args.days:]
    if len(common) < 10:
        print("共同交易日不足")
        return
    tot = sum(hold[c] * marks[c] for c in hold)
    w = {c: hold[c] * marks[c] / tot for c in hold}

    # 组合净值
    nav = 1.0
    peak = 1.0
    mdd = 0.0
    drs = []
    for dt in common:
        r = sum(w[c] * rets[c][dt] for c in w if dt in rets[c])
        nav *= (1 + r)
        drs.append(r)
        peak = max(peak, nav)
        mdd = max(mdd, (peak - nav) / peak)
    # 沪深300
    try:
        rows300 = [k for k in fetch_symbol_kline("sh000300", 400) if k["date"] <= args.date]
        d300 = {r["date"]: rows300[i + 1]["close"] / r["close"] - 1
                for i, r in enumerate(rows300[:-1])}
        nav3 = 1.0
        peak3 = 1.0
        mdd3 = 0.0
        for dt in common:
            if dt in d300:
                nav3 *= (1 + d300[dt])
                peak3 = max(peak3, nav3)
                mdd3 = max(mdd3, (peak3 - nav3) / peak3)
    except Exception:
        nav3, mdd3 = None, None

    n = len(common)
    tot_r = (nav - 1) * 100
    ann = ((1 + tot_r / 100) ** (252 / n) - 1) * 100 if n else 0
    vol = statistics.stdev(drs) * math.sqrt(252) * 100
    sharpe = (ann - 2) / vol if vol else 0
    tot3 = (nav3 - 1) * 100 if nav3 else None
    ex = tot_r - tot3 if tot3 is not None else None

    print(f"# 组合绩效 · {args.date}（近 {n} 交易日）\n")
    print(f"组合：区间 {tot_r:+.1f}% ｜ 年化 {ann:+.1f}% ｜ 波动 {vol:.0f}% ｜ 夏普 {sharpe:+.2f} ｜ MDD {mdd*100:.0f}%")
    if tot3 is not None:
        print(f"沪深300：区间 {tot3:+.1f}% ｜ MDD {mdd3*100:.0f}% ｜ 超额 {ex:+.1f}%")
    print("\n> 早期绩效为'当前持仓'回放近似；record 积累后换真实每日快照。")


if __name__ == "__main__":
    main()
