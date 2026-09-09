#!/usr/bin/env python3
"""daily_flow.py — 每日增量编排：只深析"真变了"的票，组合层每日必跑。

触发深析条件（对每只持仓）：
1. 无任何历史 record（新票/首日）→ 深析
2. 现价 vs 上次决策价 |Δ| > 5% → 深析（价格大变）
3. 均线状态反转：上次看多(加仓/建仓)但现价跌破 MA60；或上次看空
   (清仓/减仓/止损)但现价站上 MA60 → 深析（趋势破位/修复）
4. 基本面快照缓存年龄 > 18 天（接近 21 天过期，财报窗口）→ 深析

不触发 → 沿用上次 trader.action 作为当日信号（组合层用，不重复花 LLM）。

用法:
  python -m tradingagents.ashare.daily_flow --decide --date YYYY-MM-DD   # 输出需深析代码(逗号分隔)
  python -m tradingagents.ashare.daily_flow --portfolio --date ...       # 跑组合层出调仓卡
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import yaml

from tradingagents.ashare.material import fetch_daily_kline

DEFAULT_TICKERS = "/Users/vega/git/ai-hedge-fund/config/tickers.yaml"
RECORDS = Path("ashare_out")
CACHE = Path("ashare_out") / "_cache"
PRICE_TRIGGER = 0.05     # 价格变动 5% 触发
SNAP_AGE_TRIGGER = 18    # 快照缓存年龄(天)触发
BULL_ACTIONS = {"加仓", "建仓"}
BEAR_ACTIONS = {"清仓", "减仓", "止损"}


def load_holdings(path: str) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return [
        {"code": t["code"], "name": t.get("name", ""), "shares": float(t.get("shares") or 0),
         "cost": t.get("cost")}
        for t in data.get("tickers", [])
    ]


def last_record(code: str, as_of: str) -> dict | None:
    """该 code 最近一次 < as_of 的 record（as_of 当日不算——当日跑了就是新决策）。"""
    best = None
    for d in RECORDS.glob(f"{code}-*"):
        if not d.is_dir():
            continue
        rp = d / "record.json"
        if not rp.exists():
            continue
        try:
            rec = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            continue
        a = rec.get("as_of") or d.name.rsplit("-", 1)[-1]
        if a >= as_of:  # 同日或未来不算"上次"
            continue
        if best is None or a > best[0]:
            best = (a, rec)
    return best[1] if best else None


def today_price(code: str, as_of: str) -> float | None:
    try:
        rows = [k for k in fetch_daily_kline(code, 200) if k["date"] <= as_of]
        return rows[-1]["close"] if rows else None
    except Exception:
        return None


def ma60_now(code: str, as_of: str) -> float | None:
    try:
        rows = [k for k in fetch_daily_kline(code, 200) if k["date"] <= as_of]
        closes = [r["close"] for r in rows[-60:]]
        return sum(closes) / len(closes) if len(closes) >= 40 else None
    except Exception:
        return None


def snapshot_age(code: str) -> int:
    p = CACHE / f"snapshot-{code}.json"
    if not p.is_file():
        return 999
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        saved = date.fromisoformat(blob["day"])
        return (date.today() - saved).days
    except Exception:
        return 999


def decide(code: str, name: str, as_of: str) -> tuple[bool, str]:
    last = last_record(code, as_of)
    if last is None:
        return True, "无历史 record（首跑）"
    last_action = (last.get("trader") or {}).get("action", "")
    last_mark = last.get("mark")
    px = today_price(code, as_of)
    if px is None or last_mark is None:
        return True, "取价失败，重跑兜底"
    chg = abs(px / last_mark - 1)
    if chg > PRICE_TRIGGER:
        return True, f"价格变动 {chg*100:.1f}%（{last_mark}→{px}）"
    m60 = ma60_now(code, as_of)
    if m60 is not None:
        if last_action in BULL_ACTIONS and px < m60:
            return True, f"跌破 MA60({m60:.2f})，原信号{last_action}需复查"
        if last_action in BEAR_ACTIONS and px > m60:
            return True, f"站上 MA60({m60:.2f})，原信号{last_action}需复查"
    age = snapshot_age(code)
    if age > SNAP_AGE_TRIGGER:
        return True, f"快照缓存 {age} 天，财报窗口需刷新"
    return False, f"沿用上次({last_action}@{last_mark})"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decide", action="store_true", help="输出需深析的代码列表")
    ap.add_argument("--portfolio", action="store_true", help="跑组合层输出调仓卡")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--tickers", default=DEFAULT_TICKERS)
    args = ap.parse_args()

    holdings = [h for h in load_holdings(args.tickers) if h["shares"] > 0]
    if args.decide:
        hits = []
        for h in holdings:
            go, why = decide(h["code"], h["name"], args.date)
            print(f"[{'深析' if go else '沿用'}] {h['code']} {h['name']}: {why}")
            if go:
                hits.append(h["code"])
        print(f"TRIGGERED={','.join(hits)}")
        return

    if args.portfolio:
        from tradingagents.ashare.portfolio import build_portfolio, load_signals, render
        signals = load_signals(RECORDS, args.date, fallback=True)
        port = build_portfolio(holdings, signals, args.date)
        print(render(port))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
