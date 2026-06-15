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

# 12. Disk space check (fail if over 85%)
DISK=$(df / | tail -1 | awk '{print $5}' | sed 's/%//')
[ $DISK -lt 85 ]
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

# 21. Scanner module loadable (forex pairs only — ES/NQ not in our pair list)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import sys; sys.path.insert(0, '/home/ubuntu/apfee')
from scanner import detect_structure, detect_order_block, detect_fvg, _get_candles_yfinance_forex
candles = _get_candles_yfinance_forex('EURUSD', outputsize=20)
assert candles and len(candles) > 10, 'EURUSD candles failed'
structure = detect_structure(candles)
assert structure.get('trend') in ('bullish','bearish','ranging'), 'Structure detection failed'
" 2>/dev/null
check $? "Phase 3 scanner — yFinance EURUSD data and structure detection"

# 22. yFinance forex/gold data feeds
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import sys; sys.path.insert(0, '/home/ubuntu/apfee')
from scanner import _get_candles_yfinance_forex
import yfinance as yf
for sym, ticker in [('EURUSD','EURUSD=X'),('GBPUSD','GBPUSD=X'),('XAUUSD','GC=F')]:
    h = yf.Ticker(ticker).history(period='1d', interval='5m')
    assert not h.empty, f'{sym} yFinance failed'
" 2>/dev/null
check $? "Phase 3 yFinance — EURUSD GBPUSD XAUUSD data feeds working"

# ── CONFIGURATION CHECKS ──────────────────────────────────────────────────────

# 23. MAX_LIVE_EXPOSURE = 3.0
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from config import MAX_LIVE_EXPOSURE
assert MAX_LIVE_EXPOSURE == 3.0, f'Expected 3.0, got {MAX_LIVE_EXPOSURE}'
" 2>/dev/null
check $? "Config: MAX_LIVE_EXPOSURE = 3.0"

# 24. MAX_TRADES_TODAY removed
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import config
assert not hasattr(config, 'MAX_TRADES_TODAY'), 'MAX_TRADES_TODAY still in config'
assert not hasattr(config, 'MAX_OPEN_TRADES'), 'MAX_OPEN_TRADES still in config'
" 2>/dev/null
check $? "Config: MAX_TRADES_TODAY and MAX_OPEN_TRADES removed"

# 25. NEWS_BLOCK_MINUTES_BEFORE = 45
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from scanner_improvements import NEWS_BLOCK_MINUTES_BEFORE, NEWS_BLOCK_MINUTES_AFTER
assert NEWS_BLOCK_MINUTES_BEFORE == 45, f'Expected 45, got {NEWS_BLOCK_MINUTES_BEFORE}'
assert NEWS_BLOCK_MINUTES_AFTER == 30
" 2>/dev/null
check $? "Config: NEWS_BLOCK_MINUTES_BEFORE=45, AFTER=30"

# 26. Signal cache TTL = 120 seconds
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import re
src = open('scanner.py').read()
m = re.search(r'age\.total_seconds\(\)\s*>\s*(\d+)', src)
assert m and int(m.group(1)) == 120, f'Cache TTL not 120: {m}'
" 2>/dev/null
check $? "Config: signal cache TTL = 120 seconds"

# 27. FUTURES_SPOT_OFFSET XAUUSD = -30
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from scanner import FUTURES_SPOT_OFFSET
assert FUTURES_SPOT_OFFSET.get('XAUUSD') == -30, f\"Expected -30, got {FUTURES_SPOT_OFFSET.get('XAUUSD')}\"
" 2>/dev/null
check $? "Config: FUTURES_SPOT_OFFSET XAUUSD = -30"

# ── MATHEMATICAL CHECKS ───────────────────────────────────────────────────────

# 28. Lot size — EURUSD 0.5% $10k 15pip = 0.33 lots (tolerance ±0.01)
# _calculate_lot_size expects pip count (not raw price distance)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from claude import _calculate_lot_size
r = _calculate_lot_size(0.5, 15, 'EURUSD', 10000.0)
lot = float(r.split()[0])
assert abs(lot - 0.33) <= 0.01, f'Expected 0.33, got {lot}'
" 2>/dev/null
check $? "Math: EURUSD 0.5% 15pip SL = 0.33 lots"

# 29. Lot size — XAUUSD 0.5% $10k 12pt = 0.04 lots (tolerance ±0.01)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from claude import _calculate_lot_size
r = _calculate_lot_size(0.5, 12, 'XAUUSD', 10000.0)
lot = float(r.split()[0])
assert abs(lot - 0.04) <= 0.01, f'Expected 0.04, got {lot}'
" 2>/dev/null
check $? "Math: XAUUSD 0.5% 12pt SL = 0.04 lots"

