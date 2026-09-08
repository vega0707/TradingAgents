#!/usr/bin/env python3
"""daily_brief.py — 每日盘前情报：新浪7x24快讯 → 前瞻方向 + 相关 A 股候选。

三段式（防 LLM 幻觉代码）：
1. 快讯 → LLM 提炼方向，每方向只报**公司名**
2. 本地全市场代码表解析公司名 → **真实代码**（匹配不到就不给代码）
3. 腾讯批量拉候选行情 → LLM 逐只点评（关注点/风险，不喊买卖）

用法: .venv/bin/python -m tradingagents.ashare.daily_brief [--pages 4]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from tradingagents.ashare.llmio import complete_json

FEED_URL = ("https://zhibo.sina.com.cn/api/zhibo/feed?page={p}&page_size=20"
            "&zhibo_id=152&tag_id=0&dire=f&dpc=1")
CODE_CACHE = Path("ashare_out") / "_cache" / "allcodes.json"
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0"

KEEP = ["政策", "证监会", "央行", "国常会", "发改委", "工信部", "商务部", "国务院",
        "反倾销", "关税", "A股", "两市", "涨停", "板块", "行业", "半导体", "芯片",
        "新能源", "光伏", "储能", "算力", "人工智能", "机器人", "创新药", "医药",
        "白酒", "消费", "地产", "银行", "券商", "黄金", "有色", "化工", "涨价",
        "中标", "订单", "回购", "增持", "减持", "业绩", "财报", "北向", "IPO",
        "并购", "重组", "发布", "印发", "通知", "意见", "方案", "国产", "出海"]


def fetch_feed(pages: int = 4) -> list[str]:
    out: list[str] = []
    for p in range(1, pages + 1):
        try:
            req = urllib.request.Request(FEED_URL.format(p=p), headers={"User-Agent": _UA})
            data = json.loads(urllib.request.urlopen(req, timeout=12).read().decode())
            items = ((data.get("result") or {}).get("data") or {}).get("feed") or {}
            for it in items.get("list") or []:
                txt = (it.get("rich_text") or "").strip()
                if txt:
                    out.append(txt)
            if len(items.get("list") or []) < 20:
                break
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"page {p} 失败: {e}\n")
        time.sleep(0.8)
    return out


def code_table() -> dict[str, str]:
    """全市场 {公司名: 代码}，当日缓存（akshare 一次拉全）。"""
    if CODE_CACHE.is_file():
        try:
            blob = json.loads(CODE_CACHE.read_text(encoding="utf-8"))
            if blob.get("day") == time.strftime("%Y-%m-%d"):
                return blob["data"]
        except Exception:
            pass
    import akshare as ak
    df = ak.stock_info_a_code_name()
    data = {str(r["name"]).strip(): str(r["code"]) for _, r in df.iterrows()}
    CODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CODE_CACHE.write_text(json.dumps({"day": time.strftime("%Y-%m-%d"), "data": data},
                                     ensure_ascii=False), encoding="utf-8")
    return data


def resolve_names(names: list[str], table: dict[str, str]) -> list[tuple[str, str]]:
    """公司名 → (名称, 代码)。表=简称；LLM 可能给全名（含'股份有限公司'）或简称。

    匹配优先级：精确 → 简称含于全名(k in n) → 全名含于简称(n in k, n≥4字)。
    """
    out = []
    seen = set()
    for n in names:
        n = (n or "").strip()
        # 剥后缀取候选简称（"江西金力永磁科技股份有限公司" → 匹配"金力永磁"）
        if not n:
            continue
        hit = table.get(n)
        resolved = n
        if hit is None and len(n) >= 4:
            for k, v in table.items():
                if k != n and (k in n or (n in k and len(n) >= 4)):
                    hit = v
                    resolved = k
                    break
        key = hit or n
        if key in seen:
            continue
        seen.add(key)
        out.append((resolved, hit or ""))
    return out


def tx_quotes(codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    syms = ",".join(("sh" if c[0] in "6" else "sz") + c for c in codes)
    url = f"https://qt.gtimg.cn/q={syms}"
    raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": _UA}), timeout=12).read().decode("gbk", "ignore")
    out: dict[str, dict] = {}
    for line in raw.strip().split(";"):
        if "=" not in line:
            continue
        f = line.split("=")[1].strip('"').split("~")
        if len(f) > 46 and f[2] and f[3]:
            try:
                pct = float(f[32]) if f[32] else 0.0
            except ValueError:
                pct = 0.0
            out[f[2]] = {"price": float(f[3]), "pct": pct, "pe": f[39], "pb": f[46]}
    return out


def deep_dive(cands: list[dict], as_of: str) -> list[str]:
    """对候选逐只跑完整深析（run.py 全管线）→ 推荐行。

    每只独立子进程（约 5-7 分钟），产出落 ashare_out/{code}-{as_of}/。
    """
    import subprocess
    import sys as _sys

    out: list[str] = []
    for c in cands:
        code = c["code"]
        name = c["name"]
        print(f"\n[深析] {name} {code} ({as_of}) …", flush=True)
        r = subprocess.run(
            [_sys.executable, "-m", "tradingagents.ashare.run",
             "--ticker", code, "--name", name, "--date", as_of,
             "--force", "--no-cloud"],
            capture_output=True, text=True, timeout=900,
        )
        rec_p = Path("ashare_out") / f"{code}-{as_of}" / "record.json"
        if not rec_p.exists():
            out.append(f"- **{name} {code}**：深析失败（{(r.stdout+r.stderr)[-200:]}）")
            continue
        rec = json.loads(rec_p.read_text(encoding="utf-8"))
        t = rec.get("trader") or {}
        m = rec.get("manager") or {}
        act = t.get("action", "?")
        tag = {"建仓": "🟢 可入", "加仓": "🟢 可入", "持有": "◐ 持有", "观望": "◐ 观望",
               "减仓": "🔴 回避", "清仓": "🔴 回避", "止损": "🔴 回避"}.get(act, act)
        reason = (t.get("reasoning") or "")[:130]
        levels = (t.get("levels") or "")[:80]
        out.append(f"- **{name} {code}** 现价 {rec.get('mark')} ｜ {tag}"
                   f"（交易员:{act} 研经:{m.get('recommendation')}({m.get('confidence')})）")
        if reason:
            out.append(f"  理由：{reason}")
        if levels:
            out.append(f"  关键位：{levels}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=4)
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"), help="分析截至日期")
    ap.add_argument("--deep", type=int, default=0, help="对前 N 个候选跑完整深析")
    args = ap.parse_args()

    table = code_table()
    items = [t for t in fetch_feed(args.pages)
             if any(k in t for k in KEEP)][:60]
    if len(items) < 3:
        print("有效 A 股相关快讯不足，跳过简报")
        return

    # 1) 方向 + 公司名（JSON，禁给代码）
    sys1 = ("你是 A 股盘前情报分析助手。输入为快讯清单。输出 JSON：\n"
            "{\"directions\":[{\"topic\":\"方向一句话\",\"logic\":\"为什么影响A股\","
            "\"names\":[\"最直接受益的A股上市公司全名，3-5个，按受益度排序\"],"
            "\"signal\":\"什么出现说明催化落地\"}]}\n"
            "要求：只输出 JSON；names 必须是真实 A 股上市公司全名；"
            "不确定的公司不要写；最多 3 个方向。")
    d1 = complete_json("quick", sys1, "\n".join(f"- {t}" for t in items))
    dirs = d1.get("directions") or []
    if not dirs:
        print("LLM 未提炼出方向")
        return

    # 2) 解析代码 + 拉行情
    cands: list[dict] = []
    for d in dirs:
        for name, code in resolve_names(d.get("names") or [], table):
            cands.append({"topic": d.get("topic", ""), "logic": d.get("logic", ""),
                          "signal": d.get("signal", ""), "name": name, "code": code})
    cands = [c for c in cands if c["code"]]
    if not cands:
        print("公司名解析不到真实代码")
        return
    quotes = tx_quotes([c["code"] for c in cands])
    for c in cands:
        q = quotes.get(c["code"]) or {}
        c["price"] = q.get("price")
        c["pct"] = q.get("pct", 0.0)
        c["pb"] = q.get("pb", "")

    # 3) 行情注入 → LLM 点评（关注点/风险；禁止喊买卖）
    lines = [f"- {c['name']}({c['code']}) 价{c['price'] or '?'} "
             f"今{c['pct']:+.1f}% PB{c['pb'] or '?'} [方向:{c['topic']}]"
             for c in cands]
    sys2 = ("对下列'方向→候选股'清单逐只给一句话点评：① 与方向受益逻辑是否直接"
            "② 估值/位置是否已透支 ③ 值得跟踪还是回避。输出 JSON："
            "{\"comments\":[{\"code\":\"6位代码\",\"note\":\"一句话点评\"}]}。"
            "禁止推荐买入；只陈述事实与风险。")
    comments = {}
    try:
        d2 = complete_json("quick", sys2, "\n".join(lines))
        comments = {c.get("code"): c.get("note", "") for c in (d2.get("comments") or [])}
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"点评失败: {e}\n")

    md = ["# A股盘前情报 · 前瞻方向与候选", ""]
    for i, d in enumerate(dirs, 1):
        md += [f"## {i}. {d.get('topic', '')}", f"- 逻辑：{d.get('logic', '')}",
               f"- 验证信号：{d.get('signal', '')}", ""]
    md.append("## 候选标的（代码已本地校验）")
    for c in cands:
        note = comments.get(c["code"], "")
        md.append(f"- **{c['name']} {c['code']}** 价{c['price'] or '?'} "
                  f"今{c['pct']:+.1f}% ｜ {note}")
    if not args.deep:
        md.append("\n> 情报提示非投资建议；候选仅作研究池，是否入池深析另行决定。")
        print("\n".join(md))
        return

    # --deep：对候选跑完整 LLM 深析（run.py 全管线），输出入手推荐
    deep = deep_dive([c for c in cands if c["code"]][: args.deep], args.date)
    md += ["", "## 候选深析 · 交易员推荐（完整管线 10 大师+辩论）", ""]
    for r in deep:
        md.append(r)
    md.append("\n> 深析结果基于基本面快照；买入前请自行确认与组合匹配。")
    print("\n".join(md))


if __name__ == "__main__":
    main()
