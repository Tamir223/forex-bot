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

echo ""
echo "======================================"
echo "RESULTS: $PASS passed, $FAIL failed"
echo "======================================"

if [ $FAIL -gt 0 ]; then
  exit 1
fi
