#!/usr/bin/env bash
# run_daily_brief.sh · 每日盘前消息面情报（新浪7x24 → LLM 前瞻方向）→ 飞书
# 程小帮定时 8:40 调用；当日有效快讯不足 3 条则静默跳过（不推噪音）。
set -uo pipefail
cd /Users/vega/git/TradingAgents
DATE=$(date +%Y-%m-%d)
LOG=logs/brief-${DATE}.log
mkdir -p logs
KEY=/Users/vega/git/steel/servers/tx.pem
TEN_HOST="ubuntu@124.221.95.205"

[[ -f .env ]] && set -a && source .env && set +a

echo "[$DATE] brief 开始 $(date +%H:%M)" >> "$LOG"
.venv/bin/python -m tradingagents.ashare.daily_brief --pages 4 > /tmp/ashare-brief.md 2>>"$LOG"
RC=$?
if [[ $RC -ne 0 || ! -s /tmp/ashare-brief.md ]]; then
  echo "[$DATE] 简报生成失败/为空 → 不推（rc=$RC）" >> "$LOG"
  exit 0
fi
cp /tmp/ashare-brief.md "logs/brief-${DATE}.md"

# 前置一行标题后推送
sed -i '' "1i\\
# A股盘前情报 · $DATE
" /tmp/ashare-brief.md
scp -i "$KEY" -q /tmp/ashare-brief.md ubuntu@124.221.95.205:/tmp/ashare-brief.md 2>>"$LOG"
ssh -i "$KEY" ubuntu@124.221.95.205 "cd /home/ubuntu/TradingAgents && set -a && source .env && set +a && \
  BIN=\$HOME/.local/share/pnpm/global/5/.pnpm/openclaw@2026.7.1-2/node_modules/openclaw/dist/index.js && \
  NODE=\$HOME/.nvm/versions/node/v22.23.2/bin/node && \
  \$NODE \$BIN message send --channel feishu --account \"\$FEISHU_ACCOUNT\" --target \"\$FEISHU_TARGET\" -m \"\$(cat /tmp/ashare-brief.md)\"" >>"$LOG" 2>&1 \
  && echo "[$DATE] 已发飞书简报" >> "$LOG" \
  || echo "[$DATE] 简报发送失败（在 logs/brief-${DATE}.md）" >> "$LOG"
exit 0
