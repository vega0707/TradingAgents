#!/usr/bin/env python3
"""screen_forecast.py — 全市场基本面漏斗 第 3 层：业绩预期（慢速）

前两层只回答"现在便宜吗、质量过关吗"，回答不了"未来会变好吗"。本层取
机构一致预期（同花顺盈测，按票查），算：
  · 前瞻 PE   = 现价 / 预期 EPS(2026E)
  · 预期增速   = 2027E / 2026E - 1（同为全年口径，可比）
  · 覆盖机构数 = 关注度（太少说明没人研究）

节流（用户要求"一定要慢"）：串行 + 每票间隔 5-8 秒随机 + 异常即停。

用法:
  .venv/bin/python scripts/screen_forecast.py --from-quality logs/screen-quality-2026-09-15.md
  .venv/bin/python scripts/screen_forecast.py --codes 000651,600066
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

MIN_GAP, MAX_GAP = 5.0, 8.0
FIN = ("银行", "保险", "证券", "多元金融")


def spot_prices(codes: list[str]) -> dict[str, float]:
    """腾讯批量行情（1 次请求拿全部现价，比逐票轻）。"""
    if not codes:
        return {}
    syms = ",".join(("sh" if c[0] == "6" else "sz") + c for c in codes)
    try:
        raw = urllib.request.urlopen(urllib.request.Request(
            f"https://qt.gtimg.cn/q={syms}",
            headers={"User-Agent": "Mozilla/5.0"}), timeout=12).read().decode("gbk", "ignore")
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


def forecast(code: str) -> list[dict]:
    """该票的未来年度盈测（同花顺）。"""
    import akshare as ak
    df = ak.stock_profit_forecast_ths(symbol=code)
    rows = []
    for _, r in df.iterrows():
        rows.append({"year": str(r.get("年度")), "orgs": r.get("预测机构数"),
                     "mean": r.get("均值")})
    return rows


def parse_quality(path: Path) -> list[tuple[str, str, str]]:
    """从第 2 层输出解析 🟢 质量过关的票（code, name, industry）。"""
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        m = re.match(r"\|\s*(\d{6})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", line)
        if not m or m.group(1) in seen:
            continue
        if "🟢" not in line:
            continue
        seen.add(m.group(1))
        out.append((m.group(1), m.group(2).strip(), m.group(3).strip()))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-quality", default="")
    ap.add_argument("--codes", default="")
    ap.add_argument("--keep-fin", action="store_true", help="保留金融股（默认剔除）")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if args.from_quality:
        items = parse_quality(Path(args.from_quality))
        if not args.keep_fin:
            items = [x for x in items if not any(k in x[2] for k in FIN)]
    else:
        items = [(c.strip(), "", "") for c in args.codes.split(",") if c.strip()]
    if args.limit:
        items = items[: args.limit]
    if not items:
        print("没有待查标的")
        return

    prices = spot_prices([c for c, _, _ in items])
    print(f"# 基本面漏斗 · 第 3 层 业绩预期（{len(items)} 只，每票 1 次查询，"
          f"间隔 {MIN_GAP:.0f}-{MAX_GAP:.0f}s）\n")
    print("| 代码 | 名称 | 行业 | 现价 | 2026E EPS | 2027E EPS | 预期增速 | 前瞻PE | 机构数 |")
    print("|---|---|---|---|---|---|---|---|---|")
    rows = []
    for i, (code, name, ind) in enumerate(items, 1):
        try:
            fc = forecast(code)
        except Exception as exc:  # noqa: BLE001 — 异常即停，不硬撑
            sys.stderr.write(f"第 {i} 只 {code} 查询失败（{type(exc).__name__}: {str(exc)[:50]}），停止\n")
            print(f"\n> ⚠️ 在第 {i} 只中断（数据源异常）——已完成的 {i - 1} 只有效，其余稍后再跑。\n")
            break
        by = {r["year"]: r for r in fc}
        y0, y1 = str(time.localtime().tm_year), str(time.localtime().tm_year + 1)
        e0, e1 = by.get(y0, {}).get("mean"), by.get(y1, {}).get("mean")
        orgs = by.get(y0, {}).get("orgs")
        px = prices.get(code)
        try:
            e0f, e1f = float(e0), float(e1)
        except (TypeError, ValueError):
            e0f = e1f = None
        growth = (e1f / e0f - 1) * 100 if (e0f and e1f) else None
        fpe = (px / e0f) if (px and e0f) else None
        rows.append((code, name, ind, px, e0f, e1f, growth, fpe, orgs))
        e0_s = "—" if e0f is None else f"{e0f:.2f}"
        e1_s = "—" if e1f is None else f"{e1f:.2f}"
        g_s = "—" if growth is None else f"{growth:+.1f}%"
        p_s = "—" if not fpe else f"{fpe:.1f}"
        o_s = "—" if orgs is None else str(orgs)
        print(f"| {code} | {name} | {ind} | {px if px else '—'} | {e0_s} | {e1_s} | "
              f"{g_s} | {p_s} | {o_s} |")
        if i < len(items):
            time.sleep(random.uniform(MIN_GAP, MAX_GAP))

    valid = [r for r in rows if r[6] is not None]
    if valid:
        print("\n## 预期增速排序（机构预期 2027E vs 2026E）\n")
        for r in sorted(valid, key=lambda x: -x[6]):
            print(f"- **{r[1]} {r[0]}**（{r[2]}）预期增速 **{r[6]:+.1f}%**、"
                  f"前瞻 PE {r[7]:.1f}、{r[8]} 家机构")


if __name__ == "__main__":
    main()
