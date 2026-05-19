#!/bin/bash
# ============================================================
# APFEE — Deploy Script
# Run on your EC2 instance to pull latest code and restart
# Usage: bash deploy.sh
# ============================================================

set -e

echo ""
echo "========================================"
echo "  APFEE Deploy"
echo "========================================"

cd /home/ubuntu/apfee

echo "[1/4] Pulling latest code..."
git pull origin main

echo "[2/4] Activating virtual environment..."
source venv/bin/activate

echo "[3/4] Installing any new dependencies..."
pip install -q -r requirements.txt

echo "[4/4] Restarting service..."
sudo systemctl restart apfee

sleep 2
sudo systemctl status apfee --no-pager

echo ""
echo "========================================"
echo "  Deploy complete!"
echo "  View logs: sudo journalctl -u apfee -f"
echo "========================================"