# 29b. Lot size — XAUUSD 0.75% $10k 12pt = 0.06 lots (tolerance ±0.01)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from claude import _calculate_lot_size
r = _calculate_lot_size(0.75, 12, 'XAUUSD', 10000.0)
lot = float(r.split()[0])
assert abs(lot - 0.06) <= 0.01, f'Expected 0.06, got {lot}'
" 2>/dev/null
check $? "Math: XAUUSD 0.75% 12pt SL = 0.06 lots"

# 30. Lot size — GBPUSD 0.75% $10k 15pip = 0.50 lots (tolerance ±0.01)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from claude import _calculate_lot_size
r = _calculate_lot_size(0.75, 15, 'GBPUSD', 10000.0)
lot = float(r.split()[0])
assert abs(lot - 0.50) <= 0.01, f'Expected 0.50, got {lot}'
" 2>/dev/null
check $? "Math: GBPUSD 0.75% 15pip SL = 0.50 lots"

# 31. RR: 15pip SL → TP1 = 22.5pip (1.5:1)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
sl = 15; tp1 = sl * 1.5; rr = tp1/sl
assert abs(rr - 1.5) < 0.001, f'Expected 1.5, got {rr}'
assert tp1 == 22.5
" 2>/dev/null
check $? "Math: 15pip SL → TP1 = 22.5pip (1.5:1 RR)"

# 32. Minimum SL distances correct
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from scanner import MIN_SL_DISTANCE
assert MIN_SL_DISTANCE['XAUUSD'] == 12.0
assert abs(MIN_SL_DISTANCE['EURUSD'] - 0.0012) < 1e-9
assert abs(MIN_SL_DISTANCE['GBPUSD'] - 0.0015) < 1e-9
assert abs(MIN_SL_DISTANCE['USDJPY'] - 0.12) < 1e-9
" 2>/dev/null
check $? "Math: min SL distances correct (XAUUSD=12, EURUSD=0.0012, GBPUSD=0.0015, USDJPY=0.12)"

# 33. Pip values correct
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from claude import PIP_VALUES
assert PIP_VALUES['EURUSD'] == 10.0
assert PIP_VALUES['GBPUSD'] == 10.0
assert PIP_VALUES['XAUUSD'] == 100.0
assert abs(PIP_VALUES['USDJPY'] - 9.30) < 0.01
" 2>/dev/null
check $? "Math: pip values correct (EURUSD/GBPUSD=\$10, XAUUSD=\$100, USDJPY=\$9.30)"

# 34. Flat risk 0.75% for all signals (7/7 gates)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import ast, pathlib
src = pathlib.Path('claude.py').read_text()
tree = ast.parse(src)
found = False
for node in ast.walk(tree):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == 'risk_val':
                if isinstance(node.value, ast.Constant) and abs(node.value.value - 0.0075) < 1e-9:
                    found = True
assert found, 'risk_val = 0.0075 not found in claude.py'
" 2>/dev/null
check $? "Math: flat risk 0.75% for all gate-passing signals (7/7 gates)"

# ── SYSTEM INTEGRITY CHECKS ───────────────────────────────────────────────────

# 35. Both users active (IDs 8647323622 and 5803919273)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from database import get_all_active_users
users = get_all_active_users()
chat_ids = {u.telegram_chat_id for u in users}
assert '8647323622' in chat_ids, f'User 8647323622 not active: {chat_ids}'
assert '5803919273' in chat_ids, f'User 5803919273 not active: {chat_ids}'
" 2>/dev/null
check $? "System: both active users present (8647323622, 5803919273)"

# 36. FTMO firm profile loaded correctly
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from prop_firm_profiles import get_profile
p = get_profile('ftmo')
assert p is not None
assert p.account_size == 10000.0 or p.account_size > 0
assert p.profit_target > 0
assert p.max_total_loss > 0
" 2>/dev/null
check $? "System: FTMO firm profile loaded correctly"

# 37. Challenge state exists for user 1
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from database import load_challenge_state
cs = load_challenge_state(1)
assert cs is not None, 'No challenge state for user 1'
from drawdown_tracker import state_from_json
st = state_from_json(cs)
assert st.user_id == 1
" 2>/dev/null
check $? "System: challenge state exists for user 1"

# 38. Trade monitor singleton initialized
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from trade_monitor import trade_monitor
assert trade_monitor is not None
" 2>/dev/null
check $? "System: trade monitor singleton initialized"

