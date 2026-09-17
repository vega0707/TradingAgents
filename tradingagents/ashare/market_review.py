#!/usr/bin/env python3
"""market_review.py — 每日市场归因（为什么今天涨/跌）

用户 2026-09-17 的要求："你要学会问为什么？为什么今天跌了、为什么发生了这件事，
然后抽丝剥茧构建一个属于我们自己的模型，然后去验证。" —— 并指出资金和情绪也
是必须考虑的因素。

四层归因（每层先给事实，再给判断，最后给匹配）：
  ① 表象：指数涨跌、成交额、涨跌家数（广度）
  ② 结构：行业领涨/领跌两端（含个股两端）
  ③ 资金：板块主力净流入/流出两端 + 涨停/跌停家数
  ④ 事件匹配：把异动板块与隔夜海外/宏观事件挂钩

设计原则（防止变成"事后编故事"）：
  · 数据先行：每一条归因都必须能追溯到具体数字
  · 映射表标注来源：哪些关系经过回测验证、哪些只是常识假设
  · "未找到对应事件" 是合法且必须保留的结论

数据源：东财（指数+涨跌家数 f104/f105/f106、行业与资金流 clist、涨停/跌停池
push2ex）、macro（隔夜海外 + regime/政策窗口）。
用法: .venv/bin/python -m tradingagents.ashare.market_review [--date YYYYMMDD]
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import date

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
HOSTS = ("push2delay.eastmoney.com", "82.push2.eastmoney.com", "push2.eastmoney.com")

# 事件 → 板块映射。verified=True 表示该关系在我们自己的回测里验证过；
# False 表示只是常识假设（未经检验，输出时明确区分）。
EVENT_SECTOR_MAP: list[tuple[str, tuple[str, ...], bool, str]] = [
    ("美联储加息 / 美元走强", ("贵金属", "黄金", "白银", "有色"), False,
     "bt_event_fed 检验（山东黄金，2021-2026，11 次加息）：加息次日反而多为上涨"
     "（下跌占比 36%）；T+5 有弱负向（均值 -1.28%、中位 -3.15%、下跌占比 73%），"
     "但样本仅 11 个，统计功效不足——**未验证**"),
    ("国内降准 / 降息", ("证券", "保险", "房地产", "银行"), True,
     "bt_policy：PMI<50+宽松共振窗口，未来 3 月 +7.9%、胜率 68%"),
    ("油价上行", ("石油", "煤炭", "油服", "燃气"), False, "成本传导常识，未回测"),
    ("AI / 算力催化", ("半导体", "光", "计算机", "通信", "电子"), False, "主题映射常识，未回测"),
    ("地产政策", ("房地产", "房产", "建材", "家居"), False, "政策链常识，未回测"),
    ("农业 / 粮食", ("种", "养殖", "饲料", "农"), False, "主题映射常识，未回测"),
    ("AI 安全 / 数据安全", ("软件", "网络安全", "计算机"), False, "主题映射常识，未回测"),
]


def _q(path: str, extra: str = "") -> list[dict]:
    """东财 clist 类接口（多域名轮询）。"""
    for h in HOSTS:
        try:
            u = f"https://{h}{path}{extra}"
            d = json.loads(urllib.request.urlopen(
                urllib.request.Request(u, headers=UA), timeout=15).read().decode())
            rows = (d.get("data") or {}).get("diff") or []
            if rows:
                return rows
        except Exception:  # noqa: BLE001 — 换节点
            continue
    return []


def index_and_breadth() -> dict:
    """指数表现 + 涨跌家数（东财 ulist 的 f104/105/106）。"""
    for h in HOSTS:
        try:
            u = (f"https://{h}/api/qt/ulist.np/get?secids=1.000001,0.399001,1.000300"
                 f"&fltt=2&invt=2&fields=f12,f14,f2,f3,f104,f105,f106")
            d = json.loads(urllib.request.urlopen(
                urllib.request.Request(u, headers=UA), timeout=15).read().decode())
            rows = (d.get("data") or {}).get("diff") or []
            if rows:
                out = {"indices": [(r.get("f14"), r.get("f2"), r.get("f3")) for r in rows]}
                sh = next((r for r in rows if str(r.get("f12")) == "000001"), {})
                out["up"], out["down"], out["flat"] = sh.get("f104"), sh.get("f105"), sh.get("f106")
                return out
        except Exception:  # noqa: BLE001
            continue
    return {}


def industry_two_ends(n: int = 6) -> tuple[list, list]:
    p = "/api/qt/clist/get?pn=1&fs=m:90+t:2&np=1&fltt=2&invt=2&fid=f3&fields=f12,f14,f3,f62"
    up = _q(p, f"&po=1&pz={n}")
    dn = _q(p, f"&po=0&pz={n}")
    return up, dn


def moneyflow_two_ends(n: int = 6) -> tuple[list, list]:
    p = "/api/qt/clist/get?pn=1&fs=m:90+t:2&np=1&fltt=2&invt=2&fid=f62&fields=f12,f14,f3,f62"
    inflow = _q(p, f"&po=1&pz={n}")
    outflow = _q(p, f"&po=0&pz={n}")
    return inflow, outflow


def limit_pools(day: str) -> tuple[int | None, int | None]:
    """涨停/跌停家数（东财 push2ex）。"""
    def cnt(kind: str) -> int | None:
        try:
            u = (f"https://push2ex.eastmoney.com/getTopic{kind}Pool?ut=7eea3edcaed734bea9cbfc24409ed989"
                 f"&dpt=wz.ztzt&Pageindex=0&pagesize=1&sort=fbt%3Aasc&date={day}")
            d = json.loads(urllib.request.urlopen(
                urllib.request.Request(u, headers=UA), timeout=15).read().decode())
            return (d.get("data") or {}).get("tc")
        except Exception:  # noqa: BLE001
            return None
    return cnt("ZT"), cnt("DT")


def match_events(up: list, dn: list, overnight: str, fed: str) -> list[str]:
    """把异动板块与事件挂钩；匹配不上就明说。"""
    out = []
    all_marks = [(i.get("f14"), i.get("f3"), "涨") for i in up] + \
                [(i.get("f14"), i.get("f3"), "跌") for i in dn]
    hit = set()
    for label, kws, verified, note in EVENT_SECTOR_MAP:
        # 该事件当前是否有触发迹象（海外行情/美联储判定的关键词）
        active = any(k in overnight for k in ("道指", "纳指")) and (
            ("美联储" in label and "鹰" in fed) or ("美元" in label and "美元指数" in overnight)
            or ("油价" in label and "原油" in overnight) or verified is False)
        for name, pct, direction in all_marks:
            if name and any(k in name for k in kws):
                tag = "已验证" if verified else "常识假设"
                out.append(f"- **{name} {pct:+.1f}%（{direction}）** ← 关联事件：{label}"
                           f"〔{tag}〕{note}" + ("" if active else "（当前未见该事件触发迹象，匹配存疑）"))
                hit.add(name)
    missing = [f"{n} {p:+.1f}%" for n, p, _ in all_marks if n not in hit]
    if missing:
        out.append(f"- 未匹配到事件：{'、'.join(missing[:8])}——可能是资金/情绪驱动，"
                   "不强行归因到具体新闻")
    return out


def render(d: dict) -> str:
    idx = d["index"]
    lines = [f"# 市场归因 · {d['day']}", ""]
    if idx.get("indices"):
        lines.append("## ① 表象")
        for nm, last, pct in idx["indices"]:
            lines.append(f"- {nm} {last} ({pct:+.2f}%)")
        if idx.get("up") is not None:
            lines.append(f"- 涨跌家数：涨 {idx['up']} / 跌 {idx['down']} / 平 {idx['flat']}")
    lines += ["", "## ② 结构（行业两端）",
              f"- 领涨：{' / '.join(f'{i[0]}{i[1]:+.1f}%' for i in d['up'])}",
              f"- 领跌：{' / '.join(f'{i[0]}{i[1]:+.1f}%' for i in d['dn'])}", ""]
    lines += ["## ③ 资金与情绪"]
    if d["inflow"]:
        lines.append("- 主力净流入前列：" + " / ".join(
            f"{i[0]} {i[2] / 1e8:+.1f}亿" for i in d["inflow"] if isinstance(i[2], (int, float))))
    if d["outflow"]:
        lines.append("- 主力净流出前列：" + " / ".join(
            f"{i[0]} {i[2] / 1e8:+.1f}亿" for i in d["outflow"] if isinstance(i[2], (int, float))))
    zt, dt = d.get("zt"), d.get("dt")
    if zt is not None or dt is not None:
        lines.append(f"- 涨停 {zt if zt is not None else 'n/a'} 家 / 跌停 {dt if dt is not None else 'n/a'} 家")
    lines += ["", "## ④ 事件匹配"]
    lines += d.get("matches") or ["- （无数据）"]
    lines += ["", f"> 隔夜海外：{d.get('overnight') or 'n/a'}",
              f"> 宏观：{d.get('macro_note') or 'n/a'}"]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().strftime("%Y%m%d"))
    args = ap.parse_args()
    up, dn = industry_two_ends()
    inflow, outflow = moneyflow_two_ends()
    zt, dt = limit_pools(args.date)
    overnight, fed, macro_note = "", "", ""
    try:
        from tradingagents.ashare.daily_brief import overseas_snapshot
        overnight = overseas_snapshot()
    except Exception:  # noqa: BLE001
        pass
    try:
        from tradingagents.ashare.macro import collect, judge
        mj = judge(collect())
        fed = mj.get("stance", "")
        rg = (mj.get("regime") or {})
        macro_note = f"{mj.get('stance')}（{mj.get('total'):+d}）｜ {rg.get('regime')} ｜ 窗口 {rg.get('window')}"
    except Exception:  # noqa: BLE001
        pass
    data = {"day": args.date, "index": index_and_breadth(),
            "up": [(i.get("f14"), i.get("f3")) for i in up],
            "dn": [(i.get("f14"), i.get("f3")) for i in dn],
            "inflow": [(i.get("f14"), i.get("f3"), i.get("f62")) for i in inflow],
            "outflow": [(i.get("f14"), i.get("f3"), i.get("f62")) for i in outflow],
            "zt": zt, "dt": dt, "overnight": overnight, "macro_note": macro_note,
            "matches": match_events(up, dn, overnight, fed)}
    print(render(data))


if __name__ == "__main__":
    main()
