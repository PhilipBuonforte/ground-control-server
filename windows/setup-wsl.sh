#!/usr/bin/env bash
# setup-wsl.sh — install the Ground Control server on a Windows PC, inside WSL2.
#
# WHY WSL AND NOT A NATIVE WINDOWS BUILD
# The engine is a raw PTY: gc_ez_engine.py uses os.openpty, os.fork, AF_UNIX sockets,
# termios/fcntl and SIGWINCH. None of those exist on Windows. A native port means
# replacing that layer with ConPTY (pywinpty), threads instead of fork and loopback
# TCP instead of unix sockets — days of work on the riskiest file in the project.
# Inside WSL2 every one of those primitives is the real thing, so the server runs
# UNMODIFIED. Audited 2026-11-12: server.py's only module-level macOS references are
# two Path definitions (Library/Messages/chat.db and the AddressBook glob); a Path to
# a file that does not exist is harmless, so it imports clean on Linux and the iMessage
# features simply stay dark.
#
# Run this INSIDE the Ubuntu shell, not PowerShell. Safe to re-run.
set -uo pipefail

REPO="https://github.com/PhilipBuonforte/ground-control-server.git"
DIR="$HOME/ground-control"
PORT=8130
B=$'\e[34m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'
say(){ echo "${B}▸${N} $1"; }
ok(){  echo "${G}✓${N} $1"; }
warn(){ echo "${Y}!${N} $1"; }
die(){ echo "${R}✗ $1${N}"; exit 1; }

echo
echo "=== Ground Control — Windows (WSL2) setup ==="
echo

# ---- 0. Sanity: are we actually inside WSL? -------------------------------
grep -qi microsoft /proc/version 2>/dev/null \
  || die "This is not WSL. Run it inside the Ubuntu shell (type 'wsl' in PowerShell first)."
ok "running inside WSL2 ($(grep -o 'Ubuntu[^ ]*' /etc/os-release 2>/dev/null | head -1 || echo Linux))"

# ---- 1. System packages --------------------------------------------------
say "Checking system packages…"
NEED=""
for p in python3 python3-venv python3-pip git curl; do
  case "$p" in
    python3|git|curl) command -v "$p" >/dev/null 2>&1 || NEED="$NEED $p" ;;
    *) dpkg -s "$p" >/dev/null 2>&1 || NEED="$NEED $p" ;;
  esac
done
if [ -n "$NEED" ]; then
  say "installing:$NEED (sudo may ask for your password)"
  sudo apt-get update -qq || die "apt-get update failed — check WSL has network"
  sudo apt-get install -y -qq $NEED || die "apt-get install failed:$NEED"
fi
ok "python $(python3 -V 2>&1 | awk '{print $2}'), git, curl present"

# ---- 2. Claude Code ------------------------------------------------------
say "Checking for Claude Code…"
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
  ok "claude found"
else
  warn "Claude Code is not installed in WSL yet. Installing it…"
  curl -fsSL https://claude.ai/install.sh | bash \
    || warn "automatic install failed — install Claude Code manually, then re-run this script"
  export PATH="$HOME/.local/bin:$PATH"
  command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ] \
    || warn "still not found; the server will install but no sessions can start until it is"
fi

# ---- 3. The server itself ------------------------------------------------
if [ -d "$DIR/.git" ]; then
  say "Updating existing install at $DIR…"
  git -C "$DIR" pull --ff-only -q || warn "git pull failed — keeping the version already on disk"
else
  say "Cloning the server to $DIR…"
  git clone -q "$REPO" "$DIR" || die "git clone failed — check network, or the repo is private to you"
fi
[ -f "$DIR/server.py" ] || die "server.py missing in $DIR — wrong repo?"
[ -d "$DIR/ezterminfo" ] || warn "ezterminfo/ missing — the terminal may render oddly"
ok "server files in place"

# ---- 4. Python environment ----------------------------------------------
say "Installing Python dependencies (first run takes a minute)…"
[ -d "$DIR/venv" ] || python3 -m venv "$DIR/venv" || die "could not create venv"
"$DIR/venv/bin/python3" -m pip install -q --upgrade pip
"$DIR/venv/bin/python3" -m pip install -q -r "$DIR/requirements.txt" \
  || die "pip install failed. Try manually: $DIR/venv/bin/python3 -m pip install -r $DIR/requirements.txt"
ok "dependencies installed"

say "Import check (proves the Mac-only bits degrade instead of crashing)…"
( cd "$DIR" && "$DIR/venv/bin/python3" -c "import server" 2>&1 | tail -3 ) \
  && ok "server.py imports cleanly on Linux" \
  || die "server.py failed to import — paste the error above to Claude"

