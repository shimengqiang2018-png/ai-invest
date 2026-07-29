#!/bin/bash
# 网格监测脚本：一键检查所有持仓 ETF 的趋势状态
# 用法: bash tools/grid_monitor.sh

ETFS=(159915 512880 513180 512690 512010 510300 159920)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "  网格趋势监测 — $(date '+%Y-%m-%d %H:%M')"
echo "=========================================="
echo ""

STOP_LIST=""
CAUTION_LIST=""
OK_LIST=""

for etf in "${ETFS[@]}"; do
    RESULT=$(python "$SCRIPT_DIR/grid_trading.py" trend "$etf" 2>&1)
    SCORE=$(echo "$RESULT" | grep "综合评分:" | grep -oE '\-?[0-9]+')
    STATUS=$(echo "$RESULT" | grep "综合评分:" | sed 's/.*→//' | xargs)
    
    if [ "$SCORE" -le -4 ] 2>/dev/null; then
        ICON="⛔"
        STOP_LIST="$STOP_LIST  $ICON $etf: TrendScore=$SCORE → $STATUS\n"
    elif [ "$SCORE" -le -2 ] 2>/dev/null; then
        ICON="🔴"
        CAUTION_LIST="$CAUTION_LIST  $ICON $etf: TrendScore=$SCORE → $STATUS\n"
    elif [ "$SCORE" -le 0 ] 2>/dev/null; then
        ICON="🟡"
        CAUTION_LIST="$CAUTION_LIST  $ICON $etf: TrendScore=$SCORE → $STATUS\n"
    else
        ICON="✅"
        OK_LIST="$OK_LIST  $ICON $etf: TrendScore=$SCORE → $STATUS\n"
    fi
done

echo "⛔ 暂停买入:"
printf "%b" "$STOP_LIST"
echo ""
echo "🔴/🟡 谨慎运行:"
printf "%b" "$CAUTION_LIST"
echo ""
echo "✅ 正常运行:"
printf "%b" "$OK_LIST"
echo ""
echo "=========================================="
echo "规则: TrendScore ≤ -4 → 暂停买入 | ≥ 0 → 正常"
echo "=========================================="
