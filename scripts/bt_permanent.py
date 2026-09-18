#!/usr/bin/env python3
"""bt_permanent.py — A 股版永续组合回测（Permanent Portfolio）

由来（用户 2026-09-18）："回撤我觉得是整体组合来看的。你是否听过永续投资组合？"
—— 一针见血：我们的风格闸（PB>4 否决）是**单票**视角，会把黄金这类"组合
对冲腿"一刀切掉；而回撤本质是组合属性。Harry Browne 的四分法：
    25% 股票（繁荣）/ 25% 长债（通缩）/ 25% 黄金（通胀危机）/ 25% 现金（衰退）
不预测、靠再平衡获利，卖点是低回撤。

本脚本**不照搬美国结论**，用 A 股/国内资产的真实长历史验证：
  · 股票  沪深300（akshare，2002 起）
  · 债券  中债总财富指数（**含利息再投资**，2002 起）
  · 黄金  上金所 Au99.99 现货（2016-12 起）
  · 现金  固定年化 2%（货币基金近似）
重叠区间即样本期；每年首个交易日再平衡。

对比：100% 股票 / 60-40（股债） / 永续 25×4
输出：年化、波动、夏普、最大回撤、分年度表现、再平衡增益

用法: PYTHONPATH=. .venv/bin/python scripts/bt_permanent.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents.ashare.proxy import ensure_tx_tunnel, tx_env  # noqa: E402

CACHE = Path("ashare_out") / "_cache" / "bt_permanent.json"
CASH_RATE = 0.02          # 现金腿年化（货币基金近似）
RISK_FREE = 0.02


def load_series() -> dict[str, dict[str, float]]:
    """四条腿的日频序列（走隧道取 akshare）。"""
    if not ensure_tx_tunnel():
        raise SystemExit("腾讯云隧道不可用——akshare 取数会失败")
    keys = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(tx_env())
    try:
        import akshare as ak
        out: dict[str, dict[str, float]] = {}
        df = ak.stock_zh_index_daily(symbol="sh000300")
        out["stock"] = {str(r["date"])[:10]: float(r["close"]) for _, r in df.iterrows()}
        # 上证红利：A 股高股息风格（我们选股逻辑的代表，2005 起完整）
        df = ak.stock_zh_index_daily(symbol="sh000015")
        out["stock_div"] = {str(r["date"])[:10]: float(r["close"]) for _, r in df.iterrows()}
        df = ak.bond_new_composite_index_cbond(indicator="财富", period="总值")
        out["bond"] = {str(r["date"])[:10]: float(r["value"]) for _, r in df.iterrows()}
        df = ak.spot_hist_sge(symbol="Au99.99")
        out["gold"] = {str(r["date"])[:10]: float(r["close"]) for _, r in df.iterrows()}
        return out
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def stats(nav: list[float], years: float) -> dict:
    rets = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav))]
    mean = statistics.mean(rets)
    vol = statistics.stdev(rets) * (252 ** 0.5)
    ann = (nav[-1] / nav[0]) ** (1 / years) - 1
    peak, mdd = nav[0], 0.0
    for v in nav:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak)
    sharpe = (ann - RISK_FREE) / vol if vol else 0.0
    return {"ann": ann, "vol": vol, "sharpe": sharpe, "mdd": mdd}


def backtest(dates: list[str], series: dict[str, dict[str, float]],
             weights: dict[str, float], rebalance: bool = True) -> dict:
    """按权重回测；每年首个交易日再平衡。现金腿按固定年化复利。"""
    legs = [k for k in weights if k != "cash"]
    units = {k: weights.get(k, 0.0) for k in legs}
    cash = weights.get("cash", 0.0)
    navs = [1.0]
    last_year = dates[0][:4]
    for i, d in enumerate(dates):
        if i == 0:
            continue
        prev = dates[i - 1]
        for k in legs:
            if units.get(k):
                p0, p1 = series[k].get(prev), series[k].get(d)
                if p0 and p1:
                    units[k] *= p1 / p0
        cash *= (1 + CASH_RATE) ** (1 / 252)
        total = sum(units.values()) + cash
        # 年度再平衡（当年首个交易日）
        if rebalance and d[:4] != last_year:
            units = {k: total * weights.get(k, 0.0) for k in legs}
            cash = total * weights.get("cash", 0.0)
            last_year = d[:4]
        navs.append(sum(units.values()) + cash)
    return {"navs": navs}


def main() -> None:
    if CACHE.is_file():
        blob = json.loads(CACHE.read_text())
        series = blob["series"]
    else:
        series = load_series()
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"series": series}))
    dates = sorted(set(series["stock"]) & set(series["stock_div"])
                   & set(series["bond"]) & set(series["gold"]))
    years = len(dates) / 252
    print(f"# A 股版永续组合回测（{dates[0]} ~ {dates[-1]}，{len(dates)} 个交易日 ≈ {years:.1f} 年）")
    print(f"# 腿：沪深300 / 中债总财富指数(含息) / 上金所Au99.99 / 现金 {CASH_RATE:.0%}；年度再平衡\n")

    combos = {
        "100% 股票(沪深300)": {"stock": 1.0},
        "100% 上证红利": {"stock_div": 1.0},
        "永续 25×4(沪深300腿)": {"stock": 0.25, "bond": 0.25, "gold": 0.25, "cash": 0.25},
        "永续 25×4(红利腿)": {"stock_div": 0.25, "bond": 0.25, "gold": 0.25, "cash": 0.25},
    }
    print("| 组合 | 年化 | 波动 | 夏普 | **最大回撤** |")
    print("|---|---|---|---|---|")
    results = {}
    for name, w in combos.items():
        r = backtest(dates, series, w)
        s = stats(r["navs"], years)
        results[name] = (s, r["navs"])
        print(f"| {name} | {s['ann']*100:+.1f}% | {s['vol']*100:.1f}% | {s['sharpe']:.2f} | "
              f"**{s['mdd']*100:.1f}%** |")

    # 分年度
    print("\n## 分年度收益")
    years_list = sorted({d[:4] for d in dates})
    header = "| 年份 | " + " | ".join(combos) + " |"
    print(header)
    print("|" + "---|" * (len(combos) + 1))
    idx = {d: i for i, d in enumerate(dates)}
    for y in years_list:
        ds = [d for d in dates if d[:4] == y]
        if len(ds) < 20:
            continue
        row = [f"| {y} "]
        for name in combos:
            navs = results[name][1]
            i0, i1 = idx[ds[0]], idx[ds[-1]]
            row.append(f"| {(navs[i1]/navs[i0]-1)*100:+.1f}% ")
        print("".join(row) + "|")


if __name__ == "__main__":
    main()
