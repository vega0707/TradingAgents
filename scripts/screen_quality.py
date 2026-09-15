#!/usr/bin/env python3
"""screen_quality.py — 全市场基本面漏斗 第 2 层：质量过滤（慢速礼貌抓取）

背景：第 1 层（screen_fundamental.py）筛出"看起来便宜/股息高"的票，但
PB 低不等于机会——中国建筑 PB 0.36 全市场最低，其每股经营性现金流却是
-0.69、净利润增长率 -23.85%（低质量净资产，市场折价合理）。本层用财务
指标把这类"价值陷阱"剔除。

数据源：新浪财务指标页 vFD_FinancialGuideLine（**一页给两期全部字段**）
  净资产收益率(%) / 每股经营性现金流(元) / 每股收益(元) / 资产负债率(%) / 净利润增长率(%)

节流（用户 2026-09-15 明确要求"很慢很慢，别被反爬"）：
  · 串行，绝不并发
  · 每次请求间隔 4-7 秒随机
  · 每票只发 1 次请求
  · 任何连接异常立即停止整个任务（不硬刚、不重试轰炸）

用法:
  .venv/bin/python scripts/screen_quality.py --from-screen logs/screen-2026-09-15.md
  .venv/bin/python scripts/screen_quality.py --codes 601668,601169 --limit 5
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/126.0"}
URL = ("https://money.finance.sina.com.cn/corp/go.php/vFD_FinancialGuideLine/"
       "stockid/{code}/ctrl/{year}/displaytype/4.phtml")
MIN_GAP, MAX_GAP = 4.0, 7.0    # 每次请求间隔（秒），随机
FIN_INDUSTRY = ("银行", "保险", "证券", "多元金融")   # 负债率口径不同，不做负债闸


def fetch_page(code: str, year: int) -> str | None:
    """抓一页并转纯文本。异常直接抛出——由调用方决定停止。"""
    req = urllib.request.Request(URL.format(code=code, year=year), headers=UA)
    raw = urllib.request.urlopen(req, timeout=25).read()
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw.decode("gbk", "replace")))


def grab(text: str, key: str, n: int = 2) -> list[float | None]:
    """取 key 后面的前 n 个数字（页面按报告期倒序排列）。'--' 记 None。"""
    i = text.find(key)
    if i < 0:
        return [None] * n
    seg = text[i + len(key): i + len(key) + 90]
    out: list[float | None] = []
    for tok in re.findall(r"-{0,1}\d+\.?\d*|--", seg)[:n]:
        out.append(None if tok == "--" else float(tok))
    while len(out) < n:
        out.append(None)
    return out


def metrics(code: str, year: int) -> dict | None:
    """一票一次请求 → 关键财务指标（两期）。"""
    t = fetch_page(code, year)
    if not t or "净资产收益率" not in t:
        return None
    roe = grab(t, "净资产收益率(%)")
    cf = grab(t, "每股经营性现金流(元)")
    eps = grab(t, "每股收益(元)")
    debt = grab(t, "资产负债率(%)")
    grow = grab(t, "净利润增长率(%)")
    bvps = grab(t, "每股净资产_调整前(元)")
    return {"roe": roe, "cfps": cf, "eps": eps, "debt": debt, "np_growth": grow, "bvps": bvps}


def judge(m: dict, industry: str) -> tuple[str, list[str]]:
    """质量裁决 → (结论, 理由)。

    金融股（银行/保险/证券）例外：其“每股经营性现金流”含吸收存款/同业变动，
    与制造业的现金流不同义（实测浙商 4.45、兴业 -0.35 均为口径假象），
    故跳过现金流闸与负债闸，改用提示（银行真正该看不良率/拨备/净息差）。
    """
    flags: list[str] = []
    is_fin = any(k in industry for k in FIN_INDUSTRY)
    cf, eps = m["cfps"][0], m["eps"][0]
    roe0, roe1 = m["roe"][0], m["roe"][1]
    grow = m["np_growth"][0]
    debt = m["debt"][0]

    if not is_fin:
        if cf is not None and eps is not None and eps > 0:
            ratio = cf / eps
            if cf < 0:
                flags.append(f"每股经营现金流为负({cf:.2f})")
            elif ratio < 0.5:
                flags.append(f"现金流/EPS 仅 {ratio:.2f}")
        elif cf is not None and cf < 0:
            flags.append(f"每股经营现金流为负({cf:.2f})")
    if grow is not None and grow < -20:
        flags.append(f"净利润增速 {grow:.1f}%")
    if roe0 is not None and roe1 is not None and roe0 < roe1:
        flags.append(f"ROE 下滑({roe1:.1f}→{roe0:.1f})")
    if roe0 is not None and roe0 < 2:
        flags.append(f"半年 ROE 仅 {roe0:.1f}%")
    if debt is not None and debt > 75 and not is_fin:
        flags.append(f"负债率 {debt:.0f}%")

    if is_fin:
        flags.append("金融股：需看不良/拨备/息差")

    hard = [f for f in flags if "为负" in f]
    return ("🔴 疑似陷阱" if hard else ("🟡 存疑" if [f for f in flags if not f.startswith("金融股")]
                                       else "🟢 质量过关")), flags


def parse_screen(path: Path) -> list[tuple[str, str, str]]:
    """从第 1 层输出解析 (code, name, industry)，去重保序。"""
    seen, out = set(), []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*(\d{6})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", line)
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-screen", default="", help="第 1 层输出的 md 路径")
    ap.add_argument("--codes", default="", help="逗号分隔代码（与 --from-screen 二选一）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只（试点）")
    ap.add_argument("--year", type=int, default=0, help="报告年份，默认今年")
    args = ap.parse_args()

    year = args.year or time.localtime().tm_year
    if args.from_screen:
        items = parse_screen(Path(args.from_screen))
    else:
        items = [(c.strip(), "", "") for c in args.codes.split(",") if c.strip()]
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("没有待检标的")
        return

    print(f"# 基本面漏斗 · 第 2 层 质量过滤（{len(items)} 只，每票 1 次请求，"
          f"间隔 {MIN_GAP:.0f}-{MAX_GAP:.0f}s）\n")
    print("| 代码 | 名称 | 行业 | ROE(两期) | 现金流/股 | EPS | 净利增速 | 负债率 | 裁决 |")
    print("|---|---|---|---|---|---|---|---|---|")
    keep, traps = [], []
    for i, (code, name, ind) in enumerate(items, 1):
        try:
            m = metrics(code, year)
        except Exception as exc:  # noqa: BLE001 — 连接异常即停，不硬刚反爬
            sys.stderr.write(f"第 {i} 只 {code} 请求失败（{type(exc).__name__}），"
                             f"按保守策略停止：{str(exc)[:60]}\n")
            print(f"\n> ⚠️ 抓取在第 {i} 只中断（数据源拒绝连接）——已完成的 {i - 1} 只结果有效，"
                  f"其余稍后再跑。\n")
            break
        if m is None:
            sys.stderr.write(f"{code} 页面无财务数据，跳过\n")
            continue
        verdict, flags = judge(m, ind)
        (keep if verdict.startswith("🟢") else traps).append((code, name))
        f = lambda v: "—" if v is None else f"{v:.2f}"   # noqa: E731
        roe_s = "/".join("—" if x is None else f"{x:.1f}" for x in m["roe"])
        print(f"| {code} | {name} | {ind} | {roe_s} | {f(m['cfps'][0])} | {f(m['eps'][0])} | "
              f"{f(m['np_growth'][0])} | {f(m['debt'][0])} | {verdict}"
              + (f"<br>{'、'.join(flags)}" if flags else "") + " |")
        if i < len(items):
            time.sleep(random.uniform(MIN_GAP, MAX_GAP))
    print(f"\n**质量过关 {len(keep)} 只**：" + ("、".join(f"{n} {c}" for c, n in keep) or "无"))
    print(f"**疑似陷阱/存疑 {len(traps)} 只**：" + ("、".join(f"{n} {c}" for c, n in traps) or "无"))


if __name__ == "__main__":
    main()
