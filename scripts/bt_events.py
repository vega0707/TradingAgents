#!/usr/bin/env python3
"""bt_events.py — 事件日历埋伏回测：商务部反倾销终裁/初裁公告日 → 受益A股表现

口径（保守、可操作）：公告日 D（通常盘后/晚间）→ D+1 开盘买入
（真实最早可操作时点）→ 持有 D+1 收盘 / D+3 / D+5。
对照口径：D-1 收盘埋伏（理想化"提前知道"）。均减同期沪深300 = 超额。

样本：2025-07 ~ 2026-09 可查证事件（反倾销终裁/初裁、美撤双反），
每事件取板块龙头 1-2 只。样本小，结论当参考不当铁律。
数据：新浪日K（走 material 当日缓存）。请求 ≈ 标的数 + 1。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tradingagents.ashare.material import fetch_daily_kline, fetch_symbol_kline

# (公告日, 事件, 受益标的[code,name], 逻辑)
EVENTS = [
    ("2025-07-04", "欧盟白兰地反倾销终裁", [("000869", "张裕A")], "国产烈酒替代"),
    ("2025-12-16", "欧盟猪肉反倾销终裁", [("002714", "牧原股份"), ("300498", "温氏股份")], "进口减少利好猪价"),
    ("2026-03-13", "卤化丁基橡胶终裁", [("600352", "浙江龙盛")], "橡胶助剂替代(弱标的)"),
    ("2026-07-13", "美撤对华割草机双反", [("300879", "大叶股份")], "出口修复"),
    ("2026-08-13", "印度单模光纤续征反倾销", [("601869", "长飞光纤"), ("600487", "亨通光电")], "国产光纤受益"),
    ("2026-09-07", "日二氯二氢硅初裁", [("603938", "三孚股份"), ("002971", "和远气体")], "电子特气国产替代"),
]


def klines(code: str) -> list[dict]:
    try:
        return [k for k in fetch_daily_kline(code, 400)]
    except Exception:
        return []


def next_trade_day(rows: list[dict], d: str, offset: int) -> dict | None:
    """rows 中日期 ≥ d 的第 offset 个交易日（offset=0 即 d 当日或之后首个）。"""
    ge = [r for r in rows if r["date"] >= d]
    return ge[offset] if offset < len(ge) else None


def main() -> None:
    print(f"{'事件':<22}{'标的':<10}{'埋伏D-1':<10}{'公告次日开买收卖':<12}{'3日':<8}{'5日':<8}{'300同窗':<10}")
    results = []
    for day, label, targets, logic in EVENTS:
        rows300 = [k for k in fetch_symbol_kline("sh000300", 400)]
        for code, name in targets:
            rows = klines(code)
            if not rows:
                print(f"{label} {name}: 无日K")
                continue
            # 公告日 D 前最后交易日收盘（埋伏，理想化）
            d_minus = [r for r in rows if r["date"] < day]
            prev_close = d_minus[-1]["close"] if d_minus else None
            # D+1 起操作（公告盘后 → 次日开盘可买）
            d_plus = next_trade_day(rows, day, 1)  # 公告日后第一个交易日=D+0? day若交易日当晚公告→D+1=day下一交易日
            # day 本身可能是公告日(交易日盘中前?)——用 ≥ day 第 1 个交易日当"生效日开"
            day0 = next_trade_day(rows, day, 0)
            next1 = next_trade_day(rows, day, 1)
            next2 = next_trade_day(rows, day, 2)
            next4 = next_trade_day(rows, day, 4)
            if not day0 or not next1:
                continue
            # 保守口径：公告后第一个交易日开盘买(day0.open? 公告多在盘后，day0=公告当日若盘中公告已不可买)
            # 统一用 day0 开盘买 = 最早可操作；其实更保守是 next1(公告次日)开盘买
            buy_open = next1["open"]  # 公告次日开盘
            r1 = (next1["close"] / buy_open - 1) * 100 if next1 else None      # 次日收盘
            r3 = (next2["close"] / buy_open - 1) * 100 if next2 else None      # D+2 收盘(约3日)
            r5 = (next4["close"] / buy_open - 1) * 100 if next4 else None      # D+4 收盘(约5日)
            ambush = (day0["open"] / prev_close - 1) * 100 if prev_close else None  # D 开盘 vs D-1 收(理想埋伏)
            # 沪深300 同窗（从 next1 open 到 next4 close 不可能，用 close-to-close 近似）
            def hs(offset_c, offset_o):
                a = next_trade_day(rows300, day, offset_o)
                b = next_trade_day(rows300, day, offset_c)
                return (b["close"] / a["open"] - 1) * 100 if a and b else None
            hs3 = hs(2, 1)
            results.append({"label": label, "name": name, "r1": r1, "r3": r3, "r5": r5, "ambush": ambush, "hs3": hs3})
            print(f"{label[:20]:<22}{name:<10}"
                  f"{ambush if ambush is not None else 0:>+6.1f}%  "
                  f"{r1 if r1 is not None else 0:>+6.1f}%    "
                  f"{r3 if r3 is not None else 0:>+6.1f}%  {r5 if r5 is not None else 0:>+6.1f}%  {hs3 if hs3 is not None else 0:>+6.1f}%")
    print("\n=== 汇总（次日开买，仅窗口完整样本）===")
    r1s = [r["r1"] for r in results if r["r1"] is not None]
    r3s = [r["r3"] for r in results if r["r3"] is not None]
    r5s = [r["r5"] for r in results if r["r5"] is not None]
    if r1s:
        win = sum(1 for x in r1s if x > 0) / len(r1s) * 100
        print(f"次日  n={len(r1s)} 均 {sum(r1s)/len(r1s):+.2f}% 胜率{win:.0f}%")
    if r3s:
        ex = sum(r["r3"] for r in results if r["r3"] is not None) / len(r3s) \
             - sum((r["hs3"] or 0) for r in results if r["r3"] is not None) / len(r3s)
        win = sum(1 for x in r3s if x > 0) / len(r3s) * 100
        print(f"3日   n={len(r3s)} 均 {sum(r3s)/len(r3s):+.2f}% 胜率{win:.0f}% | 超额 {ex:+.2f}%")
    if r5s:
        win = sum(1 for x in r5s if x > 0) / len(r5s) * 100
        print(f"5日   n={len(r5s)} 均 {sum(r5s)/len(r5s):+.2f}% 胜率{win:.0f}%")


if __name__ == "__main__":
    main()
