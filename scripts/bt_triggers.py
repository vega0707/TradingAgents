#!/usr/bin/env python3
"""bt_triggers.py — 候选票买点触发回测（选触发类型，不再拍脑袋）

触发类型：
  ma20_cross   收盘上穿 MA20（右侧动量）
  high60_break 收盘突破 60 日新高（强动量）
  dip_buy      收盘较 MA60 乖离 < -8%（深跌价值/超跌）

口径：信号日 i 收盘确认 → 次交易日开盘买入 → 持有 h 交易日卖出（用收盘近似）。
超额 = 相对"全期随机日持有 h 日"基线。输出每类型 5/10/20 日超额与胜率。
用法: .venv/bin/python scripts/bt_triggers.py 000970 000582 ...
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tradingagents.ashare.material import fetch_daily_kline

HORIZONS = [(5, "5日"), (10, "10日"), (20, "20日")]
DIP = 0.92  # 乖离阈值


def ma_series(closes, w):
    out = [None] * len(closes)
    for i in range(len(closes)):
        if i >= w - 1:
            out[i] = sum(closes[i - w + 1:i + 1]) / w
    return out


def signals(rows, trigger):
    closes = [r["close"] for r in rows]
    n = len(closes)
    ma20 = ma_series(closes, 20)
    ma60 = ma_series(closes, 60)
    sig = []
    for i in range(60, n - 21):
        c = closes[i]
        if trigger == "ma20_cross" and ma20[i - 1] and c >= ma20[i] and closes[i - 1] < ma20[i - 1]:
            sig.append(i)
        elif trigger == "high60_break" and i >= 60 and c > max(closes[i - 60:i]):
            sig.append(i)
        elif trigger == "dip_buy" and ma60[i] and c < ma60[i] * DIP:
            sig.append(i)
    return sig, closes


def baseline(closes, h):
    xs = [closes[j + h] / closes[j] - 1 for j in range(0, len(closes) - h, 5)]
    return sum(xs) / len(xs) if xs else 0.0


def run(code, name, trigger, as_of="2026-09-08"):
    try:
        rows = [k for k in fetch_daily_kline(code, 520) if k["date"] <= as_of]
    except Exception:
        return f"{name} {code}: 无数据"
    if len(rows) < 200:
        return f"{name} {code}: 数据不足"
    sig, closes = signals(rows, trigger)
    if len(sig) < 8:
        return f"{name} {code} [{trigger}]: 信号过少({len(sig)})"
    parts = [f"n={len(sig)}"]
    for h, lab in HORIZONS:
        rs = [closes[i + h] / closes[i] - 1 for i in sig if i + h < len(closes)]
        if not rs:
            continue
        avg = sum(rs) / len(rs)
        win = sum(1 for x in rs if x > 0) / len(rs) * 100
        bl = baseline(closes, h)
        parts.append(f"{lab}超额{(avg - bl) * 100:+.1f}%(胜{win:.0f}%)")
    return f"{name} {code} [{trigger}]: " + " ".join(parts)


if __name__ == "__main__":
    codes = sys.argv[1:] or ["000970", "600548", "600196", "600332", "000582"]
    names = {"000970": "中科三环", "600548": "深高速", "600196": "复星医药",
             "600332": "白云山", "000582": "北部湾港"}
    for c in codes:
        for t in ("ma20_cross", "high60_break", "dip_buy"):
            print(run(c, names.get(c, c), t))
