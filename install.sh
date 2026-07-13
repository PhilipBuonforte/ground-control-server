#!/bin/bash
# Ground Control — one-command Mac server installer.
# Sets up the companion server that lets the Ground Control iOS + Mac apps see
# and control your Claude Code sessions from anywhere over Tailscale.
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
  curl -fsSL https://github.com/PhilipBuonforte/ground-control-server/archive/refs/heads/main.tar.gz | tar -xz -C "$TMP" || die "download failed"
  DIR="$TMP/ground-control-server-main"
fi
INSTALL_DIR="$HOME/.ground-control"
LABEL="com.groundcontrol.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo ""
echo "  ╔════════════════════════════════════╗"
echo "  ║   Ground Control — Server Setup    ║"
echo "  ╚════════════════════════════════════╝"
echo ""

# 1. Python
say "Checking for Python 3…"
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
[ -z "$PY" ] && die "Python 3 not found. Run: xcode-select --install   (or install from python.org), then re-run."
ok "Python: $PY"

# 2. Claude Code
say "Checking for Claude Code…"
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ] || [ -x /opt/homebrew/bin/claude ]; then
  ok "Claude Code found"
else
  warn "Claude Code CLI not found. Install it first — https://claude.ai/code — then re-run."
fi

# 3. Copy files (the full current server: EZ terminal engine + web terminal + hooks)
say "Installing server to $INSTALL_DIR …"
mkdir -p "$INSTALL_DIR"
for f in server.py gc_ez.py gc_ez_engine.py gc_sessions.py run_server.sh requirements.txt; do
  cp "$DIR/$f" "$INSTALL_DIR/"
done
chmod +x "$INSTALL_DIR/run_server.sh"
rm -rf "$INSTALL_DIR/static" "$INSTALL_DIR/ezterminfo"
cp -r "$DIR/static" "$INSTALL_DIR/"
cp -r "$DIR/ezterminfo" "$INSTALL_DIR/"
ok "Files copied"

# 4. Dependencies — into a private venv so we never fight macOS's managed python
say "Installing Python dependencies (first run can take a minute)…"
if [ ! -x "$INSTALL_DIR/venv/bin/python3" ]; then
  "$PY" -m venv "$INSTALL_DIR/venv" || die "couldn't create a Python venv"
fi
"$INSTALL_DIR/venv/bin/python3" -m pip install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/python3" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt" \
  || die "pip install failed. Try: $INSTALL_DIR/venv/bin/python3 -m pip install -r $INSTALL_DIR/requirements.txt"
ok "Dependencies installed"

# 5. Alert hook — how the server knows a session finished / needs you.
say "Installing the Claude Code alert hook…"
mkdir -p "$HOME/.claude/hooks"
cp "$DIR/hooks/pocket-claude-notify.py" "$HOME/.claude/hooks/pocket-claude-notify.py"
"$INSTALL_DIR/venv/bin/python3" - << 'PYEOF'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
try:
    cfg = json.load(open(p))
except (OSError, ValueError):
    cfg = {}
hooks = cfg.setdefault("hooks", {})
CMD = "python3 ~/.claude/hooks/pocket-claude-notify.py"
for event in ("Stop", "Notification", "SubagentStart", "SubagentStop"):
    entries = hooks.setdefault(event, [])
    flat = json.dumps(entries)
    if "pocket-claude-notify" not in flat:
        entries.append({"hooks": [{"type": "command", "command": CMD}]})
json.dump(cfg, open(p, "w"), indent=2)
print("hook registered for Stop / Notification / SubagentStart / SubagentStop")
PYEOF
ok "Alerts wired up"

# 6. launchd service — auto-start on login, restart on crash. run_server.sh owns
#    the port, keeps deps present, and self-heals the Tailscale HTTPS route.
say "Setting up auto-start service…"
cat > "$PLIST" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string>
    <string>$INSTALL_DIR/run_server.sh</string>
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
sleep 4
if curl -s http://127.0.0.1:8130/api/health | grep -q ok; then
  ok "Server running on port 8130"
else
  warn "Server didn't answer yet — check $INSTALL_DIR/server.err"
fi

# 7. Tailscale — the secure tunnel between your phone and this Mac.
echo ""
say "Setting up secure remote access (Tailscale)…"
TS=""
for c in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
  if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then TS="$c"; break; fi
done
URL=""
if [ -z "$TS" ]; then
  warn "Tailscale is not installed yet."
  echo "    1. Install it (free): https://tailscale.com/download  — Mac AND iPhone"
  echo "    2. Sign in with the SAME account on both devices"
  echo "    3. Re-run this installer — it will finish remote access automatically"
elif ! "$TS" status >/dev/null 2>&1; then
  warn "Tailscale is installed but not signed in."
  echo "    Open the Tailscale app, sign in, then re-run this installer."
else
  ok "Tailscale connected"
  "$TS" serve status 2>/dev/null | grep -q 8130 || "$TS" serve --bg 8130 >/dev/null 2>&1 \
    && ok "HTTPS remote access enabled" \
    || warn "Couldn't enable HTTPS. In login.tailscale.com/admin/dns turn on 'HTTPS Certificates', then re-run."
  URL="$("$TS" status --json 2>/dev/null | "$INSTALL_DIR/venv/bin/python3" -c 'import json,sys; d=json.load(sys.stdin); print("https://"+d["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
fi

echo ""
echo "  ╔════════════════════════════════════╗"
echo -e "  ║   ${GREEN}Setup complete!${NC}                  ║"
echo "  ╚════════════════════════════════════╝"
echo ""
if [ -n "$URL" ]; then
  echo -e "  Your server address (paste into the apps):"
  echo -e "     ${GREEN}$URL${NC}"
else
  echo "  Once Tailscale is signed in on this Mac, re-run the installer and it"
  echo "  will print your address (looks like https://your-mac.your-tailnet.ts.net)."
fi
echo ""
echo "  Next steps:"
echo "  1. iPhone app  → TestFlight: TESTFLIGHT_LINK_HERE"
echo "  2. Mac app     → https://github.com/PhilipBuonforte/ground-control-server/releases/latest"
echo "  3. Open either app → paste your server address → done."
echo ""
