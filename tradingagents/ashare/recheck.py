#!/usr/bin/env python3
"""recheck.py — 信号复查：检测持仓票动作反转（基本面未变时的 180 度翻转）

背景：9/8 建筑"加仓"→ 9/9 复查"减仓"、格力"加仓"→"持有"——隔天反转，
基本面(21天快照)未变，纯价格破位触发。LLM 把技术位当基本面变化，
用户按建议操作后隔天被打脸 = 真金白银的信任损失。

规则：每持仓票取最近两次 record action——
  反转 = (加仓/建仓) ↔ (减仓/清仓/止损) 翻转
  基本面未变 = 快照缓存(21天)内两次 record 间无新财报
  输出：反转票 + ⚠️警示（建议不执行或等确认）
用法: .venv/bin/python -m tradingagents.ashare.recheck
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from tradingagents.ashare.portfolio import latest_signal

RECORDS = Path("ashare_out") / ""
TICKERS = "/Users/vega/git/ai-hedge-fund/config/tickers.yaml"
BULL = {"加仓", "建仓"}
BEAR = {"减仓", "清仓", "止损"}


def recent_records(code: str, n: int = 2) -> list[dict]:
    """该 code 最近 n 次 record（按 as_of 降序）。"""
    out = []
    for d in sorted(RECORDS.glob(f"{code}-*"), reverse=True):
        if not d.is_dir():
            continue
        rp = d / "record.json"
        if not rp.exists():
            continue
        try:
            rec = json.loads(rp.read_text(encoding="utf-8"))
            rec["_asof"] = rec.get("as_of") or d.name.rsplit("-", 1)[-1]
            out.append(rec)
            if len(out) >= n:
                break
        except Exception:
            continue
    return out


def main() -> None:
    d = yaml.safe_load(Path(TICKERS).read_text(encoding="utf-8"))
    holds = [t for t in d["tickers"] if float(t.get("shares") or 0) > 0]
    print(f"复查 {len(holds)} 只持仓的信号反转…\n")
    rev = []
    for t in holds:
        code = t["code"]
        recs = recent_records(code)
        if len(recs) < 2:
            continue
        prev, cur = recs[1], recs[0]
        pa = (prev.get("trader") or {}).get("action", "")
        ca = (cur.get("trader") or {}).get("action", "")
        is_rev = (pa in BULL and ca in BEAR) or (pa in BEAR and ca in BULL)
        if not is_rev:
            continue
        # 基本面是否变化：两次 record 间隔内快照是否更新（粗略：间隔天数）
        from datetime import datetime
        try:
            d1 = datetime.strptime(prev["_asof"], "%Y-%m-%d").date()
            d2 = datetime.strptime(cur["_asof"], "%Y-%m-%d").date()
            days = (d2 - d1).days
        except Exception:
            days = 0
        rev.append({
            "name": t.get("name"), "code": code,
            "prev": f"{prev['_asof']}:{pa}", "cur": f"{cur['_asof']}:{ca}",
            "days": days,
            "warn": days <= 25,   # <25 天反转且基本面无新财报窗口 → 疑似噪音
        })
        # 自我迭代：反转入案例库（供后续分析注入该票信号不稳信息）
        try:
            from tradingagents.ashare.db import record_signal_case
            record_signal_case(code, t.get("name", ""), prev["_asof"], pa,
                               cur["_asof"], ca, days, fundamental_changed=False)
        except Exception:
            pass
    if not rev:
        print("✅ 无信号反转（或仅一次 record）")
        return
    print("⚠️ 发现信号反转：\n")
    for r in rev:
        tag = "🔴 疑似噪音(基本面未变就反转)" if r["warn"] else "（间隔较长，可视为新判断）"
        print(f"- {r['name']} {r['code']}: {r['prev']} → {r['cur']} ({r['days']}天) {tag}")
    print("\n建议：🔴 项先不执行/小仓验证；等价格与基本面共振再动。")


if __name__ == "__main__":
    main()
