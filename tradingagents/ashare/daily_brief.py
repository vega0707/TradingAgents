#!/usr/bin/env python3
"""daily_brief.py — 每日消息面情报：新浪7x24快讯 → LLM 分组 → 前瞻方向。

用途：早盘前/每日跑一次，识别政策/行业/公司层面的潜在催化，输出
"可能提前布局的方向"。纯情报提示，非荐股——LLM 给方向与逻辑，
不给具体买入指令。

用法: .venv/bin/python -m tradingagents.ashare.daily_brief [--pages 3]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

from tradingagents.ashare.llmio import complete

FEED_URL = ("https://zhibo.sina.com.cn/api/zhibo/feed?page={p}&page_size=20"
            "&zhibo_id=152&tag_id=0&dire=f&dpc=1")
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0"

# A股相关关键词（命中才保留；过滤纯海外/无关）
KEEP = ["政策", "证监会", "央行", "国常会", "发改委", "工信部", "商务部", "国务院",
        "反倾销", "关税", "A股", "两市", "涨停", "板块", "行业", "半导体", "芯片",
        "新能源", "光伏", "储能", "算力", "人工智能", "机器人", "创新药", "医药",
        "白酒", "消费", "地产", "银行", "券商", "黄金", "有色", "化工", "涨价",
        "中标", "订单", "回购", "增持", "减持", "业绩", "财报", "北向", "IPO",
        "并购", "重组", "数据", "发布", "印发", "通知", "意见", "方案"]
SKIP = ["美股", "欧股", "日经", "美联储", "特朗普", "比特币", "加密货币", "港股"]


def fetch_feed(pages: int = 3) -> list[dict]:
    out: list[dict] = []
    for p in range(1, pages + 1):
        try:
            req = urllib.request.Request(FEED_URL.format(p=p), headers={"User-Agent": _UA})
            data = json.loads(urllib.request.urlopen(req, timeout=12).read().decode())
            items = ((data.get("result") or {}).get("data") or {}).get("feed") or {}
            lst = items.get("list") or []
            for it in lst:
                txt = (it.get("rich_text") or "").strip()
                if txt:
                    out.append({"t": it.get("create_time", ""), "txt": txt})
            if len(lst) < 20:
                break
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"page {p} 失败: {e}\n")
        time.sleep(1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=3)
    args = ap.parse_args()

    items = fetch_feed(args.pages)
    print(f"拉取新浪7x24快讯 {len(items)} 条")
    kept = []
    for it in items:
        t = it["txt"]
        if any(k in t for k in SKIP) and not any(k in t for k in ("反倾销", "关税")):
            continue
        if any(k in t for k in KEEP):
            kept.append(t[:300])
    if len(kept) < 3:
        print("有效 A 股相关快讯不足，跳过简报")
        return
    print(f"命中 A股相关 {len(kept)} 条")

    news_text = "\n".join(f"- {t}" for t in kept[:60])
    system = ("你是 A 股盘前情报分析助手。根据快讯清单，输出：\n"
              "## 今日关注方向\n按重要性列出 2-4 个潜在催化方向，每项含：\n"
              "- 事件/政策：一句话\n- 受益逻辑：为什么影响 A 股\n"
              "- 涉及板块（不含具体代码）：…\n- 验证信号：什么出现说明催化落地\n"
              "## 风险提示\n需警惕的利空/不确定性。\n"
              "要求：只依据快讯事实，不编造；明确区分【事实】与【推测】。")
    try:
        brief = complete("quick", system, news_text, max_tokens=1800)
    except Exception as e:  # noqa: BLE001
        print(f"LLM 简报失败: {e}")
        return
    print("\n" + brief)


if __name__ == "__main__":
    main()
