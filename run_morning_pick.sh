#!/usr/bin/env bash
# run_morning_pick.sh · 早盘推荐 job（交易日 6:00）：消息→挑候选→LLM 深析→入手推荐
# 深析 as_of=今天（盘前 kline 取昨收，PIT 正确）；候选深析上限 5 只 ~40min，
# 9:30 开盘前完成推送。非交易日/快讯不足自动静默。
set -uo pipefail
cd /Users/vega/git/TradingAgents
DATE=$(date +%Y-%m-%d)
LOG=logs/morning-${DATE}.log
mkdir -p logs
KEY=/Users/vega/git/steel/servers/tx.pem
TEN_HOST="ubuntu@124.221.95.205"

[[ -f .env ]] && set -a && source .env && set +a

echo "[$DATE] morning pick 开始 $(date +%H:%M)" >> "$LOG"
.venv/bin/python -m tradingagents.ashare.daily_brief --pages 5 --date "$DATE" --deep 5 \
    > /tmp/ashare-morning.md 2>>"$LOG"
RC=$?
if [[ $RC -ne 0 || ! -s /tmp/ashare-morning.md ]]; then
  echo "[$DATE] 生成失败/为空 → 静默（rc=$RC）" >> "$LOG"
  exit 0
fi
# 无候选深析段则说明快讯不足，不推
if ! grep -q "候选深析" /tmp/ashare-morning.md; then
  echo "[$DATE] 无深析候选 → 静默" >> "$LOG"
  exit 0
fi
cp /tmp/ashare-morning.md "logs/morning-${DATE}.md"

scp -i "$KEY" -q /tmp/ashare-morning.md ubuntu@124.221.95.205:/tmp/ashare-morning.md 2>>"$LOG"
ssh -i "$KEY" ubuntu@124.221.95.205 "cd /home/ubuntu/TradingAgents && set -a && source .env && set +a && \
  BIN=\$HOME/.local/share/pnpm/global/5/.pnpm/openclaw@2026.7.1-2/node_modules/openclaw/dist/index.js && \
  NODE=\$HOME/.nvm/versions/node/v22.23.2/bin/node && \
  \$NODE \$BIN message send --channel feishu --account \"\$FEISHU_ACCOUNT\" --target \"\$FEISHU_TARGET\" -m \"\$(cat /tmp/ashare-morning.md)\"" >>"$LOG" 2>&1 \
  && echo "[$DATE] 已发早盘推荐" >> "$LOG" \
  || echo "[$DATE] 早盘推荐发送失败（在 logs/morning-${DATE}.md）" >> "$LOG"
exit 0
