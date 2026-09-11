#!/usr/bin/env bash
# ashare_daily.sh · 每日 13:00：持仓 ashare 分析(rounds=2) + 汇总卡 + 飞书
# 本地版：LLM 走程小帮同款 xiaobang 网关；tickers 权威在本地 ai-hedge-fund/config
set -uo pipefail
cd /Users/vega/git/TradingAgents
DATE=$(date +%Y-%m-%d)
LOG=logs/ashare-${DATE}.log
mkdir -p logs
[[ -f .env ]] && set -a && source .env && set +a
echo "[$DATE] ashare 批跑开始 $(date +%H:%M)" >> "$LOG"
.venv/bin/python -m tradingagents.ashare.run_holdings \
    --tickers /Users/vega/git/ai-hedge-fund/config/tickers.yaml --rounds 2 \
    >> "$LOG" 2>&1
RC=$?
echo "[$DATE] 批跑结束 rc=$RC $(date +%H:%M)" >> "$LOG"
.venv/bin/python -m tradingagents.ashare.make_daily_card > /tmp/ashare-card.md 2>/dev/null
cp /tmp/ashare-card.md "logs/card-${DATE}.md"
echo "[$DATE] 卡片已生成 logs/card-${DATE}.md（如需飞书推送请配 FEISHU_ACCOUNT/TARGET 并接 openclaw/程小帮）" >> "$LOG"
exit $RC
