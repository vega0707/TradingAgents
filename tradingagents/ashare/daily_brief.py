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
        "并购", "重组", "发布", "印发", "通知", "意见", "方案", "国产", "出海",
        # 海外宏观（2026-09-17 补）：词表原来只有国内政策与行业，导致美联储加息
        # 当天 11 条相关快讯被丢弃 3 条（含"摩根士丹利：加息加剧亚洲股市回调风险"），
        # 早盘简报完全没提这事儿。
        "美联储", "联储", "加息", "降息", "美债", "美元", "汇率", "美股",
        "纳指", "道指", "标普", "非农", "CPI", "通胀", "原油", "油价", "联邦基金"]


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


def overseas_snapshot() -> str:
    """隔夜海外关键行情一行文本（东财，1 次请求，多子域轮询）。

    美联储决议/美股/美元/商品是 A 股开盘前最重要的外部输入。2026-09-17 美联储
    2023-07 以来首次加息（+25bp 至 3.75%-4%、点阵图年内还要加），早盘简报却
    一个字没提 —— 词表缺海外宏观词把 11 条相关快讯滤掉了大半。本函数提供
    独立于快讯的硬数据。
    注：新浪 int_dji 是陈旧值（46247 vs 实际 51461），美股/商品取东财。
    """
    labels = {"DJIA": "道指", "NDX": "纳指", "SPX": "标普500", "UDI": "美元指数",
              "GC00Y": "纽约金", "CL00Y": "NYMEX原油"}
    secids = "100.DJIA,100.NDX,100.SPX,100.UDI,101.GC00Y,102.CL00Y"
    for host in ("82.push2.eastmoney.com", "push2delay.eastmoney.com", "push2.eastmoney.com"):
        try:
            url = (f"https://{host}/api/qt/ulist.np/get?secids={secids}"
                   f"&fltt=2&invt=2&fields=f12,f2,f3")
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            d = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8"))
            rows = (d.get("data") or {}).get("diff") or []
            parts = []
            for r in rows:
                nm = labels.get(str(r.get("f12")))
                if nm and isinstance(r.get("f3"), (int, float)):
                    parts.append(f"{nm} {r.get('f2')} ({r['f3']:+.2f}%)")
            if parts:
                return " | ".join(parts)
        except Exception:  # noqa: BLE001 — 换下一个行情节点
            continue
    return ""


