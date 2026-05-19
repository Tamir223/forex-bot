#!/bin/bash
# ============================================================
# APFEE — AWS EC2 Setup Script
# Run once on a fresh Ubuntu 22.04 t2.micro instance
# Usage: bash aws-setup.sh
# ============================================================

set -e

echo ""
echo "========================================"
echo "  APFEE AWS Setup"
echo "========================================"
echo ""

# System update
echo "[1/8] Updating system..."
sudo apt-get update -qq && sudo apt-get upgrade -y -qq

# Python and dependencies
echo "[2/8] Installing Python and dependencies..."
sudo apt-get install -y -qq python3 python3-pip python3-venv git libpq-dev nginx certbot python3-certbot-nginx

# Clone or pull repo
echo "[3/8] Setting up repo..."
if [ -d "/home/ubuntu/apfee" ]; then
    cd /home/ubuntu/apfee && git pull origin main
else
    git clone https://github.com/YOUR_GITHUB_USERNAME/prop-firm-engine.git /home/ubuntu/apfee
    cd /home/ubuntu/apfee
fi

# Virtual environment
echo "[4/8] Creating Python virtual environment..."
cd /home/ubuntu/apfee
python3 -m venv venv
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

# Env file
echo "[5/8] Setting up environment file..."
if [ ! -f ".env" ]; then
    cp .env.template .env
    echo ""
    echo "  >>> .env file created. Edit it now:"
    echo "  >>> nano /home/ubuntu/apfee/.env"
    echo ""
fi

# Nginx config for Stripe webhook SSL
echo "[6/8] Configuring Nginx..."
sudo tee /etc/nginx/sites-available/apfee > /dev/null << 'NGINX'
server {
    listen 80;
    server_name _;

    location /webhook/stripe {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /health {
        proxy_pass http://127.0.0.1:8000;
    }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/apfee /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl restart nginx

# Systemd service
echo "[7/8] Creating systemd service..."
sudo tee /etc/systemd/system/apfee.service > /dev/null << SERVICE
[Unit]
Description=APFEE Trading Bot
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/apfee
Environment=PATH=/home/ubuntu/apfee/venv/bin
ExecStart=/home/ubuntu/apfee/venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable apfee

# Log rotation
echo "[8/8] Setting up log rotation..."
sudo tee /etc/logrotate.d/apfee > /dev/null << 'LOGROTATE'
/var/log/apfee/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
LOGROTATE

mkdir -p /home/ubuntu/apfee/logs

echo ""
echo "========================================"
echo "  Setup complete!"
echo ""
echo "  Next steps:"
echo "  1. Edit your .env file:"
echo "     nano /home/ubuntu/apfee/.env"
echo ""
echo "  2. Start APFEE:"
echo "     sudo systemctl start apfee"
echo ""
echo "  3. Check it's running:"
echo "     sudo systemctl status apfee"
echo ""
echo "  4. View live logs:"
echo "     sudo journalctl -u apfee -f"
echo "========================================"
