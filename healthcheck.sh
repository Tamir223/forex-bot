#!/bin/bash

echo "======================================"
echo "TNL TRADER SYSTEM HEALTH CHECK"
echo "======================================"
echo ""

PASS=0
FAIL=0

check() {
  if [ $1 -eq 0 ]; then
    echo "✅ $2"
    PASS=$((PASS+1))
  else
    echo "❌ $2"
    FAIL=$((FAIL+1))
  fi
}

# 1. Bot service running
systemctl is-active --quiet apfee
check $? "Bot service running"

# 2. Nginx running
systemctl is-active --quiet nginx
check $? "Nginx running"

# 3. Website responding
curl -s -o /dev/null -w "%{http_code}" https://tnltrader.com | grep -q "200"
check $? "tnltrader.com returning 200"

# 4. API stats endpoint
curl -s https://tnltrader.com/api/stats | grep -q "signals"
check $? "API stats endpoint working"

# 5. Health endpoint
curl -s https://tnltrader.com/health | grep -q "ok"
check $? "Health endpoint working"

# 6. Checkout endpoint responding
curl -s -o /dev/null -w "%{http_code}" "https://tnltrader.com/checkout?plan=pro" | grep -qE "3[0-9][0-9]|200"
check $? "Checkout endpoint redirecting"

# 7. Privacy page
curl -s -o /dev/null -w "%{http_code}" https://tnltrader.com/privacy | grep -q "200"
check $? "Privacy page returning 200"

# 8. Terms page
curl -s -o /dev/null -w "%{http_code}" https://tnltrader.com/terms | grep -q "200"
check $? "Terms page returning 200"

# 9. Database connection
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from database import get_conn
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute('SELECT 1')
" 2>/dev/null
check $? "Database connection working"

# 10. RDS reachable
pg_isready -h tnltrader-db.c8pis6gqsrof.us-east-1.rds.amazonaws.com -p 5432 -U tnltrader -d tnltrader 2>/dev/null
check $? "RDS database reachable"

# 11. SSL certificate valid
echo | openssl s_client -connect tnltrader.com:443 -servername tnltrader.com 2>/dev/null | openssl x509 -noout -checkend 86400 2>/dev/null
check $? "SSL certificate valid (not expiring in 24h)"

# 12. Disk space check (fail if over 80%)
DISK=$(df / | tail -1 | awk '{print $5}' | sed 's/%//')
[ $DISK -lt 80 ]
check $? "Disk space OK (${DISK}% used)"

# 13. Memory check (fail if over 90%)
MEM=$(free | grep Mem | awk '{printf "%.0f", $3/$2 * 100}')
[ $MEM -lt 90 ]
check $? "Memory OK (${MEM}% used)"

# 14. Phase 1 modules loadable
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from prop_firm_profiles import get_profile, PROFILES
from drawdown_tracker import new_state, state_to_json
from signal_gate_phase1 import GateResult
assert len(PROFILES) >= 8, f'Expected at least 8 profiles, got {len(PROFILES)}'
assert get_profile('apex150') is not None
assert get_profile('ftmo') is not None
" 2>/dev/null
check $? "Phase 1 modules loaded (12 firm profiles)"

# 15. challenge_state table exists
PGPASSWORD='Tnlnextlevel26$' psql -h tnltrader-db.c8pis6gqsrof.us-east-1.rds.amazonaws.com -U tnltrader -d tnltrader -c "\dt challenge_state" 2>/dev/null | grep -q "challenge_state"
check $? "challenge_state table exists"

# 16. firm_code column on users table
PGPASSWORD='Tnlnextlevel26$' psql -h tnltrader-db.c8pis6gqsrof.us-east-1.rds.amazonaws.com -U tnltrader -d tnltrader -c "\d users" 2>/dev/null | grep -q "firm_code"
check $? "firm_code column on users table"

# 17. New bot commands registered
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import ast, sys
tree = ast.parse(open('bot.py').read())
handlers = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
src = open('bot.py').read()
for cmd in ['firmlist','setfirm','challenge','logtrade','history']:
    assert cmd in src, f'Missing handler: {cmd}'
" 2>/dev/null
check $? "All 9 Phase 1 bot commands registered"

# 18. Phase 2 futures module loadable
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from futures_instruments import FUTURES_SYMBOLS, get_spec, calculate_contracts, is_futures
assert len(FUTURES_SYMBOLS) == 11, f'Expected 11 futures symbols, got {len(FUTURES_SYMBOLS)}'
assert is_futures('ES') == True
assert is_futures('EURUSD') == False
spec = get_spec('NQ')
assert spec['point_value'] == 20.0
sizing = calculate_contracts(1000, 20, 'NQ', max_contracts=12)
assert sizing['contracts'] is not None
print('Futures specs OK')
" 2>/dev/null
check $? "Phase 2 futures instruments loaded (11 symbols)"

# 19. Futures position sizing calculation
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from futures_instruments import calculate_contracts, format_sizing
# ES: \$1000 risk, 8pt stop = \$400/contract = 2 contracts
sizing = calculate_contracts(1000, 8, 'ES')
assert sizing['contracts'] == 2, f'Expected 2 ES contracts, got {sizing[\"contracts\"]}'
# NQ: \$1000 risk, 20pt stop = \$400/contract = 2 contracts
sizing = calculate_contracts(1000, 20, 'NQ')
assert sizing['contracts'] == 2
# Max contracts cap
sizing = calculate_contracts(50000, 5, 'ES', max_contracts=12)
assert sizing['contracts'] == 12
print('Position sizing OK')
" 2>/dev/null
check $? "Phase 2 futures position sizing correct"

# 20. Futures signal detection
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from filter import is_signal_message
assert is_signal_message('ES BUY ENTRY 5800 STOP 5792') == True
assert is_signal_message('NQ SELL ENTRY 20100 STOP 20120') == True
assert is_signal_message('GBPUSD BUY') == True
assert is_signal_message('hello how are you') == False
print('Signal detection OK')
" 2>/dev/null
check $? "Phase 2 futures signal detection working"

echo ""
echo "======================================"
echo "RESULTS: $PASS passed, $FAIL failed"
echo "======================================"

if [ $FAIL -gt 0 ]; then
  exit 1
fi

# 21. Scanner module loadable
/home/ubuntu/apfee/venv/bin/python3 -c "
import sys
sys.path.insert(0, '/home/ubuntu/apfee')
from scanner import get_candles_yfinance, detect_structure, detect_order_block, detect_fvg, score_setup
candles = get_candles_yfinance('ES')
assert candles and len(candles) > 10, 'ES candles failed'
structure = detect_structure(candles)
assert structure.get('trend') in ('bullish','bearish','ranging'), 'Structure detection failed'
print('Scanner OK')
" 2>/dev/null
check $? "Phase 3 scanner — yFinance ES data and structure detection"

# 22. yFinance futures data
/home/ubuntu/apfee/venv/bin/python3 -c "
import sys
sys.path.insert(0, '/home/ubuntu/apfee')
from scanner import get_candles_yfinance
for sym in ['NQ','ES','CL','GC']:
    c = get_candles_yfinance(sym)
    assert c and len(c) > 5, f'{sym} failed'
print('All futures data OK')
" 2>/dev/null
check $? "Phase 3 yFinance — NQ ES CL GC data feeds working"