def code_table() -> dict[str, str]:
    """全市场 {公司名: 代码}。缓存放宽 7 天 + 取数失败用旧缓存兜底。

    教训(2026-09-10)：当日缓存隔天必重拉，数据源连不上时 5 次退避重试
    卡死早盘任务——A 股代码表月度级变化，7 天缓存足够；失败读旧缓存。
    2026-09-16：数据源由 akshare 换为东财 clist 直连（见 _em_code_table）。
    """
    cache = None
    if CODE_CACHE.is_file():
        try:
            cache = json.loads(CODE_CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = None
    if cache:
        from datetime import datetime
        try:
            saved = datetime.strptime(cache.get("day", ""), "%Y-%m-%d").date()
            if (datetime.now().date() - saved).days < 7:
                return cache["data"]
        except Exception:
            pass
    try:
        data = _em_code_table()
        if not data:
            raise RuntimeError("东财代码表为空")
        CODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CODE_CACHE.write_text(json.dumps({"day": time.strftime("%Y-%m-%d"), "data": data},
                                         ensure_ascii=False), encoding="utf-8")
        return data
    except Exception:
        if cache and cache.get("data"):
            return cache["data"]   # 取数失败 → 旧代码表兜底（代码表月级稳定）
        raise


def _em_code_table() -> dict[str, str]:
    """全市场 {名称: 代码}：东财 clist 分页直连（替代 akshare.stock_info_a_code_name）。

    一次 100 只、约 56 页，只取 f12(代码)/f14(名称) 两字段。多子域轮询——
    实测 push2 断连时 82.push2 / push2delay 仍通。7 天缓存（见 code_table）。
    """
    hosts = ("82.push2.eastmoney.com", "push2delay.eastmoney.com", "push2.eastmoney.com")
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    out: dict[str, str] = {}
    for pn in range(1, 70):
        rows = []
        for host in hosts:
            try:
                url = (f"https://{host}/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2"
                       f"&fid=f12&fs={fs}&fields=f12,f14")
                req = urllib.request.Request(url, headers={"User-Agent": _UA})
                d = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
                rows = ((d.get("data") or {}).get("diff") or [])
                if rows:
                    break
            except Exception:  # noqa: BLE001 — 换下一个行情节点
                continue
        if not rows:
            break
        for r in rows:
            if r.get("f12") and r.get("f14"):
                out[str(r["f14"]).strip()] = str(r["f12"])
        if len(rows) < 100:
            break
        time.sleep(0.15)
    return out


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


def _already_deep(code: str, as_of: str, days: int = 3) -> str | None:
    """近 days 天该候选是否已深析 → 返回日期或 None（去重，不重复花 LLM）。

    目录名 {code}-YYYY-MM-DD 定长切分（不能 rsplit——日期含连字符，同
    make_daily_card 教训；2026-09-10 曾 rsplit 出 '10' 致 strptime 崩）。
    """
    from datetime import datetime
    try:
        asof = datetime.strptime(as_of, "%Y-%m-%d").date()
    except ValueError:
        return None
    best = None
    for d in Path("ashare_out").glob(f"{code}-*"):
        if not d.is_dir() or not (d / "record.json").is_file():
            continue
        a = d.name[-10:]  # 定长取 YYYY-MM-DD
        try:
            da = datetime.strptime(a, "%Y-%m-%d").date()
        except ValueError:
            continue
        if da < asof and (best is None or da > best):
            best = da
    if best and (asof - best).days <= days:
        return best.isoformat()
    return None


def current_equity() -> float:
    """当前持仓股票市值（读 tickers + 现价），供单票上限/手数计算。"""
    import yaml
    try:
        d = yaml.safe_load(Path("/Users/vega/git/ai-hedge-fund/config/tickers.yaml").read_text(encoding="utf-8"))
        eq = 0.0
        for t in d["tickers"]:
            s = float(t.get("shares") or 0)
            if s <= 0:
                continue
            rows = [k for k in fetch_daily_kline(t["code"], 5)]
            if rows:
                eq += s * rows[-1]["close"]
        return eq or 100_000.0
    except Exception:
        return 100_000.0


def high_gate(c: dict, price: float) -> list[str]:
    """风格闸：高位/高估检查（用户硬约束：不追高、低回撤）。"""
    flags = []
    pb = c.get("pb")
    if pb:
        try:
            if float(pb) > 4:
                flags.append(f"PB {pb} 倍(>4)")
        except ValueError:
            pass
    try:
        rows = [k for k in fetch_daily_kline(c["code"], 90)]
        closes = [r["close"] for r in rows[-60:]]
        hi60 = max(closes)
        if price and hi60 and (hi60 - price) / hi60 < 0.05:
            flags.append("贴 60 日高点(<5%)")
    except Exception:
        pass
    return flags


def advice_card(c: dict, rec_line: str, as_of: str) -> list[str]:
    """可执行卡：裁决/风格闸/几手/关键位/止损。"""
    import json

    out = [rec_line]
    price = c.get("price")
    gates = high_gate(c, price or 0)
    equity = current_equity()
    cap = equity * 0.05
    if price:
        hand_cost = price * 100
        lots = int(cap // hand_cost) if hand_cost else 0
        if lots < 1:
            out.append(f"  💰 1手={hand_cost/10000:.1f}万 > 单票上限 {cap/10000:.1f}万(组合5%)"
                       f"——**当前不适合建仓**（除非明确超配意愿）")
        else:
            out.append(f"  💰 单票上限 {cap/10000:.1f}万 → 最多 {lots} 手（1手={hand_cost/10000:.1f}万）")
    if gates:
        # 裁决唯一：风格闸命中时同一张卡里不能既喊"可入"又说冲突
        # （用户 2026-09-10 / 09-14 两次指出"前面说可入后面说别追，到底听哪个"）
        if "🟢 可入" in out[0]:
            out[0] = out[0].replace("🟢 可入", "⚠️ **不建议**（不满足低回撤）")
        out.append(f"  ⚠️ 风格闸命中：{'、'.join(gates)}——与你的低回撤原则冲突，仅建议小仓试探或回避")
    rec_p = Path("ashare_out") / f"{c['code']}-{as_of}" / "record.json"
    if rec_p.exists():
        rec = json.loads(rec_p.read_text(encoding="utf-8"))
        t = rec.get("trader") or {}
        # 细节(reasoning/levels/stop_loss)在 decision.json
        dec_p = Path("ashare_out") / f"{c['code']}-{as_of}" / "decision.json"
        dt = {}
        if dec_p.exists():
            dt = (json.loads(dec_p.read_text(encoding="utf-8")) or {}).get("trader") or {}
        if dt.get("levels"):
            out.append(f"  📍 关键位：{dt['levels'][:110]}")
        if dt.get("stop_loss"):
            out.append(f"  🛑 止损：{dt['stop_loss'][:90]}")
        elif t.get("stop_loss"):
            out.append(f"  🛑 止损：{t['stop_loss'][:90]}")
    return out


def deep_one(code: str, name: str, as_of: str) -> str:
    """单只完整深析 → 推荐行文本。"""
    import json
    import subprocess
    import sys as _sys

    # 进度日志一律走 stderr：stdout 是卡片正文（重定向进 /tmp/ashare-morning.md）
    sys.stderr.write(f"[深析] {name} {code} ({as_of}) …\n")
    sys.stderr.flush()
    r = subprocess.run(
        [_sys.executable, "-m", "tradingagents.ashare.run",
         "--ticker", code, "--name", name, "--date", as_of,
         "--force", "--no-cloud"],
        capture_output=True, text=True, timeout=900,
    )
    rec_p = Path("ashare_out") / f"{code}-{as_of}" / "record.json"
    if not rec_p.exists():
        return f"- **{name} {code}**：深析失败（{(r.stdout + r.stderr)[-200:]}）"
    rec = json.loads(rec_p.read_text(encoding="utf-8"))
    t = rec.get("trader") or {}
    m = rec.get("manager") or {}
    act = t.get("action", "?")
    tag = {"建仓": "🟢 可入", "加仓": "🟢 可入", "持有": "◐ 持有", "观望": "◐ 观望",
           "减仓": "🔴 回避", "清仓": "🔴 回避", "止损": "🔴 回避"}.get(act, act)
    reason = (t.get("reasoning") or "")[:130]
    levels = (t.get("levels") or "")[:80]
    out = [f"- **{name} {code}** 现价 {rec.get('mark')} ｜ {tag}"
           f"（交易员:{act} 研经:{m.get('recommendation')}({m.get('confidence')})）"]
    if reason:
        out.append(f"  理由：{reason}")
    if levels:
        out.append(f"  关键位：{levels}")
    return "\n".join(out)


def deep_dive(cands: list[dict], as_of: str) -> tuple[list[str], int, int]:
    """并行深析候选（去重+预热财务+2路 LLM）。返回 (行, 成功数, 失败数)。"""
    from concurrent.futures import ThreadPoolExecutor

    # 去重：近 3 天深析过的跳过
    todo = []
    for c in cands:
        done = _already_deep(c["code"], as_of)
        if done:
            sys.stderr.write(f"[跳过] {c['name']} {c['code']} 近 3 天({done})已深析\n")
        else:
            todo.append(c)
    if not todo:
        return [], 0, 0

    # 预热：财务快照串行种缓存（防并发打爆 datacenter 源）
    from tradingagents.ashare.material import build_material
    warmed = []
    for c in todo:
        try:
            m = build_material(c["code"], as_of, c["name"])
            if m.fundamentals_ok:
                warmed.append(c)
                sys.stderr.write(f"[预热] {c['name']} 财务 OK\n")
            else:
                sys.stderr.write(f"[预热] {c['name']} 基本面缺失仍尝试深析\n")
                warmed.append(c)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[预热] {c['name']} 失败({str(e)[:80]})，仍深析\n")
            warmed.append(c)

    results = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(deep_one, c["code"], c["name"], as_of): c for c in warmed}
        by_code = {futs[f]["code"]: f for f in futs}
        for c in warmed:  # 按候选顺序收结果（并行但输出有序）
            f = by_code[c["code"]]
            results.append((c, f.result()))
    ok = sum(1 for _, x in results if "深析失败" not in x)
    fail = len(results) - ok
    return results, ok, fail


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

    # 隔夜海外（美联储/美股/美元/商品）：A 股开盘前最重要的外部输入，独立于快讯。
    # 2026-09-17 教训：美联储 2023 年 7 月以来首次加息当天，简报一个字没提。
    os_text = overseas_snapshot()

    # 1) 方向 + 公司名（JSON，禁给代码）
    sys1 = ("你是 A 股盘前情报分析助手。输入为隔夜海外行情 + 快讯清单。输出 JSON：\n"
            "{\"directions\":[{\"topic\":\"方向一句话\",\"logic\":\"为什么影响A股\","
            "\"names\":[\"最直接受益的A股上市公司全名，3-5个，按受益度排序\"],"
            "\"signal\":\"什么出现说明催化落地\"}]}\n"
            "要求：只输出 JSON；names 必须是真实 A 股上市公司全名；"
            "不确定的公司不要写；最多 3 个方向。\n"
            "若海外构成系统性风险（美联储加息/美债飙升/美元大涨/美股大跌），"
            "必须把它作为第 1 个方向，并分别给出受损与受益板块。")
    user1 = ((f"【隔夜海外】{os_text}\n\n" if os_text else "")
             + "【A股快讯】\n" + "\n".join(f"- {t}" for t in items))
    d1 = complete_json("quick", sys1, user1)
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

    # --deep：对候选跑完整 LLM 深析（去重+预热+2路并行）+ 可执行卡
    deep, ok_n, fail_n = deep_dive([c for c in cands if c["code"]][: args.deep], args.date)
    if not deep:
        print("候选均近 3 天已深析 → 无新深析，跳过推荐")
        return
    md += ["", f"## 候选深析 · 交易员推荐（完成 {ok_n}/{ok_n+fail_n} 只" +
           (f"，{fail_n} 只失败" if fail_n else "") + "）", ""]
    for c, line in deep:
        for row in advice_card(c, line, args.date):
            md.append(row)
        md.append("")
    md.append("> 裁决规则：深析(10大师)为准；但风格闸命中(高PB/贴高点)时与低回撤原则冲突，只建议小仓/回避。")
    print("\n".join(md))


if __name__ == "__main__":
    main()
