#!/usr/bin/env bash
# run_local_ashare.sh · 本地 A 股每日流程 + 飞书推送（程小帮定时任务调用）
# 增量架构：只深析"真变了"的票（daily_flow --decide），组合层每日必跑，
# 无动作不推。FORCE_FULL=1 强制全量深析（手动周末/重建用）。
set -uo pipefail
cd /Users/vega/git/TradingAgents
DATE=$(date +%Y-%m-%d)
LOG=logs/ashare-${DATE}.log
mkdir -p logs
KEY=/Users/vega/git/steel/servers/tx.pem
TEN_HOST="ubuntu@124.221.95.205"
PY=".venv/bin/python"
TICKERS=/Users/vega/git/ai-hedge-fund/config/tickers.yaml

# 1. 同步腾讯云权威持仓到本地
ssh -i "$KEY" "$TEN_HOST" "cat ~/ai-hedge-fund/config/tickers.yaml" > $TICKERS 2>>"$LOG"
echo "[$DATE] tickers 已同步 $(date +%H:%M)" >> "$LOG"

[[ -f .env ]] && set -a && source .env && set +a

# 2. 增量判断：哪些票需要 LLM 深析
echo "[$DATE] 增量判断 $(date +%H:%M)" >> "$LOG"
if [[ "${FORCE_FULL:-0}" == "1" ]]; then
  echo "FORCE_FULL=1 → 全量深析" >> "$LOG"
  ONLY_ALL=$($PY -c "
import yaml
d=yaml.safe_load(open('$TICKERS'))
print(','.join(t['code'] for t in d['tickers'] if t.get('shares') and t['shares']>0))")
  ONLY="$ONLY_ALL"
else
  DECIDE_OUT=$($PY -m tradingagents.ashare.daily_flow --decide --date "$DATE" --tickers "$TICKERS" 2>>"$LOG")
  echo "$DECIDE_OUT" >> "$LOG"
  ONLY=$(echo "$DECIDE_OUT" | grep '^TRIGGERED=' | cut -d= -f2)
fi

RC=0
if [[ -n "$ONLY" ]]; then
  # 3a. 对触发票跑 LLM 深析
  echo "[$DATE] 深析触发: $ONLY $(date +%H:%M)" >> "$LOG"
  $PY -m tradingagents.ashare.run_holdings \
      --tickers "$TICKERS" --rounds 2 --force --only "$ONLY" \
      >> "$LOG" 2>&1
  RC=$?
  echo "[$DATE] 深析结束 rc=$RC $(date +%H:%M)" >> "$LOG"
else
  echo "[$DATE] 无触发 → 跳过 LLM 深析（沿用上次决策）" >> "$LOG"
fi

# 3b. 组合层每日必跑（价格/信号动态权重，秒级）
$PY -m tradingagents.ashare.daily_flow --portfolio --date "$DATE" --tickers "$TICKERS" \
    > "logs/portfolio-${DATE}.md" 2>>"$LOG"
echo "[$DATE] 组合层已更新 → logs/portfolio-${DATE}.md" >> "$LOG"

# 4. 每日决策单：合并 调仓单 + 当日深析信号 + 观察池触发 → 每天必推
#    （用户要求：天天推，但要有策略/调仓单/理由）
SIGNALS=$($PY -m tradingagents.ashare.make_daily_card --signals "$DATE" 2>/dev/null)
WL_OUT=$($PY -m tradingagents.ashare.watchlist_check --date "$DATE" 2>/dev/null)

{
  cat "logs/portfolio-${DATE}.md"
  if [[ -n "$SIGNALS" ]]; then
    echo ""
    echo "## 今日深析新信号"
    echo "$SIGNALS"
  fi
  if [[ "$WL_OUT" != *"安静"* && -n "$WL_OUT" ]]; then
    echo ""
    echo "$WL_OUT"
    echo "$WL_OUT" > "logs/watchlist-${DATE}.md" 2>/dev/null || true
  fi
} > /tmp/ashare-daily.md
cp /tmp/ashare-daily.md "logs/daily-${DATE}.md"
echo "[$DATE] 每日决策单生成（信号:$([ -n "$SIGNALS" ] && echo 有 || echo 无)，观察池:$([ "$WL_OUT" != *"安静"* ] && echo 触发 || echo 安静)）" >> "$LOG"

# 5. 推送每日决策单
if [[ -s /tmp/ashare-daily.md ]]; then
  scp -i "$KEY" -q /tmp/ashare-daily.md ubuntu@124.221.95.205:/tmp/ashare-daily.md 2>>"$LOG"
  ssh -i "$KEY" ubuntu@124.221.95.205 "cd /home/ubuntu/TradingAgents && set -a && source .env && set +a && \
    BIN=\$HOME/.local/share/pnpm/global/5/.pnpm/openclaw@2026.7.1-2/node_modules/openclaw/dist/index.js && \
    NODE=\$HOME/.nvm/versions/node/v22.23.2/bin/node && \
    \$NODE \$BIN message send --channel feishu --account \"\$FEISHU_ACCOUNT\" --target \"\$FEISHU_TARGET\" -m \"\$(cat /tmp/ashare-daily.md)\"" >>"$LOG" 2>&1 \
    && echo "[$DATE] 已发每日决策单" >> "$LOG" \
    || echo "[$DATE] 决策单发送失败（在 logs/daily-${DATE}.md）" >> "$LOG"
fi
exit $RC