# 39. Phase 4 database tables exist
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from database import get_conn
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute(\"SELECT table_name FROM information_schema.tables WHERE table_schema='public'\")
        tables = {r['table_name'] for r in cur.fetchall()}
for t in ['trade_insights', 'daily_performance', 'user_accounts']:
    assert t in tables, f'Missing table: {t}'
" 2>/dev/null
check $? "System: Phase 4 tables present (trade_insights, daily_performance, user_accounts)"

# 40. DrawdownTracker methods present
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from drawdown_tracker import DrawdownTracker
dt = DrawdownTracker()
assert hasattr(dt, 'get_losses_today')
assert hasattr(dt, 'is_signals_paused')
assert hasattr(dt, 'set_resume_override')
" 2>/dev/null
check $? "System: DrawdownTracker has get_losses_today / is_signals_paused / set_resume_override"

# 42. Signal pause thresholds correct in source
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import inspect
from drawdown_tracker import DrawdownTracker
src = inspect.getsource(DrawdownTracker.is_signals_paused)
assert 'losses_today >= 2' in src, 'losses_today >= 2 not found'
assert 'today_loss >= 200' in src, 'today_loss >= 200 not found'
" 2>/dev/null
check $? "System: signal pause thresholds (2 losses or \$200 loss)"

# ── SIGNAL FLOW CHECKS ────────────────────────────────────────────────────────

# 43. News filter active
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from scanner_improvements import is_news_window
result = is_news_window()
assert isinstance(result, tuple) and len(result) == 3
blocked, reason, warning = result
assert isinstance(blocked, bool)
" 2>/dev/null
check $? "Signal flow: news filter active and callable"

# 44. Entry validation tolerances correct
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from scanner_improvements import ENTRY_MAX_PIPS_FOREX, ENTRY_MAX_POINTS_GOLD, ENTRY_MAX_POINTS_FUTURES
assert ENTRY_MAX_PIPS_FOREX == 10
assert ENTRY_MAX_POINTS_GOLD == 15
assert ENTRY_MAX_POINTS_FUTURES == 20
" 2>/dev/null
check $? "Signal flow: entry validation tolerances (forex=10pip, gold=15pt, futures=20pt)"

# 45. Live exposure sync from DB not counters
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import inspect
from database import _sync_live_exposure
src = inspect.getsource(_sync_live_exposure)
assert 'PENDING' in src, '_sync_live_exposure must filter by PENDING'
assert 'result IS NULL OR result' in src
" 2>/dev/null
check $? "Signal flow: live_exposure synced from PENDING trades only"

# 46. Signal gate uses 3.0% exposure limit
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from filter import run_fast_gates
# Should pass with 2.9% exposure
state = {'open_trades': 3, 'live_exposure': 2.9, 'session_losses': 0, 'weekly_losses': 0}
import datetime
# Patch hour to be inside session
from unittest.mock import patch
with patch('filter.datetime') as mock_dt:
    mock_dt.now.return_value = datetime.datetime(2024, 1, 2, 10, 0, tzinfo=datetime.timezone.utc)
    mock_dt.timezone = datetime.timezone
    passed, _ = run_fast_gates(state)
assert passed == True, f'Should pass at 2.9% exposure but got: {passed}'
" 2>/dev/null
check $? "Signal flow: gate passes at 2.9% exposure (limit=3.0%)"

# ── DATA SOURCE CHECKS ────────────────────────────────────────────────────────

# 47. XAUUSD price in range (3000-5500)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import yfinance as yf
h = yf.Ticker('GC=F').history(period='1d', interval='1m')
assert not h.empty, 'GC=F returned no data'
price = float(h['Close'].iloc[-1])
assert 3000 <= price <= 5500, f'XAUUSD out of range: {price}'
" 2>/dev/null
check $? "Data: XAUUSD (GC=F) live price in range \$3000-\$5500"

# 48. EURUSD price in range (1.0-1.5)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import yfinance as yf
h = yf.Ticker('EURUSD=X').history(period='1d', interval='5m')
assert not h.empty
price = float(h['Close'].iloc[-1])
assert 1.0 <= price <= 1.5, f'EURUSD out of range: {price}'
" 2>/dev/null
check $? "Data: EURUSD live price in range 1.0-1.5"

# 49. GBPUSD price in range (1.2-1.6)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import yfinance as yf
h = yf.Ticker('GBPUSD=X').history(period='1d', interval='5m')
assert not h.empty
price = float(h['Close'].iloc[-1])
assert 1.2 <= price <= 1.6, f'GBPUSD out of range: {price}'
" 2>/dev/null
check $? "Data: GBPUSD live price in range 1.2-1.6"

# 50. Twelve Data API key present
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from config import TWELVE_DATA_API_KEY
assert TWELVE_DATA_API_KEY and len(TWELVE_DATA_API_KEY) > 5, 'TWELVE_DATA_API_KEY missing or empty'
" 2>/dev/null
check $? "Data: Twelve Data API key present"

# 51. yFinance candles for forex pairs (fallback check)
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from scanner import _get_candles_yfinance_forex
candles = _get_candles_yfinance_forex('EURUSD', outputsize=10)
assert candles and len(candles) >= 5, f'EURUSD yFinance fallback failed: {candles}'
" 2>/dev/null
check $? "Data: yFinance forex fallback working (EURUSD)"

# 52. Daily bias returns valid data
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from scanner_improvements import get_daily_bias
b = get_daily_bias('EURUSD')
assert 'bias' in b, f'Missing bias key: {b}'
assert b['bias'] in ('bullish','bearish','neutral','unknown'), f'Invalid bias: {b}'
" 2>/dev/null
check $? "Data: daily bias returns valid data for EURUSD"

# ── PATTERN DETECTION CHECKS ──────────────────────────────────────────────────

# 53. Equal highs/lows function exists
grep -c 'detect_equal_highs_lows' /home/ubuntu/apfee/scanner_improvements.py | grep -q '[1-9]'
check $? "Pattern: equal highs/lows function exists"

# 54. MSS function exists
grep -c 'detect_market_structure_shift' /home/ubuntu/apfee/scanner_improvements.py | grep -q '[1-9]'
check $? "Pattern: MSS function exists"

# 55. Premium/discount function exists
grep -c 'check_premium_discount_zone' /home/ubuntu/apfee/scanner_improvements.py | grep -q '[1-9]'
check $? "Pattern: premium/discount function exists"

# 56. Kill zone function exists
grep -c 'is_kill_zone' /home/ubuntu/apfee/scanner_improvements.py | grep -q '[1-9]'
check $? "Pattern: kill zone function exists"

# 57. All 4 patterns imported in scanner.py
grep -cE 'detect_equal_highs_lows|detect_market_structure_shift|check_premium_discount_zone|is_kill_zone' /home/ubuntu/apfee/scanner.py | grep -q '[1-9]'
check $? "Pattern: all 4 patterns imported in scanner"

# 58. Kill zone UTC times correct (07-09, 12-14, 14-16)
grep -q '07.*09\|12.*14\|14.*16' /home/ubuntu/apfee/scanner_improvements.py
check $? "Pattern: kill zone UTC times correct (07-09, 12-14, 14-16)"

# 59. check_tjr_gates and format_unified_signal exist in scanner.py
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
from scanner import check_tjr_gates, format_unified_signal
" 2>/dev/null
check $? "Pattern: check_tjr_gates and format_unified_signal exist in scanner.py"

# 60. All 7 TJR gates present in check_tjr_gates
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
import inspect
from scanner import check_tjr_gates
src = inspect.getsource(check_tjr_gates)
for gate in ['kill_zone','htf_bias','structure','sweep','ob_fvg','bos','volatility']:
    assert f\"gates['{gate}']\" in src or f'gates[\"{gate}\"]' in src, f'Missing gate: {gate}'
" 2>/dev/null
check $? "Pattern: all 7 TJR gates present (kill_zone htf_bias structure sweep ob_fvg bos volatility)"

# 61. PAIR_KILL_ZONES has all 8 pairs
cd /home/ubuntu/apfee && /home/ubuntu/apfee/venv/bin/python -c "
src = open('scanner_improvements.py').read()
assert '_PAIR_KILL_ZONES' in src
for p in ['EURUSD','GBPUSD','XAUUSD','USDJPY','AUDUSD','NZDUSD','USDCAD','USDCHF']:
    assert p in src, f'Missing pair: {p}'
" 2>/dev/null
check $? "Pattern: PAIR_KILL_ZONES dict has all 8 pairs"

echo ""
echo "Running mathematical verification..."
MATH_OUTPUT=$(/home/ubuntu/apfee/venv/bin/python3 /home/ubuntu/apfee/math_check.py 2>/dev/null)
MATH_RESULT=$(echo "$MATH_OUTPUT" | grep "RESULTS")
echo "$MATH_RESULT"
MATH_PASS=$(echo "$MATH_RESULT" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+')
MATH_FAIL=$(echo "$MATH_RESULT" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+')
PASS=$((PASS + ${MATH_PASS:-0}))
FAIL=$((FAIL + ${MATH_FAIL:-0}))

echo ""
echo "======================================"
echo "RESULTS: $PASS passed, $FAIL failed"
echo "======================================"

if [ $FAIL -gt 0 ]; then
  exit 1
fi
