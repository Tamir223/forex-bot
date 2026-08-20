#!/bin/bash
# deploy-web.sh — deploy a website file (HTML/CSS) from the git repo to the live site.
#
# Usage:
#   ./deploy-web.sh index.html
#   ./deploy-web.sh index.html "updated hero section copy"
#
# What it does:
#   1. git add + commit + push the file
#   2. Copy it into /var/www/html (where nginx actually serves the live site from)
#   3. Verify the push landed on origin/main
#   4. Confirm the live site returns it correctly
#
# Only works from inside /home/ubuntu/apfee (the repo root).

set -e  # stop immediately if any step fails, rather than continuing on a broken state

FILE="$1"
MSG="${2:-Update $1}"   # use a default commit message if none given

if [ -z "$FILE" ]; then
    echo "Usage: ./deploy-web.sh <filename> [\"commit message\"]"
    echo "Example: ./deploy-web.sh index.html \"fix hero headline\""
    exit 1
fi

if [ ! -f "$FILE" ]; then
    echo "Error: $FILE not found in $(pwd)"
    echo "Run this from /home/ubuntu/apfee, and check the filename."
    exit 1
fi

echo "=== 1. Committing and pushing $FILE ==="
git add "$FILE"
if git diff --cached --quiet; then
    echo "No changes to commit — $FILE is identical to the last commit. Skipping git steps."
else
    git commit -m "$MSG"
    git push origin main
fi

echo ""
echo "=== 2. Confirming the push landed on origin/main ==="
git log --oneline -1

echo ""
echo "=== 3. Deploying $FILE to the live webroot ==="
sudo rm -f "/var/www/html/$FILE"
sudo cp "/home/ubuntu/apfee/$FILE" "/var/www/html/$FILE"
sudo chown www-data:www-data "/var/www/html/$FILE"
echo "Copied to /var/www/html/$FILE"

echo ""
echo "=== 4. Verifying against the live domain ==="
if [ "$FILE" = "index.html" ]; then
    URL="https://tnltrader.com"
elif [ "$FILE" = "privacy.html" ]; then
    URL="https://tnltrader.com/privacy"
elif [ "$FILE" = "terms.html" ]; then
    URL="https://tnltrader.com/terms"
else
    URL="https://tnltrader.com/$FILE"
fi

LOCAL_SIZE=$(wc -c < "$FILE")
LIVE_SIZE=$(curl -sL "$URL" | wc -c)

echo "Local file size:  $LOCAL_SIZE bytes"
echo "Live page size:   $LIVE_SIZE bytes"

if [ "$LOCAL_SIZE" = "$LIVE_SIZE" ]; then
    echo ""
    echo "✅ Deployed and verified — $URL matches the file you just edited."
else
    echo ""
    echo "⚠️  Sizes don't match — the live page may not have updated, or there's caching involved."
    echo "   Try a hard-refresh in your browser (Cmd+Shift+R) before assuming something's wrong."
fi
