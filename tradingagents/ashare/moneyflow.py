#!/usr/bin/env python3
"""moneyflow.py — 资金流检测（公开数据近似，非"庄家意图"）

新浪个股资金流：总净流入 + 超大单(r0)/大单(r1)分档。
定位：检测"资金异动"(连续大额净流入/流出) 作为短期信号补充；
不试图解读庄家意图——大单流向是公开近似，主力会反侦察。
用法: .venv/bin/python -m tradingagents.ashare.moneyflow 600519 002498
"""
from __future__ import annotations

import json
import sys
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/126.0",
      "Referer": "https://finance.sina.com.cn"}


def _prefix(code: str) -> str:
    return ("sh" if code[0] in "6" else "sz") + code


def fund_flow(code: str, days: int = 20) -> list[dict]:
    """近 days 日资金流（新浪，按日降序返回）。"""
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0"
           f"&daima={_prefix(code)}")
    req = urllib.request.Request(url, headers=UA)
    raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace")
    data = json.loads(raw)
    out = []
    for d in data:
        try:
            out.append({
                "date": d["opendate"],
                "net": float(d["netamount"]),        # 总净流入(元)
                "super": float(d.get("r0_net") or 0),  # 超大单净流入
                "big": float(d.get("r1_net") or 0),    # 大单净流入(需字段存在)
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


def summarize(code: str, name: str = "") -> str:
    rows = fund_flow(code)
    if not rows:
        return f"{name or code}: 无资金流数据"
    rows = rows[:10]
    last = rows[0]
    net5 = sum(r["net"] for r in rows[:5]) / 1e8
    net10 = sum(r["net"] for r in rows[:10]) / 1e8
    sup5 = sum(r["super"] for r in rows[:5]) / 1e8
    # 大单主导度：近5日超大单占总净流入比例（净流入为负时取反看流出结构）
    dom = (sup5 / net5 * 100) if abs(net5) > 0.1 else 0
    trend = "流入" if net5 > 0 else "流出"
    strength = abs(net5)
    level = "强" if strength > 3 else ("中" if strength > 1 else "弱")
    note = []
    if abs(dom) > 60:
        note.append(f"超大单主导({dom:.0f}%)")
    last_super = last["super"] / 1e8
    if last_super > 0:
        note.append("昨日超大单净流入")
    return (f"{name or code} {code}: 近5日净{trend}{abs(net5):.2f}亿({level})"
            f" ｜ 10日累计 {net10:+.2f}亿 ｜ 超大单5日 {sup5:+.2f}亿"
            + ("".join(f" ｜ {n}" for n in note) if note else ""))


if __name__ == "__main__":
    for c in sys.argv[1:] or ["600519", "002498"]:
        print(summarize(c))
