#!/usr/bin/env python3
"""screen_edge.py — 全市场基本面漏斗 第 2.5 层：边际改善 + 趋势位置

为什么需要这层：三层漏斗筛出的候选（宇通客车/华润江中/森马服饰）经完整
LLM 深析后全部被判"观望"，理由高度一致——
  · 当期同比转弱（宇通 H1 指标全面低于去年同期、华润江中营收 -10%）
  · 跌破 MA20/MA60（还在下跌途中）
即：静态便宜 ≠ 买点。本层补齐这两个已被回测验证的维度：
  1) 边际改善闸：今年同期 vs 去年同期（同 MM-DD），ROE/EPS/每股现金流/负债率
     至少 2 项改善
  2) 趋势位置闸：现价 ≥ MA20（右侧确认）或 距 60 日低 > 5%（不接飞刀）
     —— 依据我们自己的回测：追跌负期望、右侧才有效

数据源：东财主要财务指标（datacenter-web，**1 次请求给 20 期**，含同期可比
        的报告期；自带 ROEJQTZ/MGJYXJJETZ/ZCFZLTZ 同比字段）
        注：新浪财务指标页只有相邻两期（H1 与 Q1），做不了同比，故不用。
K 线：本地缓存（material.fetch_daily_kline），零外部请求。

节流（用户要求"很慢很慢"）：串行 + 每票 1 次请求 + 间隔 5-8s 随机 + 异常即停。

用法:
  .venv/bin/python scripts/screen_edge.py --from-quality logs/screen-quality-2026-09-15.md
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"}
API = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_F10_FINANCE_MAINFINADATA"
       "&columns=ALL&filter=(SECUCODE%3D%22{secu}%22)&pageSize=20"
       "&sortColumns=REPORT_DATE&sortTypes=-1")
MIN_GAP, MAX_GAP = 5.0, 8.0
FIN = ("银行", "保险", "证券", "多元金融")


def fin_history(code: str) -> list[dict]:
    """该票近 20 期主要财务指标（东财，1 次请求）。"""
    secu = f"{code}.{'SH' if code[0] == '6' else 'SZ'}"
    req = urllib.request.Request(API.format(secu=secu), headers=UA)
    d = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
    return (d.get("result") or {}).get("data") or []


def yoy_pair(hist: list[dict]) -> tuple[dict, dict] | None:
    """(今年最新期, 去年同期)。同期 = 报告日 MM-DD 相同、年份差 1。"""
    if not hist:
        return None
    cur = hist[0]
    mmdd = cur["REPORT_DATE"][5:10]
    yr = cur["REPORT_DATE"][:4]
    for r in hist[1:]:
        if r["REPORT_DATE"][5:10] == mmdd and r["REPORT_DATE"][:4] != yr:
            return cur, r
    return None


def position(code: str) -> dict:
    """趋势位置：本地 K 线（缓存），零外部请求。"""
    try:
        from tradingagents.ashare.material import fetch_daily_kline
        cl = [r["close"] for r in fetch_daily_kline(code, 90)]
        if len(cl) < 60:
            return {}
        px = cl[-1]
        return {"px": px, "ma20": sum(cl[-20:]) / 20, "dist_lo": (px / min(cl[-60:]) - 1) * 100,
                "right": px >= sum(cl[-20:]) / 20}
    except Exception:
        return {}


def parse_quality(path: Path, keep_fin: bool) -> list[tuple[str, str, str]]:
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "🟢" not in line:
            continue
        m = re.match(r"\|\s*(\d{6})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", line)
        if not m or m.group(1) in seen:
            continue
        if not keep_fin and any(k in m.group(3) for k in FIN):
            continue
        seen.add(m.group(1))
        out.append((m.group(1), m.group(2).strip(), m.group(3).strip()))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-quality", default="")
    ap.add_argument("--codes", default="")
    ap.add_argument("--keep-fin", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    items = (parse_quality(Path(args.from_quality), args.keep_fin) if args.from_quality
             else [(c.strip(), "", "") for c in args.codes.split(",") if c.strip()])
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("没有待检标的")
        return

    print(f"# 基本面漏斗 · 第 2.5 层 边际改善 + 趋势位置（{len(items)} 只，"
          f"每票 1 次请求，间隔 {MIN_GAP:.0f}-{MAX_GAP:.0f}s）\n")
    print("| 代码 | 名称 | 行业 | 报告期 | ROE 同比 | 每股现金流 同比 | EPS 同比 | 负债率 同比 | 改善项 | 位置 | 裁决 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    graded: list[tuple[str, str, str, int, str]] = []
    for i, (code, name, ind) in enumerate(items, 1):
        try:
            hist = fin_history(code)
        except Exception as exc:  # noqa: BLE001 — 异常即停，不硬撑
            sys.stderr.write(f"第 {i} 只 {code} 失败（{type(exc).__name__}: {str(exc)[:60]}），停止\n")
            print(f"\n> ⚠️ 在第 {i} 只中断——已完成 {i - 1} 只有效，其余稍后再跑。\n")
            break
        pair = yoy_pair(hist)
        if not pair:
            sys.stderr.write(f"{code} 无同期可比报告期，跳过\n")
            if i < len(items):
                time.sleep(random.uniform(MIN_GAP, MAX_GAP))
            continue
        cur, prev = pair
        pos = position(code)
        # 改善项（同口径对比）
        fields = (("ROEJQ", True), ("MGJYXJJE", True), ("EPSJB", True), ("ZCFZL", False))
        checks, deltas = [], []
        for key, up_better in fields:
            a, b = cur.get(key), prev.get(key)
            if a is None or b is None:
                continue
            ok = (a > b) if up_better else (a < b)
            checks.append(ok)
            arrow = "↑" if a > b else ("↓" if a < b else "=")
            deltas.append(f"{b:.2f}→{a:.2f}{arrow}")
        while len(deltas) < 4:
            deltas.append("—")
        improved, ran = sum(1 for c in checks if c), len(checks)
        if ran >= 2 and improved >= 2:
            if not pos:
                verdict = "🟡 位置未知"
            elif pos["right"] or pos["dist_lo"] > 5:
                verdict = "⭐ 可关注"
            else:
                verdict = "🟡 边际改善但位置差"
        elif ran >= 2:
            verdict = "🔴 边际恶化"
        else:
            verdict = "🟡 数据不足"
        pos_s = (f"{'右侧' if pos.get('right') else '左侧'}·距低{pos.get('dist_lo', 0):.1f}%"
                 if pos else "—")
        print(f"| {code} | {name} | {ind} | {cur['REPORT_DATE'][:10]} | {deltas[0]} | {deltas[1]} | "
              f"{deltas[2]} | {deltas[3]} | {improved}/{ran} | {pos_s} | {verdict} |")
        graded.append((code, name, verdict, improved, pos_s))
        if i < len(items):
            time.sleep(random.uniform(MIN_GAP, MAX_GAP))

    pick = [g for g in graded if g[2].startswith("⭐")]
    print(f"\n## ⭐ 可关注（便宜 + 质量过关 + 边际改善 + 位置不差）：{len(pick)} 只\n")
    for c, n, v, imp, p in pick:
        print(f"- **{n} {c}**｜改善 {imp} 项｜{p}")
    if not pick:
        print("（无——说明当前'便宜'与'边际转好+位置健康'没有交集。这本身就是结论："
              "宁可空仓，也不接下跌途中的便宜货）")


if __name__ == "__main__":
    main()
