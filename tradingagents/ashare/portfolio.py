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
from tradingagents.ashare.valuation import fair_value

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


def avg_corr(pool: list[str], as_of: str) -> float | None:
    """持仓日收益平均相关系数 → 同质化指标（>0.5 高度同步，分散不足）。"""
    by_code = {c: returns_by_date(c, as_of) for c in pool}
    common = sorted(set.intersection(*[set(r.keys()) for r in by_code.values()]))
    if len(common) < 40:
        return None
    xs = {c: [by_code[c][d] for d in common] for c in pool}
    n = len(common)

    def pearson(a, b):
        ma = sum(a) / n
        mb = sum(b) / n
        num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
        da = math.sqrt(sum((x - ma) ** 2 for x in a))
        db = math.sqrt(sum((x - mb) ** 2 for x in b))
        return num / (da * db) if da and db else 0.0

    vals = []
    cs = list(pool)
    for i in range(len(cs)):
        for j in range(i + 1, len(cs)):
            vals.append(pearson(xs[cs[i]], xs[cs[j]]))
    return sum(vals) / len(vals) if vals else None


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
        closes = [k["close"] for k in klines]
        n = len(closes)

        def _ma(w):
            return sum(closes[-w:]) / min(w, n) if n >= w else None

        stats[code] = {
            "name": h["name"] or code, "shares": h["shares"],
            "cost": h["cost"], "sigma": sigma,
            "factor": 1.0, "signal": "无信号", "mark": mark,
            "ma20": _ma(20), "ma60": _ma(60),          # 右侧/触发位判断
            "prev": closes[-2] if n >= 2 else None,     # 上穿判断
            "ma20_y": (sum(closes[-21:-1]) / 20) if n >= 21 else None,
            "hi60": max(closes[-60:]) if n >= 60 else None,  # 价格闸
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
    corr = avg_corr(pool, as_of)

    return {
        "as_of": as_of, "risk_on": risk_on, "hs300": hs300,
        "total_scale": total_scale, "w": w, "stats": stats,
        "pool": pool, "corr": corr, **bt,
    }


def render(port: dict, capital: float = 1_000_000) -> str:
    """持仓状态与建议（2026-09-08 起废弃精确目标权重——回测证明池内
    低波加权无优势；只保留三类有用输出：大盘开关/信号动作/风险提示）。"""
    if "error" in port:
        return f"组合层：{port['error']}"
    as_of = port["as_of"]
    stats = port["stats"]
    equity = sum(s["shares"] * (s["mark"] or 0) for s in stats.values() if s["mark"])
    n_pos = sum(1 for s in stats.values() if s["shares"] > 0)

    risk = ("🟢 大盘多头（沪深300 > MA200）——可满仓"
            if port["risk_on"]
            else "🔴 大盘破 MA200 —— 纪律：总仓位降至一半防守（沪深300 {}）".format(int(port["hs300"])))

    lines = [f"# 持仓状态与建议 · {as_of}", "",
             f"{risk} ｜ 持仓 {n_pos} 只 ｜ 股票市值 {equity/10000:.1f}万", ""]

    # 2.5 估值锚：每只持仓 现价 vs 合理价（低估/高估），主源限流自动切新浪
    vlines = []
    for c, s in stats.items():
        if not s["shares"] or not s["mark"]:
            continue
        v = fair_value(c, port.get("as_of") or as_of, name=s["name"])
        if v:
            src_mark = "·" if v["src"] == "sina" else ""
            vlines.append((v["delta_pct"],
                f"- {s['name']} {c} {v['price']:.1f} vs 合理 {v['fair_price']:.0f}"
                f" {v['delta_pct']:+.0f}% {v['zone']}{src_mark}"))
    if vlines:
        lines += ["## 估值锚（现价 vs 合理价，ROE-PB 模型）"]
        for _, l in sorted(vlines):
            lines.append(l)
        lines.append("· = 新浪源(akshare 限流时)")
        lines.append("")

    # 1. 信号动作（LLM 交易员拍板；A股整手=100股，股数按整手给）
    def hands_text(sh: int) -> str:
        return f"{sh // 100} 手({sh} 股)"

    actions = []
    for c, s in stats.items():
        if not s["shares"] or not s["mark"]:
            continue
        act = s.get("signal") or ""
        sh = int(s["shares"])
        ma20, ma60 = s.get("ma20"), s.get("ma60")

        def buy_lots_text(price: float, cur_mv: float) -> str:
            """可买手数（单票上限 5% 组合市值）+ 建议首批（一半）。"""
            cap = equity * 0.05
            room = cap - cur_mv
            if price <= 0 or room < price * 100:
                return "已接近单票上限 5%，不建议再加"
            lots = int(room // (price * 100))
            first = max(1, lots // 2)
            return f"可加 {lots} 手（上限 5%≈{cap/10000:.1f}万），建议首批 {first} 手"

        if act == "清仓":
            actions.append(f"- 🔴 **{s['name']} {c}** 清仓：现有 {hands_text(sh)}，信号清仓")
        elif act == "止损":
            actions.append(f"- 🔴 **{s['name']} {c}** 止损：现有 {hands_text(sh)}，跌破防守位止损")
        elif act == "减仓":
            if sh <= 100:
                actions.append(f"- 🟡 **{s['name']} {c}** 减仓：仅 {sh} 股(1 手以内)，要减即清仓，或先不动")
            else:
                cut = (sh // 2) // 100 * 100
                actions.append(f"- 🟡 **{s['name']} {c}** 减仓：现有 {hands_text(sh)}，建议先减 {cut//100} 手({cut} 股)观察")
        elif act == "建仓":
            actions.append(f"- 🟢 **{s['name']} {c}** 建仓：{buy_lots_text(s['mark'], 0)}")
        elif act == "加仓":
            # 价格闸：贴 60 日高（<5%）→ 高位不喊加（防逢高加仓）
            hi60 = s.get("hi60")
            at_high = bool(hi60 and s["mark"] and (hi60 - s["mark"]) / hi60 < 0.05)
            right_now = bool(s.get("prev") is not None and ma20 and s["mark"] >= ma20)
            # 用户要求：给"到价提醒"具体价格（可抄进券商设监控）——agent 滞后，
            # 价格触发才可执行。两类：回踩位(MA60×0.88) / 右侧位(站稳MA20)
            dip_p = ma60 * 0.88 if ma60 else None
            lots_txt = buy_lots_text(s["mark"], sh * s["mark"])
            import re as _re
            m = _re.search(r"可加 (\d+) 手.*首批 (\d+) 手", lots_txt)
            lots, first = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            if right_now:
                cue2 = f"② 现价已站上右侧位({ma20:.2f})→可直接首批 {first} 手"
            else:
                cue2 = f"② ≥{ma20:.2f} 元站稳 加 {first} 手（右侧位）"
            cue = (f"📌 到价提醒：① ≤{dip_p:.2f} 元 加 {first} 手（回踩位 dip）；{cue2}"
                   if (dip_p and ma20 and lots) else f"（{lots_txt}）")
            flag = "✅ 右侧已建立" if right_now else "⏳ 右侧未建立"
            if at_high:
                actions.append(f"- ⏸️ **{s['name']} {c}** 加仓信号：现价贴 60 日高({hi60:.2f})——"
                               f"高位暂缓，等回踩 | {cue}")
            else:
                sym = "🟢" if right_now else "⏳"
                actions.append(f"- {sym} **{s['name']} {c}** 加仓（{flag}，现价 {s['mark']:.2f}，"
                               f"可加 {lots} 手/首批 {first} 手）| {cue}")
    if actions:
        lines += ["## 信号动作（交易员拍板）", *actions, ""]
    else:
        lines += ["## 信号动作", "全部持有/观望——无强制动作", ""]

    # 2. 风险提示（非命令，提醒）
    warns = []
    for c, s in stats.items():
        if not s["shares"] or not s["mark"]:
            continue
        pct = s["shares"] * s["mark"] / equity * 100 if equity else 0
        if pct > 40:
            warns.append(f"⚠️ **{s['name']}** 占 {pct:.0f}%——严重集中，若单票出事组合伤筋动骨，值得主动降")
        elif pct > 30:
            warns.append(f"◐ {s['name']} 占 {pct:.0f}%——偏集中，留意即可（非必须动作）")
    if not port["risk_on"]:
        warns.append("⚠️ 大盘在 MA200 下方：历史上此阶段满仓的 MDD 接近翻倍，降仓是纪律不是预测")
    corr = port.get("corr")
    if corr is not None and corr > 0.5:
        warns.append(f"⚠️ 持仓平均相关系数 {corr:.2f}——高度同质（红利/基建/金融同涨同跌），"
                     "真正的分散要靠加低相关资产（消费/医药/资源），池内调权重没用")
    elif corr is not None:
        warns.append(f"持仓平均相关 {corr:.2f}——分散度尚可")
    if warns:
        lines += ["## 风险提示", *warns, ""]

    # 3. 持仓概览
    lines += ["## 持仓概览", "| 股票 | 信号 | 现价 | 占比 | 市值 |", "|---|---|---|---|---|"]
    rows = []
    for c, s in stats.items():
        if not s["shares"]:
            continue
        pct = s["shares"] * (s["mark"] or 0) / equity * 100 if equity else 0
        rows.append((pct, f"| {s['name']} {c} | {s['signal'] or '-'} | {s['mark']:.2f} | {pct:.0f}% | {s['shares']*s['mark']/10000:.2f}万 |"))
    for _, r in sorted(rows, reverse=True):
        lines.append(r)
    lines += ["", "> 动作均为建议；执行后把成交发我，我更新持仓重算。"]
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
