"""notify_feishu.py — 把批跑结果汇总成飞书 markdown 卡片发出（webhook）。

用法:
  python -m tradingagents.ashare.notify_feishu <卡片.md>
      [--title "📊 A股持仓日报"]
环境: FEISHU_WEBHOOK（飞书自定义机器人 webhook）
卡片: interactive card 的 markdown 元素，正文即文件内容。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path


def send_markdown_card(webhook: str, markdown: str, title: str | None = None) -> int:
    elements: list[dict] = []
    if title:
        elements.append({"tag": "markdown",
                         "content": f"**{title}**\n"})
    elements.append({"tag": "markdown", "content": markdown})
    body = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title or "持仓日报"},
                       "template": "blue"},
            "elements": elements,
        },
    }
    req = urllib.request.Request(
        webhook, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    if data.get("code") not in (0, None) and not data.get("ok"):
        raise RuntimeError(f"feishu webhook error: {data}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("card", help="markdown 卡片文件路径")
    ap.add_argument("--title", default="📊 A股持仓决策 · 双跑")
    ap.add_argument("--webhook", default=None)
    args = ap.parse_args()

    webhook = args.webhook or os.environ.get("FEISHU_WEBHOOK", "")
    if not webhook:
        print("未配置 FEISHU_WEBHOOK，跳过飞书通知（卡片已在本机）")
        return
    md = Path(args.card).read_text(encoding="utf-8")
    # 飞书卡片单条容量有限，超过 24KB 截断尾部
    if len(md.encode("utf-8")) > 24000:
        md = md.encode("utf-8")[:24000].decode("utf-8", "ignore") + "\n…(截断)"
    send_markdown_card(webhook, md, args.title)
    print("已发送飞书卡片")


if __name__ == "__main__":
    sys.exit(main())
