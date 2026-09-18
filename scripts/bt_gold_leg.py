#!/usr/bin/env python3
"""bt_gold_leg.py — 黄金腿选什么：黄金 ETF 还是黄金股？

用户 2026-09-18："核心看一下黄金买什么呢？股票还是？"

差别在本质：
  · 黄金 ETF（518880）＝ 直接持有金价，纯商品暴露，与股票低相关
  · 黄金股（山东黄金等）＝ 金价的杠杆 + 经营杠杆 + 股票 beta
    → 金价涨时弹性更大，但大盘跌时跟着跌，且有公司/矿难/成本风险

作为"组合对冲腿"，关键指标是**与股票的相关性**（越低头寸越纯）。
本脚本用 5 年实测（2021-09~2026-09，新浪 K 线口径）回答：
  1. 各自与沪深300 的日收益相关性
  2. 各自与金价（Au99.99）的相关性（是不是真的在跟踪黄金）
  3. 放进永续框架（股25/债25/金25/现25）后的年化、波动、夏普、最大回撤

用法: PYTHONPATH=. .venv/bin/python scripts/bt_gold_leg.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents.ashare.proxy import ensure_tx_tunnel, tx_env  # noqa: E402

CACHE = Path("ashare_out") / "_cache" / "bt_gold_leg.json"
CASH_RATE = 0.02
RISK_FREE = 0.02


def sina_kline(code: str, n: int = 1300) -> dict[str, float]:
    """新浪日 K（前复权口径由接口默认，用于同口径比较）。"""
    from tradingagents.ashare.material import fetch_daily_kline
    return {r["date"]: float(r["close"]) for r in fetch_daily_kline(code, n)}


def ak_series() -> dict[str, dict[str, float]]:
    """金价 / 沪深300 / 中债（akshare，走隧道）。"""
    if not ensure_tx_tunnel():
        raise SystemExit("隧道不可用")
    keys = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(tx_env())
    try:
        import akshare as ak
        out = {}
        df = ak.spot_hist_sge(symbol="Au99.99")
        out["gold_spot"] = {str(r["date"])[:10]: float(r["close"]) for _, r in df.iterrows()}
        df = ak.stock_zh_index_daily(symbol="sh000300")
        out["hs300"] = {str(r["date"])[:10]: float(r["close"]) for _, r in df.iterrows()}
        df = ak.bond_new_composite_index_cbond(indicator="财富", period="总值")
        out["bond"] = {str(r["date"])[:10]: float(r["value"]) for _, r in df.iterrows()}
        return out
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def corr(a: dict[str, float], b: dict[str, float]) -> float | None:
    days = sorted(set(a) & set(b))
    if len(days) < 60:
        return None
    ra = [a[days[i]] / a[days[i - 1]] - 1 for i in range(1, len(days))]
    rb = [b[days[i]] / b[days[i - 1]] - 1 for i in range(1, len(days))]
    ma, mb = statistics.mean(ra), statistics.mean(rb)
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(len(ra)))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((x - mb) ** 2 for x in rb) ** 0.5
    return num / (da * db) if da and db else None


def backtest(dates: list[str], series: dict[str, dict[str, float]],
             weights: dict[str, float]) -> list[float]:
    legs = [k for k in weights if k != "cash"]
    units = {k: weights.get(k, 0.0) for k in legs}
    cash = weights.get("cash", 0.0)
    navs, last_year = [1.0], dates[0][:4]
    for i, d in enumerate(dates):
        if i == 0:
            continue
        prev = dates[i - 1]
        for k in legs:
            p0, p1 = series[k].get(prev), series[k].get(d)
            if p0 and p1 and units.get(k):
                units[k] *= p1 / p0
        cash *= (1 + CASH_RATE) ** (1 / 252)
        total = sum(units.values()) + cash
        if d[:4] != last_year:
            units = {k: total * weights.get(k, 0.0) for k in legs}
            cash = total * weights.get("cash", 0.0)
            last_year = d[:4]
        navs.append(sum(units.values()) + cash)
    return navs


def mstats(navs: list[float]) -> dict:
    years = len(navs) / 252
    ann = (navs[-1] / navs[0]) ** (1 / years) - 1
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
    vol = statistics.stdev(rets) * (252 ** 0.5)
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    return {"ann": ann, "vol": vol, "mdd": mdd,
            "sharpe": (ann - RISK_FREE) / vol if vol else 0.0}


def main() -> None:
    if CACHE.is_file():
        series = json.loads(CACHE.read_text())
    else:
        series = ak_series()
        series["gold_etf"] = sina_kline("518880")
        series["sd_gold"] = sina_kline("600547")    # 山东黄金
        series["zjky"] = sina_kline("601899")       # 紫金矿业
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(series))

    print("# 黄金腿选型：ETF vs 黄金股（同区间对比）\n")
    print("## ① 与沪深300 的相关性（越低越像'对冲腿'）")
    for key, label in (("gold_etf", "黄金ETF 518880"), ("gold_spot", "金价 Au99.99"),
                       ("sd_gold", "山东黄金 600547"), ("zjky", "紫金矿业 601899")):
        c = corr(series[key], series["hs300"])
        print(f"- {label:<16} 与沪深300 相关 {c:+.2f}" if c is not None else f"- {label}: 样本不足")
    print("\n## ② 与金价的相关性（看是否真在跟踪黄金）")
    for key, label in (("gold_etf", "黄金ETF 518880"), ("sd_gold", "山东黄金"), ("zjky", "紫金矿业")):
        c = corr(series[key], series["gold_spot"])
        print(f"- {label:<16} 与金价相关 {c:+.2f}" if c is not None else f"- {label}: 样本不足")

    dates = sorted(set(series["hs300"]) & set(series["bond"]) & set(series["gold_spot"])
                   & set(series["gold_etf"]) & set(series["sd_gold"]) & set(series["zjky"]))
    print(f"\n## ③ 放进永续框架（股25/债25/金25/现25，{dates[0]} ~ {dates[-1]}）")
    print("| 黄金腿用 | 年化 | 波动 | 夏普 | 最大回撤 |")
    print("|---|---|---|---|---|")
    for leg, label in (("gold_etf", "黄金ETF"), ("sd_gold", "山东黄金"), ("zjky", "紫金矿业")):
        navs = backtest(dates, series,
                        {"hs300": 0.25, "bond": 0.25, leg: 0.25, "cash": 0.25})
        s = mstats(navs)
        print(f"| {label} | {s['ann']*100:+.1f}% | {s['vol']*100:.1f}% | {s['sharpe']:.2f} | "
              f"**{s['mdd']*100:.1f}%** |")
    # 纯腿对比（100% 黄金腿）
    print("\n## ④ 单腿本身（100% 持有）")
    print("| 标的 | 年化 | 波动 | 最大回撤 |")
    print("|---|---|---|---|")
    for leg, label in (("gold_etf", "黄金ETF"), ("sd_gold", "山东黄金"), ("zjky", "紫金矿业")):
        navs = backtest(dates, series, {leg: 1.0})
        s = mstats(navs)
        print(f"| {label} | {s['ann']*100:+.1f}% | {s['vol']*100:.1f}% | {s['mdd']*100:.1f}% |")


if __name__ == "__main__":
    main()
