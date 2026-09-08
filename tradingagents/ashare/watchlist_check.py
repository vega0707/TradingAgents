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
DIP = 0.92  # 与回测一致：MA60 乖离 -8%
STATE = Path(__file__).resolve().parents[2] / "ashare_out" / "_cache" / "watchlist_state.json"


def kline_state(code: str, as_of: str) -> dict | None:
    """现价/MA20/MA60 及昨日 MA20（上穿判断需要）。"""
    try:
        rows = [k for k in fetch_daily_kline(code, 200) if k["date"] <= as_of]
        if len(rows) < 65:
            return None
        closes = [r["close"] for r in rows]
        n = len(closes)
        px = closes[-1]

        def ma(w, end):
            seg = closes[end - w + 1:end + 1]
            return sum(seg) / len(seg)

        return {"px": px, "prev": closes[-2], "ma20": ma(20, n - 1),
                "ma20_y": ma(20, n - 2), "ma60": ma(60, n - 1)}
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()
    if not WL.is_file():
        print("无 watchlist.yaml")
        return
    data = yaml.safe_load(WL.read_text(encoding="utf-8"))
    # 状态：上一轮是否已在触发区——只在"新进入"当天推，避免天天重复
    prev_state: dict[str, bool] = {}
    if STATE.is_file():
        try:
            import json
            prev_state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            prev_state = {}
    cur_state: dict[str, bool] = {}
    triggers = []
    quiet = []
    for w in data.get("watchlist", []):
        code = w["code"]
        name = w.get("name", code)
        st = kline_state(code, args.date)
        if not st:
            quiet.append(f"{name} {code}: 数据不足")
            continue
        px = st["px"]
        tt = w.get("trigger_type", "dip_buy")
        fired = False
        note = ""
        if tt == "ma20_cross":
            if st["ma20_y"] and st["prev"] < st["ma20_y"] and px >= st["ma20"]:
                fired = True
                note = f"上穿 MA20({st['ma20']:.2f}) 右侧动量信号"
        elif tt == "dip_buy":
            target = st["ma60"] * DIP
            if px <= target:
                fired = True
                note = f"深跌乖离买点（价 {px:.2f} ≤ MA60×0.92 = {target:.2f}）"
        cur_state[code] = fired
        risk = w.get("risk_line")
        risk_hit = bool(risk and px < risk)
        # 新进入触发区 或 破风险线 → 才推
        if (fired and not prev_state.get(code)) or (risk_hit and not prev_state.get(code)):
            ev = note
            if risk_hit:
                ev += f"；⚠️跌破风险线 {risk} → 建议剔除"
            triggers.append(f"- **{name} {code}** 现价 {px:.2f} ｜ {ev}")
        elif fired:
            quiet.append(f"{name} {code}: 已在触发区内（上次已提示）")
        else:
            quiet.append(f"{name} {code} 价{px:.2f}（MA20 {st['ma20']:.2f}/MA60 {st['ma60']:.2f}）无触发")
    try:
        import json
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(cur_state), encoding="utf-8")
    except Exception:
        pass
    if triggers:
        print(f"# 观察池触发 · {args.date}\n")
        print("\n".join(triggers))
    else:
        print(f"观察池安静（{len(quiet)} 只均无触发）")


if __name__ == "__main__":
    main()
