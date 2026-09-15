#!/usr/bin/env python3
"""screen_fundamental.py — 全市场基本面漏斗（第 1 层：估值 / 股息 / 质量）

设计意图（用户 2026-09-15 明确）：不做涨跌幅排序（那是事后），
而是从全市场找"估值低、股息率高、ROE 稳"的标的。

数据：东财 clist 全市场分页（每页 100，约 56 页）
  f12=代码 f14=名称 f2=现价 f9=PE f23=PB f20=总市值 f100=行业 f37=ROE f133=股息率

分层输出：
  A 低估值   PB < 1.2 且 PE < 20（PE>0）且 ROE ≥ 8%
  B 高股息   股息率 > 4% 且 ROE ≥ 8%
  C 双优     PB < 1.5 且 股息率 ≥ 3.5% 且 ROE ≥ 8%
统一质量闸：ROE ≥ 8%（防"便宜有理由"的价值陷阱）、市值 ≥ 80 亿（防小票）

用法: .venv/bin/python scripts/screen_fundamental.py [--check 600941,601088]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
# 东财行情节点是多个子域：实测 push2 断连时 82.push2 / push2delay 仍可通
# （2026-09-15）。某一节点被限不代表接口不可用，轮询子域比反复重试同一域名有效。
HOSTS = ("82.push2.eastmoney.com", "push2delay.eastmoney.com", "push2.eastmoney.com")
PATH = ("/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2"
        # 按代码排序（fid=f12）而非涨跌幅：盘中排序会漂移，导致分页重复+漏票
        # （实测 falt=f3 时 5559 只里重复 113、漏约 1000）
        "&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
        "&fields=f12,f14,f2,f9,f23,f20,f100,f37,f133")
MIN_MKT_CAP = 8e9      # 80 亿
# 东财 f37 是最新报告期 ROE：9 月处于半年报季 → 口径为「半年未年化」。
# 年化 8% ≈ 半年 4%，用 8 当门槛会误杀一半（神华半年 5.83% 实际年化 11.7%）。
MIN_ROE_H1 = 4.0       # %（半年口径）
SLEEP = 0.15           # 分页间隔
NO_DIV = 12.0          # 股息率 >12% 视为含特别分红/口径异常，不计入高股息组
BAD_NAME = ("ST", "*ST", "退")


def fetch_page(pn: int, tries: int = 2) -> list[dict]:
    """一页数据；轮询子域（外循环）优于同域重试（内循环）。"""
    for host in HOSTS:
        for i in range(tries):
            try:
                req = urllib.request.Request(f"https://{host}{PATH.format(pn=pn)}", headers=UA)
                d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
                return ((d.get("data") or {}).get("diff") or [])
            except Exception as exc:  # noqa: BLE001 — 东财间歇断连，换域/退避
                if i == tries - 1 and host == HOSTS[-1]:
                    sys.stderr.write(f"page {pn} 全域名失败: {str(exc)[:50]}\n")
                else:
                    time.sleep(2 * (i + 1))
    return []


def fetch_all() -> list[dict]:
    """全市场（约 56 页）。先探针再分页；中途连续失败即停，已拉数据保留。"""
    first: list[dict] = []
    for probe in range(4):
        first = fetch_page(1)
        if first:
            break
        sys.stderr.write(f"探针失败({probe + 1}/4)，等 30s 再试\n")
        time.sleep(30)
    if not first:
        return []
    out, pn, fail_streak = list(first), 2, 0
    while pn <= 60:
        page = fetch_page(pn)
        if page:
            fail_streak = 0
            out.extend(page)
        else:
            fail_streak += 1
            if fail_streak >= 3:
                sys.stderr.write(f"连续 3 页失败，停在 pn={pn}（已拉到 {len(out)} 只）\n")
                break
        pn += 1
        time.sleep(SLEEP)
    return out


def num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", default="", help="逗号分隔代码，打印其原始字段供口径核对")
    args = ap.parse_args()

    rows = fetch_all()
    # 分页按涨跌排序、盘中数据变动会导致同一票被返回多次 → 按代码去重
    rows = list({str(r.get("f12")): r for r in rows}.values())
    rows = [r for r in rows if not any(b in str(r.get("f14") or "") for b in BAD_NAME)]
    sys.stderr.write(f"去重→ {len(rows)} 只\n")
    if not rows:
        print("数据源不可用（东财分页全失败），稍后重试")
        return

    if args.check:
        want = set(args.check.split(","))
        for r in rows:
            if str(r.get("f12")) in want:
                print(f"[口径自检] {r.get('f14')} {r.get('f12')} PE={r.get('f9')} PB={r.get('f23')} "
                      f"ROE={r.get('f37')} 股息率={r.get('f133')} 市值={num(r.get('f20'))/1e8:.0f}亿")
        print()

    def cap_yi(r) -> float:
        c = num(r.get("f20"))
        return c / 1e8 if c else 0.0

    # 基础池：盈利为正 + 质量闸（半年 ROE）+ 规模闸
    pool = [r for r in rows
            if (num(r.get("f9")) or -1) > 0
            and (num(r.get("f37")) or 0) >= MIN_ROE_H1
            and cap_yi(r) >= MIN_MKT_CAP / 1e8]

    def show(title: str, items: list[dict], key, limit: int = 18) -> None:
        items = sorted(items, key=key)[:limit]
        print(f"## {title}（{len(items)} 只）")
        print("| 代码 | 名称 | 行业 | 现价 | PE | PB | ROE | 股息率 | 市值 |")
        print("|---|---|---|---|---|---|---|---|---|")
        for r in items:
            roe = num(r.get("f37")) or 0
            div = num(r.get("f133"))
            div_s = f"{div:.2f}%" if div is not None else "—"
            flag = " ⚠️" if (div or 0) > NO_DIV else ""
            print(f"| {r.get('f12')} | {r.get('f14')} | {r.get('f100')} | {r.get('f2')} | "
                  f"{r.get('f9')} | {r.get('f23')} | {roe:.1f}%≈{roe*2:.0f}%年 | {div_s}{flag} | "
                  f"{cap_yi(r):.0f}亿 |")
        print()

    low_pb = [r for r in pool
              if (num(r.get("f23")) or 99) < 1.2 and (num(r.get("f9")) or 999) < 20]
    high_div = [r for r in pool
                if 4.0 < (num(r.get("f133")) or 0) <= NO_DIV]
    both = [r for r in pool
            if (num(r.get("f23")) or 99) < 1.5 and 3.5 <= (num(r.get("f133")) or 0) <= NO_DIV]

    print(f"# 全市场基本面漏斗 · 第 1 层（{len(rows)} 只 → 质量+规模池 {len(pool)} 只）\n")
    print(f"> 质量闸：ROE(半年口径) ≥ {MIN_ROE_H1}%（≈年化 {MIN_ROE_H1*2:.0f}%）、市值 ≥ {MIN_MKT_CAP/1e8:.0f} 亿、剔除 ST/退市\n"
          "> 数据源：东财 clist（PE/PB 为动态口径；股息率为近 12 个月累计，含特别分红时偏高，⚠️ 表示 >12% 需逐个核实）\n")
    show("A. 低估值（PB<1.2 且 PE<20，按 PB 升序）", low_pb, lambda r: num(r.get("f23")) or 99)
    show("B. 高股息（股息率>4%，按股息率降序）", high_div, lambda r: -(num(r.get("f133")) or 0))
    show("C. 双优（PB<1.5 且 股息率≥3.5%，按股息率降序）", both,
         lambda r: -(num(r.get("f133")) or 0))


if __name__ == "__main__":
    main()
