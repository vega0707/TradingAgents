"""reconcile.py — 对账 job（handover 增量 B，只读）。

扫 ashare_out/*/record.json，对每条已到期样本（as_of 后第 h 个交易日收盘已
出现，h∈{5,20}）拉真实行情算收益/超额，产出 _scoreboard/scoreboard.{json,md}。

铁律：
- 只读：不修改任何 record.json / decision 文件。
- 无前视：锚点=as_of 收盘(mark)；未来价格只用于对账窗口。
- horizon 固定 [5,20]；样本 <10 不出 IC、不进战绩注入。
- 统计口径见 scoreboard.md 头部；方向不计 abstain/invalid（单独计数，不猜）。

CLI:
  python -m tradingagents.ashare.reconcile                 # 全量到期样本
  python -m tradingagents.ashare.reconcile --horizon 20
  python -m tradingagents.ashare.reconcile --since 2026-09-01
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from tradingagents.ashare.material import fetch_daily_kline, fetch_symbol_close
from tradingagents.ashare.records import HORIZONS

SCOREBOARD_DIR = Path("ashare_out/_scoreboard")

# 中文 enum -> 方向（§3.2 口径表）
_MASTER_SIGN = {"bullish": 1, "neutral": 0, "bearish": -1, "abstain": 0}
_MANAGER_REC = {"买入": 1, "增持": 1, "卖出": -1, "减持": -1,
                "持有": 0, "不评级": 0}
_TRADER_ACT = {"加仓": 1, "建仓": 1, "减仓": -1, "清仓": -1, "止损": -1,
               "持有": 0, "观望": 0}


def load_records(root: Path = Path("ashare_out")) -> list[dict]:
    out = []
    for p in sorted((root / "_scoreboard").parent.glob("*/record.json")):
        if "_scoreboard" in p.parts:
            continue
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def future_close_at_h(rows: list[dict], as_of: str, h: int) -> float | None:
    """as_of 之后第 h 根日K的收盘；不足 h 根返回 None（样本未成熟）。"""
    after = [r for r in rows if r["date"] > as_of]
    if len(after) >= h:
        return after[h - 1]["close"]
    return None


def window_ret_and_excess(record: dict, rows: list[dict], bench_rows: list[dict],
                          h: int) -> dict | None:
    """返回 {ret, excess}；行情不足/无 mark 返回 None（= 未成熟样本）。"""
    if not record.get("mark"):
        return None
    close_h = future_close_at_h(rows, record["as_of"], h)
    if close_h is None:
        return None
    ret = close_h / record["mark"] - 1
    excess = None
    if record.get("benchmark_mark") and bench_rows:
        b_h = future_close_at_h(bench_rows, record["as_of"], h)
        if b_h is not None:
            excess = ret - (b_h / record["benchmark_mark"] - 1)  # 个股 − 基准
    return {"ret": ret, "excess": excess}


def spearman(xs: list, ys: list) -> float | None:
    """秩相关；样本 < 10 或常数列返回 None（零新增依赖，pandas rank）。"""
    if len(xs) < 10 or len(xs) != len(ys):
        return None
    x = pd.Series(xs).rank()
    y = pd.Series(ys).rank()
    if x.nunique() < 2 or y.nunique() < 2:
        return None
    return float(x.corr(y))


def _direction(entity_type: str, entity_id: str, value) -> int | None:
    """口径表方向映射；未知值返回 None（invalid，不计命中）。"""
    table = {"master": _MASTER_SIGN, "manager": _MANAGER_REC,
             "trader": _TRADER_ACT}.get(entity_type, {})
    if entity_type == "master":
        return table.get(str(value))
    return table.get(str(value).strip())


def aggregate(records: list[dict], horizon: int,
              kline_fn=None, bench_kline_fn=None) -> dict:
    """对已到期样本做逐实体统计（实体=每位大师 + manager + trader）。

    kline_fn(ticker)->rows / bench_kline_fn(symbol)->rows 可注入，默认走新浪
    网络；单测传合成行情，保持离线。
    """
    from collections import defaultdict
    kline_fn = kline_fn or (lambda t: fetch_daily_kline(t, days=400))
    bench_kline_fn = bench_kline_fn or (lambda _s: fetch_symbol_kline("sh000300", days=400))
    samples: dict[tuple, list] = defaultdict(list)  # (type,id) -> 样本
    insufficient = 0
    unknown = 0

    for rec in records:
        try:
            rows = kline_fn(rec["ticker"])
        except Exception:
            insufficient += 1
            continue
        bench_rows = bench_kline_fn("000300") if rec.get("benchmark_mark") else []
        w = window_ret_and_excess(rec, rows, bench_rows, horizon)
        if w is None:
            insufficient += 1
            continue

        entities: list[tuple[str, str, object, object]] = []
        for m in rec.get("masters", []):
            entities.append(("master", m["master"], m.get("signal"), m.get("conviction")))
        mgr = rec.get("manager", {})
        entities.append(("manager", "manager", mgr.get("recommendation"), mgr.get("confidence")))
        trd = rec.get("trader", {})
        entities.append(("trader", "trader", trd.get("action"), None))

        for etype, eid, value, strength in entities:
            d = _direction(etype, eid, value)
            if d is None:
                unknown += 1
                continue
            samples[(etype, eid)].append({
                "dir": d, "ret": w["ret"], "excess": w["excess"],
                "strength": (strength if isinstance(strength, (int, float)) else None),
                "ticker": rec["ticker"], "as_of": rec["as_of"],
            })

    rows_out = []
    for (etype, eid), ss in sorted(samples.items()):
        directional = [s for s in ss if s["dir"] != 0]
        hits = sum(1 for s in directional
                   if (s["dir"] == 1 and s["ret"] > 0)
                   or (s["dir"] == -1 and s["ret"] < 0))
        rets = [s["ret"] for s in directional]
        exs = [s["excess"] for s in directional if s["excess"] is not None]
        strengths = [(s["strength"], s["ret"]) for s in directional
                     if s["strength"] is not None]
        row = {
            "entity_type": etype, "entity_id": eid,
            "n_total": len(ss), "n_dir": len(directional),
            "n_hit": hits,
            "hit_rate": (hits / len(directional)) if directional else None,
            "mean_ret": (sum(rets) / len(rets)) if rets else None,
            "mean_excess": (sum(exs) / len(exs)) if exs else None,
            "ic": (spearman([s for s, _ in strengths], [r for _, r in strengths])
                   if len(strengths) >= 10 else None),
        }
        rows_out.append(row)

    return {"horizon": horizon, "insufficient": insufficient, "unknown": unknown,
            "entities": rows_out, "as_of_generated": date.today().isoformat()}


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def render_md(scoreboard: dict) -> str:
    lines = [
        "# A股决策记分牌（对账结果）",
        "",
        f"- 生成日期：{scoreboard['generated']} ｜ 数据截至最近到期样本",
        "- 口径：方向命中=看多样本 ret>0 / 看空样本 ret<0；中性/无效单独计数不猜；",
        "  超额=个股收益−沪深300同窗收益；IC=置信度与 ret 的秩相关（样本≥10 才出）",
        "- 铁律：样本 <10 不出 IC、不进战绩注入；对账只读不回写决策",
        "",
    ]
    for agg in scoreboard["scoreboards"]:
        lines.append(f"## horizon {agg['horizon']} 交易日")
        lines.append(f"未到期样本（行情不足）: {agg['insufficient']} ｜ 无效/未知方向: {agg['unknown']}")
        lines.append("")
        lines.append("| 实体 | n_dir | 命中 | 命中率 | 均值收益 | 均值超额 | IC |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in agg["entities"]:
            tag = f"{r['entity_type']}:{r['entity_id']}"
            ic = f"{r['ic']:.2f}" if r["ic"] is not None else "-"
            lines.append(f"| {tag} | {r['n_dir']} | {r['n_hit']} | "
                         f"{r['hit_rate'] * 100:.0f}% | {_pct(r['mean_ret'])} | "
                         f"{_pct(r['mean_excess'])} | {ic} |")
        lines.append("")
    return "\n".join(lines)


def build_scoreboard(records: list[dict], horizons: list[int] = HORIZONS,
                     since: str | None = None, kline_fn=None,
                     bench_kline_fn=None) -> dict:
    recs = [r for r in records if not since or r["as_of"] >= since]
    scoreboards = [aggregate(recs, h, kline_fn=kline_fn,
                             bench_kline_fn=bench_kline_fn) for h in horizons]
    return {"generated": date.today().isoformat(),
            "n_records": len(recs), "scoreboards": scoreboards}


def save_scoreboard(sb: dict) -> None:
    SCOREBOARD_DIR.mkdir(parents=True, exist_ok=True)
    (SCOREBOARD_DIR / "scoreboard.json").write_text(
        json.dumps(sb, ensure_ascii=False, indent=1), encoding="utf-8")
    md = render_md(sb)
    (SCOREBOARD_DIR / "scoreboard.md").write_text(md, encoding="utf-8")
    stamp = date.today().strftime("%Y%m%d")
    (SCOREBOARD_DIR / f"scoreboard-{stamp}.json").write_text(
        json.dumps(sb, ensure_ascii=False, indent=1), encoding="utf-8")


def load_scoreboard() -> dict | None:
    p = SCOREBOARD_DIR / "scoreboard.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def scoreboard_note(sb: dict | None, min_samples: int = 10) -> str:
    """把达标（样本≥min_samples）大师的战绩压成客观数字注文（handover 增量 C）。

    纪律：只转述数字，不写"该信谁"；无达标实体返回空串；5日/20日两档都列。
    """
    if not sb:
        return ""
    by_id: dict[str, dict] = {}
    for agg in sb.get("scoreboards", []):
        for r in agg.get("entities", []):
            if r["entity_type"] != "master":
                continue
            m = by_id.setdefault(r["entity_id"], {"n": 0, "parts": []})
            m["n"] = max(m["n"], r.get("n_total", 0))
            if r.get("n_dir", 0) >= min_samples:
                hit = r.get("hit_rate")
                ex = r.get("mean_excess")
                m["parts"].append(
                    f"h{r['horizon']}:命中率 {hit * 100:.0f}%"
                    + (f"/超额均值 {ex * 100:+.1f}%" if ex is not None else "")
                    + f"({r['n_dir']}样本)")
    lines = ["[近期战绩参考（客观数字，样本≥10 才列出；未达标者不采信）]"]
    any_ = False
    for mid in sorted(by_id):
        m = by_id[mid]
        if m["parts"] and m["n"] >= min_samples:
            lines.append(f"  {mid:10s} " + " ｜ ".join(m["parts"]))
            any_ = True
    if not any_:
        return ""
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="A股决策对账（只读）")
    ap.add_argument("--horizon", type=int, default=None, help="只对账该 horizon")
    ap.add_argument("--since", default=None, help="只对账 as_of >= 该日期的样本")
    ap.add_argument("--print-only", action="store_true", help="只打印不落盘")
    args = ap.parse_args()

    records = load_records()
    hs = [args.horizon] if args.horizon else HORIZONS
    sb = build_scoreboard(records, hs, since=args.since)
    print(f"样本总数 {sb['n_records']}（horizon {hs}）")
    for agg in sb["scoreboards"]:
        print(f"  h{agg['horizon']}: 未到期 {agg['insufficient']} ｜ "
              f"实体 {len(agg['entities'])}")
    if args.print_only:
        print(render_md(sb))
    else:
        save_scoreboard(sb)
        print("已写 →", SCOREBOARD_DIR)


if __name__ == "__main__":
    sys.exit(main())
