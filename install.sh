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
# PreToolUse(AskUserQuestion) → exact wizard-question JSON for the chat card
pre = hooks.setdefault("PreToolUse", [])
if "pocket-claude-notify" not in json.dumps(pre):
    pre.append({"matcher": "AskUserQuestion",
                "hooks": [{"type": "command", "command": CMD}]})
json.dump(cfg, open(p, "w"), indent=2)
print("hook registered for Stop / Notification / Subagent / PreToolUse(AskUserQuestion)")
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

# ── Mac app: install/refresh from the latest release ─────────────────────────
# One command does everything: server AND app, no manual download, and the
# quarantine strip means macOS never shows the "could not verify" block.
echo ""
echo "→ Installing the Ground Control Mac app…"
APP_TMP="$(mktemp -d)"
if curl -fsSL -o "$APP_TMP/app.zip" \
     "https://github.com/PhilipBuonforte/ground-control-server/releases/latest/download/GroundControl-mac.zip" 2>/dev/null; then
  ditto -x -k "$APP_TMP/app.zip" "$APP_TMP" 2>/dev/null || true
  if [ -d "$APP_TMP/Ground Control.app" ]; then
    osascript -e 'tell application "Ground Control" to quit' >/dev/null 2>&1 || true
    sleep 1
    rm -rf "/Applications/Ground Control.app"
    cp -R "$APP_TMP/Ground Control.app" "/Applications/Ground Control.app"
    xattr -dr com.apple.quarantine "/Applications/Ground Control.app" 2>/dev/null || true
    open "/Applications/Ground Control.app" || true
    ok "Mac app installed to /Applications (launched)"
  else
    warn "Downloaded app looked wrong — grab it manually from the releases page."
  fi
else
  warn "Couldn't download the Mac app — grab it from the releases page."
fi
rm -rf "$APP_TMP"

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
# ---- SELF-TEST -------------------------------------------------------------
# Actually create a session and confirm it lives, then remove it. Without this the
# installer printed "Setup complete!" on a machine where sessions could never start,
# and the user found out hours later staring at an empty app with no error anywhere
# (2026-09-17). An installer that cannot prove it works has not finished.
# Text Assistant send scripts. The server shells out to
# ~/.claude/skills/send-text/send_imessage.sh to reply to a message; without it the
# Text Assistant can READ threads but every send silently fails (it returns false and
# says nothing). That path is not optional — it is what server.py looks for.
if [ -d "$DIR/messaging" ]; then
  mkdir -p "$HOME/.claude/skills/send-text"
  cp "$DIR/messaging/send_imessage.sh" "$HOME/.claude/skills/send-text/send_imessage.sh"
  cp "$DIR/messaging/_verify_send.py"  "$HOME/.claude/skills/send-text/_verify_send.py"
  chmod +x "$HOME/.claude/skills/send-text/send_imessage.sh" \
           "$HOME/.claude/skills/send-text/_verify_send.py"
  ok "Text Assistant: send scripts installed"
fi

# Reading the Messages database needs Full Disk Access for the PYTHON BINARY that
# runs the server — a manual step no installer can perform. Tell the user plainly,
# because the failure mode is an inbox that looks empty rather than an error.
if [ -x "$INSTALL_DIR/venv/bin/python3" ]; then
  if "$INSTALL_DIR/venv/bin/python3" - <<'PYFDA' 2>/dev/null
import sqlite3, os, sys
db = os.path.expanduser("~/Library/Messages/chat.db")
try:
    sqlite3.connect("file:%s?mode=ro" % db, uri=True).execute("select 1 from message limit 1")
except Exception:
    sys.exit(1)
PYFDA
  then
    ok "Text Assistant: can read your Messages"
  else
    warn "Text Assistant: CANNOT read your Messages yet (inbox will look empty)"
    echo "     Grant Full Disk Access to this exact binary, then re-run:"
    echo "       $(readlink -f "$INSTALL_DIR/venv/bin/python3" 2>/dev/null || echo "$INSTALL_DIR/venv/bin/python3")"
    echo "     System Settings → Privacy & Security → Full Disk Access → + → press"
    echo "     Cmd+Shift+G and paste that path."
  fi
fi

# gc-doctor: one command that dumps everything a helper needs. Installed next to the
# server and symlinked onto PATH when we can, so "run gc-doctor and paste it" replaces
# an evening of back-and-forth.
if [ -f "$DIR/gc-doctor" ]; then
  cp "$DIR/gc-doctor" "$INSTALL_DIR/gc-doctor" && chmod +x "$INSTALL_DIR/gc-doctor"
  if [ -w /usr/local/bin ] || mkdir -p /usr/local/bin 2>/dev/null; then
    ln -sf "$INSTALL_DIR/gc-doctor" /usr/local/bin/gc-doctor 2>/dev/null \
      && ok "Diagnostics installed — run: gc-doctor" \
      || ok "Diagnostics installed — run: ~/.ground-control/gc-doctor"
  else
    ok "Diagnostics installed — run: ~/.ground-control/gc-doctor"
  fi
fi

echo ""
echo "  Checking that sessions actually start…"
SELFTEST_DIR="$INSTALL_DIR/.selftest"; mkdir -p "$SELFTEST_DIR"
ST_JSON="$(curl -s -m 60 -X POST "http://127.0.0.1:8130/api/new-session" \
  -H 'Content-Type: application/json' \
  -d "{\"cwd\":\"$SELFTEST_DIR\",\"name\":\"setup-check\"}" 2>/dev/null)"
if echo "$ST_JSON" | grep -q '"ok":true'; then
  echo -e "  ${GREEN}✓${NC} a real session started and is running"
  echo "     (it is named 'setup-check' and is removed automatically — if you see it"
  echo "      appear in the app and vanish a moment later, that is this test cleaning"
  echo "      up after itself, not a problem)"
  ST_ID="$(echo "$ST_JSON" | sed -n 's/.*"session_id":"\([^"]*\)".*/\1/p')"
  curl -s -m 15 -X POST "http://127.0.0.1:8130/api/session/$ST_ID/archive" >/dev/null 2>&1
  "$INSTALL_DIR/venv/bin/python3" - <<'PYCLEAN' >/dev/null 2>&1 || true
import sys, os
sys.path.insert(0, os.path.expanduser("~/.ground-control"))
import gc_ez
gc_ez.kill("setup-check")
PYCLEAN
else
  echo ""
  echo -e "  ${RED}✗ Sessions cannot start on this Mac.${NC}"
  echo "  The server installed fine, but creating a session failed. Here is why:"
  echo ""
  echo "$ST_JSON" | "$INSTALL_DIR/venv/bin/python3" -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print("   " + (d.get("error") or "unknown error"))
    for line in (d.get("detail") or "").splitlines():
        print("   " + line)
except Exception:
    print("   (no response from the server)")' 2>/dev/null
  echo ""
  echo "  Most common causes:"
  echo "    • Claude Code is installed but you have never signed in — run: claude"
  echo "    • Claude Code is not on your PATH — run: claude --version"
  echo ""
  echo "  Fix that and re-run this installer."
  echo "  For a full report to send to whoever is helping you, run:  gc-doctor"
  echo ""
  exit 1
fi

echo ""
echo "  Next steps:"
echo "  1. iPhone app  → TestFlight: https://testflight.apple.com/join/AgWRZhPJ"
echo "  2. The Mac app just opened → it finds this Mac on its own. Nothing to paste."
echo "     (The server address above is for your PHONE.)"
echo ""
echo "  To update everything later: re-run this same command."
echo ""