# ---- 5. Alert hook ------------------------------------------------------
say "Registering the Claude Code alert hook…"
mkdir -p "$HOME/.claude/hooks"
if [ -f "$DIR/hooks/pocket-claude-notify.py" ]; then
  cp "$DIR/hooks/pocket-claude-notify.py" "$HOME/.claude/hooks/pocket-claude-notify.py"
  "$DIR/venv/bin/python3" - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
try:
    cfg = json.load(open(p))
except Exception:
    cfg = {}
CMD = "python3 ~/.claude/hooks/pocket-claude-notify.py"
hooks = cfg.setdefault("hooks", {})
for event in ("Stop", "Notification", "SubagentStart", "SubagentStop"):
    entries = hooks.setdefault(event, [])
    if not any(CMD in json.dumps(e) for e in entries):
        entries.append({"hooks": [{"type": "command", "command": CMD}]})
pre = hooks.setdefault("PreToolUse", [])
if not any(CMD in json.dumps(e) for e in pre):
    pre.append({"matcher": "AskUserQuestion", "hooks": [{"type": "command", "command": CMD}]})
tmp = p + ".tmp"
json.dump(cfg, open(tmp, "w"), indent=2)
os.replace(tmp, p)        # atomic — a half-written settings.json silently breaks alerts
print("  settings.json updated")
PY
  ok "alert hook registered"
else
  warn "hook file not in the repo — alerts will not fire (everything else still works)"
fi

# ---- 6. Keep it running -------------------------------------------------
# launchd does not exist here. WSL2 Ubuntu supports systemd, but only if it is
# switched on in /etc/wsl.conf, so handle both worlds.
say "Setting up auto-start…"
mkdir -p "$HOME/.ground-control"
if [ -d /run/systemd/system ]; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/ground-control.service" <<UNIT
[Unit]
Description=Ground Control server
After=network.target

[Service]
ExecStart=$DIR/venv/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port $PORT
WorkingDirectory=$DIR
Restart=always
RestartSec=3
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now ground-control.service >/dev/null 2>&1
  # linger keeps the service alive when no shell is open
  sudo loginctl enable-linger "$USER" >/dev/null 2>&1 || warn "could not enable linger; server stops when WSL closes"
  sleep 3
  systemctl --user is-active --quiet ground-control.service \
    && ok "systemd service running" \
    || warn "service not active — check: systemctl --user status ground-control"
else
  warn "systemd is off in this WSL distro. Enabling it is one line + a restart:"
  echo "      printf '[boot]\\nsystemd=true\\n' | sudo tee /etc/wsl.conf"
  echo "      then in PowerShell:  wsl --shutdown    (and reopen Ubuntu)"
  say "starting the server in the background for now…"
  pkill -f "uvicorn server:app" 2>/dev/null
  ( cd "$DIR" && nohup "$DIR/venv/bin/python3" -m uvicorn server:app \
      --host 0.0.0.0 --port "$PORT" > "$HOME/.ground-control/server.log" 2>&1 & )
  sleep 4
fi

# ---- 7. Self-test -------------------------------------------------------
echo
say "Self-test…"
HEALTH=$(curl -s -m 8 "http://127.0.0.1:$PORT/api/health" 2>/dev/null)
if echo "$HEALTH" | grep -q '"ok"'; then
  ok "server answering on port $PORT  ->  $HEALTH"
else
  warn "no answer on port $PORT. Log tail:"
  tail -20 "$HOME/.ground-control/server.log" 2>/dev/null || true
  [ -d /run/systemd/system ] && systemctl --user status ground-control --no-pager 2>&1 | tail -15
  die "server is not up — paste the above to Claude"
fi
[ -x "$DIR/gc-doctor" ] && { say "Running gc-doctor…"; "$DIR/gc-doctor" 2>&1 | tail -25; }

echo
ok "Ground Control is installed."
echo
echo "  Open on this PC:   http://localhost:$PORT/groundzero"
echo "  Terminal view:     http://localhost:$PORT/terminal"
echo
echo "  NEXT, two things only you can do:"
echo "   1. Authenticate Claude Code once:   claude    (then log in)"
echo "   2. To reach this PC from your phone, install Tailscale in WSL:"
echo "        curl -fsSL https://tailscale.com/install.sh | sh"
echo "        sudo tailscale up        # prints a link you open in a browser"
echo "        tailscale ip -4          # put http://<that-ip>:$PORT in the phone app"
echo
