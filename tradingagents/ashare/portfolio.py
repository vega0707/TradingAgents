#!/usr/bin/env python3
"""portfolio.py — 组合层：把单票信号聚合成低回撤/高夏普的目标权重。

理论（详见记忆 portfolio-layer-design）：
- 权重 ∝ 1/σ（风险平价最简版，呼应低波异象）——低波票多配
- 单票 cap 15%（截断后重分配）
- 大盘趋势开关：沪深300 收盘 < MA200 → 总仓位 ×0.5（砍尾部回撤）
- 信号闸：清仓/止损 → 权重 0；减仓 → ×0.6；持有/观望/加仓 → 保留

数据全部走 material 缓存函数（当日每票一次拉取，之后缓存命中）。
用法:
  python -m tradingagents.ashare.portfolio [--date 2026-09-08]
      [--tickers <path>] [--records-dir ashare_out] [--capital 1000000]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

import yaml

from tradingagents.ashare.material import fetch_daily_kline, fetch_symbol_kline

DEFAULT_TICKERS = "/Users/vega/git/ai-hedge-fund/config/tickers.yaml"
SIGNAL_TO_FACTOR = {"清仓": 0.0, "止损": 0.0, "减仓": 0.6, "持有": 1.0, "观望": 1.0, "加仓": 1.0}
SINGLE_CAP = 0.15          # 单票上限
CASH_RATIO_OFF = 0.5       # 大盘破 MA200 时的总仓位系数
RISK_FREE = 0.02           # 无风险利率(估算，仅夏普展示)


def load_holdings(path: str) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [
        {"code": t["code"], "name": t.get("name", ""), "shares": float(t.get("shares") or 0),
         "cost": t.get("cost")}
        for t in data.get("tickers", [])
    ]


def latest_signal(records_dir: Path, code: str) -> dict | None:
    """该 ticker 最近一次 record 的信号（任意日期目录，增量模式沿用用）。"""
    best = None
    for d in records_dir.glob(f"{code}-*"):
        if not d.is_dir():
            continue
        rp = d / "record.json"
        if not rp.exists():
            continue
        try:
            rec = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        as_of = rec.get("as_of", d.name.rsplit("-", 1)[-1])
        if best is None or as_of > best["as_of"]:
            best = {"as_of": as_of, "rec": rec}
    if best is None:
        return None
    rec = best["rec"]
    return {
        "action": (rec.get("trader") or {}).get("action", ""),
        "mark": rec.get("mark"),
        "name": rec.get("name") or code,
        "fundamentals_ok": rec.get("fundamentals_ok"),
        "as_of": best["as_of"],
    }


def load_signals(records_dir: Path, as_of: str, fallback: bool = True) -> dict[str, dict]:
    """读当日 record.json → {code: {action, mark, name, fundamentals_ok}}。

    fallback=True 时无当日 record 的票回退到最近一次 record（增量模式下
    多数票当天不深析，沿用最近信号）。
    """
    out: dict[str, dict] = {}
    found: set[str] = set()
    for d in sorted(records_dir.glob(f"*-{as_of}")):
        code = d.name[: -len(as_of) - 1]
        rp = d / "record.json"
        if not rp.exists():
            continue
        rec = json.loads(rp.read_text(encoding="utf-8"))
        out[code] = {
            "action": (rec.get("trader") or {}).get("action", ""),
            "mark": rec.get("mark"),
            "name": rec.get("name") or code,
            "fundamentals_ok": rec.get("fundamentals_ok"),
        }
        found.add(code)
    if fallback:
        for code in {d.parent.name.split("-")[0] for d in records_dir.glob("*-*/record.json") if d.is_file()} - found:
            sig = latest_signal(records_dir, code)
            if sig:
                sig.pop("as_of", None)
                out[code] = sig
    return out


def returns_by_date(code: str, as_of: str, n: int = 120) -> dict[str, float]:
    """该票 ≤as_of 的近 n 根日K → {date: 日收益}（按日历日对齐回放用）。"""
    try:
        klines = [k for k in fetch_daily_kline(code, n + 5) if k["date"] <= as_of]
    except Exception:
        return {}
    closes = [(k["date"], k["close"]) for k in klines]
    out = {}
    for i in range(1, len(closes)):
        if closes[i - 1][1] > 0:
            out[closes[i][0]] = closes[i][1] / closes[i - 1][1] - 1
    return out


def backtest_stats(w: dict[str, float], pool: list[str], as_of: str, scale: float = 1.0) -> dict:
    """按日历日对齐的加权组合回放 → {ann_ret, ann_vol, sharpe, mdd}。

    scale=总仓位系数（大盘破 MA200 时 0.5，余下视为 0 收益现金），
    回放把 scale 乘进日收益——真实反映"半仓现金"下的波动与回撤。
    """
    by_code = {c: returns_by_date(c, as_of) for c in pool}
    common = sorted(set.intersection(*(set(r.keys()) for r in by_code.values())))
    if len(common) < 30:
        return {"ann_ret": 0.0, "ann_vol": 0.0, "sharpe": 0.0, "mdd": 0.0}
    nav, peak, mdd = 1.0, 1.0, 0.0
    dr = []
    for d in common[-120:]:
        r_stock = sum(w[c] * by_code[c][d] for c in pool if d in by_code[c])
        r = scale * r_stock  # 余下 (1-scale) 为现金，日收益 0
        nav *= (1 + r)
        dr.append(r)
        peak = max(peak, nav)
        mdd = max(mdd, (peak - nav) / peak)
    mean_d = sum(dr) / len(dr)
    std_d = math.sqrt(sum((x - mean_d) ** 2 for x in dr) / (len(dr) - 1)) if len(dr) > 1 else 0
    ann_vol = std_d * math.sqrt(252)
    ann_ret = (1 + mean_d) ** 252 - 1
    sharpe = (ann_ret - RISK_FREE) / ann_vol if ann_vol > 0 else 0.0
    return {"ann_ret": ann_ret, "ann_vol": ann_vol, "sharpe": sharpe, "mdd": mdd}


def market_state() -> tuple[bool, float]:
    """沪深300 收盘 vs MA200 → (risk_on, close)。"""
    try:
        rows = [k for k in fetch_symbol_kline("sh000300", 260) if k["date"] <= date.today().isoformat()]
        if len(rows) < 200:
            return True, rows[-1]["close"] if rows else 0.0
        closes = [r["close"] for r in rows]
        ma200 = sum(closes[-200:]) / 200
        return closes[-1] >= ma200, closes[-1]
    except Exception:
        return True, 0.0


def vol_from_klines(klines: list[dict], n: int = 120) -> float:
    """近 n 根日K的年化波动率σ（权重用）。"""
    closes = [k["close"] for k in klines[-n:]]
    if len(closes) < 30:
        return 0.0
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def build_portfolio(holdings, signals, as_of: str) -> dict:
    """核心：返回权重方案 + 组合统计。"""
    # 1. 每票波动率（全走缓存）
    stats: dict[str, dict] = {}
    for h in holdings:
        code = h["code"]
        try:
            klines = [k for k in fetch_daily_kline(code, 200) if k["date"] <= as_of]
        except Exception:
            klines = []
        sigma = vol_from_klines(klines)
        # 现价一律用今日最新收盘（增量模式下信号可能沿用旧 record，价必须今天的）
        mark = klines[-1]["close"] if klines else None
        stats[code] = {
            "name": h["name"] or code, "shares": h["shares"],
            "cost": h["cost"], "sigma": sigma,
            "factor": 1.0, "signal": "无信号", "mark": mark,
        }
        if code in signals:
            sig = signals[code]
            stats[code]["signal"] = sig["action"] or "无信号"
            stats[code]["factor"] = SIGNAL_TO_FACTOR.get(sig["action"], 1.0)
            stats[code]["fundamentals_ok"] = sig["fundamentals_ok"]

    # 2. 入池：持仓票(shares>0)保留；已清仓票(shares=0)仅当信号为建仓/加仓才回场
    #    （观望/持有对空仓=不回，避免清仓票被低波权重拉回）
    pool = []
    for c, s in stats.items():
        if s["factor"] <= 0:
            continue
        if s["shares"] > 0:
            pool.append(c)
        elif c in signals and signals[c]["action"] in ("建仓", "加仓"):
            pool.append(c)
    if not pool:
        return {"error": "无可入池标的（全部清仓/止损或无持仓）"}

    # 3. 信号加权波动率倒数：w ∝ factor/σ（减仓票降权；σ下限防极端权重）
    weighted = {c: stats[c]["factor"] / max(stats[c]["sigma"], 0.08) for c in pool}
    raw_sum = sum(weighted.values())
    w = {c: weighted[c] / raw_sum for c in pool}
    for _ in range(6):  # cap 截断重分配
        over = {c: x for c, x in w.items() if x > SINGLE_CAP}
        if not over:
            break
        w = {c: (SINGLE_CAP if c in over else x) for c, x in w.items()}
        used = sum(SINGLE_CAP for _ in over) + sum(x for c, x in w.items() if c not in over)
        if used >= 1.0 - 1e-9:
            break
        left = {c: x for c, x in w.items() if c not in over}
        lsum = sum(left.values())
        if lsum <= 0:
            break
        w = {c: (SINGLE_CAP if c in over else x / lsum * (1 - len(over) * SINGLE_CAP)) for c, x in w.items()}

    # 4. 大盘开关
    risk_on, hs300 = market_state()
    total_scale = 1.0 if risk_on else CASH_RATIO_OFF

    # 5. 组合统计：目标权重按日历日对齐回放（样本内，仅参考）
    bt = backtest_stats(w, pool, as_of, scale=total_scale)

    return {
        "as_of": as_of, "risk_on": risk_on, "hs300": hs300,
        "total_scale": total_scale, "w": w, "stats": stats,
        "pool": pool, **bt,
    }


def render(port: dict, capital: float = 1_000_000) -> str:
    if "error" in port:
        return f"组合层：{port['error']}"
    as_of = port["as_of"]
    risk = "✅ 大盘多头（沪深300 > MA200）" if port["risk_on"] else "⚠️ 大盘破 MA200 → 总仓位 ×0.5"
    lines = [
        f"# A股组合方案 · {as_of}", "",
        f"{risk} ｜ 沪深300 {port['hs300']:.0f}",
        f"入池 {len(port['pool'])} 只｜总仓位系数 {port['total_scale']:.2f}",
        "", "| 股票 | 信号 | 现价 | σ(年化) | 目标权重 | 目标市值 | 建议 |",
        "|---|---|---|---|---|---|---|",
    ]
    stats, w, pool = port["stats"], port["w"], port["pool"]
    for c in sorted(pool, key=lambda x: -w[x]):
        s = stats[c]
        mark = s["mark"] or s["cost"] or 0
        target_mv = capital * port["total_scale"] * w[c]
        cur_mv = s["shares"] * (s["mark"] or s["cost"] or 0)
        factor = s["factor"]
        if factor == 0:
            advice = "清仓"
        elif factor < 1:
            advice = "减仓"
        elif s["shares"] == 0:
            advice = "建仓"
        else:
            advice = "持有"
        act = s["signal"] or "-"
        lines.append(
            f"| {s['name']} {c} | {act} | {mark:.2f} | {s['sigma']*100:.0f}% | "
            f"{w[c]*100:.1f}% | {target_mv/10000:.1f}万 | {advice} |"
        )
    lines += [
        "", "## 组合指标（样本内，历史回放）",
        f"- 年化收益 {port['ann_ret']*100:.1f}% ｜ 年化波动 {port['ann_vol']*100:.1f}%",
        f"- **夏普 {port['sharpe']:.2f}** ｜ **最大回撤 {port['mdd']*100:.1f}%**",
        "- ⚠️ 样本内统计仅作参考，实盘以样本外滚动验证为准",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--tickers", default=DEFAULT_TICKERS)
    ap.add_argument("--records-dir", default="ashare_out")
    ap.add_argument("--capital", type=float, default=1_000_000)
    args = ap.parse_args()

    holdings = load_holdings(args.tickers)
    records = Path(args.records_dir)
    signals = load_signals(records, args.date)
    port = build_portfolio(holdings, signals, args.date)
    print(render(port, args.capital))


if __name__ == "__main__":
    main()
