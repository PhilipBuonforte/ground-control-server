#!/bin/bash
# Ground Control — one-command Mac server installer.
# Sets up the companion server that lets the Ground Control iOS app control
# your Claude Code sessions from anywhere over Tailscale.
set -e

BLUE='\033[0;34m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
say() { echo -e "${BLUE}▸${NC} $1"; }
ok()  { echo -e "${GREEN}✓${NC} $1"; }
warn(){ echo -e "${YELLOW}!${NC} $1"; }
die() { echo -e "${RED}✗ $1${NC}"; exit 1; }

DIR="$( cd "$( dirname "${BASH_SOURCE[0]:-.}" )" && pwd )"
# When run via `curl | bash` there are no local files — fetch them.
if [ ! -f "$DIR/server.py" ]; then
  echo "Downloading Ground Control server…"
  TMP=$(mktemp -d)
  curl -fsSL https://github.com/PhilipBuonforte/ground-control-server/archive/refs/heads/main.tar.gz | tar -xz -C "$TMP" || { echo "download failed"; exit 1; }
  DIR="$TMP/ground-control-server-main"
fi
INSTALL_DIR="$HOME/.ground-control"
LABEL="com.groundcontrol.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo ""
echo "  ╔════════════════════════════════════╗"
echo "  ║   Ground Control — Server Setup     ║"
echo "  ╚════════════════════════════════════╝"
echo ""

# 1. Python
say "Checking for Python 3…"
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v $c)"; break; fi
done
[ -z "$PY" ] && die "Python 3 not found. Install it from https://www.python.org/downloads/ then re-run."
ok "Python: $PY"

# 2. Claude Code
say "Checking for Claude Code…"
if ! command -v claude >/dev/null 2>&1 && [ ! -f "$HOME/.local/bin/claude" ]; then
  warn "Claude Code CLI not found on PATH. The server still installs, but you need Claude Code (claude.ai/code) with some sessions for anything to show up."
else
  ok "Claude Code found"
fi

# 3. Copy files
say "Installing server to $INSTALL_DIR …"
mkdir -p "$INSTALL_DIR"
cp "$DIR/server.py" "$INSTALL_DIR/"
cp -r "$DIR/static" "$INSTALL_DIR/"
cp "$DIR/requirements.txt" "$INSTALL_DIR/"
ok "Files copied"

# 4. Dependencies
say "Installing Python dependencies (this can take a minute)…"
"$PY" -m pip install --user --quiet --break-system-packages -r "$INSTALL_DIR/requirements.txt" 2>/dev/null \
  || "$PY" -m pip install --user --quiet -r "$INSTALL_DIR/requirements.txt" \
  || die "pip install failed. Try: $PY -m pip install -r $INSTALL_DIR/requirements.txt"
ok "Dependencies installed"

# 5. Generate web-push (VAPID) keys if missing
if [ ! -f "$INSTALL_DIR/vapid.json" ]; then
  say "Generating notification keys…"
  "$PY" - << PYEOF
import base64, json
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
priv = ec.generate_private_key(ec.SECP256R1())
pb = priv.private_numbers().private_value.to_bytes(32, 'big')
pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
b = lambda x: base64.urlsafe_b64encode(x).rstrip(b'=').decode()
json.dump({"private_key": b(pb), "public_key": b(pub)}, open("$INSTALL_DIR/vapid.json", "w"))
PYEOF
  ok "Keys generated"
fi

# 6. launchd service (auto-start, restart on crash)
say "Setting up auto-start service…"
cat > "$PLIST" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string>
    <string>$INSTALL_DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key><string>$INSTALL_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$INSTALL_DIR/server.log</string>
  <key>StandardErrorPath</key><string>$INSTALL_DIR/server.err</string>
</dict></plist>
PLISTEOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
sleep 3
if curl -s http://127.0.0.1:8130/api/health | grep -q ok; then
  ok "Server running on port 8130"
else
  warn "Server didn't answer yet — check $INSTALL_DIR/server.err"
fi

# 7. Tailscale
echo ""
say "Setting up secure remote access (Tailscale)…"
TS=""
for c in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
  if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then TS="$c"; break; fi
done
if [ -z "$TS" ]; then
  warn "Tailscale not installed."
  echo "    1. Install it: https://tailscale.com/download"
  echo "    2. Sign in (same account on your Mac + iPhone)"
  echo "    3. Re-run this installer to finish remote access"
else
  ok "Tailscale found"
  "$TS" serve --bg --https=443 http://127.0.0.1:8130 >/dev/null 2>&1 \
    && ok "HTTPS remote access enabled" \
    || warn "Couldn't enable HTTPS. In the Tailscale admin console (login.tailscale.com/admin/dns), turn on 'HTTPS Certificates', then re-run."
  URL="$($TS status --json 2>/dev/null | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print("https://"+d["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
fi

echo ""
echo "  ╔════════════════════════════════════╗"
echo -e "  ║   ${GREEN}Setup complete!${NC}                   ║"
echo "  ╚════════════════════════════════════╝"
echo ""
if [ -n "$URL" ]; then
  echo -e "  Your server address (paste into the app):"
  echo -e "     ${GREEN}$URL${NC}"
else
  echo "  Once Tailscale is signed in, your address will look like:"
  echo "     https://your-mac.your-tailnet.ts.net"
fi
echo ""
echo "  Open Ground Control on your iPhone → enter that address → done."
echo ""
