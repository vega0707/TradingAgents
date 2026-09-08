#!/usr/bin/env python3
"""watchlist_check.py — 每日检查观察池：价格异动 / 触发位穿破 → 输出提示。

并入每日 12:00 流程尾部：有触发才值得推；全安静则只记日志。
用法: .venv/bin/python -m tradingagents.ashare.watchlist_check [--date YYYY-MM-DD]
输出: 触发行(有则非空)
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import yaml

from tradingagents.ashare.material import fetch_daily_kline

WL = Path(__file__).resolve().parents[2] / "config" / "watchlist.yaml"


def last_close(code: str, as_of: str) -> tuple[float | None, float | None, float | None, float | None]:
    """(现价, MA20, MA60, 60日低)"""
    try:
        rows = [k for k in fetch_daily_kline(code, 200) if k["date"] <= as_of]
        if not rows:
            return None, None, None, None
        closes = [r["close"] for r in rows]
        px = closes[-1]
        ma20 = sum(closes[-20:]) / min(20, len(closes))
        ma60 = sum(closes[-60:]) / min(60, len(closes))
        lo60 = min(closes[-60:])
        return px, ma20, ma60, lo60
    except Exception:
        return None, None, None, None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    if not WL.is_file():
        print("无 watchlist.yaml")
        return
    data = yaml.safe_load(WL.read_text(encoding="utf-8"))
    triggers = []   # 值得推的触发事件
    quiet = []      # 无触发标的（仅日志）
    for w in data.get("watchlist", []):
        code = w["code"]
        name = w.get("name", code)
        px, ma20, ma60, lo60 = last_close(code, args.date)
        if px is None:
            continue
        ev = []
        # 右侧信号：站上 MA20（此前在下方 → 趋势转好起点）
        if ma20 and px >= ma20:
            ev.append(f"站上 MA20({ma20:.2f})——右侧信号出现")
        # 风险线
        risk = w.get("risk_line")
        if risk and px < risk:
            ev.append(f"⚠️跌破风险线 {risk} → 建议剔除观察池")
        if ev:
            triggers.append(f"- **{name} {code}** 现价 {px} ｜ " + "；".join(ev))
            tb = w.get("trigger_buy")
            if tb:
                triggers.append(f"   买点观察：{tb}")
        else:
            above60 = f"站上MA60" if ma60 and px >= ma60 else "MA60下方"
            quiet.append(f"{name} {code} 价{px} MA20下方/{above60} — 无触发")
    if triggers:
        print(f"# 观察池触发 · {args.date}\n")
        print("\n".join(triggers))
    else:
        print(f"观察池安静（{len(quiet)} 只均无触发）")


if __name__ == "__main__":
    main()
