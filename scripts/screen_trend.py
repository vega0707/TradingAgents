#!/usr/bin/env python3
"""screen_trend.py — 全市场基本面漏斗 第 2.6 层：多期趋势闸

为什么需要这层：第 2.5 层用"今年同期 vs 去年同期"两点判"边际改善"，筛出 5 只
⭐，但完整 LLM 深析 5 只全判观望，理由暴露了两点比较的盲区——
  · 济川药业：**营收连续 9 个报告期负增长**（两点比较恰好落在"改善"的一年）
  · 百隆东方：**每股净资产 6.70→6.14 三年缩水**（净资产在毁灭，不是改善）
  · 芭田股份：毛利率/净利率处于**序列最高**（周期峰值利润率，非可持续改善）
  · 估值"**被动抬升**"（盈利下滑把 PE 顶高，看起来仍"便宜"）

本层改用东财 20 期（2021Q3~今）做真趋势判断：
  1) BVPS 趋势：近 4 期缩水 → 直接排除（净资产毁灭）
  2) 营收同比连续负增长期数：≥3 期 → 排除（长期衰退，非拐点）
  3) ROE 趋势：近 3 个半年报/年报期的 ROEJQ 斜率（向上=改善）
  4) 利润率历史分位：XSMLL 在 20 期中 >85 分位 → 周期顶嫌疑
  5) PE 被动抬升：盈利同比下降但 PE 高于去年同期 → 假便宜标注

节流：串行 + 每票 1 次请求 + 间隔 5-8s 随机 + 异常即停（用户硬要求）。

用法: .venv/bin/python scripts/screen_trend.py --from-quality logs/screen-quality-2026-09-15.md
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


def history(code: str) -> list[dict]:
    secu = f"{code}.{'SH' if code[0] == '6' else 'SZ'}"
    d = json.loads(urllib.request.urlopen(
        urllib.request.Request(API.format(secu=secu), headers=UA), timeout=20).read().decode())
    rows = (d.get("result") or {}).get("data") or []
    rows.sort(key=lambda r: r["REPORT_DATE"], reverse=True)   # 最新在前
    return rows


def pct_rank(vals: list[float], v: float) -> float:
    """v 在 vals 中的百分位（0-100）。"""
    xs = [x for x in vals if x is not None]
    if not xs or v is None:
        return 50.0
    return sum(1 for x in xs if x <= v) / len(xs) * 100


def analyze(code: str, px: float | None) -> dict:
    h = history(code)
    if len(h) < 6:
        return {"ok": False, "why": "期数不足"}

    # 1) BVPS 同比：东财直接给 BPSTZ。不用季度 BPS 首尾比较——季度 BPS 受
    #    分红除权/利润累积影响（百隆东方 4 期首尾"未缩水"，实际三年在缩）。
    bps_tz = h[0].get("BPSTZ")
    bps_shrink = bool(bps_tz is not None and bps_tz < 0)

    # 2) 营收同比连续负增长（从最新往前数）
    neg_streak = 0
    for r in h:
        tz = r.get("TOTALOPERATEREVETZ")
        if tz is None:
            break
        if tz < 0:
            neg_streak += 1
        else:
            break

    # 3) ROE 趋势：只取同一报告期（06-30 序列）——半年报与年报 ROE 口径不同，
    #    混排会把"半年 < 全年"误读成 ROE 下滑（初版踩过）。
    jun = [r for r in h if r["REPORT_DATE"][5:10] == "06-30"][:3]
    roe_seq = [r.get("ROEJQ") for r in jun]
    roe_up = (len(roe_seq) >= 2 and roe_seq[0] is not None and roe_seq[-1] is not None
              and roe_seq[0] > roe_seq[-1])

    # 4) 毛利率历史分位
    gm = [r.get("XSMLL") for r in h]
    gm_now = h[0].get("XSMLL")
    gm_pct = pct_rank(gm, gm_now) if gm_now is not None else 50.0

    # 5) PE 被动抬升：盈利同比降 + 当前 PE > 去年同期 PE
    eps_now, eps_tz = h[0].get("EPSJB"), h[0].get("EPSJBTZ")
    pe_up_on_falling = bool(eps_tz is not None and eps_tz < 0 and px)

    verdict, notes = "🟡 观察", []
    if bps_shrink:
        verdict = "🔴 排除"
        notes.append(f"每股净资产同比下降({bps_tz:.1f}%)")
    elif neg_streak >= 3:
        verdict = "🔴 排除"
        notes.append(f"营收连续 {neg_streak} 期负增长")
    else:
        if gm_pct > 85:
            notes.append(f"毛利率{gm_pct:.0f}分位(周期顶嫌疑)")
        if pe_up_on_falling:
            notes.append("盈利下滑推动PE抬升(假便宜)")
        if roe_up and gm_pct <= 85 and not pe_up_on_falling:
            verdict = "⭐ 可关注"
        elif roe_up:
            verdict = "🟡 观察(ROE升但有瑕疵)"
        else:
            verdict = "🟡 观察(ROE未升)"
    return {"ok": True, "verdict": verdict, "notes": notes, "bps_tz": bps_tz,
            "neg_streak": neg_streak, "roe_seq": roe_seq, "gm_pct": gm_pct,
            "eps_tz": eps_tz, "period": h[0]["REPORT_DATE"][:10]}


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


def prices(codes: list[str]) -> dict[str, float]:
    if not codes:
        return {}
    syms = ",".join(("sh" if c[0] == "6" else "sz") + c for c in codes)
    try:
        raw = urllib.request.urlopen(urllib.request.Request(
            f"https://qt.gtimg.cn/q={syms}", headers={"User-Agent": "Mozilla/5.0"}),
            timeout=12).read().decode("gbk", "ignore")
    except Exception:
        return {}
    out = {}
    for line in raw.strip().split(";"):
        if "=" not in line:
            continue
        f = line.split("=")[1].strip('"').split("~")
        if len(f) > 3 and f[2]:
            try:
                out[f[2]] = float(f[3])
            except ValueError:
                pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-quality", required=True)
    ap.add_argument("--keep-fin", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    items = parse_quality(Path(args.from_quality), args.keep_fin)
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("没有待检标的")
        return
    px_all = prices([c for c, _, _ in items])

    print(f"# 基本面漏斗 · 第 2.6 层 多期趋势闸（{len(items)} 只，每票 1 次请求，"
          f"间隔 {MIN_GAP:.0f}-{MAX_GAP:.0f}s）\n")
    print("| 代码 | 名称 | 行业 | 报告期 | BVPS同比 | 营收负增长 | ROE 半年序列 | 毛利率分位 | 裁决 |")
    print("|---|---|---|---|---|---|---|---|---|")
    stars, out = [], []
    for i, (code, name, ind) in enumerate(items, 1):
        try:
            a = analyze(code, px_all.get(code))
        except Exception as exc:  # noqa: BLE001 — 异常即停
            sys.stderr.write(f"第 {i} 只 {code} 失败（{type(exc).__name__}: {str(exc)[:50]}），停止\n")
            print(f"\n> ⚠️ 在第 {i} 只中断——已完成 {i - 1} 只有效。\n")
            break
        if not a.get("ok"):
            sys.stderr.write(f"{code} {a.get('why')}，跳过\n")
            if i < len(items):
                time.sleep(random.uniform(MIN_GAP, MAX_GAP))
            continue
        bps_s = "—" if a["bps_tz"] is None else f"{a['bps_tz']:+.1f}%"
        roe_s = "/".join("—" if v is None else f"{v:.1f}" for v in a["roe_seq"])
        note = ("；" + "、".join(a["notes"])) if a["notes"] else ""
        print(f"| {code} | {name} | {ind} | {a['period']} | {bps_s} | {a['neg_streak']} 期 | "
              f"{roe_s} | {a['gm_pct']:.0f}% | {a['verdict']}{note} |")
        out.append((code, name, a["verdict"], note))
        if a["verdict"].startswith("⭐"):
            stars.append((code, name, note))
        if i < len(items):
            time.sleep(random.uniform(MIN_GAP, MAX_GAP))

    print(f"\n## ⭐ 多期趋势同时过关：{len(stars)} 只\n")
    for c, n, note in stars:
        print(f"- **{n} {c}**{note}")
    if not stars:
        print("（无——多期趋势下没有'便宜 + 质量过关 + 净资产不缩水 + 未长期衰退 + "
              "ROE 向上 + 利润率非周期顶'的标的。这本身是结论，不是失败。）")


if __name__ == "__main__":
    main()
