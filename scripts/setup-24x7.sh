#!/bin/bash
# Setup script for 24/7 autonomous Facebook Marketplace posting via hermes-agent
# Usage: bash scripts/setup-24x7.sh [portal_url]

set -euo pipefail

PORTAL_URL="${1:-}"
STATE_FILE="$HOME/.hermes/re-state.json"
SERVICE_FILE="$HOME/.config/systemd/user/hermes-gateway.service"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "=== Hermes 24/7 Setup ==="
echo ""

# 1. Check prerequisites
echo "[1/5] Checking prerequisites..."
for var in ADSPOWER_API_URL ADSPOWER_API_KEY OPENROUTER_API_KEY TELEGRAM_BOT_TOKEN; do
    if grep -q "^${var}=" "$HOME/.hermes/.env" 2>/dev/null; then
        echo "  ✓ $var"
    else
        echo "  ✗ $var — MISSING in ~/.hermes/.env"
        exit 1
    fi
done

# 2. Install systemd service
echo ""
echo "[2/5] Installing systemd service..."
mkdir -p "$HOME/.config/systemd/user"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Hermes Agent Gateway (24/7 messaging + cron scheduler)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${REPO_DIR}
ExecStart=$(which python3) -m gateway.run
Restart=always
RestartSec=10
TimeoutStopSec=300
WatchdogSec=300
EnvironmentFile=${HOME}/.hermes/.env

StandardOutput=journal
StandardError=journal
SyslogIdentifier=hermes-gateway

[Install]
WantedBy=default.target
EOF
echo "  ✓ Service file written to $SERVICE_FILE"

# 3. Enable and start the service
echo ""
echo "[3/5] Enabling systemd service..."
systemctl --user daemon-reload
systemctl --user enable hermes-gateway.service
systemctl --user start hermes-gateway.service
echo "  ✓ hermes-gateway.service enabled and started"

# WSL: enable lingering so the service runs without an active login session
if grep -qi microsoft /proc/version 2>/dev/null; then
    echo ""
    echo "  WSL detected — enabling loginctl linger for $USER..."
    loginctl enable-linger "$USER" 2>/dev/null || echo "  ⚠ loginctl enable-linger failed (may need: sudo loginctl enable-linger $USER)"
fi

# 4. Initialize state file
echo ""
echo "[4/5] Initializing state file..."
if [ ! -f "$STATE_FILE" ]; then
    python3 -c "
import json, os
from datetime import datetime
state = {
    'portal_url': '${PORTAL_URL}',
    'extracted_listings': [],
    'posting_queue': [],
    'accounts': ['5', '22', '24', '32', '33', '50'],
    'daily_post_counts': {},
    'last_post_times': {},
    'stats': {'total_extracted': 0, 'total_posted': 0, 'total_failed': 0},
    'created_at': datetime.now().isoformat()
}
with open(os.path.expanduser('~/.hermes/re-state.json'), 'w') as f:
    json.dump(state, f, indent=2)
print('  ✓ Created', os.path.expanduser('~/.hermes/re-state.json'))
"
else
    echo "  ✓ State file already exists: $STATE_FILE"
fi

# 5. Summary
echo ""
echo "[5/5] Setup complete!"
echo ""
echo "=== Status ==="
systemctl --user status hermes-gateway.service --no-pager | head -10
echo ""
echo "=== Next Steps ==="
echo ""
echo "1. Set the portal URL (if not provided):"
echo "   python3 -c \"import json; d=json.load(open('$STATE_FILE')); d['portal_url']='https://realmmlp.ca/YOUR_LINK'; json.dump(d, open('$STATE_FILE','w'), indent=2)\""
echo ""
echo "2. Create the cron jobs via Telegram or CLI:"
echo "   hermes cron create --name 'fb-poster' --schedule 'every 35m' --skill real-estate-assistant --deliver origin --prompt 'Run one cycle of the autonomous posting pipeline. Post the next pending listing. Use 5+ photos.'"
echo "   hermes cron create --name 'fb-extractor' --schedule '0 6 * * *' --skill real-estate-assistant --deliver origin --prompt 'Extract new listings from portal. Download 5+ photos each. Update state.'"
echo ""
echo "3. Monitor:"
echo "   journalctl --user -u hermes-gateway -f          # live logs"
echo "   hermes cron list                                  # see scheduled jobs"
echo "   cat ~/.hermes/re-state.json | python3 -m json.tool  # see posting state"
echo ""
echo "4. Manage:"
echo "   systemctl --user stop hermes-gateway              # stop"
echo "   systemctl --user restart hermes-gateway           # restart"
echo "   hermes cron pause fb-poster                       # pause posting"
