#!/usr/bin/env python3
"""bt_event_fed.py — 验证"美联储加息 → 黄金跌"这条映射

背景：market_review.py 的事件映射表里，我把"美联储加息/美元走强 → 贵金属"
标成了〔已验证〕，依据只是 2026-09-17 单日观察。单次观察不是验证——用户要求
"构建模型然后去验证"，这里补上。

检验设计：
  · 事件：美联储利率决议中的**加息**（与上次相比上调）——akshare 历史序列
  · 标的：山东黄金 600547（A 股黄金龙头，用腾讯日 K 约 8 年）
  · 反应窗口：决议日 T（北京时间凌晨公布）→ A 股 T 日收盘 vs T-1 收盘（1 日），
    以及 T+3、T+5 日
  · 对照：全部交易日的同窗口收益（基准）+ 降息事件（反向检验）

判读：如果"加息→黄金跌"成立，加息组的 T 日平均收益应显著低于基准；
若与基准无异，则该映射只是叙事，应从〔已验证〕降级为〔未验证〕。

用法: PYTHONPATH=. .venv/bin/python scripts/bt_event_fed.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents.ashare.proxy import ensure_tx_tunnel, tx_env  # noqa: E402

GOLD = "600547"       # 山东黄金
CACHE = Path("ashare_out") / "_cache" / "bt_event_fed.json"
# akshare 的美联储序列落后于现实（只到 2025-10），手动补最近事件
EXTRA_EVENTS: list[tuple[str, str]] = [("2026-09-16", "hike")]


def _direct_get(url: str, headers: dict) -> dict:
    """直连：临时清代理（urllib 不认 socks）。"""
    keys = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        return json.loads(urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=20).read().decode())
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def gold_daily() -> dict[str, float]:
    """山东黄金日收盘。用新浪（腾讯 fqkline 上限 641 天，不够覆盖加息周期）。"""
    from tradingagents.ashare.material import fetch_daily_kline
    rows = fetch_daily_kline(GOLD, 1300)
    return {r["date"]: float(r["close"]) for r in rows}


def fed_events() -> tuple[list[str], list[str]]:
    """美联储加息日 / 降息日（yyyy-mm-dd）——akshare 历史决议序列，走隧道。"""
    if not ensure_tx_tunnel():
        return [], []
    keys = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")
    saved = {k: os.environ.get(k) for k in keys}
    os.environ.update(tx_env())
    try:
        import akshare as ak
        df = ak.macro_bank_usa_interest_rate()
        col_date = next((c for c in df.columns if "日期" in str(c) or "date" in str(c).lower()), None)
        col_val = next((c for c in df.columns if "今值" in str(c) or "value" in str(c).lower()), None)
        rows = [(str(r[col_date])[:10], float(r[col_val]))
                for _, r in df.iterrows()
                if col_date and col_val and r[col_val] is not None]
        rows.sort()
        hikes, cuts = [], []
        for i in range(1, len(rows)):
            d0, v0 = rows[i - 1]
            d1, v1 = rows[i]
            if v1 > v0:
                hikes.append(d1)
            elif v1 < v0:
                cuts.append(d1)
        for d, kind in EXTRA_EVENTS:
            (hikes if kind == "hike" else cuts).append(d)
        return sorted(hikes), sorted(cuts)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 美联储序列获取失败: {type(exc).__name__} {str(exc)[:60]}")
        return [], []
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def fwd_ret(dates: list[str], px: dict[str, float], k: int) -> list[float]:
    """事件日 → 未来 k 个交易日收益（从事件日前一日收盘算起）。"""
    ds = sorted(px)
    idx = {d: i for i, d in enumerate(ds)}
    out = []
    for d in dates:
        # 事件日或其后第一个交易日
        cand = [x for x in ds if x >= d]
        if not cand:
            continue
        i = idx[cand[0]]
        if i == 0 or i + k >= len(ds):
            continue
        out.append((px[ds[i + k]] / px[ds[i - 1]] - 1) * 100)
    return out


def baseline(px: dict[str, float], k: int) -> list[float]:
    ds = sorted(px)
    return [(px[ds[i + k]] / px[ds[i]] - 1) * 100 for i in range(len(ds) - k)]


def show(name: str, xs: list[float], base: list[float]) -> None:
    if len(xs) < 3:
        print(f"  {name:<22} 样本不足({len(xs)})")
        return
    m, bm = statistics.mean(xs), statistics.mean(base)
    win = sum(1 for x in xs if x < 0) / len(xs) * 100
    print(f"  {name:<22} 均值 {m:+6.2f}%  中位 {statistics.median(xs):+6.2f}%  "
          f"下跌占比 {win:5.1f}%  样本 {len(xs)}  | 基准 {bm:+.2f}%  差 {m - bm:+.2f}%")


def main() -> None:
    if CACHE.is_file():
        blob = json.loads(CACHE.read_text())
        px = blob["px"]
        hikes, cuts = blob["hikes"], blob["cuts"]
    else:
        hikes, cuts = fed_events()
        px = gold_daily()
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"px": px, "hikes": hikes, "cuts": cuts}))
    print(f"# 检验：美联储加息 → 黄金股下跌？（山东黄金 {GOLD}，{len(px)} 个交易日）")
    print(f"# 加息事件 {len(hikes)} 次 / 降息事件 {len(cuts)} 次"
          f"（价格区间 {min(px)} ~ {max(px)}；事件仅在区间内的会命中）\n")
    for k in (1, 3, 5):
        print(f"### 事件后 {k} 个交易日")
        base = baseline(px, k)
        show(f"加息({len(hikes)}次)", fwd_ret(hikes, px, k), base)
        show(f"降息({len(cuts)}次)", fwd_ret(cuts, px, k), base)
        print()


if __name__ == "__main__":
    main()
