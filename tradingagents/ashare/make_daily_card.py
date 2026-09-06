"""make_daily_card.py — 把当日批跑记录合成一张飞书 markdown 汇总卡。

读 ashare_out/<ticker>-<date>/{record.json,decision.json}，每只输出 2-4 行：
名称/现价/研经裁决/交易员动作/关键位与理由摘要。
用法:
  python -m tradingagents.ashare.make_daily_card <date> [out.md]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

OUT_ROOT = Path("ashare_out")


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
    as_of = sys.argv[1] if len(sys.argv) > 1 else None
    out_path = sys.argv[2] if len(sys.argv) > 2 else None
    dirs = sorted(OUT_ROOT.glob(f"*-{as_of}")) if as_of else sorted(OUT_ROOT.glob("*/record.json"))
    if as_of:
        items = [(p.name.rsplit("-", 1)[0], as_of) for p in dirs]
    else:
        items = []
        for p in dirs:
            code, d = p.parent.name.rsplit("-", 1)
            items.append((code, d))
    codes = items
    lines = [f"# A股持仓决策日报 · {as_of or ('多日混合 ' + str(len(set(d for _, d in items))))}", "",
             f"共 {len(codes)} 只｜双跑观察（与 hedge-fund 共识并存，暂不改仓位）", ""]
    for code, d in codes:
        lines.append(one_card(code, d))
    md = "\n".join(lines)
    if out_path:
        Path(out_path).write_text(md, encoding="utf-8")
        print(f"写 {out_path} ({len(md)} 字符)")
    else:
        print(md)


if __name__ == "__main__":
    main()
