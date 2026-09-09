"""run.py — 单票入口：python -m tradingagents.ashare.run --ticker 601318

示例（对比今日决策卡）：
  python -m tradingagents.ashare.run --ticker 601318 --name 中国平安 \
      --shares 200 --cost 35.98 --date 2026-09-04
可选 --rounds N 调辩论轮数（默认 2）；--no-save 不落盘。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from tradingagents.ashare.material import build_material, fetch_symbol_close, latest_trade_date
from tradingagents.ashare.records import build_record, push_cloud, record_exists, write_record_local
from tradingagents.ashare.team import run_team
from tradingagents.default_config import DEFAULT_CONFIG

_SIGN_ZH = {"bullish": "看多", "neutral": "中性", "bearish": "看空", "abstain": "弃权"}


def build_card(mat, rec) -> str:
    n_bull = sum(1 for v in rec.views if v["signal"] == "bullish")
    n_neu = sum(1 for v in rec.views if v["signal"] == "neutral")
    n_bear = sum(1 for v in rec.views if v["signal"] == "bearish")
    n_abs = sum(1 for v in rec.views if v["signal"] == "abstain")
    m = rec.manager
    t = rec.trader

    lines = [
        f"📊 {mat.name} {mat.ticker} ｜ {mat.as_of} 收盘 {mat.mark}",
        f"大师观点：{n_bull}多 · {n_neu}中 · {n_bear}空 · {n_abs}弃权",
        "",
        "—— 多空辩论（各 1 句要旨）——",
        f"多方：{rec.debate[0]['text'][:150]}" if rec.debate else "",
        f"空方：{rec.debate[-1]['text'][:150]}" if rec.debate else "",
        "",
        f"研究经理：{m.get('recommendation','')}（置信 {m.get('confidence','-')}）",
        f"  裁决：{m.get('rationale','')[:200]}",
        f"  计划：{m.get('plan','')[:150]}",
        "",
        f"🛒 交易员拍板：{t.get('action','')}",
        f"  理由：{t.get('reasoning','')[:220]}",
    ]
    if t.get("levels"):
        lines.append(f"  关键位：{t['levels'][:160]}")
    if t.get("stop_loss"):
        lines.append(f"  止损：{t['stop_loss'][:140]}")
    if t.get("position_note"):
        lines.append(f"  仓位：{t['position_note'][:140]}")
    return "\n".join(l for l in lines if l)


def _last_judgment(ticker: str, as_of: str) -> dict | None:
    """该票 < as_of 的最近一次 record 判断摘要（供一致性注入）。"""
    best = None
    for d in Path("ashare_out").glob(f"{ticker}-*"):
        if not d.is_dir():
            continue
        rp = d / "record.json"
        if not rp.exists():
            continue
        try:
            rec = json.loads(rp.read_text(encoding="utf-8"))
            a = rec.get("as_of") or d.name.rsplit("-", 1)[-1]
            if a >= as_of:
                continue
            if best is None or a > best["asof"]:
                best = {"asof": a, "rec": rec}
        except Exception:
            continue
    if best is None:
        return None
    rec = best["rec"]
    return {
        "asof": best["asof"],
        "action": (rec.get("trader") or {}).get("action", "?"),
        "rec": (rec.get("manager") or {}).get("recommendation", "?"),
        "reason": ((rec.get("trader") or {}).get("reasoning") or "")[:120],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="TradingAgents A股 单票研究")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--name", default="")
    ap.add_argument("--date", default=None)
    ap.add_argument("--shares", type=float, default=None)
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="同 ticker+as_of 已有 record.json 时也覆盖重写")
    ap.add_argument("--no-cloud", action="store_true",
                    help="只写本地 record.json，不推云端主仓")
    ap.add_argument("--no-scoreboard", action="store_true",
                    help="不给 manager/trader 注入战绩参考")
    args = ap.parse_args()

    as_of = latest_trade_date(args.date)
    t0 = time.time()
    print(f"[1/3] 拉取 {args.ticker} 数据（截至 {as_of}）…", flush=True)
    mat = build_material(args.ticker, as_of, args.name)
    # 上次判断注入：基本面未更新时防日度价格噪声翻转结论（9/8 建筑 加仓→9/9 减仓 教训）
    prev = _last_judgment(args.ticker, as_of)
    if prev:
        mat.prev_note = (
            f"【一致性要求】上次分析（{prev['asof']}）结论：交易员 {prev['action']}"
            f"（研经 {prev['rec']}），要点：{prev['reason']}\n"
            "本次基本面快照未更新（同一报告期，21 天窗口内）。除非出现显著新证据"
            "（财报披露/重大公告/基本面实质变化），不要仅因日度价格波动或均线穿破就"
            "翻转买卖方向——若改变结论，请明确列出相对上次的新证据；没有则应维持方向，"
            "最多调整仓位建议。技术位只决定执行时机，不推翻基本面方向。")
    print(f"      现价 {mat.mark}，基本面{'可用' if mat.fundamentals_ok else '不可用(ETF/缺失)'}"
          f"{'，带上次判断' if prev else ''}", flush=True)

    if record_exists(args.ticker, as_of) and not args.force and not args.no_save:
        print(f"✋ {args.ticker}@{as_of} 已有 record.json——防污染拒绝重跑；确需覆盖加 --force")
        return

    print(f"[2/3] 10 位大师并行分析…", flush=True)
    if not args.no_scoreboard:
        from tradingagents.ashare.reconcile import load_scoreboard, scoreboard_note
        note = scoreboard_note(load_scoreboard())
        if note:
            print("      战绩注入：有（样本≥10 的大师战绩已附给 manager/trader）",
                  flush=True)
    else:
        note = ""
    rec = run_team(mat, shares=args.shares, cost=args.cost, rounds=args.rounds,
                   scoreboard_note=note)
    card = build_card(mat, rec)
    print(card)
    print(f"\n[3/3] 完成，耗时 {time.time()-t0:.0f}s", flush=True)

    if not args.no_save:
        out = Path("ashare_out") / f"{args.ticker}-{as_of}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "material.md").write_text(mat.render(), encoding="utf-8")
        (out / "views.json").write_text(
            json.dumps(rec.views, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "debate.md").write_text(
            "\n\n".join(f"【{d['side']}·{d['round']}】\n{d['text']}" for d in rec.debate),
            encoding="utf-8")
        (out / "decision.json").write_text(
            json.dumps({"manager": rec.manager, "trader": rec.trader},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "card.md").write_text(card, encoding="utf-8")

        # 对账记录：本地原子写 + 推云端主仓
        print("      拉取基准(沪深300)…", flush=True)
        bench = fetch_symbol_close("sh000300", as_of)
        if bench is None:
            print("      ⚠ 沪深300 取不到，benchmark_mark=null（对账只出绝对指标）")
        record = build_record(mat, rec, args.shares, args.cost, bench, {
            "quick": DEFAULT_CONFIG["quick_think_llm"],
            "deep": DEFAULT_CONFIG["deep_think_llm"],
        })
        try:
            path = write_record_local(record, force=args.force)
            print(f"      已保存 → {path}")
        except FileExistsError as exc:
            print(f"✋ {exc}")
        if not args.no_cloud:
            status, ok = push_cloud(record)
            print(f"      云端：{status}")
        else:
            print("      云端：跳过（--no-cloud）")
        print(f"完整产物 → {out}/")


if __name__ == "__main__":
    sys.exit(main())
