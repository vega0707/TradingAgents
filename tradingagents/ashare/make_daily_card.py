"""make_daily_card.py — 把当日批跑记录合成一张飞书 markdown 汇总卡。

读 ashare_out/<ticker>-<date>/{record.json,decision.json}，每只输出 2-4 行：
名称/现价/研经裁决/交易员动作/关键位与理由摘要。
用法:
  python -m tradingagents.ashare.make_daily_card <date> [out.md]
  python -m tradingagents.ashare.make_daily_card --signals <date>   # 只打调仓信号

信号规则：trader.action 命中 {加仓,减仓,清仓,建仓,止损} 才视为需要推送；
全为 持有/观望（无操作）时 --signals 输出为空 → 调用方可跳过飞书推送。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUT_ROOT = Path("ashare_out")

# 有实际调仓含义、值得推送的动作；持有/观望不在其中（用户不看无变化日报）。
SIGNAL_ACTIONS = {"加仓", "减仓", "清仓", "建仓", "止损"}


def _code_and_date(dirname: str) -> tuple[str, str]:
    """'000651-2026-09-08' → ('000651', '2026-09-08')。

    目录名 = <code>-YYYY-MM-DD，日期定长 10；不能 rsplit('-')（code 后缀与
    日期都含连字符，rsplit 一次会把 '2026-09' 误并进 code）。
    """
    return dirname[:-11], dirname[-10:]


def load_records(as_of: str | None = None) -> list[tuple[str, str, dict, dict]]:
    """返回 [(code, as_of, record, decision)]；as_of 省略时取全部 record 目录。"""
    if as_of:
        pairs = [
            (_code_and_date(p.name)[0], as_of)
            for p in sorted(OUT_ROOT.glob(f"*-{as_of}"))
        ]
    else:
        pairs = [
            _code_and_date(p.parent.name)
            for p in sorted(OUT_ROOT.glob("*/record.json"))
        ]
    out = []
    for code, d in pairs:
        rec_p = OUT_ROOT / f"{code}-{d}" / "record.json"
        if not rec_p.exists():
            continue
        dec_p = OUT_ROOT / f"{code}-{d}" / "decision.json"
        out.append((
            code, d,
            json.loads(rec_p.read_text(encoding="utf-8")),
            json.loads(dec_p.read_text(encoding="utf-8")) if dec_p.exists() else {},
        ))
    return out


def action_signals(as_of: str | None = None) -> list[str]:
    """有调仓信号的行（"名称 代码: 动作"）；全持有/观望返回空列表。"""
    out = []
    for code, _d, rec, _dec in load_records(as_of):
        act = (rec.get("trader") or {}).get("action", "")
        if act in SIGNAL_ACTIONS:
            out.append(f"{rec.get('name') or code} {code}: {act}")
    return out


def one_card(code: str, as_of: str) -> str:
    d = OUT_ROOT / f"{code}-{as_of}"
    rec_p, dec_p = d / "record.json", d / "decision.json"
    if not rec_p.exists():
        return f"- {code}：无记录\n"
    rec = json.loads(rec_p.read_text(encoding="utf-8"))
    dec = json.loads(dec_p.read_text(encoding="utf-8")) if dec_p.exists() else {}
    name = rec.get("name") or code
    mark = rec.get("mark")
    m = rec.get("manager") or {}
    t = rec.get("trader") or {}
    conf = m.get("confidence")
    line = (f"- **{name} {code}** 现价 {mark} ｜ 研经:{m.get('recommendation','-')}"
            f"(置信 {conf}) ｜ **交易员:{t.get('action','-')}**")
    extra = []
    td = dec.get("trader", {})
    if td.get("levels"):
        extra.append(f"  关键位:{td['levels'][:110]}")
    if td.get("reasoning"):
        extra.append(f"  理由:{td['reasoning'][:120]}")
    return line + ("\n" + "\n".join(extra) if extra else "") + "\n"


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--signals":
        as_of = args[1] if len(args) > 1 else None
        for s in action_signals(as_of):
            print(s)
        return

    as_of = args[0] if args else None
    out_path = args[1] if len(args) > 1 else None
    items = load_records(as_of)
    if not items:
        md = "# A股持仓决策日报 · 无记录\n"
    else:
        dates = sorted({d for _, d, *_ in items})
        title = as_of or ("多日混合 " + ",".join(dates))
        lines = [f"# A股持仓决策日报 · {title}", "",
                 f"共 {len(items)} 只｜双跑观察（与 hedge-fund 共识并存，暂不改仓位）", ""]
        for code, d, _rec, _dec in items:
            lines.append(one_card(code, d))
        md = "\n".join(lines)
    if out_path:
        Path(out_path).write_text(md, encoding="utf-8")
        print(f"写 {out_path} ({len(md)} 字符)")
    else:
        print(md)


if __name__ == "__main__":
    main()
