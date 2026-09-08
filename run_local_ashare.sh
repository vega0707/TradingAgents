#!/usr/bin/env bash
# run_local_ashare.sh · 本地 A 股分析 + 飞书推送（程小帮定时任务调用）
# 流程：同步腾讯云持仓 → 本地跑分析 → 生成卡片 → scp 到服务器 → openclaw 发原群
set -uo pipefail
cd /Users/vega/git/TradingAgents
DATE=$(date +%Y-%m-%d)
LOG=logs/ashare-${DATE}.log
mkdir -p logs
KEY=/Users/vega/git/steel/servers/tx.pem
TEN_HOST="ubuntu@124.221.95.205"

# 1. 同步腾讯云权威持仓到本地
ssh -i "$KEY" "$TEN_HOST" "cat ~/ai-hedge-fund/config/tickers.yaml" > /Users/vega/git/ai-hedge-fund/config/tickers.yaml 2>>"$LOG"
echo "[$DATE] tickers 已同步 $(date +%H:%M)" >> "$LOG"

# 2. 跑分析（.env 已配 ADA Coding Plan）。--force：当日定时批跑覆盖当天
#    已有 record（如盘中试跑/旧数据版污染），跨天不冲突。
[[ -f .env ]] && set -a && source .env && set +a
echo "[$DATE] ashare 批跑开始 $(date +%H:%M)" >> "$LOG"
.venv/bin/python -m tradingagents.ashare.run_holdings \
    --tickers /Users/vega/git/ai-hedge-fund/config/tickers.yaml --rounds 2 --force \
    >> "$LOG" 2>&1
RC=$?
echo "[$DATE] 批跑结束 rc=$RC $(date +%H:%M)" >> "$LOG"

# 3. 生成当日汇总卡（make_daily_card 显式带日期，避免混入历史 record）
.venv/bin/python -m tradingagents.ashare.make_daily_card "$DATE" > /tmp/ashare-card.md 2>/dev/null
cp /tmp/ashare-card.md "logs/card-${DATE}.md"

# 4. 信号闸门：全部持有/观望（无调仓动作）→ 不推飞书（仅批跑成功时跳过，失败保留退出码）
SIGNALS=$(.venv/bin/python -m tradingagents.ashare.make_daily_card --signals "$DATE" 2>/dev/null)
if [[ $RC -eq 0 && -z "$SIGNALS" ]]; then
  echo "[$DATE] 全部持有/观望，无调仓信号 → 跳过飞书推送（卡片在 logs/card-${DATE}.md）" >> "$LOG"
  exit 0
fi
echo "[$DATE] 调仓信号:" >> "$LOG"
echo "$SIGNALS" >> "$LOG"

# 5. 推送：scp 卡片到服务器，服务器 openclaw 发原群
if [[ -s /tmp/ashare-card.md ]]; then
  scp -i "$KEY" -q /tmp/ashare-card.md ubuntu@124.221.95.205:/tmp/ashare-card.md 2>>"$LOG"
  ssh -i "$KEY" ubuntu@124.221.95.205 "cd /home/ubuntu/TradingAgents && set -a && source .env && set +a && \
    BIN=\$HOME/.local/share/pnpm/global/5/.pnpm/openclaw@2026.7.1-2/node_modules/openclaw/dist/index.js && \
    NODE=\$HOME/.nvm/versions/node/v22.23.2/bin/node && \
    \$NODE \$BIN message send --channel feishu --account \"\$FEISHU_ACCOUNT\" --target \"\$FEISHU_TARGET\" -m \"\$(cat /tmp/ashare-card.md)\"" >>"$LOG" 2>&1 \
    && echo "[$DATE] 已发飞书卡片" >> "$LOG" \
    || echo "[$DATE] 飞书发送失败（卡片在 logs/card-${DATE}.md）" >> "$LOG"
fi
exit $RC
