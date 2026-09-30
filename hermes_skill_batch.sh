#!/bin/bash
# Installs the trading + computer-handling skill batch into Hermes.
# Waits for the GitHub API rate-limit reset first (unauthenticated = 60/hr).
export PATH="/home/nagendra/.local/bin:/home/nagendra/.hermes/node/bin:$PATH"
export HOME=/home/nagendra

# If a GITHUB_TOKEN exists in ~/.hermes/.env (uncommented), export it for 5000/hr
TOK=$(grep -E '^GITHUB_TOKEN=' ~/.hermes/.env | cut -d= -f2- | tr -d '"')
[ -n "$TOK" ] && export GITHUB_TOKEN="$TOK"

# wait until at least 1 request is available (max 45 min)
for i in $(seq 1 90); do
  REMAIN=$(curl -s https://api.github.com/rate_limit | python3 -c "
import json,sys
try: print(json.load(sys.stdin)['resources']['core']['remaining'])
except Exception: print(-1)")
  [ "$REMAIN" = "-1" ] || [ "$REMAIN" -gt 2 ] && break
  echo "[$(date +%H:%M)] rate limit still 0 — waiting 30s"
  sleep 30
done
echo "[$(date +%H:%M)] proceeding (remaining=$REMAIN)"

SKILLS=(
  "skills-sh/binance/binance-skills-hub/binance"
  "skills-sh/binance/binance-skills-hub/binance-trading-signal"
  "skills-sh/binance/binance-skills-hub/binance-agentic-wallet"
  "skills-sh/hypier/tradingview-quantitative-skills/tradingview-quantitative"
  "skills-sh/himself65/finance-skills/tradingview-reader"
  "skills-sh/marketcalls/vectorbt-backtesting-skills/backtest"
  "skills-sh/tradermonty/claude-trading-skills/backtest-expert"
  "skills-sh/wshobson/agents/backtesting-frameworks"
  "skills-sh/gracefullight/stock-checker/backtesting-trading-strategies"
  "official/finance/stocks"
  "official/blockchain/hyperliquid"
  "official/blockchain/evm"
  "official/blockchain/solana"
  "browse-sh/browserbase.com/browser"
  "browse-sh/browserbase.com/browser-trace"
)

OK=0; FAIL=0
for S in "${SKILLS[@]}"; do
  echo "=== installing: $S"
  if hermes skills install "$S" --yes >/tmp/skill_install_last.log 2>&1; then
    OK=$((OK+1)); echo "    OK"
  else
    FAIL=$((FAIL+1)); echo "    FAILED:"; tail -2 /tmp/skill_install_last.log | sed 's/^/    /'
  fi
  sleep 8   # stay well under the rate budget
done

echo "[$(date +%H:%M)] BATCH DONE: ok=$OK failed=$FAIL"
hermes skills list > /home/nagendra/.hermes/skills_installed.txt 2>&1
